# This file is part of RiakKit.
#
# RiakKit is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# RiakKit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with RiakKit.  If not, see <http://www.gnu.org/licenses/>.

from copy import copy, deepcopy
from weakref import WeakValueDictionary

from riakkit.simple.basedocument import BaseDocumentMetaclass, BaseDocument, SimpleDocument
from riakkit.commons.properties import BaseProperty, MultiReferenceProperty, ReferenceProperty
from riakkit.commons import uuid1Key, getUniqueListGivenBucketName, getProperty, walkParents
from riakkit.queries import *
from riakkit.commons.exceptions import *

from riak import RiakObject
from riak.mapreduce import RiakLink

_document_classes = {}

def getClassGivenBucketName(bucket_name):
  """Gets the class associated with a bucket name.

  Args:
    bucket_name: The bucket name. String

  Returns:
    A document subclass associated with that bucket name

  Raises:
    KeyError if bucket_name is not used.
  """
  return _document_classes[bucket_name]


class DocumentMetaclass(BaseDocumentMetaclass):
  """Meta class that the Document class is made from.

  Checks for bucket_name in each class, as those are necessary.
  """

  def __new__(cls, clsname, parents, attrs):
    if clsname == "Document":
      return type.__new__(cls, clsname, parents, attrs)

    client = getProperty("client", attrs, parents)
    if client is None:
      return type.__new__(cls, clsname, parents, attrs)

    meta = {}
    uniques = []
    references_col_classes = []
    references = []

    for name in attrs.keys():
      if isinstance(attrs[name], BaseProperty):
        meta[name] = prop = attrs.pop(name)
        refcls = getattr(prop, "reference_class", False)
        prop.name = name
        if refcls and not issubclass(refcls, Document):
          raise TypeError("ReferenceProperties for Document must be another Document!")

        colname = getattr(prop, "collection_name", False)
        if colname:
          if colname in prop.reference_class._meta:
            raise RiakkitError("%s already in %s!" % (colname, prop.reference_class))
          references_col_classes.append((colname, prop.reference_class, name))
          references.append(name)
        elif prop.unique: # Unique is not allowed with anything that has backref
          prop.unique_bucket = client.bucket(getUniqueListGivenBucketName(attrs["bucket_name"], name))
          uniques.append(name)

    all_parents = reversed(walkParents(parents))
    for p_cls in all_parents:
      meta.update(p_cls._meta)
      uniques.extend(p_cls._uniques)

    attrs["_meta"] = meta
    attrs["_uniques"] = uniques
    attrs["instances"] = WeakValueDictionary()
    attrs["_references"] = references

    new_class = type.__new__(cls, clsname, parents, attrs)

    bucket_name = attrs.get("bucket_name", None)
    if bucket_name is not None:
      if bucket_name in _document_classes:
        raise RiakkitError("Bucket name of %s already exists in the registry!"
                              % new_class.bucket_name)
      else:
        _document_classes[bucket_name] = new_class

      new_class.bucket = client.bucket(bucket_name)

    for colname, rcls, back_name in references_col_classes:
      rcls._meta[colname] = MultiReferenceProperty(reference_class=new_class)
      rcls._meta[colname].name = colname
      rcls._meta[colname].is_reference_back = back_name

    return new_class

class Document(SimpleDocument):
  """The base Document class for other classes to extend from.

  There are a couple of class variables that needs to be filled out. First is
  client. client is an instance of a RiakClient. The other is bucket_name. This
  is the name of the bucket to be stored in Riak. It must not be shared with
  another Document subclass. Lastly, you may set the  to True or False

  Class variables that's an instance of the BaseType will be the schema of the
  document.
  """

  __metaclass__ = DocumentMetaclass
  _clsType = 2

  def __init__(self, key=uuid1Key, saved=False, **kwargs):
    """Creates a new document from a bunch of keyword arguments.

    Args:
      key: A string/unicode key or a function that returns a string/unicode key.
           The function takes in 1 argument, and that argument is the kwargs
           that's passed in. Defaults to a lambda function that returns
           uuid1().hex

      saved: Is this object already saved? True or False
      kwargs: Keyword arguments that will fill up the object with data.
    """
    if callable(key):
      key = key(kwargs)

    if not isinstance(key, basestring):
      raise KeyError("%s is not a proper key!" % key)

    if key in self.__class__.instances:
      raise KeyError("%s already exists! Use get instead!" % key)

    self.__dict__["key"] = key

    self._obj = self.bucket.get(self.key) if saved else None
    self._links = set()
    self._indexes = {}
    self._saved = saved

    BaseDocument.__init__(self, **kwargs)

    self.__class__.instances[self.key] = self

  def save(self, w=None, dw=None):
    """Saves the document into the database.

    This will save the object to the database. All linked objects will be saved
    as well.

    Args:
      w: W value
      dw: DW value
    """
    dataToBeSaved = self.serialize()
    uniquesToBeDeleted = []
    othersToBeSaved = []

    # Process uniques
    for name in self._uniques:
      if self._data.get(name, None) is None:
        if self._obj: # TODO: could be somehow refactored, as this condition is always true?
          originalValue = self._obj.get_data().get(name, None)
          if originalValue is not None:
            uniquesToBeDeleted.append((self._meta[name].unique_bucket, originalValue))
      else:
        changed = False
        if self._obj:
          originalValue = self._obj.get_data().get(name, None)
          if self._data[name] != originalValue and originalValue is not None:
            uniquesToBeDeleted.append((self._meta[name].unique_bucket, originalValue))
            changed = True
        else:
          changed = True

        if changed and self._meta[name].unique_bucket.get(dataToBeSaved[name]).exists():
          raise ValueError("'%s' already exists for '%s'!" % (self._data[name], name))

    # Process references
    for name in self._references:
      if self._obj:
        originalValues = self._obj.get_data().get(name, None)
        if originalValues is None:
          originalValues = []
        elif not isinstance(originalValues, list):
          originalValues = [originalValues]
      else:
        originalValues = []

      if isinstance(self._meta[name], ReferenceProperty):
        docs = [self._data[name]]
      else:
        docs = self._data[name]

      dockeys = set()
      colname = self._meta[name].collection_name

      for doc in docs: # These are foreign documents
        if doc is None:
          continue

        dockeys.add(doc.key)

        currentList = doc._data.get(colname, [])
        found = False # Linear search algorithm. Maybe binary search??
        for d in currentList:
          if d.key == self.key:
            found = True
            break
        if not found:
          currentList.append(self)
          doc._data[colname] = currentList
          othersToBeSaved.append(doc)

      for dockey in originalValues:
        if dockey is None:
          continue

        # This means that this specific document is not in the current version,
        # but last version. Hence it needs to be cleaned from the last version.
        if dockey not in dockeys:
          try:
            doc = self._meta[name].reference_class.load(dockey, True)
          except NotFoundError: # TODO: Another hackjob? This is _probably_ due to we're back deleting the reference.
            continue

          currentList = doc._data.get(colname, [])

          # TODO: REFACTOR WITH ABOVE'S LINEAR SEARCH ALGO
          for i, d in enumerate(currentList):
            if d.key == self.key:
              currentList.pop(i)
              doc._data[colname] = currentList
              othersToBeSaved.append(doc)
              break


    if self._obj:
      self._obj.set_data(dataToBeSaved)
    else:
      self._obj = self.bucket.new(self.key, dataToBeSaved)

    self._obj.set_links(self.links(True), True)
    self._obj.set_indexes(self.indexes())

    self._obj.store(w=w, dw=dw)
    for name in self._uniques:
      obj = self._meta[name].unique_bucket.new(self._data[name], {"key" : self.key})
      obj.store(w=w, dw=dw)

    for bucket, key in uniquesToBeDeleted:
      bucket.get(key).delete()

    self._saved = True

    for doc in othersToBeSaved:
      doc.save(w, dw)

    return self

  def reload(self, r=None, vtag=None):
    """Reloads the object from the database.

    This grabs the most recent version of the object from the database and
    updates the document accordingly. The data will change if the object
    from the database has been changed at one point.

    This only works if the object has been saved at least once before.

    Returns:
      self for OOP.

    Raises:
      NotFoundError: if the object hasn't been saved before.
    """
    if self._obj:
      self._obj.reload(r=r, vtag=vtag)
      self.deserialize(deepcopy(self._obj.get_data()))
      # Handle 2i, Links
      self._saved = True
    else:
      raise NotFoundError("Object not saved!")

  def delete(self, rw=None):
    """Deletes this object from the database. Same interface as riak-python.

    However, this object can still be resaved. Not sure what you would do
    with it, though.
    """
    def deleteBackRef(col_name, docs):
      docs_to_be_saved = []
      for doc in docs:
        if doc._meta[col_name].deleteReference(doc, self):
          docs_to_be_saved.append(doc)

      return docs_to_be_saved

    if self._obj is not None:
      docs_to_be_saved = []
      for k in self._meta:
        # is_reference_back is for deleting the document that has the collection_name
        # collection_name is the document that gives out collection_name
        col_name = getattr(self._meta[k], "is_reference_back", False) or getattr(self._meta[k], "collection_name", False)

        if col_name:
          docs = self._data.get(k, [])
          if docs is not None:
            if isinstance(docs, Document):
              docs = [docs]
            docs_to_be_saved.extend(deleteBackRef(col_name, docs))

      self.__class__.instances.pop(self.key, False)

      self._obj.delete(rw=rw)

      for name in self._uniques:
        if self._data[name] is not None:
          obj = self._meta[name].unique_bucket.get(self._data[name])
          obj.delete()

      self._obj = None
      self._saved = False

      for doc in docs_to_be_saved:
        doc.save()

  def links(self, riakLinks=False):
    """Gets all the links.

    Args:
      riakLinks: Defaults to False. If True, it will return a list of RiakLinks

    Returns:
      A set of (document, tag) or [RiakLink, RiakLink]"""
    if riakLinks:
      return [RiakLink(self.bucket_name, d.key, t) for d, t in self._links]
    return copy(self._links)

  @staticmethod
  def _getLinksFromRiakObj(robj):
    objLinks = robj.get_links()
    links = set()
    for link in objLinks:
      tag = link.get_tag()
      c = getClassGivenBucketName(link.get_bucket())
      links.add((c.load(link.get(), True), tag))
    return links

  @classmethod
  def load(cls, robj, cached=False, r=None):
    """Construct a Document based object given a RiakObject.

    Args:
      riak_obj: The RiakObject that the document is suppose to build from.
      cached: Reload the object or not if it's found in the pool of objects.

    Returns:
      A Document object (whichever subclass this was called from).
    """

    if isinstance(robj, RiakObject):
      key = robj.get_key()
    else:
      key = robj

    try:
      doc = cls.instances[key]
    except KeyError:
      robj = cls.bucket.get(key, r)
      if not robj.exists():
        raise NotFoundError("%s not found!" % key)

      # This is done before so that deserialize won't recurse
      # infinitely with collection_name. This wouldn't cause an problem as
      # deserialize calls for the loading of the referenced document
      # from cache, which load this document from cache, and it see that it
      # exists, finish loading the referenced document, then come back and finish
      # loading this document.

      doc = cls(key, saved=True)
      cls.instances[key] = doc

      doc.deserialize(robj.get_data())
      doc.setIndexes(cls._getIndexesFromRiakObj(robj))
      doc.setLinks(cls._getLinksFromRiakObj(robj))
      doc._obj = robj
    else:
      if not cached:
        doc.reload()

    return doc

  get = load

  @classmethod
  def exists(cls, key, r=None):
    """Check if a key exists.

    Args:
      key: The key to check if exists or not.
      r: The R value

    Returns:
      True if the key exists, false otherwise.
    """
    return cls.bucket.get(key, r).exists()

  @classmethod
  def search(cls, querytext):
    """Searches through the bucket with some query text.

    The bucket must have search installed via search-cmd install BUCKETNAME. The
    class must have been marked to be  with cls. = True.

    Args:
      querytext: The query text as outlined in the python-riak documentations.

    Returns:
      A MapReduceQuery object. Similar to the RiakMapReduce object.

    Raises:
      NotImplementedError: if the class is not marked .
    """
    query_obj = cls.client.search(cls.bucket_name, querytext)
    return MapReduceQuery(cls, query_obj)

  @classmethod
  def solrSearch(cls, querytext, **kwargs):
    return SolrQuery(cls, cls.client.solr().search(cls.bucket_name, querytext, **kwargs))

  @classmethod
  def indexLookup(cls, index, startkey, endkey=None):
    """Short hand for creating a new mapreduce index

    Args:
      index: The index field
      startkey: The starting key
      endkey: The ending key. If not none, search a range. Default: None

    Returns:
      A RiakMapReduce object
    """
    return MapReduceQuery(cls, cls.client.index(cls.bucket_name, index, startkey, endkey))

  @classmethod
  def mapreduce(cls):
    """Shorthand for creating a query object for map reduce.

    Returns:
      A RiakMapReduce object.
    """
    return cls.client.add(cls.bucket_name)

import time
from django.core.cache.backends.db import BaseDatabaseCacheClass
from django.db import connections, router
from bson.errors import InvalidDocument
from bson.binary import Binary
from pymongo import ASCENDING
try:
    import cPickle as pickle
except ImportError:
    import pickle

class CacheClass(BaseDatabaseCacheClass):
    def validate_key(self, key):
        if '.' in key or '$' in key:
            raise ValueError("Cache keys must not contain '.' or '$' "
                             "if using MongoDB cache backend")
        super(CacheClass, self).validate_key(key)

    def _get_collection(self):
        db = router.db_for_read(self.cache_model_class)
        return connections[db].db_connection[self._table]

    def get(self, key, default=None):
        self.validate_key(key)
        collection = self._get_collection()
        document = collection.find_one({'_id' : key})

        if document is None:
            return default

        if document['e'] < time.time():
            # outdated document, delete it and pretend it doesn't exist
            collection.remove({'_id' : key})
            return default

        pickled_obj = document.get('p')
        if pickled_obj is not None:
            return pickle.loads(pickled_obj)
        else:
            return document['v']

    def set(self, key, value, timeout=None):
        self._base_set(key, value, timeout, force_set=True)

    def add(self, key, value, timeout=None):
        return self._base_set(key, value, timeout, force_set=False)

    def _base_set(self, key, value, timeout, force_set=False):
        self.validate_key(key)
        collection = self._get_collection()

        if collection.count() > self._max_entries:
            self._cull()

        now = time.time()
        expires = now + (timeout or self.default_timeout)
        new_document = {'_id' : key, 'v' : value, 'e' : expires}
        if not force_set:
            current_document = collection.find_one({'_id' : key})
            if current_document is not None and current_document['e'] >= now:
                # do not overwrite existing, non-expired entries.
                return False

        try:
            collection.save(new_document)
        except InvalidDocument:
            # value can't be serialized to BSON, fall back to pickle.

            # TODO: Suppress PyMongo warning here by writing a PyMongo patch
            # that allows BSON to be passed as document to .save
            new_document['p'] = Binary(pickle.dumps(new_document.pop('v'), protocol=2))
            collection.save(new_document)

    def incr(self, key, delta=1):
        # TODO: If PyMongo eventually implements findAndModify, use it.
        self.validate_key(key)
        collection = self._get_collection()
        document = collection.find_one({'_id' : key})
        if document is None:
            raise ValueError("Key %r not found" % key)
        new_value = document['v'] + delta
        collection.update({'_id' : key}, {'$set' : {'v' : new_value}})
        return new_value

    def delete(self, key):
        self.validate_key(key)
        self._get_collection().remove({'_id' : key})

    def clear(self):
        self._get_collection().drop()

    def _cull(self):
        collection = self._get_collection()
        collection.remove({'e' : {'$lt' : time.time()}})
        # remove all expired entries
        count = collection.count()
        assert count > self._max_entries
        if count > self._max_entries:
            # still too much entries left
            cut = collection.find({}, {'e' : 1}) \
                            .sort('e', ASCENDING) \
                            .skip(count / self._cull_frequency).limit(1)[0]
            collection.remove({'e' : {'$lt' : cut['e']}}, safe=True)
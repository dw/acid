#
# Copyright 2013, David Wilson.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Minimalist object DBMS for Python

See http://centidb.readthedocs.org/
"""

from __future__ import absolute_import

import cPickle as pickle
import cStringIO
import functools
import importlib
import itertools
import operator
import os
import re
import struct
import sys
import time
import uuid
import warnings
import zlib

import keycoder
from keycoder import tuplize

__all__ = '''Store Collection Record Index Encoder KEY_ENCODER PICKLE_ENCODER
    PLAIN_PACKER ZLIB_PACKER next_greater open'''.split()

KIND_NULL = chr(15)
KIND_NEG_INTEGER = chr(20)
KIND_INTEGER = chr(21)
KIND_BOOL = chr(30)
KIND_BLOB = chr(40)
KIND_TEXT = chr(50)
KIND_UUID = chr(90)
KIND_KEY = chr(95)
KIND_SEP = chr(102)
INVERT_TBL = ''.join(chr(c ^ 0xff) for c in xrange(256))
IndexKeyBuilder = None

ITEMGETTER_0 = operator.itemgetter(0)
ITEMGETTER_1 = operator.itemgetter(1)


def open(engine, **kwargs):
    """Look up an engine class named by `engine`, instantiate it as
    `engine(**kwargs)` and wrap the result in a :py:class:`Store`. `engine`
    can either be a name from :py:mod:`centidb.support` or a fully qualified
    name for a class in another module.

    ::

        >>> # Uses centidb.support.SkiplistEngine
        >>> centidb.open('SkiplistEngine')

        >>> # Uses mymodule.BlarghEngine
        >>> centidb.open('mymodule.BlarghEngine')
    """
    modname, _, classname = engine.rpartition('.')
    module = importlib.import_module(modname or 'centidb.support')
    return Store(getattr(module, classname)(**kwargs))

def decode_offsets(s):
    io = cStringIO.StringIO(s)
    getc = functools.partial(io.read, 1)
    more = functools.partial(keycoder.unpack_int, getc, io.read)
    pos = 0
    out = [0]
    for _ in xrange(more()):
        pos += more()
        out.append(pos)
    return out, io.tell()

_encode_pat = re.compile(r'[\x00\x01]')
_encode_subber = lambda m: '\x01\x01' if m.group(0) == '\x00' else '\x01\x02'

def next_greater(s):
    """Given a bytestring `s`, return the most compact bytestring that is
    greater than any value prefixed with `s`, but lower than any other value.

    ::

        >>> assert next_greater('') == '\\x00'
        >>> assert next_greater('\\x00') == '\\x01'
        >>> assert next_greater('\\xff') == '\\xff\\x00'
        >>> assert next_greater('\\x00\\x00') == '\\x00\\x01')
        >>> assert next_greater('\\x00\\xff') == '\\x01')
        >>> assert next_greater('\\xff\\xff') == '\\x01')

    """
    assert s
    # Based on the Plyvel `bytes_increment()` function.
    s2 = s.rstrip('\xff')
    return s2 and (s2[:-1] + chr(ord(s2[-1]) + 1))

def _eat(pred, it, total_only=False):
    if not pred:
        return it
    total = 0
    true = 0
    for elem in it:
        total += 1
        true += elem is not None
    if total_only:
        return total
    return total, true

def __kcmp(fn, o):
    return fn(o[1])
_kcmp = functools.partial(functools.partial, __kcmp)


class Encoder(object):
    """Instances of this class represent an encoding.

        `name`:
            ASCII string uniquely identifying the encoding. A future version
            may use this to verify the encoding matches what was used to create
            the :py:class:`Collection`. For encodings used as compressors, this
            name is persisted forever in :py:class:`Store`'s metadata after
            first use.

        `unpack`:
            Function to deserialize an encoded value. It may be called with **a
            buffer object containing the encoded bytestring** as its argument,
            and should return the decoded value. If your encoder does not
            support :py:func:`buffer` objects (many C extensions do), then
            convert the buffer using :py:func:`str`.

        `pack`:
            Function to serialize a value. It is called with the value as its
            sole argument, and should return the encoded bytestring.
    """
    def __init__(self, name, unpack, pack):
        self.name = name
        self.unpack = unpack
        self.pack = pack

class Index(object):
    """Provides query and manipulation access to a single index on a
    Collection. You should not create this class directly, instead use
    :py:meth:`Collection.add_index` and the :py:attr:`Collection.indices`
    mapping.

    :py:meth:`Index.get` and the iteration methods take a common set of
    arguments that are described below:

        `args`:
            Prefix of the index entries to to be matched, or ``None`` or the
            empty tuple to indicate all index entries should be matched.

        `reverse`:
            If ``True``, iteration should begin with the last naturally ordered
            match returned first, and end with the first naturally ordered
            match returned last.

        `txn`:
            Transaction to use, or ``None`` to indicate the default behaviour
            of the storage engine.

        `max`:
            Maximum number of index records to return.
    """
    def __init__(self, coll, info, func):
        self.coll = coll
        self.store = coll.store
        self.engine = self.store.engine
        self.info = info
        #: The index function.
        self.func = func
        self.prefix = self.store.prefix + keycoder.pack_int(info['idx'])
        self._decode = functools.partial(keycoder.unpacks, self.prefix)

    def _iter(self, txn, key, lo, hi, reverse, max, include):
        if lo is None:
            lo = self.prefix
        else:
            lo = keycoder.packs(self.prefix, lo)

        if hi is None:
            hi = next_greater(self.prefix)
            if not (key and reverse):
                include = False
        else:
            # This is a broken mess. When doing reverse queries we must account
            # for the key tuple of the index key. next_greater() may fail if
            # the last byte of the index tuple is FF. Needs a better solution.
            hi = next_greater(keycoder.packs(self.prefix, hi)) # TODO WTF
            assert hi

        if key is not None:
            if reverse:
                hi = next_greater(keycoder.packs(self.prefix, key)) # TODO
                assert hi
                include = False
            else:
                lo = keycoder.packs(self.prefix, key)

        if reverse:
            it = (txn or self.engine).iter(hi, True)
            pred = lo.__le__
        else:
            it = (txn or self.engine).iter(lo, False)
            pred = hi.__ge__ if include else hi.__gt__
        it = itertools.takewhile(pred, it)
        if max is not None:
            it = itertools.islice(it, max)
        for key, _ in it:
            key = self._decode(key)
            if not key:
                break
            yield key

    def count(self, args=None, lo=None, hi=None, max=None, include=False,
              txn=None):
        """Return a count of index entries matching the parameter
        specification."""
        return sum(1 for _ in self._iter(txn, args, lo, hi, 0, max, include))

    def pairs(self, args=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None):
        """Yield all (tuple, key) pairs in the index, in tuple order. `tuple`
        is the tuple returned by the user's index function, and `key` is the
        key of the matching record.
        
        `Note:` the yielded sequence is a list, not a tuple."""
        return self._iter(txn, args, lo, hi, reverse, max, include)

    def tups(self, args=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None):
        """Yield all index tuples in the index, in tuple order. The index tuple
        is the part of the entry produced by the user's index function, i.e.
        the index's natural "value"."""
        return itertools.imap(ITEMGETTER_0,
            self.pairs(args, lo, hi, reverse, max, include, txn))

    def keys(self, args=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None):
        """Yield all keys in the index, in tuple order."""
        return itertools.imap(ITEMGETTER_1,
            self.pairs(args, lo, hi, reverse, max, include, txn))

    def items(self, args=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None, rec=False):
        """Yield all `(key, value)` items referred to by the index, in tuple
        order. If `rec` is ``True``, :py:class:`Record` instances are yielded
        instead of record values."""
        for idx_key, key in self.pairs(args, lo, hi, reverse, max,
                                       include, txn):
            obj = self.coll.get(key, txn=txn, rec=rec)
            if obj:
                yield key, obj
            else:
                warnings.warn('stale entry in %r, requires rebuild' % (self,))

    def values(self, args=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None, rec=None):
        """Yield all values referred to by the index, in tuple order. If `rec`
        is ``True``, :py:class:`Record` instances are yielded instead of record
        values."""
        return itertools.imap(ITEMGETTER_1,
            self.items(args, lo, hi, reverse, max, include, txn, rec))

    def find(self, args=None, lo=None, hi=None, reverse=None, include=False,
             txn=None, rec=None, default=None):
        """Return the first matching record from the index, or None. Like
        ``next(itervalues(), default)``."""
        it = self.values(args, lo, hi, reverse, None, include, txn, rec)
        v = next(it, default)
        if v is default and rec and default is not None:
            v = Record(self.coll, default)
        return v

    def has(self, x, txn=None):
        """Return True if an entry with the exact tuple `x` exists in the
        index."""
        x = tuplize(x)
        tup, key = next(self.pairs(x), (None, None))
        return tup == x

    def get(self, x, txn=None, rec=None, default=None):
        """Return the first matching record referred to by the index, in tuple
        order. If `rec` is ``True`` a :py:class:`Record` instance is returned
        of the record value."""
        for tup in self.items(lo=x, hi=x, include=False, rec=rec):
            return tup[1]
        if rec and default is not None:
            return Record(self.coll, default)
        return default

    def gets(self, xs, txn=None, rec=None, default=None):
        """Yield `get(x)` for each `x` in the iterable `xs`."""
        return (self.get(x, txn, rec, default) for x in xs)

class Record(object):
    """Wraps a record value with its last saved key, if any.

    :py:class:`Record` instances are usually created by the
    :py:class:`Collection` and :py:class:`Index`
    ``get()``/``put()``/``iter*()`` functions. They are primarily used to track
    index keys that were valid for the record when it was loaded, allowing many
    operations to be avoided if the user deletes or modifies it within the same
    transaction. The class is only required when modifying existing records.

    It is possible to avoid using the class when `Collection.derived_keys =
    True`, however this hurts perfomance as it forces :py:meth:`Collectionput`
    to first check for any existing record with the same key, and therefore for
    any existing index keys that must first be deleted.

    *Note:* you may create :py:class:`Record` instances directly, **but you
    must not modify any attributes except** :py:attr:`Record.data`, or
    construct it using any arguments except `coll` and `data`, otherwise index
    corruption will likely occur.
    """
    def __init__(self, coll, data, _key=None, _batch=False,
            _txn_id=None, _index_keys=None):
        #: :py:class:`Collection` this record belongs to. This is always reset
        #: after a successful :py:meth:`Collection.put`.
        self.coll = coll
        #: The actual record value. This may be user-supplied Python object
        #: recognized by the collection's value encoder.
        self.data = data
        #: Key for this record when it was last saved, or ``None`` if the
        #: record is deleted or has never been saved.
        self.key = _key
        #: True if the record was loaded from a physical key that contained
        #: other records. Used internally to know when to explode batches
        #: during saves.
        self.batch = _batch
        #: Transaction ID this record was visible in. Used internally to
        #: ensure records from distinct transactions aren't mixed.
        self.txn_id = _txn_id
        self.index_keys = _index_keys

    def __eq__(self, other):
        return isinstance(other, Record) and \
            other.coll is self.coll and other.data == self.data and \
            other.key == self.key

    def __repr__(self):
        s = ','.join(map(repr, self.key or ()))
        return '<Record %s:(%s) %r>' % (self.coll.info['name'], s, self.data)

class Collection(object):
    """Provides access to a record collection contained within a
    :py:class:`Store`, and ensures associated indices update consistently when
    changes are made.

        `store`:
            :py:class:`Store` the collection belongs to. If metadata for the
            collection does not already exist, it will be populated during
            construction.

        `name`:
            ASCII string used to identify the collection, aka. the key of the
            collection itself.

        `key_func`, `txn_key_func`:
            Key generator for records about to be saved. `key_func` takes one
            argument, the record's value, and should return a tuple of
            primitive values that will become the record's key.  If the
            function returns a lone primitive value, it will be wrapped in a
            1-tuple.

            Alternatively, `txn_key_func` may be used to access the current
            transaction during key assignment. It is invoked as
            `txn_key_func(txn, value)`, where `txn`  is a reference to the
            active transaction, or :py:class:`Store`'s engine if no transaction
            was supplied.

            If neither function is given, keys are assigned using a
            transactional counter (like auto-increment in SQL). See
            `counter_name`.

        `derived_keys`:
            If ``True``, indicates the key function derives a record's key from
            its value, and should be re-invoked for each change. If the key
            changes, the previous key and index entries are automatically
            deleted.

            ::

                # Since names are used as keys, if a person record changes
                # name, its key must also change.
                coll = Collection(store, 'people',
                    key_func=lambda person: person['name'],
                    derived_keys=True)

            If ``False``, record keys are preserved across saves, so long as
            `get(rec=True)` and `put(<Record instance>)` are used. In either
            case, `put(..., key=...)` may be used to override default behavior.

        `blind`:
            If ``True``, indicates the key function never reassigns the same
            key twice, for example when using a time-based key. In this case,
            checks for old records with the same key may be safely skipped,
            significantly improving performance.

            This mode is always active when a collection has no indices
            defined, and does not need explicitly set in that case.

        `encoder`:
            :py:class:`Encoder` used to serialize record values to bytestrings;
            defaults to ``PICKLE_ENCODER``.

        `packer`:
            :py:class:`Encoder` used to compress one or more serialized record
            values as a unit. Used only if `packer=` isn't specified during
            :py:meth:`Collection.put` or :py:meth:`Collection.batch`. Defaults
            to ``PLAIN_PACKER`` (uncompressed).

        `counter_name`:
            Specifies the name of the :py:class:`Store` counter to use when
            generating auto-incremented keys. If unspecified, defaults to
            ``"key:<name>"``. Unused when `key_func` or `txn_key_func`
            are specified.
    """
    def __init__(self, store, name, key_func=None, txn_key_func=None,
            derived_keys=False, blind=False, encoder=None, packer=None,
            _idx=None, counter_name=None):
        """Create an instance; see class docstring."""
        self.store = store
        self.engine = store.engine
        if _idx is not None:
            self.info = {'name': name, 'idx': _idx, 'index_for': None}
        else:
            self.info = store._get_info(name, idx=_idx)
        self.prefix = store.prefix + keycoder.pack_int(self.info['idx'])
        if not (key_func or txn_key_func):
            counter_name = counter_name or ('key:%(name)s' % self.info)
            txn_key_func = lambda txn, _: store.count(counter_name, txn=txn)
            derived_keys = False
            blind = True
        self.key_func = key_func
        self.txn_key_func = txn_key_func
        self.derived_keys = derived_keys
        self.blind = blind
        self.encoder = encoder or PICKLE_ENCODER
        self.encoder_prefix = self.store.add_encoder(self.encoder)
        #: Default packer used when calls to :py:meth:`Collection.put` do not
        #: specify a `packer=` argument. Defaults to ``PLAIN_PACKER``.
        self.packer = packer or PLAIN_PACKER
        #: Dict mapping indices added using :py:meth:`Collection.add_index` to
        #: :py:class:`Index` instances representing them.
        #:
        #: ::
        #:
        #:      idx = coll.add_index('some index', lambda v: v[0])
        #:      assert coll.indices['some index'] is idx
        self.indices = {}

    def add_index(self, name, func):
        """Associate an index with the collection. Index metadata will be
        created in the storage engine it it does not exist. Returns the `Index`
        instance describing the index. This method may only be invoked once for
        each unique `name` for each collection.

        .. note::
            Only index metadata is persistent. You must invoke
            :py:meth:`Collection.add_index` with the same arguments every time
            you create a :py:class:`Collection` instance.

        `name`:
            ASCII name for the index.

        `func`:
            Index key generation function accepting one argument, the record
            value. It should return a single primitive value, a tuple of
            primitive values, a list of primitive values, or a list of tuples
            of primitive values.

            .. caution::
                The index function must have no side-effects, as it may be
                invoked repeatedly.

            Example:

            ::

                coll = Collection(store, 'people')
                coll.add_index('name', lambda person: person['name'])

                coll.put({'name': 'David'})
                coll.put({'name': 'Charles'})
                coll.put({'name': 'Charles'})
                coll.put({'name': 'Andrew'})

                it = coll.indices['name'].iterpairs()
                assert list(it) == [
                    (('Andrew',),   (4,)),
                    (('Charles',),  (2,)),
                    (('Charles',),  (3,)),
                    (('David',),    (1,))
                ]
        """
        assert name not in self.indices
        info_name = 'index:%s:%s' % (self.info['name'], name)
        info = self.store._get_info(info_name, index_for=self.info['name'])
        index = Index(self, info, func)
        self.indices[name] = index
        if IndexKeyBuilder:
            self._index_keys = IndexKeyBuilder(self.indices.values()).build
        return index

    def _logical_iter(self, it, reverse):
        #   * When iterating forward, if first yielded key lacks collection
        #     prefix, result of iteration is empty.
        #   * When iterating reverse, if first yielded key lacks collection
        #     prefix, discard, then behave as forward.
        #   * Records are discarded in the direction of iteration until
        #     startpred() or not self.prefix.
        #   * Records are yielded following startpred() until not endpred() or
        #     not self.prefix.
        tup = next(it, None)
        if tup and tup[0][:len(self.prefix)] == self.prefix:
            it = itertools.chain((tup,), it)
        for key, value in it:
            keys = keycoder.unpacks(self.prefix, key)
            if not keys:
                return

            lenk = len(keys)
            if lenk == 1:
                yield False, keys[0], self._decompress(value)
            else: # Batch record.
                offsets, dstart = decode_offsets(value)
                data = self._decompress(buffer(value, dstart))
                keys.reverse()
                if reverse:
                    rit = xrange(lenk - 1, -1, -1)
                else:
                    rit = xrange(lenk)
                for i in rit:
                    key = keys[i]
                    offs = offsets[i]
                    size = offsets[i+1] - offs
                    yield True, key, buffer(data, offs, size)

    # -----------------------------------------------------------
    # prefix: a
    #                          _iter(key=ad, reverse=True)
    #                         /_iter(hi=ad, reverse=True)
    #                        //
    #       aa     ab     aedc     af     ba
    #       ^             ^               ^
    #       |             |               |
    #       |             |               |
    #  .iter(prefix)      |        .iter(next_greater(prefix))
    #                 .iter(ad)
    # -----------------------------------------------------------
    # _iter(, , , False): lokey=prefix, hikey=ng(prefix)
    #                     startpred=lokey, endpred=
    def _iter(self, txn, key, lo, hi, reverse, max_, include, max_phys):
        if key is not None:
            key = tuplize(key)
            if reverse:
                hi = key
                include = True
            else:
                lo = key

        if lo is None:
            lokey = self.prefix
        else:
            lo = tuplize(lo)
            lokey = keycoder.packs(self.prefix, lo)

        if hi is None:
            hikey = next_greater(self.prefix)
            include = False
        else:
            hi = tuplize(hi)
            hikey = keycoder.packs(self.prefix, hi)

        if reverse:
            startkey = hikey
            startpred = hi and (hi.__lt__ if include else hi.__le__)
            endpred = lo and lo.__ge__
        else:
            startkey = lokey
            startpred = None
            endpred = hi and (hi.__ge__ if include else hi.__gt__)

        it = (txn or self.engine).iter(startkey, reverse)
        if max_phys is not None:
            it = itertools.islice(it, max_phys)

        it = self._logical_iter(it, reverse)
        if max_ is not None:
            it = itertools.islice(it, max_)
        if startpred:
            it = itertools.dropwhile(_kcmp(startpred), it)
        if endpred:
            it = itertools.takewhile(_kcmp(endpred), it)
        return it

    def _decompress(self, s):
        encoder = self.store.get_encoder(s[0])
        return encoder.unpack(buffer(s, 1))

    def _index_keys(self, key, obj):
        idx_keys = []
        for idx in self.indices.itervalues():
            lst = idx.func(obj)
            for idx_key in lst if type(lst) is list else [lst]:
                idx_keys.append(keycoder.packs(idx.prefix, [idx_key, key]))
        return idx_keys

    def items(self, key=None, lo=None, hi=None, reverse=False, max=None,
            include=False, txn=None, rec=None, raw=False):
        """Yield all `(key tuple, value)` tuples in key order. If `rec` is
        ``True``, :py:class:`Record` instances are yielded instead of record
        values."""
        txn_id = getattr(txn or self.engine, 'txn_id', None)
        it = self._iter(txn, key, lo, hi, reverse, max, include, None)
        for batch, key, data in it:
            obj = data if raw else self.encoder.unpack(data)
            if rec:
                obj = Record(self, obj, key, batch, txn_id,
                             self._index_keys(key, obj))
            yield key, obj

    def keys(self, key=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None, rec=None):
        """Yield key tuples in key order."""
        return itertools.imap(ITEMGETTER_0,
            self.items(key, lo, hi, reverse, max, include, txn, rec, True))

    def values(self, key=None, lo=None, hi=None, reverse=None, max=None,
            include=False, txn=None, rec=None, raw=False):
        """Yield record values in key order. If `rec` is ``True``,
        :py:class:`Record` instances are yielded instead of record values."""
        return itertools.imap(ITEMGETTER_1,
            self.items(key, lo, hi, reverse, max, include, txn, rec, raw))

    def gets(self, keys, default=None, rec=False, txn=None):
        """Yield `get(k)` for each `k` in the iterable `keys`."""
        return (self.get(x, default, rec, txn) for k in keys)

    def find(self, key=None, lo=None, hi=None, reverse=None, include=False,
             txn=None, rec=None, raw=None, default=None):
        """Return the first matching record, or None. Like ``next(itervalues(),
        default)``."""
        it = self.values(key, lo, hi, reverse, None, include, txn, rec, raw)
        v = next(it, default)
        if v is default and rec and default is not None:
            v = Record(self.coll, default)
        return v

    def get(self, key, default=None, rec=False, txn=None, raw=False):
        """Fetch a record given its key. If `key` is not a tuple, it is wrapped
        in a 1-tuple. If the record does not exist, return ``None`` or if
        `default` is provided, return it instead. If `rec` is ``True``, return
        a :py:class:`Record` instance for use when later re-saving the record,
        otherwise only the record's value is returned. If `rec` is ``True``,
        return the record without unpacking it."""
        key = tuplize(key)
        it = self._iter(txn, None, key, key, False, None, True, None)
        tup = next(it, None)
        if tup:
            txn_id = getattr(txn or self.engine, 'txn_id', None)
            obj = tup[2] if raw else self.encoder.unpack(tup[2])
            if rec:
                obj = Record(self, obj, key, tup[0], txn_id,
                             self._index_keys(key, obj))
            return obj

        if default is not None:
            return Record(self, default) if rec else default
        return

    def batch(self, lo=None, hi=None, max_recs=None, max_bytes=None,
              max_keylen=None, preserve=True, packer=None, txn=None,
              max_phys=None, grouper=None):
        """
        Search the key range *lo..hi* for individual records, combining them
        into a batches.

        Returns `(found, made, last_key)` indicating the number of records
        combined, the number of batches produced, and the last key visited
        before `max_phys` was exceeded.

        Batch size is controlled via `max_recs` and `max_bytes`; at least one
        must not be ``None``. Larger sizes may cause pathological behaviour in
        the storage engine (for example, space inefficiency). Since batches are
        fully decompressed before any member may be accessed via
        :py:meth:`get() <Collection.get>` or :py:meth:`iteritems()
        <Collection.iteritems>`, larger sizes may slow decompression, waste IO
        bandwidth, and temporarily use more RAM.

            `lo`:
                Lowest search key.

            `hi`:
                Highest search key.

            `max_recs`:
                Maximum number of records contained by any single batch. When
                this count is reached, the current batch is saved and a new one
                is created.

            `max_bytes`:
                Maximum size in bytes of the batch record's value after
                compression, or ``None`` for no maximum size. When not
                ``None``, values are recompressed after each member is
                appended, in order to test if `maxbytes` has been reached. This
                is inefficient, but provides the best guarantee of final record
                size. Single records are skipped if they exceed this size when
                compressed individually.

            `max_keylen`:
                Maximum size in bytes of the batch record's key part, or
                ``None`` for no maximum size.

            `preserve`:
                If ``True``, then existing batch records in the database are
                left untouched. When one is found within `lo..hi`, the
                currently building batch is finished and the found batch is
                skipped over.

                If ``False``, found batches are exploded and their members
                contribute to the currently building batch.

            `packer`:
                Specifies the value compressor to use. If ``None``, defaults to
                the `packer=` argument given to the :py:class:`Collection`
                constructor, or uncompressed.

            `txn`:
                Transaction to use, or ``None`` to indicate the default
                behaviour of the storage engine.

            `max_phys`:
                Maximum number of physical keys to visit in any particular
                call. A collection may be incrementally batched by repeatedly
                invoking :py:meth:`Collection.batch` with `max` set, and `lo`
                set to `last_key` of the previous run, until `found` returns
                ``0``. This allows batching to complete over several
                transactions without blocking other users.

            `grouper`:
                Specifies a grouping function used to decide when to avoid
                compressing unrelated records. The function is passed a
                record's value. A new batch is triggered each time the
                function's return value changes.

        """
        assert max_keylen is None, 'max_keylen is not implemented.'
        assert max_bytes or max_recs, 'max_bytes and/or max_recs is required.'
        txn = txn or self.engine
        packer = packer or self.packer
        it = self._iter(txn, None, lo, hi, False, None, True, max_phys)
        groupval = None
        items = []

        for batch, key, data in it:
            if preserve and batch:
                self._write_batch(txn, items, packer)
            else:
                txn.delete(keycoder.packs(self.prefix, key))
                items.append((key, data))
                if max_bytes:
                    _, encoded = self._prepare_batch(items, packer)
                    if len(encoded) > max_bytes:
                        items.pop()
                        self._write_batch(txn, items, packer)
                        items.append((key, data))
                done = max_recs and len(items) == max_recs
                if (not done) and grouper:
                    val = grouper(self.encoder.unpack(data))
                    done = val != groupval
                    groupval = val
                if done:
                    self._write_batch(txn, items, packer)
        self._write_batch(txn, items, packer)

    def _write_batch(self, txn, items, packer):
        if items:
            phys, data = self._prepare_batch(items, packer)
            txn.put(phys, data)
            del items[:]

    def _prepare_batch(self, items, packer):
        packer_prefix = self.store._encoder_prefix.get(packer)
        if not packer_prefix:
            packer_prefix = self.store.add_encoder(packer)
        keytups = [key for key, _ in reversed(items)]
        phys = keycoder.packs(self.prefix, keytups)
        io = cStringIO.StringIO()

        if len(items) == 1:
            io.write(packer_prefix + packer.pack(items[0][1]))
        else:
            io.write(keycoder.pack_int(len(items)))
            for _, data in items:
                io.write(keycoder.pack_int(len(data)))
            io.write(packer_prefix)
            concat = ''.join(data for _, data in items)
            io.write(packer.pack(concat))
        return phys, io.getvalue()

    def _split_batch(self, rec, txn):
        assert rec.key and rec.batch
        assert False
        it = _iter(txn, rec.key, None, None, None, None, None)
        keys, data = next(it, (None, None))
        assert len(keys) > 1 and rec.key in keys, \
            'Physical key missing: %r' % (rec.key,)

        assert 0
        objs = self.encoder.loads_many(self._decompress(data))
        for i, obj in enumerate(objs):
            if keys[-(1 + i)] != rec.key:
                self.put(Record(self, obj), txn, key=keys[-(1 + i)])
        (txn or self.engine).delete(phys)
        rec.key = None
        rec.batch = False

    def _reassign_key(self, rec, txn):
        if rec.key and not self.derived_keys:
            return rec.key
        elif self.txn_key_func:
            return tuplize(self.txn_key_func(txn or self.engine, rec.data))
        return tuplize(self.key_func(rec.data))

    def puts(self, recs, txn=None, packer=None, eat=True):
        """Invoke :py:meth:`put` for each element in the iterable `recs`. If
        `eat` is ``True``, returns the number of items processed, otherwise
        returns an iterator that lazily calls :py:meth:`put` and yields its
        return values."""
        return _eat(eat, (self.put(rec, txn, packer) for rec in recs), True)

    def putitems(self, it, txn=None, packer=None, eat=True):
        """Invoke :py:meth:`put(y, key=x)` for each (x, y) in the iterable
        `it`. If `eat` is ``True``, returns the number of items processed,
        otherwise returns an iterator that lazily calls :py:meth:`put` and
        yields its return values."""
        return _eat(eat, (self.put(y, txn, packer, x) for x, y in it), True)

    def put(self, rec, txn=None, packer=None, key=None, blind=False):
        """Create or overwrite a record.

            `rec`:
                The value to put; may either be a value recognised by the
                collection's `encoder` or a :py:class:`Record` instance, such
                as returned by ``get(..., rec=True)``. It is strongly advised
                to prefer use of :py:class:`Record` instances during
                read-modify-write transactions as it allows :py:meth:`put` to
                avoid many database operations.

            `txn`:
                Transaction to use, or ``None`` to indicate the default
                behaviour of the storage engine.

            `packer`:
                Encoding to use to compress the value. Defaults to
                :py:attr:`Collection.packer`.

            `key`:
                If specified, overrides the use of collection's key function
                and forces a specific key. Use with caution.

            `blind`:
                If ``True``, skip checks for any old record assigned the same
                key. Automatically enabled when a collection has no indices, or
                when `blind=` is passed to :py:class:`Collection`'s
                constructor.

                While this significantly improves performance, enabling it for
                a collection with indices and in the presence of old records
                with the same key will lead to inconsistent indices.
                :py:meth:`Index.iteritems` will issue a warning and discard
                obsolete keys when this is detected, however other index
                methods will not.
        """
        if type(rec) is not Record:
            rec = Record(self, rec)
        obj_key = key or self._reassign_key(rec, txn)
        index_keys = self._index_keys(obj_key, rec.data)
        txn = txn or self.engine

        if rec.coll is self and rec.key:
            if rec.batch:
                # Old key was part of a batch, explode the batch.
                self._split_batch(rec, txn)
            elif rec.key != obj_key:
                # New version has changed key, delete old.
                txn.delete(keycoder.packs(self.prefix, rec.key))
            if index_keys != rec.index_keys:
                for index_key in rec.index_keys or ():
                    txn.delete(index_key)
        elif self.indices and not (blind or self.blind):
            # TODO: delete() may be unnecessary when no indices are defined
            # Old key might already exist, so delete it.
            self.delete(obj_key)

        packer = packer or self.packer
        packer_prefix = self.store._encoder_prefix.get(packer)
        if not packer_prefix:
            packer_prefix = self.store.add_encoder(packer)
        txn.put(keycoder.packs(self.prefix, obj_key),
                packer_prefix + packer.pack(self.encoder.pack(rec.data)))
        for index_key in index_keys:
            txn.put(index_key, '')
        rec.coll = self
        rec.key = obj_key
        rec.index_keys = index_keys
        return rec

    def deletes(self, objs, txn=None, eat=True):
        """Invoke :py:meth:`delete` for each element in the iterable `objs`. If
        `eat` is ``True``, returns a tuple containing the number of keys
        processed, and the number of records deleted, otherwise returns an
        iterator that lazily calls :py:meth:`delete` and yields its return
        values.

        ::

            keys = request.form['names'].split(',')
            for rec in coll.deletes(key):
                if rec:
                    print '%(name)s was deleted.' % (rec.data,)

            # Summary version.
            keys, deleted = coll.deletes(request.form['names'].split(','))
            print 'Deleted %d names of %d provided.' % (deleted, keys)
        """
        return _eat(eat, (self.delete(obj) for obj in objs))

    def delete(self, obj, txn=None):
        """Delete a record by key or using a :py:class:`Record` instance. The
        deleted record is returned if it existed.

        `obj`:
            Record to delete; may be a :py:class:`Record` instance, or a tuple,
            or a primitive value.
        """
        if isinstance(obj, Record):
            rec = obj
        else:
            rec = self.get(obj, rec=True)
        if rec and rec.key: # todo rec.key must be set
            if rec.batch:
                self._split_batch(rec, txn)
            else:
                delete = (txn or self.engine).delete
                delete(keycoder.packs(self.prefix, rec.key))
                for index_key in rec.index_keys or ():
                    delete(index_key)
            rec.key = None
            rec.batch = False
            rec.index_keys = None
            return rec

    def delete_values(self, vals, txn=None, eat=True):
        """Invoke :py:meth:`delete_value` for each element in the iterable
        `vals`. If `eat` is ``True``, returns a tuple containing the number of
        keys processed, and the number of records deleted, otherwise returns an
        iterator that lazily calls :py:meth:`delete_value` and yields its
        return values."""
        return _eat(eat, (self.delete_value(v) for v in vals))

    def delete_value(self, val, txn=None):
        """Delete a record value without knowing its key. The deleted record is
        returned, if it existed.

        `Note`: it is impossible (and does not make sense) to delete by value
        when ``derived_keys=False``, since the key function will generate an
        unrelated ID for the value. Example:

        ::

            coll = Collection(store, 'people',
                key_func=lambda person: person['name'],
                derived_keys=True)
            val = {"name": "David"}
            coll.put(val)
            # key_func will generate the correct key:
            call.delete_value(val)
        """
        assert self.derived_keys
        return self.delete(self.key_func(val), txn)

class Store(object):
    """Represents access to the underlying storage engine, and manages
    counters.

        `prefix`:
            Prefix for all keys used by any associated object (record, index,
            counter, metadata). This allows the storage engine's key space to
            be shared amongst several users.
    """
    def __init__(self, engine, prefix=''):
        self.engine = engine
        self.prefix = prefix
        self._encoder_prefix = (
            dict((e, keycoder.pack_int(1 + i))
                 for i, e in enumerate(_ENCODERS)))
        self._prefix_encoder = (
            dict((keycoder.pack_int(1 + i), e)
                 for i, e in enumerate(_ENCODERS)))
        self._meta = Collection(self, '\x00meta', _idx=3,
            encoder=KEY_ENCODER, key_func=lambda t: t[:2])
        self._encoder_coll = Collection(self, '\x00encoders', _idx=2,
            encoder=KEY_ENCODER, key_func=ITEMGETTER_0)
        self._info_coll = Collection(self, '\x00collections', _idx=0,
            encoder=KEY_ENCODER, key_func=ITEMGETTER_0)
        self._counter_coll = Collection(self, '\x00counters', _idx=1,
            encoder=KEY_ENCODER, key_func=ITEMGETTER_0)

    def collection(self, name, *args, **kwargs):
        """Shorthand for `centidb.Collection(self, *args, **kwargs)`."""
        return Collection(self, name, *args, **kwargs)

    _INFO_KEYS = ('name', 'idx', 'index_for')
    def _get_info(self, name, idx=None, index_for=None):
        t = self._info_coll.get(name)
        if not t:
            idx = idx or self.count('\x00collections_idx', init=10)
            t = self._info_coll.put((name, idx, index_for)).data
        assert t == (name, idx or t[1], index_for)
        return dict(itertools.izip(self._INFO_KEYS, t))

    def add_encoder(self, encoder):
        """Register an :py:class:`Encoder` so that :py:class:`Collection` can
        find it during decompression/unpacking."""
        try:
            return self._encoder_prefix[encoder]
        except KeyError:
            t = self._encoder_coll.get(encoder.name)
            if not t:
                idx = self.count('\x00encoder_idx', init=10)
                assert idx <= 240
                t = self._encoder_coll.put((encoder.name, idx)).data
                self._encoder_prefix[encoder] = keycoder.pack_int(idx)
                self._prefix_encoder[keycoder.pack_int(idx)] = encoder
            return keycoder.pack_int(t[1])

    def get_encoder(self, prefix):
        """Get a registered :py:class:`Encoder` given its string prefix, or
        raise an error."""
        try:
            return self._prefix_encoder[prefix]
        except KeyError:
            dct = dict((v, k) for k, v in self._encoder_coll.itervalues())
            idx = keycoder.unpack_int_s(prefix)
            raise ValueError('Missing encoder: %r / %d' % (dct.get(idx), idx))

    def count(self, name, n=1, init=1, txn=None):
        """Increment a counter and return its previous value. The counter is
        created if it doesn't exist.

            `name`:
                Name of the counter. Names beginning with ``"\\x00"`` are
                reserved by the implementation.

            `n`:
                Number to add to the counter. If ``0`` or ``None``, return the
                counter's value without incrementing it.

            `init`:
                Initial value to give counter if it doesn't exist.

            `txn`:
                Transaction to use, or ``None`` to indicate the default
                behaviour of the storage engine.
        """
        default = (name, init)
        rec = self._counter_coll.get(name, default, rec=True, txn=txn)
        val = long(rec.data[1])
        if n:
            rec.data = (name, val + n)
            self._counter_coll.put(rec, txn=txn)
        return val

# Hack: disable speedups while testing or reading docstrings.
if os.path.basename(sys.argv[0]) not in ('sphinx-build', 'pydoc') and \
        os.getenv('CENTIDB_NO_SPEEDUPS') is None:
    try:
        from _centidb import *
    except ImportError:
        pass

#: Encode Python tuples using keycoder.packs()/keycoder.unpacks().
KEY_ENCODER = Encoder('key', functools.partial(keycoder.unpack, ''),
                             functools.partial(keycoder.packs, ''))

#: Encode Python objects using the cPickle version 2 protocol."""
PICKLE_ENCODER = Encoder('pickle', lambda b: pickle.loads(str(b)),
                         functools.partial(pickle.dumps, protocol=2))

#: Perform no compression at all.
PLAIN_PACKER = Encoder('plain', str, lambda o: o)

#: Compress bytestrings using zlib.compress()/zlib.decompress().
ZLIB_PACKER = Encoder('zlib', zlib.decompress, zlib.compress)

_ENCODERS = (KEY_ENCODER, PICKLE_ENCODER, PLAIN_PACKER, ZLIB_PACKER)

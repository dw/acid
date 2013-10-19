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
Iterator implementations.
"""

from __future__ import absolute_import

import acid
from acid import keylib


def decode_offsets(s):
    """Given a string, decode an array of offsets at the start of the string. A
    varint indicates the length of the array, followed by one varint for each
    element, which is a delta from the previous element, starting at 0.
    """
    ba = bytearray(s)
    length = len(ba)
    count, pos = keylib.read_int(ba, 0, length, 0)

    out = [0]
    for _ in xrange(count):
        i, pos = keylib.read_int(ba, pos, length, 0)
        out.append(out[-1] + i)
    return out, pos


class Result(object):
    """Interface for a single element from an iterator's result set. Iterator
    classes do not return :py:class:`Result` instances, they only return
    objects satisfying the same interface."""

    #: Equivalent to `keys[index]`. Always the sole key in a non-batch
    #: iterator, or the current key in a batch iterator.
    key = None

    #: List of :py:class:`acid.Key` decoded from the physical engine key.
    keys = None

    #: Object satisfying the :py:class:`buffer` interface that represents the
    #: raw record data.
    data = None

    #: For a :py:class:`BatchRangeIterator`, the current key. Unused for a
    #: :py:class:`RangeIterator`.
    key = None

    #: For a :py:class:`BatchRangeIterator`, the current key's index into
    #: the batch. Unused for :py:class:`RangeIterator`.
    index = None


class Iterator(object):
    # Various defaults set here to avoid necessity for repeat initialization.
    _lo = None
    _hi = None
    _lo_pred = bool
    _hi_pred = bool
    _remain = -1

    def set_lo(self, key, closed=True):
        """Set the lower bound to `key`. If `closed` is ``True``, include the
        lower bound in the result set, otherwise exclude it."""
        self._lo = keylib.Key(key)
        self._lo_pred = getattr(self._lo, ('__lt__', '__le__')[closed])

    def set_hi(self, key, closed=False):
        """Set the upper bound to `key`. If `closed` is ``True``, include the
        upper bound in the result set, otherwise exclude it."""
        self._hi = keylib.Key(key)
        self._hi_pred = getattr(self._hi, ('__gt__', '__ge__')[closed])

    def set_prefix(self, key):
        """Set the lower bound to `>= key` and the upper bound to `<
        prefix_bound(key)`. If :py:meth:`prefix_bound
        <acid.keylib.Key.prefix_bound>` returns ``None``, then set no upper
        bound (i.e. iterate to the end of the collection).
        """
        key = keylib.Key(key)
        self.set_lo(key, True)
        pbound = key.prefix_bound()
        if pbound is not None:
            self.set_hi(pbound, False)

    def set_max(self, max_):
        """Set the maximum size of the result set."""
        assert max_ >= 0, 'Result set size must be >= 0'
        self._remain = max_

    def set_exact(self, key):
        """Set the lower and upper bounds such that `key` is the only key
        returned, if it exists."""
        key = keylib.Key(key)
        self._lo = key
        self._hi = key
        self._lo_pred = key.__le__
        self._hi_pred = key.__ge__


class RangeIterator(Iterator):
    """Provides bidirectional iteration of a range of keys.

        `engine`:
            :py:class:`acid.engines.Engine` instance to iterate.
        `prefix`:
            Bytestring prefix for all keys.
    """

    def __init__(self, engine, prefix):
        self.engine = engine
        self.prefix = prefix

    @property
    def key(self):
        return self.keys[0]

    def _step(self):
        """Step the iterator once, saving the new key and data. Returns True if
        the iterator is still within the bounds of the collection prefix,
        otherwise False."""
        keys_raw, self.data = next(self.it, ('', ''))
        keys = keylib.KeyList.from_raw(keys_raw, self.prefix)
        self.keys = keys
        return keys is not None

    def forward(self):
        """Begin yielding objects satisfying the :py:class:`Result` interface,
        from `lo`..`hi`. Note the yielded object is reused, so references to it
        should not be held."""
        if self._lo is None:
            key = self.prefix
        else:
            key = self._lo.to_raw(self.prefix)

        self.it = self.engine.iter(key, False)
        # Fetch the first key. If _step() returns false, then first key is
        # beyond collection prefix. Cease iteration.
        go = self._step()

        # When lo(closed=False), skip the start key.
        if go and not self._lo_pred(self.keys[0]):
            go = self._step()

        remain = self._remain
        while go and remain and self._hi_pred(self.keys[0]):
            yield self
            remain -= 1
            go = self._step()

    def reverse(self):
        """Begin yielding objects satisfying the :py:class:`Result` interface,
        from `hi`..`lo`. Note the yielded object is reused, so references to it
        should not be held."""
        if self._hi is None:
            key = keylib.next_greater(self.prefix)
        else:
            key = self._hi.to_raw(self.prefix)

        self.it = self.engine.iter(key, True)

        # We may have seeked to first record of next prefix, so skip first
        # returned result.
        go = self._step()
        if not go:
            go = self._step()

        # We should now be positioned on the first record >= self._hi. When
        # hi(closed=False), skip the first result.
        if go and not self._hi_pred(self.keys[0]):
            go = self._step()

        remain = self._remain
        while go and remain and self._lo_pred(self.keys[0]):
            yield self
            remain -= 1
            go = self._step()


class BatchRangeIterator(Iterator):
    """Provides bidirectional iteration of a range of keys, treating >1-length
    keys as batch records.

        `engine`:
            :py:class:`acid.engines.Engine` instance to iterate.

        `prefix`:
            Bytestring prefix for all keys.

        `compressor`:
            :py:class:`acid.encoders.Compressor` instance used to decompress
            batch keys.
    """
    _max_phys = -1
    _index = 0

    def __init__(self, engine, prefix, compressor):
        self.engine = engine
        self.prefix = prefix
        self.compressor = compressor

    def set_max_phys(self, max_phys):
        """Set the maximum number of physical records to visit."""
        self._max_phys = max_phys

    def _step(self):
        """Progress one step through the batch, or fetch another physical
        record if the batch is exhausted. Returns ``True`` so long as the
        collection range has not been exceeded."""
        # Previous record was non-batch, or previous batch exhausted. Need to
        # fetch another record.
        if not self._index:
            # Have we visited maximum number of physical records? If so, stop
            # iteration.
            if not self._max_phys:
                return False
            self._max_phys -= 1

            # Get the next record and decode its key. from_raw() returns None
            # if the key's prefix doesn't match self.prefix, which indicates
            # we've reached the end of the collection.
            phys_key, self.raw = next(self.it, ('', ''))
            self.keys = keylib.KeyList.from_raw(phys_key, self.prefix)
            if not self.keys:
                return False

            self.phys_key = phys_key
            lenk = len(self.keys)
            # Single record.
            if lenk == 1:
                self.key = self.keys[0]
                self.data = self.raw
                self._index = 0
                return True

            # Decode the array of logical record offsets and save it, along
            # with the decompressed concatenation of all records.
            self.offsets, dstart = decode_offsets(self.raw)
            self.concat = self.compressor.unpack(buffer(self.raw, dstart))
            self._index = lenk

        self._index -= 1
        if self._reverse:
            idx = self._index
        else:
            idx = (len(self.keys) - self._index) - 1
        start = self.offsets[idx]
        stop = self.offsets[idx + 1]
        self.key = self.keys[-1 + -idx]
        self.data = self.concat[start:stop]
        return True

    def batch_items(self):
        """Yield `(key, value)` pairs that are present in the current batch.
        Used to implement batch split, may be removed in future."""
        for index in xrange(len(self.keys) -1, -1, -1):
            start = self.offsets[index]
            stop = self.offsets[index + 1]
            key = self.keys[-1 + -index]
            data = self.concat[start:stop]
            yield keylib.Key(key), data

    def forward(self):
        """Begin yielding objects satisfying the :py:class:`Result` interface,
        from `lo`..`hi`. Note the yielded object is reused, so references to it
        should not be held."""
        if self._lo is None:
            key = self.prefix
        else:
            key = self._lo.to_raw(self.prefix)

        self.it = self.engine.iter(key, False)
        self._reverse = False
        # Fetch the first key. If _step() returns false, then first key is
        # beyond collection prefix. Cease iteration.
        go = self._step()

        # When lo(closed=False), skip the start key.
        while go and not self._lo_pred(self.key):
            go = self._step()

        remain = self._remain
        while go and remain and self._hi_pred(self.key):
            yield self
            remain -= 1
            go = self._step()

    def reverse(self):
        """Begin yielding objects satisfying the :py:class:`Result` interface,
        from `lo`..`hi`. Note the yielded object is reused, so references to it
        should not be held."""
        if self._hi is None:
            key = keylib.next_greater(self.prefix)
        else:
            key = self._hi.to_raw(self.prefix)

        self.it = self.engine.iter(key, True)
        self._reverse = True

        # Fetch the first key. If _step() returns false, then we may have
        # seeked to first record of next prefix, so skip first returned result.
        go = self._step()
        if not go:
            go = self._step()

        # When hi(closed=False), skip the start key.
        while go and not self._hi_pred(self.key):
            go = self._step()

        remain = self._remain
        while go and remain and self._lo_pred(self.key):
            yield self
            remain -= 1
            go = self._step()


def from_args(it, key, lo, hi, prefix, reverse, max_, include, max_phys):
    """This function is a stand-in until the core.py API is refurbished."""
    if key:
        it.set_exact(key)
        return it.forward()
    elif prefix:
        it.set_prefix(prefix)
    else:
        if lo:
            it.set_lo(lo)
        if hi:
            it.set_hi(hi, include)

    if max_:
        it.set_max(max_)
    if max_phys:
        it.set_max_phys(max_phys)

    if reverse:
        return it.reverse()
    else:
        return it.forward()


if acid._use_speedups:
    try:
        from acid._iterators import *
        from acid._keylib import decode_offsets
    except ImportError:
        pass

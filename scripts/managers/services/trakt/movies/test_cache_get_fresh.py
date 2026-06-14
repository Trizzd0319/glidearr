"""TraktMovieCacheManager.get_fresh — single-pass freshness+read.

The enrich_movies migration (ML Step 8) relies on get_fresh's was_fresh flag being
byte-identical to is_fresh() so the cache_hit/fetched tallies don't shift. The one
subtlety is a fresh-but-corrupt entry: is_fresh()=True while get()=None, and
get_fresh must report (True, None) — fresh, but unreadable. These assert exactly
that equivalence across the missing / fresh / expired / corrupt states.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager


class _Logger:
    def log_debug(self, *a, **k):
        pass


def _cache(tmp_path, ttl=1000):
    # Bypass the heavy BaseManager __init__; get_fresh only needs base_dir/ttl/logger.
    c = TraktMovieCacheManager.__new__(TraktMovieCacheManager)
    c.base_dir = Path(tmp_path)
    c.ttl = ttl
    c.logger = _Logger()
    return c


def _write_gz(path: Path, data):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)


def test_get_fresh_missing(tmp_path):
    c = _cache(tmp_path)
    assert c.get_fresh(999) == (False, None)
    assert c.is_fresh(999) is False              # equivalence


def test_get_fresh_present_and_fresh(tmp_path):
    c = _cache(tmp_path)
    data = {"cast": [], "crew": []}
    _write_gz(c._path(1), data)
    was_fresh, got = c.get_fresh(1)
    assert was_fresh is True and got == data
    assert c.is_fresh(1) is True                 # equivalence
    assert c.get(1) == data


def test_get_fresh_expired(tmp_path):
    c = _cache(tmp_path, ttl=10)
    _write_gz(c._path(2), {"cast": [], "crew": []})
    old = time.time() - 100                       # well past the 10s ttl
    os.utime(c._path(2), (old, old))
    assert c.get_fresh(2) == (False, None)
    assert c.is_fresh(2) is False                 # equivalence


def test_get_fresh_corrupt_but_fresh(tmp_path):
    c = _cache(tmp_path)
    # a fresh file that isn't valid gzip JSON -> is_fresh True, get None
    c._path(3).write_bytes(b"not a gzip payload")
    was_fresh, got = c.get_fresh(3)
    assert was_fresh is True and got is None      # THE edge: fresh yet unreadable
    assert c.is_fresh(3) is True                  # is_fresh agrees it's fresh
    assert c.get(3) is None                       # get agrees it's unreadable

"""Runtime cache readers must treat a 0-byte poison file as a clean miss (never a
fresh hit, never read as phantom-empty enrichment) — movie + show twins."""
from __future__ import annotations

import gzip
import json

from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager
from scripts.managers.services.trakt.shows.cache import TraktShowCacheManager


class _Log:
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _write_gz(path, data):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)


def test_movie_cache_zero_byte_is_miss(tmp_path):
    m = TraktMovieCacheManager.__new__(TraktMovieCacheManager)
    m.logger, m.ttl, m.base_dir = _Log(), 999_999, tmp_path
    m._dirs = {k: tmp_path for k in ("people", "ratings", "summary", "related")}

    (tmp_path / "5.json.gz").write_bytes(b"")                 # 0-byte poison
    assert m.get_people(5) == {} and m.get_ratings(5) == {}
    assert m.is_fresh(5) is False
    assert m.get_fresh(5) == (False, None)

    _write_gz(tmp_path / "6.json.gz", {"cast": [{"name": "X"}]})
    assert m.get_people(6) == {"cast": [{"name": "X"}]}
    assert m.is_fresh(6) is True


def test_show_cache_zero_byte_is_miss(tmp_path):
    s = TraktShowCacheManager.__new__(TraktShowCacheManager)
    s.logger, s.ttl = _Log(), 999_999
    s._dirs = {k: tmp_path for k in ("people", "ratings", "related", "summary")}

    (tmp_path / "7.json.gz").write_bytes(b"")
    assert s.get_people(7) == {} and s.get_ratings(7) == {}
    assert s.get_related(7) == []
    assert s.is_fresh(7) is False

    _write_gz(tmp_path / "8.json.gz", {"cast": []})
    assert s.is_fresh(8) is True

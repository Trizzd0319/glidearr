"""Cache-hygiene tests: 0-byte bucket files (killed write / file-sync dehydration)
must be treated as uncached so the daemon re-fetches them, and purged at boot."""
from __future__ import annotations

import gzip
import json
import os
import time

import scripts.support.daemons.enrich_daemon as ed


def _write_gz(path, data):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)


def test_is_cached_rejects_zero_byte_and_missing(tmp_path):
    _write_gz(tmp_path / "1.json.gz", {"a": 1})
    (tmp_path / "2.json.gz").write_bytes(b"")          # 0-byte poison
    assert ed.is_cached(tmp_path, 1) is True
    assert ed.is_cached(tmp_path, 2) is False          # → re-fetch
    assert ed.is_cached(tmp_path, 3) is False          # missing


def test_is_cached_rejects_stale(tmp_path):
    good = tmp_path / "1.json.gz"
    _write_gz(good, {"a": 1})
    old = time.time() - ed.CACHE_TTL_S - 100
    os.utime(good, (old, old))
    assert ed.is_cached(tmp_path, 1) is False


def test_purge_empty_caches(tmp_path, monkeypatch):
    (tmp_path / "movies").mkdir()
    (tmp_path / "shows").mkdir()
    _write_gz(tmp_path / "movies" / "1.json.gz", {"x": 1})
    (tmp_path / "movies" / "2.json.gz").write_bytes(b"")
    (tmp_path / "shows" / "3.json.gz").write_bytes(b"")
    monkeypatch.setattr(ed, "MOVIE_BUCKETS", {"people": tmp_path / "movies"})
    monkeypatch.setattr(ed, "SHOW_BUCKETS", {"people": tmp_path / "shows"})

    assert ed.purge_empty_caches(dry_run=False) == 2
    assert (tmp_path / "movies" / "1.json.gz").exists()          # good kept
    assert not (tmp_path / "movies" / "2.json.gz").exists()      # 0-byte gone
    assert not (tmp_path / "shows" / "3.json.gz").exists()

    (tmp_path / "movies" / "4.json.gz").write_bytes(b"")
    assert ed.purge_empty_caches(dry_run=True) == 1              # counts but…
    assert (tmp_path / "movies" / "4.json.gz").exists()          # …does not delete in dry_run

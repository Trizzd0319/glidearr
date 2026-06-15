"""Tests for the run-scoped write-through movie_files cache on
RadarrCacheMovieFilesManager.

The real load()/save() are exercised against a tmp parquet (path monkeypatched);
the invariant under test is that the in-memory cache is indistinguishable from
always re-reading the parquet from disk."""
from __future__ import annotations

import hashlib

import pandas as pd
import pandas.testing as pdt

from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(tmp_path):
    """Build the manager without __init__ (which needs config/registry), wire only
    what load()/save() touch, and point the parquet path at tmp."""
    m = RadarrCacheMovieFilesManager.__new__(RadarrCacheMovieFilesManager)
    m.logger = _Log()
    m._df_cache = {}
    m._parquet_path = lambda inst: tmp_path / f"{inst}.parquet"
    return m


def _df(rows):
    return pd.DataFrame(rows)


def test_load_after_save_serves_cache_even_if_file_deleted(tmp_path):
    m = _mgr(tmp_path)
    assert m.save("standard", _df([{"title": "A", "year": 2001, "movie_id": 1}])) is True
    m._parquet_path("standard").unlink()          # remove the on-disk parquet
    assert list(m.load("standard")["title"]) == ["A"]   # cache still serves it


def test_load_returns_a_copy(tmp_path):
    m = _mgr(tmp_path)
    m.save("standard", _df([{"title": "A", "year": 2001, "movie_id": 1}]))
    first = m.load("standard")
    first.loc[0, "title"] = "MUTATED"             # mutate the loaded frame, do NOT save
    assert m.load("standard").loc[0, "title"] == "A"    # next load is unaffected


def test_cache_miss_reads_and_coerces_from_disk(tmp_path):
    m = _mgr(tmp_path)
    col = next(iter(RadarrCacheMovieFilesManager._NUMERIC_COLUMNS))
    # Write the parquet directly (bypassing save) with a numeric column stored as text.
    _df([{"title": "A", col: "5"}]).to_parquet(m._parquet_path("standard"), index=False)
    out = m.load("standard")                      # empty cache -> disk read + coercion
    assert pd.api.types.is_numeric_dtype(out[col])
    assert out.loc[0, col] == 5


def test_save_copies_in_so_later_mutation_does_not_leak(tmp_path):
    m = _mgr(tmp_path)
    df = _df([{"title": "A", "year": 2001, "movie_id": 1}])
    m.save("standard", df)
    df.loc[0, "title"] = "MUTATED_AFTER_SAVE"     # keep mutating the local frame
    assert m.load("standard").loc[0, "title"] == "A"    # cache holds the saved snapshot


def test_per_instance_isolation(tmp_path):
    m = _mgr(tmp_path)
    m.save("standard", _df([{"title": "S", "year": 2001, "movie_id": 1}]))
    m.save("ultra", _df([{"title": "U", "year": 2002, "movie_id": 2}]))
    assert list(m.load("standard")["title"]) == ["S"]
    assert list(m.load("ultra")["title"]) == ["U"]


def test_reset_run_cache_forces_fresh_disk_read(tmp_path):
    m = _mgr(tmp_path)
    m.save("standard", _df([{"title": "A", "year": 2001, "movie_id": 1}]))
    # Overwrite the parquet out-of-band; cache should still serve the old frame...
    _df([{"title": "B", "year": 2002, "movie_id": 2}]).to_parquet(
        m._parquet_path("standard"), index=False)
    assert list(m.load("standard")["title"]) == ["A"]
    m.reset_run_cache()                            # ...until the cache is reset
    assert list(m.load("standard")["title"]) == ["B"]


def test_cache_hit_equals_disk_reload(tmp_path):
    # The core behaviour-preservation invariant: a cache hit == a fresh disk read.
    m = _mgr(tmp_path)
    m.save("standard", _df([
        {"title": "B", "year": 2002, "movie_id": 2, "watchability_score": 10},
        {"title": "A", "year": 2001, "movie_id": 1, "watchability_score": 42},
    ]))
    hit = m.load("standard")
    m.reset_run_cache()
    disk = m.load("standard")
    pdt.assert_frame_equal(hit.reset_index(drop=True), disk.reset_index(drop=True))


def test_cache_does_not_change_the_written_parquet(tmp_path):
    df = _df([{"title": "B", "year": 2002, "movie_id": 2},
              {"title": "A", "year": 2001, "movie_id": 1}])
    m = _mgr(tmp_path)
    m.save("standard", df)
    warm = m._parquet_path("standard").read_bytes()
    # Same save with the cache disabled (the pre-change behaviour).
    m2 = _mgr(tmp_path)
    m2._df_cache = None
    m2._parquet_path = lambda inst: tmp_path / "nocache.parquet"
    m2.save("standard", df)
    cold = (tmp_path / "nocache.parquet").read_bytes()
    assert hashlib.sha256(warm).hexdigest() == hashlib.sha256(cold).hexdigest()

"""Regression guard for the cache key→path collision.

CacheKeyBuilder used Path.with_suffix, which treats the text after the last dot as
an extension and swaps it out. A single dotted key part like "radarr.tags.standard"
therefore lost ".standard" → "radarr.tags.json", collapsing every per-instance key
onto ONE shared file and cross-contaminating instances. These tests assert distinct
per-instance paths AND values through the REAL CacheKeyBuilder / GlobalCacheManager —
a fake that stores by the full key string (as several other suites do) cannot catch
this, because the collision only exists once a key becomes a filesystem path.
"""
from __future__ import annotations

from pathlib import Path

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder


def _kb(tmp_path) -> CacheKeyBuilder:
    return CacheKeyBuilder(base_dir=str(tmp_path))


def _gc(tmp_path) -> GlobalCacheManager:
    gc = GlobalCacheManager()
    # Point every path builder at the throwaway dir so we never touch the real cache.
    gc.key_builder.base_dir = Path(tmp_path)
    gc.json_handler.key_builder.base_dir = Path(tmp_path)
    gc.cache_root = Path(tmp_path)
    return gc


# ── path-level: per-instance dotted keys must NOT collapse ─────────────────────────
def test_dotted_per_instance_keys_get_distinct_paths(tmp_path):
    kb = _kb(tmp_path)
    paths = {inst: kb.build_cache_path(f"radarr.tags.{inst}") for inst in ("standard", "ultra", "test")}
    names = {inst: p.name for inst, p in paths.items()}
    assert len(set(names.values())) == 3, names               # three distinct files
    for inst, name in names.items():
        assert inst in name                                   # the instance survives in the filename
    assert "radarr.tags.json" not in set(names.values())      # not the old collapsed name


def test_per_instance_movie_variants_do_not_collide(tmp_path):
    # Same instance, three payloads: the trailing dot-segment (.full/.enriched/.dataframe)
    # was stripped, collapsing all three onto radarr.movies.<inst>.json.
    kb = _kb(tmp_path)
    full = kb.build_cache_path("radarr.movies.standard.full")
    enriched = kb.build_cache_path("radarr.movies.standard.enriched")
    dataframe = kb.build_cache_path("radarr.movies.standard.dataframe")
    assert len({full, enriched, dataframe}) == 3


def test_slash_keys_remain_per_instance(tmp_path):
    kb = _kb(tmp_path)
    a = kb.build_cache_path(*"radarr/custom_formats/standard".split("/"))
    b = kb.build_cache_path(*"radarr/custom_formats/ultra".split("/"))
    assert a != b
    assert a.name == "standard.json" and b.name == "ultra.json"


# ── path-level: embedded suffixes must be preserved, not doubled ───────────────────
def test_embedded_parquet_suffix_not_doubled(tmp_path):
    kb = _kb(tmp_path)
    key = "radarr/standard/library_series_enriched.parquet"
    p = kb.build_parquet_path(*key.split("/"))
    assert p.name == "library_series_enriched.parquet"        # exactly one .parquet


def test_embedded_last_updated_suffix_not_doubled(tmp_path):
    kb = _kb(tmp_path)
    key = "radarr/standard/tags.last_updated"
    p = kb.build_cache_path(*key.split("/"), suffix=".last_updated")
    assert p.name == "tags.last_updated"                      # not tags.last_updated.last_updated


def test_plain_key_gets_json_suffix(tmp_path):
    kb = _kb(tmp_path)
    p = kb.build_cache_path(*"radarr/standard/library".split("/"))
    assert p.name == "library.json"


# ── value-level: the real cache must isolate instances end-to-end ─────────────────
def test_global_cache_isolates_dotted_instances(tmp_path):
    gc = _gc(tmp_path)
    gc.set("radarr.tags.standard", ["std-tag"])
    gc.set("radarr.tags.ultra", ["ultra-tag"])
    gc.set("radarr.tags.test", ["test-tag"])
    assert gc.get("radarr.tags.standard") == ["std-tag"]
    assert gc.get("radarr.tags.ultra") == ["ultra-tag"]       # would have read "std/ultra/test" last-write-wins before
    assert gc.get("radarr.tags.test") == ["test-tag"]


def test_global_cache_isolates_movie_variants(tmp_path):
    gc = _gc(tmp_path)
    gc.set("radarr.movies.standard.full", [{"id": 1}])
    gc.set("radarr.movies.standard.enriched", [{"id": 1, "enriched": True}])
    gc.set("radarr.movies.standard.dataframe", [{"id": 1, "df": True}])
    assert gc.get("radarr.movies.standard.full") == [{"id": 1}]
    assert gc.get("radarr.movies.standard.enriched") == [{"id": 1, "enriched": True}]
    assert gc.get("radarr.movies.standard.dataframe") == [{"id": 1, "df": True}]

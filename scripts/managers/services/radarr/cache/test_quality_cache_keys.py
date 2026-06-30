"""RadarrQualityCacheManager: a refresh_* write must be visible to the matching get_*.

Previously the writer and reader used different cache keys (refresh wrote
"radarr.quality_profiles.<inst>" / "radarr.custom_formats.<inst>"; the getters read
"radarr.<inst>.quality.*"), so a read never saw what a refresh wrote — and the write
itself raised TypeError because it passed compressed= to a set() that doesn't accept
it. Exercised through the REAL GlobalCacheManager so the keys actually become files.
"""
from __future__ import annotations

from pathlib import Path

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.services.radarr.cache.quality import RadarrQualityCacheManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _Api:
    """Serves per-instance qualityprofile / customformat lists."""
    def __init__(self, profiles, cfs):
        self._profiles, self._cfs = profiles, cfs

    def _make_request(self, inst, endpoint, fallback=None):
        if endpoint == "qualityprofile":
            return [dict(p) for p in self._profiles.get(inst, [])]
        if endpoint == "customformat":
            return [dict(c) for c in self._cfs.get(inst, [])]
        return fallback


def _gc(tmp_path) -> GlobalCacheManager:
    gc = GlobalCacheManager()
    gc.key_builder.base_dir = Path(tmp_path)
    gc.json_handler.key_builder.base_dir = Path(tmp_path)
    gc.cache_root = Path(tmp_path)
    return gc


def _mgr(tmp_path, profiles, cfs):
    m = RadarrQualityCacheManager.__new__(RadarrQualityCacheManager)
    m.radarr_api = _Api(profiles, cfs)
    m.global_cache = _gc(tmp_path)
    m.logger = _Logger()
    return m


def test_refresh_then_get_quality_profiles_round_trips(tmp_path):
    m = _mgr(tmp_path, {"standard": [{"id": 1, "name": "HD-1080p"}]}, {})
    assert m.get_quality_profiles("standard") == []           # cold
    m.refresh_quality_profiles("standard")
    assert m.get_quality_profiles("standard") == [{"id": 1, "name": "HD-1080p"}]


def test_refresh_then_get_custom_formats_round_trips(tmp_path):
    m = _mgr(tmp_path, {}, {"standard": [{"id": 9, "name": "x265"}]})
    assert m.get_custom_formats("standard") == []             # cold
    m.refresh_custom_formats("standard")
    assert m.get_custom_formats("standard") == [{"id": 9, "name": "x265"}]


def test_quality_cache_is_per_instance(tmp_path):
    profiles = {"standard": [{"id": 1, "name": "HD-1080p"}], "ultra": [{"id": 2, "name": "Remux-2160p"}]}
    cfs = {"standard": [{"id": 9, "name": "x265"}], "ultra": [{"id": 10, "name": "x265-4k"}]}
    m = _mgr(tmp_path, profiles, cfs)
    for inst in ("standard", "ultra"):
        m.refresh_quality_profiles(inst)
        m.refresh_custom_formats(inst)
    assert m.get_quality_profiles("standard") == [{"id": 1, "name": "HD-1080p"}]
    assert m.get_quality_profiles("ultra") == [{"id": 2, "name": "Remux-2160p"}]
    assert m.get_custom_formats("standard") == [{"id": 9, "name": "x265"}]
    assert m.get_custom_formats("ultra") == [{"id": 10, "name": "x265-4k"}]

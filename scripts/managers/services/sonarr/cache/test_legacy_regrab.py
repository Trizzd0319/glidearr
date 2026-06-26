"""SonarrCacheEpisodeFilesManager.regrab_legacy_codecs — gated curative pass: finds owned legacy-codec
files, confirms a modern replacement via interactive search, grabs it (Sonarr replaces on import).
Default-OFF; dry_run previews; budget-capped + cooldown-laddered; never deletes."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


class _Log:
    def __init__(self): self.info = []; self.grids = []
    def log_info(self, m="", *a, **k): self.info.append(str(m))
    def log_grid(self, h, rows, title="", cap=None): self.grids.append((title, h, rows))
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _Api:
    """Serves a fixed episode map + interactive-search result; records POST grabs."""
    def __init__(self, releases_by_eid, ep_by_series):
        self.releases = releases_by_eid
        self.eps = ep_by_series
        self.grabs = []

    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        if ep == "release" and method == "POST":
            self.grabs.append(payload)
            return {"id": 1}
        if ep.startswith("episode?seriesId="):
            return self.eps.get(int(ep.split("=")[1]), [])
        if ep.startswith("release?episodeId="):
            return self.releases.get(int(ep.split("=")[1]), [])
        return fallback


def _rel(title, res, guid="g", score=0):
    return {"title": title, "quality": {"quality": {"resolution": res}}, "customFormatScore": score,
            "rejected": False, "guid": guid, "indexerId": 7}


def _mgr(df, api, cache, dry_run, cfg):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = cfg
    m.global_cache = cache
    m.sonarr_api = api
    m.logger = _Log()
    m.dry_run = dry_run
    m._resolve_instance = lambda i: i or "standard"
    m.load = lambda i: df
    return m


_ROW = {"series_id": 1, "episode_file_id": 11, "series_title": "Old Show", "video_codec": "XviD",
        "resolution": 480, "season_number": 1, "episode_number": 1, "watch_count": 3}
_EPS = {1: [{"id": 101, "episodeFileId": 11, "seasonNumber": 1, "episodeNumber": 1}]}
_LKEY = "sonarr/legacy_regrab/standard"


def _cfg(extra=None):
    cp = {"report": True, "legacy_regrab": True, "legacy_regrab_budget": 10}
    cp.update(extra or {})
    return {"scoring": {"codec_profiles": cp}}


def test_inert_when_flag_off():
    api = _Api({101: [_rel("Old.Show.S01E01.x264", 480)]}, _EPS)
    m = _mgr(pd.DataFrame([_ROW]), api, _Cache(), dry_run=False,
             cfg={"scoring": {"codec_profiles": {"legacy_regrab": False}}})
    assert m.regrab_legacy_codecs("standard") == {}
    assert api.grabs == []


def test_dry_run_previews_without_grabbing_or_burning_cooldown():
    api = _Api({101: [_rel("Old.Show.S01E01.480p.x264-NT", 480, guid="gg")]}, _EPS)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=True, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["previewed"] == 1 and out["grabbed"] == 0
    assert api.grabs == []                       # nothing grabbed
    assert _LKEY not in cache.d                   # cooldown ledger not written in dry-run
    assert m.logger.grids and m.logger.grids[0][2][0][-1] == "would-grab"


def test_real_run_grabs_and_records_ledger():
    api = _Api({101: [_rel("Old.Show.S01E01.480p.x264-NT", 480, guid="gg")]}, _EPS)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["grabbed"] == 1
    assert api.grabs == [{"guid": "gg", "indexerId": 7}]       # grabbed by guid, no delete
    assert cache.d[_LKEY]["11"]["result"] == "grabbed"


def test_no_modern_release_leaves_file_and_records_no_release():
    api = _Api({101: [_rel("Old.Show.S01E01.XviD", 480)]}, _EPS)   # only legacy available
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["grabbed"] == 0 and out["no_release"] == 1 and api.grabs == []
    assert cache.d[_LKEY]["11"]["result"] == "no_release"


def test_cooldown_skips_recently_attempted_files():
    api = _Api({101: [_rel("Old.Show.S01E01.x264", 480)]}, _EPS)
    cache = _Cache({_LKEY: {"11": {"at": datetime.now(tz=timezone.utc).isoformat(), "result": "no_release"}}})
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["checked"] == 0 and api.grabs == []

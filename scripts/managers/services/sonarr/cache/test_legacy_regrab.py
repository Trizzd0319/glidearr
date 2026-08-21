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
        self.searches = []          # episode ids an interactive search was issued for

    def disk_free_gb(self, instance):
        # GLD-ACQ-30 holds work when free space is under the acquire floor. These
        # tests are not about that gate, and inf is the real method's own "no
        # constraint" value, so the floor never fires here.
        return float("inf")

    def _make_request(self, instance, ep, method="GET", payload=None, fallback=None):
        if ep == "release" and method == "POST":
            self.grabs.append(payload)
            return {"id": 1}
        if ep.startswith("episode?seriesId="):
            return self.eps.get(int(ep.split("=")[1]), [])
        if ep.startswith("release?episodeId="):
            eid = int(ep.split("=")[1])
            self.searches.append(eid)
            return self.releases.get(eid, [])
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


def test_dry_run_defers_release_checks_by_default():
    """DEFAULT dry-run: no interactive release searches at all.

    Each check is a live indexer round-trip (~2s of blocked wall) spent only to
    NAME the release a live run would grab, so the default budget is 0 and the
    pass previews the QUEUE instead. Still: nothing grabbed, cooldown ledger
    untouched."""
    api = _Api({101: [_rel("Old.Show.S01E01.480p.x264-NT", 480, guid="gg")]}, _EPS)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=True, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["previewed"] == 0 and out["grabbed"] == 0
    assert out["deferred"] == 1                   # queued for a live run / the daemon
    assert api.grabs == []                        # nothing grabbed
    assert _LKEY not in cache.d                   # cooldown ledger not written in dry-run


def test_dry_run_samples_releases_when_budget_set():
    """Opt-in dry-run sampling (scoring.codec_profiles.legacy_regrab_dry_run_budget>0)
    restores the old naming preview — still zero grabs, still no cooldown burn."""
    cfg = _cfg()
    cfg.setdefault("scoring", {}).setdefault("codec_profiles", {})["legacy_regrab_dry_run_budget"] = 1
    api = _Api({101: [_rel("Old.Show.S01E01.480p.x264-NT", 480, guid="gg")]}, _EPS)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=True, cfg=cfg)
    out = m.regrab_legacy_codecs("standard")
    assert out["previewed"] == 1 and out["grabbed"] == 0
    assert api.grabs == []
    assert _LKEY not in cache.d
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


# ── GLD-SON-25: rows whose episode_file_id no longer exists ──────────────────────
def test_stale_pointer_is_retired_without_searching():
    """The row's file was already replaced, so it must NOT be searched.

    episode_file_id is a POINTER and every lane that replaces a file invalidates it.
    The parquet rebuilds on a per-series clock, so a replaced file leaves a row
    pointing at a dead id. Resolving that row by season/episode finds the episode -
    but it now holds a DIFFERENT file, and on the live library those replacements were
    already x264. Searching would re-grab an already-modern file, so the pass retires
    the dead pointer instead. The ledger is keyed by file id, so recording the OLD id
    leaves the live file's id free for the next parquet refresh.
    """
    eps = {1: [{"id": 101, "episodeFileId": 99, "seasonNumber": 1, "episodeNumber": 1}]}
    api = _Api({101: [_rel("Old.Show.S01E01.x264", 480, guid="gg")]}, eps)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["superseded"] == 1
    assert out["searched"] == 0                       # never reached an indexer
    assert api.searches == [] and api.grabs == []     # and issued no search, no grab
    assert out["checked"] == 1                        # checked counts the ITERATION
    assert cache.d[_LKEY]["11"]["result"] == "superseded"   # dead pointer retired
    assert "99" not in cache.d[_LKEY]                 # the LIVE file id stays free


def test_unresolvable_row_is_counted_but_never_benched():
    """No episode matches by file id OR by season/episode.

    A failed `episode?seriesId=` fetch lands here too, so persisting would bench a file
    for 14 days on a transient error - the mistake GLD-SON-02 fixed on the search side.
    Counted and named instead, so it cannot read as 'searched and found nothing'."""
    eps = {1: [{"id": 777, "episodeFileId": 42, "seasonNumber": 9, "episodeNumber": 9}]}
    api = _Api({}, eps)
    cache = _Cache()
    m = _mgr(pd.DataFrame([_ROW]), api, cache, dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert out["unresolved"] == 1 and out["searched"] == 0
    assert api.searches == []
    assert cache.d.get(_LKEY, {}) == {}               # NOT benched


def test_live_pointer_still_searches_normally():
    """Regression guard: an intact pointer takes the fast path and does search."""
    api = _Api({101: [_rel("Old.Show.S01E01.480p.x264-NT", 480, guid="gg")]}, _EPS)
    m = _mgr(pd.DataFrame([_ROW]), api, _Cache(), dry_run=False, cfg=_cfg())
    out = m.regrab_legacy_codecs("standard")
    assert api.searches == [101]                      # resolved by file id, searched
    assert out["searched"] == 1
    assert out["superseded"] == 0 and out["unresolved"] == 0

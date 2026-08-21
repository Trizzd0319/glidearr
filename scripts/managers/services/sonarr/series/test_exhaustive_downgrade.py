"""Exhaustive step-down policy — "deletion is the TRUE last resort" (TV).

The Sonarr twin of radarr/quality/test_exhaustive_downgrade.py. Under
``space_exhaustive_downgrade`` (DEFAULT ON) the TV pass:
  * plans every series above the 720p floor down to it (ceiling dropped — planner-level
    behaviour is covered in machine_learning/space/test_downgrade_planner.py);
  * realizes files only while free space NET of the replacements it queued this run is
    below the band top U (a deleted file frees space NOW, its replacement imports LATER —
    chasing that spike would shrink the whole library);
  * honours the TIGHTER of ``tv_downgrade_realize_cap`` and
    ``space_downgrade_max_regrabs_per_run``; deferred files keep their copies and, still
    being above the floor, stay out of the delete pool.

Stub manager via ``object.__new__`` — the heavy __init__/registry is bypassed.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.series.space_pressure import SonarrSpacePressureManager

_GIB = 1024 ** 3


def _rel(guid, res, size_gib, sn=1, en=1):
    """``guid`` is the short label; ``title`` must look like a real release NAME.

    GLD-ACQ-27's identity gate runs BEFORE the picker and drops any release that does
    not name THIS series and cover THIS episode, so a bare ``"e1101"`` never survives
    to be ranked. These name the series the fixture builds ("S") and its episode.
    """
    return {"guid": guid, "indexerId": 3,
            "title": f"S.S{sn:02d}E{en:02d}.{res}p.{guid}-GRP",
            "size": int(size_gib * _GIB),
            "quality": {"quality": {"resolution": res}}}


class _Log:
    def __init__(self): self.msgs = []
    def log_info(self, m): self.msgs.append(m)
    def log_warning(self, m): self.msgs.append(m)
    def log_error(self, m): self.msgs.append(m)
    def log_debug(self, *a, **k): pass
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


class _Ef:
    def __init__(self, df): self._df = df; self.saved = None
    def load(self, instance): return self._df.copy()
    def save(self, instance, df): self.saved = df
    def _get_episode_id(self, instance, sid, sn, en): return sid * 1000 + sn * 100 + en


class _Api:
    def __init__(self, pick_gib): self.pick_gib = pick_gib; self.calls = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method))
        if endpoint.startswith("series/") and method == "GET":
            return {"id": int(endpoint.split("/")[1]), "qualityProfileId": 13}
        if endpoint.startswith("release?episodeId="):
            # _Ef._get_episode_id is sid*1000 + sn*100 + en, so the season/episode the
            # identity gate wants to see in the title are recoverable from the id.
            _eid = int(endpoint.split("=")[1])
            _sn, _en = (_eid // 100) % 10, _eid % 100
            return [_rel(f"e{_eid}", 1080, self.pick_gib, sn=_sn, en=_en)]
        if method == "DELETE":
            return True          # base contract: a successful DELETE returns True
        return {}


_PROFILES = [
    {"id": 11, "name": "HD-720p", "items": [{"allowed": True, "quality": {"resolution": 720, "name": "q720"}}]},
    {"id": 12, "name": "HD-1080p", "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "q1080"}}]},
    {"id": 13, "name": "UHD", "items": [{"allowed": True, "quality": {"resolution": 2160, "name": "q2160"}}]},
]


def _series(n_eps, size_gib=50.0, score=1):
    return pd.DataFrame([
        dict(series_id=1, series_title="S", watchability_score=score, keep_policy=None,
             resolution=2160, size_bytes=int(size_gib * _GIB), runtime_seconds=2700,
             last_watched_at=None, air_date_utc=None, universe_credit=0.0,
             episode_file_id=500 + e, season_number=1, episode_number=e)
        for e in range(1, n_eps + 1)
    ])


def _mgr(df, *, U, pick_gib, realize_cap=100, regrab_cap=None, exhaustive=True, dry_run=False):
    m = object.__new__(SonarrSpacePressureManager)
    m.config = {"space_exhaustive_downgrade": exhaustive,
                "tv_downgrade_realize_cap": realize_cap}
    if regrab_cap is not None:
        m.config["space_downgrade_max_regrabs_per_run"] = regrab_cap
    m.dry_run = dry_run
    m.logger = _Log()
    m.global_cache = None
    m.sonarr_api = _Api(pick_gib)
    ef = _Ef(df)
    m._get_episode_files_manager = lambda: ef                       # type: ignore[attr-defined]
    m._fetch_ranked_profiles = lambda inst: _PROFILES               # type: ignore[attr-defined]
    m._fetch_hd720p_profile = lambda inst: _PROFILES[0]             # type: ignore[attr-defined]
    m._space_targets = lambda inst=None: (U / 1.1, U)               # type: ignore[attr-defined]
    return m, ef


def test_tv_inflight_accounting_prevents_a_second_wave():
    # 4 x 50 GiB files replaced by 45 GiB grabs → the real net gain is 5 GiB apiece. Booking
    # the queued replacements stops the pass reading the 200 GiB deletion spike as headroom.
    m, _ = _mgr(_series(4), U=100.0, pick_gib=45.0)
    st = m.run_downgrades("standard", 0.0)
    assert st["realized"] == 4 and st["stopped_at_target"] == 0
    assert round(st["realized_reclaim_gb"]) == 200 and round(st["inflight_regrab_gb"]) == 180


def test_tv_pass_stops_at_U():
    # 5 GiB replacements → net +45 GiB per file; free+net crosses U=100 before the 4th file.
    m, _ = _mgr(_series(4), U=100.0, pick_gib=5.0)
    st = m.run_downgrades("standard", 0.0)
    assert st["realized"] == 3 and st["stopped_at_target"] == 1


def test_tv_regrab_cap_defers_the_remainder():
    # The TIGHTER cap wins: space_downgrade_max_regrabs_per_run=2 beats a realize cap of 100.
    m, _ = _mgr(_series(4), U=10_000.0, pick_gib=5.0, regrab_cap=2)
    st = m.run_downgrades("standard", 0.0)
    assert st["realized"] == 2 and st["deferred"] == 2
    assert sum(1 for e, meth in m.sonarr_api.calls if meth == "DELETE") == 2


def test_tv_below_floor_pick_is_counted():
    m, _ = _mgr(_series(1), U=10_000.0, pick_gib=5.0)
    # Replaces the class fake wholesale, so it has to honour the same contracts:
    # a successful DELETE returns True, and a grab returns a body.
    m.sonarr_api._make_request = (
        lambda inst, ep, method="GET", payload=None, fallback=None:
        ({"id": 1, "qualityProfileId": 13} if ep.startswith("series/") and method == "GET"
         else [_rel("sd", 480, 1.0)] if ep.startswith("release?episodeId=")
         else True if method == "DELETE"
         else {"id": 1} if ep == "release" and method == "POST"
         else {}))
    st = m.run_downgrades("standard", 0.0)
    assert st["realized"] == 1 and st["below_floor_picks"] == 1


def test_tv_legacy_mode_neither_caps_at_U_nor_uses_the_regrab_cap():
    m, _ = _mgr(_series(4), U=100.0, pick_gib=5.0, regrab_cap=1, exhaustive=False)
    st = m.run_downgrades("standard", 0.0)
    assert st["stopped_at_target"] == 0 and st["deferred"] == 0
    assert st["realized"] == 4                       # only tv_downgrade_realize_cap (100) applies


# ══ the episode DELETE pool: 720p floor invariant ════════════════════════════════
from scripts.managers.services.sonarr.cache.episode_files import (          # noqa: E402
    SonarrCacheEpisodeFilesManager,
)


def _ep_df():
    return pd.DataFrame([
        dict(episode_file_id=1, marked_for_deletion=True, watchability_score=1, resolution=2160,
             size_bytes=5 * _GIB, series_id=1, season_number=1, episode_number=1, series_title="A"),
        dict(episode_file_id=2, marked_for_deletion=True, watchability_score=1, resolution=1080,
             size_bytes=5 * _GIB, series_id=1, season_number=1, episode_number=2, series_title="A"),
        dict(episode_file_id=3, marked_for_deletion=True, watchability_score=1, resolution=720,
             size_bytes=5 * _GIB, series_id=1, season_number=1, episode_number=3, series_title="A"),
        dict(episode_file_id=4, marked_for_deletion=True, watchability_score=1, resolution=480,
             size_bytes=5 * _GIB, series_id=1, season_number=1, episode_number=4, series_title="A"),
    ])


def _ef_mgr(exhaustive=True):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = {"space_exhaustive_downgrade": exhaustive}
    m.logger = _Log()
    m._build_protected_file_ids = lambda df, now: frozenset()       # type: ignore[attr-defined]
    return m


def test_tv_delete_pool_only_admits_at_or_below_the_720_floor():
    m = _ef_mgr(exhaustive=True)
    cands = m.build_delete_candidates("inst", _ep_df())
    assert {c["fid"] for c in cands} == {3, 4}       # the 2160/1080 files must shrink first
    assert m.last_skipped_downgradable == 2


def test_tv_delete_pool_legacy_mode_ignores_resolution():
    m = _ef_mgr(exhaustive=False)
    assert {c["fid"] for c in m.build_delete_candidates("inst", _ep_df())} == {1, 2, 3, 4}
    assert m.last_skipped_downgradable == 0


def test_tv_delete_pool_unknown_resolution_stays_deletable():
    df = _ep_df().assign(resolution=None)
    assert len(_ef_mgr(exhaustive=True).build_delete_candidates("inst", df)) == 4

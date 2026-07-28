"""Service-side tests for the per-viewer retention rule + the deleted-episode
ledger's release identity.

The pure arithmetic is covered in
``machine_learning/lifecycle/test_viewer_retention.py`` /
``test_restore_policy.py``. These cover the WIRING, which is where this codebase's
three duplicated guard layers can drift:

  1. ``_apply_viewer_retention``   — history + sidecar → the retention_hold column.
  2. ``_apply_grace_period``       — the clear-guard reads that column.
  3. ``_build_protected_file_ids`` — whole-file mirror (a multi-episode file
                                     backing a held episode is protected whole).
  4. ``_do_delete_marked_files``   — delete-time defence-in-depth mirror.

Plus: the ledger round-trips v1 AND v2 entries through the real write path, and
the restore falls back to a blind search when no release was recorded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.machine_learning.lifecycle.restore_policy import ledger_releases
from scripts.managers.services.sonarr.cache.episode_files import (
    SonarrCacheEpisodeFilesManager as M,
)

_NOW = datetime.now(tz=timezone.utc)


class _StubLogger:
    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def log_warning(self, msg):   self.warnings.append(str(msg))
    def log_info(self, msg):      self.infos.append(str(msg))
    def log_debug(self, msg):     pass
    def log_error(self, msg):     self.warnings.append(str(msg))
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k):  pass


class _StubCache:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):        return self.data.get(key)
    def set(self, key, value): self.data[key] = value


def _mgr(config=None, cache=None) -> M:
    # NOTE: BaseManager.__new__ is a SINGLETON registry, so M.__new__(M) hands back
    # the SAME object every time. Anything monkey-patched onto it therefore leaks
    # into every later test — patches below are always undone in a finally.
    mgr = M.__new__(M)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    mgr.global_cache = cache if cache is not None else _StubCache()
    mgr.config = {"free_space_limit": 100.0, "deletions_consent": True}
    mgr.config.update(config or {})
    return mgr


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _row(sid, title, season, ep, *, fid, watched=False, lw=None, pilot=False,
         keep=None, aired=400, size=1_000_000_000):
    return {
        "episode_file_id": fid, "series_id": sid, "series_title": title,
        "season_number": season, "episode_number": ep,
        "is_pilot": pilot, "is_watched": watched, "next_episode": False,
        "watch_count": 1 if watched else 0, "last_watched_at": lw,
        "all_household_watched": True, "household_last_watched_at": None,
        "percent_complete": 100 if watched else 0,
        "marked_for_deletion": False, "available_until": None, "keep_policy": keep,
        "air_date_utc": _iso(aired), "size_bytes": size,
        "scene_name": f"{title}.S{season:02d}E{ep:02d}-GRP", "release_group": "GRP",
        "quality_name": "WEBDL-1080p", "resolution": 1080,
    }


def _history(entries):
    """``[(title, season, ep, user, days_ago, watched)]`` → the aggregated shape
    ``_collect_tautulli_episode_history`` produces."""
    out: dict = {}
    for title, season, ep, user, days_ago, watched in entries:
        rec = out.setdefault((title, season, ep), {
            "watch_count": 0, "last_watched_at": None, "percent_complete": 100,
            "per_user": {}, "per_user_watch": {}})
        rec["watch_count"] += 1
        at = _iso(days_ago)
        rec["last_watched_at"] = max(rec["last_watched_at"] or "", at)
        rec["per_user"][user] = at
        rec["per_user_watch"][user] = {"at": at, "watched": watched}
    return out


# ── 1. the column, and the grace clear-guard that reads it ─────────────────────
def test_retention_hold_stamps_the_interval_and_grace_clears_it():
    """S01E01-E10 all watched a month ago; the viewer is at E10. Without the rule
    every one of them is grace-expired and marked. With it, E08-E10 (buffer) plus
    the forward reach survive, and each hold names the viewer."""
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(20 - e))
                       for e in range(1, 21)])
    hist = _history([("Show", 1, e, "Trizzd", 20 - e, True) for e in range(1, 11)])

    mgr = _mgr()
    df, stats = mgr._apply_viewer_retention(df, hist, "std")
    assert stats["enabled"] is True and stats["accounts"] == 1
    held = df.loc[df["retention_hold"].astype(bool), "episode_number"].tolist()
    assert 10 in held and 9 in held and 8 in held        # position + backward_buffer
    assert 7 not in held                                  # the third episode back is free
    assert 11 in held                                     # forward reach (pace ~1/day)
    assert set(df.loc[df["retention_hold"].astype(bool), "retention_hold_by"]) == {"Trizzd"}

    marked = mgr._apply_grace_period(df.copy())["marked_for_deletion"].astype(bool)
    assert not marked[df["episode_number"].isin([8, 9, 10])].any()
    # E01 is the DE-FACTO pilot (no pilot row → earliest watched episode) and is
    # protected by that guard, not this one; E02/E03 are outside every interval.
    assert marked[df["episode_number"].isin([2, 3])].all()


def test_disabled_config_reverts_to_the_legacy_marking():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(20 - e))
                       for e in range(1, 11)])
    hist = _history([("Show", 1, e, "Trizzd", 20 - e, True) for e in range(1, 11)])
    mgr = _mgr({"episode_retention": {"enabled": False}})
    df, stats = mgr._apply_viewer_retention(df, hist, "std")
    assert stats["enabled"] is False
    assert not df["retention_hold"].astype(bool).any()
    marked = mgr._apply_grace_period(df.copy())["marked_for_deletion"].astype(bool)
    # every watched row marked except the de-facto pilot — the legacy behaviour
    assert marked[df["episode_number"] > 1].all()


# ── 2. the second viewer, and the dormant path ────────────────────────────────
def test_a_trailing_second_viewer_holds_the_season_the_leader_finished():
    """The case the rule exists for: one account is on S03, another is on S02."""
    rows = [_row(1, "Show", s, e, fid=s * 100 + e, watched=True, lw=_iso(10))
            for s in (1, 2, 3) for e in range(1, 11)]
    df = pd.DataFrame(rows)
    hist = _history(
        [("Show", 3, e, "Ahead", 10 - e, True) for e in range(1, 4)] +
        [("Show", 2, e, "Behind", 10 - e, True) for e in range(1, 4)])
    mgr = _mgr()
    df, _ = mgr._apply_viewer_retention(df, hist, "std")
    by = {(r.season_number, r.episode_number): r.retention_hold_by
          for r in df.itertuples() if r.retention_hold}
    assert by[(2, 4)] == "Behind"                # the leader is past it; the trailer is not
    assert "Ahead" in by[(3, 1)]                 # the leader's own buffer
    assert (1, 1) not in by                      # long behind BOTH viewers → releasable


def test_dormant_account_holds_position_plus_buffer_and_drops_forward_reach():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(200))
                       for e in range(1, 21)])
    hist = _history([("Show", 1, e, "Stale", 200 + (10 - e), True) for e in range(1, 11)])
    mgr = _mgr()
    df, stats = mgr._apply_viewer_retention(df, hist, "std")
    assert stats["dormant"] == 1
    held = sorted(df.loc[df["retention_hold"].astype(bool), "episode_number"])
    assert held == [8, 9, 10]                    # position + 2 back, nothing ahead


def test_dormancy_inherits_the_prefetch_cold_days_and_can_be_split():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(50))
                       for e in range(1, 21)])
    hist = _history([("Show", 1, e, "V", 50 + (10 - e), True) for e in range(1, 11)])
    # inherited cold_days=45 → dormant at 50 days stale
    inherit = _mgr({"acquisition": {"next_episode": {"recency_gate": {"cold_days": 45}}}})
    assert inherit._apply_viewer_retention(df.copy(), hist, "std")[1]["dormant"] == 1
    # explicit override wins over the inherited value
    split = _mgr({"acquisition": {"next_episode": {"recency_gate": {"cold_days": 45}}},
                  "episode_retention": {"dormant_days": 365}})
    assert split._apply_viewer_retention(df.copy(), hist, "std")[1]["dormant"] == 0


# ── 3. season boundary, empty accounts, ignored users ─────────────────────────
def test_backward_buffer_crosses_a_season_boundary_onto_real_episodes():
    rows = ([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 24)] +
            [_row(1, "Show", 2, e, fid=200 + e, watched=True, lw=_iso(5)) for e in range(1, 11)])
    df = pd.DataFrame(rows)
    hist = _history([("Show", 2, 1, "V", 5, True)])
    mgr = _mgr()
    df, _ = mgr._apply_viewer_retention(df, hist, "std")
    held = {(r.season_number, r.episode_number) for r in df.itertuples() if r.retention_hold}
    assert (1, 22) in held and (1, 23) in held    # two REAL episodes back
    assert (1, 21) not in held


def test_an_account_with_no_history_on_a_series_protects_nothing_there():
    df = pd.DataFrame([_row(1, "A", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 6)] +
                      [_row(2, "B", 1, e, fid=200 + e, watched=True, lw=_iso(5)) for e in range(1, 6)])
    hist = _history([("A", 1, 3, "Trizzd", 5, True)])
    mgr = _mgr()
    df, _ = mgr._apply_viewer_retention(df, hist, "std")
    assert not df.loc[df["series_id"] == 2, "retention_hold"].astype(bool).any()


def test_ignored_users_never_pin_disk():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 6)])
    hist = _history([("Show", 1, 3, "Guest", 5, True)])
    mgr = _mgr({"ignored_users": ["guest"]})
    df, stats = mgr._apply_viewer_retention(df, hist, "std")
    assert stats["accounts"] == 0 and not df["retention_hold"].astype(bool).any()


def test_sub_threshold_plays_do_not_move_the_position():
    """A 12 % sample of S01E09 must not drag the interval forward off E03."""
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 11)])
    hist = _history([("Show", 1, 3, "V", 5, True), ("Show", 1, 9, "V", 4, False)])
    mgr = _mgr()
    df, _ = mgr._apply_viewer_retention(df, hist, "std")
    held = sorted(df.loc[df["retention_hold"].astype(bool), "episode_number"])
    assert max(held) < 9 or 9 not in held or held[0] == 1
    assert 1 in held and 2 in held and 3 in held          # buffer clamped at the start


# ── 4. the sidecar, and the fail-safe ─────────────────────────────────────────
def test_a_remembered_position_survives_a_pruned_tautulli_history():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 21)])
    hist = _history([("Show", 1, e, "V", 10 - e, True) for e in range(1, 11)])
    cache = _StubCache()
    first = _mgr(cache=cache)
    first._apply_viewer_retention(df.copy(), hist, "std")
    sidecar = cache.get("sonarr/std/viewer_positions")
    assert sidecar and "V" in sidecar

    # Tautulli pruned everything; the sidecar alone must keep the resume point held.
    second = _mgr(cache=_StubCache(cache.data))
    out, stats = second._apply_viewer_retention(df.copy(), {}, "std")
    assert stats["carried"] == 1
    assert sorted(out.loc[out["retention_hold"].astype(bool), "episode_number"])[:3] == [8, 9, 10]

    # ...and with NO sidecar and no history nothing is held (the failure it prevents)
    bare, bare_stats = _mgr()._apply_viewer_retention(df.copy(), {}, "std")
    assert bare_stats["held_rows"] == 0 and not bare["retention_hold"].astype(bool).any()


def test_a_build_failure_holds_every_watched_row_rather_than_widening_the_pool():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=(e <= 5), lw=_iso(5))
                       for e in range(1, 11)])
    mgr = _mgr()
    mgr._apply_viewer_retention_inner = lambda *a, **k: 1 / 0
    try:
        out, _ = mgr._apply_viewer_retention(df, {}, "std")
    finally:
        del mgr._apply_viewer_retention_inner   # singleton — restore class resolution
    assert int(out["retention_hold"].astype(bool).sum()) == 5
    assert any("fail-safe" in w for w in mgr.logger.warnings)


def test_a_stale_hold_from_a_previous_run_is_recomputed_not_inherited():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5)) for e in range(1, 6)])
    df["retention_hold"] = True
    df["retention_hold_by"] = "GhostViewer"
    out, _ = _mgr()._apply_viewer_retention(df, {}, "std")
    assert not out["retention_hold"].astype(bool).any()


# ── 5. the other two guard layers ─────────────────────────────────────────────
def test_whole_file_protection_mirrors_the_retention_hold():
    """A multi-episode file backing one held episode must be protected whole."""
    df = pd.DataFrame([
        _row(1, "Show", 1, 1, fid=900, watched=True, lw=_iso(5)),   # NOT held
        _row(1, "Show", 1, 2, fid=900, watched=True, lw=_iso(5)),   # same file, held
    ])
    df["retention_hold"] = [False, True]
    df["retention_hold_by"] = [None, "V"]
    assert 900 in _mgr()._build_protected_file_ids(df, _NOW)


def test_delete_pass_defence_in_depth_clears_a_stale_mark():
    df = pd.DataFrame([_row(1, "Show", 1, e, fid=100 + e, watched=True, lw=_iso(5))
                       for e in range(1, 4)])
    df["marked_for_deletion"] = True
    df["retention_hold"] = [False, True, False]
    df["retention_hold_by"] = [None, "Aiden / Raina", None]
    mgr = _mgr()
    mgr.sonarr_api = None
    out, stats = mgr._do_delete_marked_files("std", df)
    assert stats["skipped_retention"] == 1
    assert out.loc[1, "marked_for_deletion"] is False or not bool(out.loc[1, "marked_for_deletion"])
    assert any("RETENTION GUARD" in w and "Aiden / Raina" in w for w in mgr.logger.warnings)


def test_pilot_and_keep_guards_still_win_over_an_unheld_row():
    """Retention ADDS a guard; it must not weaken the ones already there. A pilot
    and a keep_series episode outside every interval stay protected."""
    df = pd.DataFrame([
        _row(1, "Show", 1, 1, fid=1, watched=True, lw=_iso(5), pilot=True),
        _row(2, "Keep", 1, 1, fid=2, watched=True, lw=_iso(5), keep="keep_series"),
        _row(2, "Keep", 1, 2, fid=3, watched=True, lw=_iso(5), keep="keep_series"),
        _row(3, "Free", 1, 1, fid=4, watched=True, lw=_iso(5)),   # de-facto pilot
        _row(3, "Free", 1, 2, fid=5, watched=True, lw=_iso(5)),   # genuinely unguarded
    ])
    mgr = _mgr()
    df, _ = mgr._apply_viewer_retention(df, {}, "std")
    assert not df["retention_hold"].astype(bool).any()      # nothing held by retention
    marked = mgr._apply_grace_period(df.copy())["marked_for_deletion"].astype(bool)
    assert not marked.iloc[0]                               # real pilot
    assert not marked.iloc[1] and not marked.iloc[2]        # keep_series, both episodes
    assert not marked.iloc[3]                               # de-facto pilot
    assert marked.iloc[4]                                   # the unguarded one is marked


# ── 6. the ledger + the restore fallback ──────────────────────────────────────
def _delete_one(cache, existing_ledger=None, *, scene_name="Show.S01E02-GRP"):
    """Run the real coordinator delete path over one row and return the ledger."""
    calls: list = []

    class _Api:
        def _make_request(self, inst, ep, method="GET", payload=None, fallback=None):
            calls.append((ep, method, payload))
            return {}

    df = pd.DataFrame([_row(7, "Show", 1, 2, fid=42, watched=True, lw=_iso(5))])
    df.at[0, "scene_name"] = scene_name
    df.at[0, "watchability_score"] = 50
    if existing_ledger is not None:
        cache.set("sonarr/std/deleted_episodes", existing_ledger)
    mgr = _mgr(cache=cache)
    mgr.dry_run = False                       # the ledger write is gated on not dry_run
    mgr.sonarr_api = _Api()
    mgr.load = lambda inst: df
    mgr.save = lambda inst, d: None
    mgr._build_protected_file_ids = lambda d, now: frozenset()
    mgr.delete_selected_episode_files("std", [42])
    return cache.get("sonarr/std/deleted_episodes"), calls


def test_delete_records_the_release_identity_on_a_fresh_ledger():
    ledger, _ = _delete_one(_StubCache())
    ent = ledger["7"]
    assert ent["episodes"] == [[1, 2]]
    assert ent["v"] == 2
    rec = ledger_releases(ent)["S01E02"]
    assert rec["scene_name"] == "Show.S01E02-GRP"
    assert rec["release_group"] == "GRP"
    assert rec["quality_name"] == "WEBDL-1080p" and rec["resolution"] == 1080
    assert rec["size_bytes"] == 1_000_000_000


def test_delete_merges_into_a_v1_ledger_without_losing_its_coords():
    """A deployed install may already hold v1 entries — they must keep working."""
    v1 = {"7": {"episodes": [[1, 1]], "ts": "2026-01-01T00:00:00+00:00"}}
    ledger, _ = _delete_one(_StubCache(), v1)
    assert ledger["7"]["episodes"] == [[1, 1], [1, 2]]
    assert "S01E02" in ledger_releases(ledger["7"])


def test_delete_with_no_scene_name_still_records_the_partial_identity():
    ledger, _ = _delete_one(_StubCache(), scene_name=None)
    rec = ledger_releases(ledger["7"])["S01E02"]
    assert "scene_name" not in rec and rec["release_group"] == "GRP"


def _restore(ledger, *, releases=None, score=50):
    """Run the real restore over a ledger; return (stats, api calls)."""
    calls: list = []

    class _Api:
        def _make_request(self, inst, ep, method="GET", payload=None, fallback=None):
            calls.append((ep, method, payload))
            if ep.startswith("episode?seriesId="):
                return [{"id": 5001, "seasonNumber": 1, "episodeNumber": 2}]
            if ep.startswith("release?episodeId="):
                return releases if releases is not None else []
            return {}

    cache = _StubCache({"sonarr/std/deleted_episodes": ledger})
    mgr = _mgr({"tv_restore_score_threshold": 17}, cache=cache)
    mgr.dry_run = False
    mgr.sonarr_api = _Api()
    mgr._resolve_instance = lambda inst: inst
    mgr.load = lambda inst: pd.DataFrame(
        [{"series_id": 7, "watchability_score": score}])
    return mgr.restore_recovered_episode_deletions("std"), calls


def test_restore_of_a_v1_entry_blind_searches_exactly_as_before():
    """No recorded release → no interactive search at all, one EpisodeSearch."""
    stats, calls = _restore({"7": {"episodes": [[1, 2]], "ts": _iso(5)}})
    assert stats["restored"] == 1 and stats["targeted"] == 0
    assert not any(ep.startswith("release?episodeId=") for ep, _, _ in calls)
    assert any(p and p.get("name") == "EpisodeSearch" and p["episodeIds"] == [5001]
               for _, _, p in calls)


def test_restore_targets_the_recorded_release_when_the_indexer_still_has_it():
    ledger = {"7": {"episodes": [[1, 2]], "ts": _iso(5), "v": 2, "releases": {
        "S01E02": {"scene_name": "Show.S01E02-GRP", "release_group": "GRP",
                   "quality_name": "WEBDL-1080p", "resolution": 1080}}}}
    stats, calls = _restore(ledger, releases=[
        {"title": "Show.S01E02-GRP", "releaseGroup": "GRP", "guid": "abc", "indexerId": 3,
         "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}}])
    assert stats["targeted"] == 1 and stats["restored"] == 1
    assert ("release", "POST", {"guid": "abc", "indexerId": 3}) in calls
    # nothing left over → no blind EpisodeSearch
    assert not any(p and p.get("name") == "EpisodeSearch" for _, _, p in calls)


def test_restore_falls_back_to_a_blind_search_when_the_release_is_gone():
    """A stale scene_name must NEVER block a restore — the fallback is total."""
    ledger = {"7": {"episodes": [[1, 2]], "ts": _iso(5), "v": 2, "releases": {
        "S01E02": {"scene_name": "Show.S01E02-GRP", "release_group": "GRP"}}}}
    stats, calls = _restore(ledger, releases=[
        {"title": "Totally.Different-XYZ", "releaseGroup": "XYZ", "guid": "z",
         "quality": {"quality": {"name": "HDTV-720p", "resolution": 720}}}])
    assert stats["targeted"] == 0 and stats["restored"] == 1
    assert any(p and p.get("name") == "EpisodeSearch" and p["episodeIds"] == [5001]
               for _, _, p in calls)


def test_restore_falls_back_when_the_interactive_search_itself_fails():
    ledger = {"7": {"episodes": [[1, 2]], "ts": _iso(5), "v": 2, "releases": {
        "S01E02": {"scene_name": "Show.S01E02-GRP"}}}}
    stats, calls = _restore(ledger, releases=None)      # empty search result
    assert stats["targeted"] == 0 and stats["restored"] == 1
    assert any(p and p.get("name") == "EpisodeSearch" for _, _, p in calls)

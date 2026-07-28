"""END-TO-END wiring for the GLOBAL watched bar, on the real service code paths.

The pure rule is covered in
``machine_learning/lifecycle/test_watched_definition.py``. These cover the two
things only the service can prove:

  1. the PRODUCER — a raw Tautulli history row at 2% complete aggregates to
     ``watch_count 0`` (and therefore ``is_watched False``) while
     ``percent_complete`` / ``last_watched_at`` survive untouched, in BOTH the
     Sonarr episode path and the Radarr movie path;
  2. the CONSEQUENCE — that sample does not start the 3-hour grace clock, and a
     stale mark left by the old definition is CLEARED rather than acted on.

"A 30-second sample no longer queues the file for deletion" is the whole point of
the exercise, so it is demonstrated here end to end, not asserted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.machine_learning.lifecycle.grace_policy import (
    episode_grace_decision,
    movie_grace_decision,
)
from scripts.managers.services.radarr.cache.movie_files import (
    RadarrCacheMovieFilesManager as RM,
)
from scripts.managers.services.sonarr.cache.episode_files import (
    SonarrCacheEpisodeFilesManager as SM,
)

_NOW = datetime.now(tz=timezone.utc)


class _StubLogger:
    def __init__(self):
        self.infos: list[str] = []

    def log_info(self, msg):      self.infos.append(str(msg))
    def log_warning(self, msg):   self.infos.append(str(msg))
    def log_debug(self, msg):     pass
    def log_error(self, msg):     self.infos.append(str(msg))
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k):  pass


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _epoch(days_ago: float) -> int:
    return int((_NOW - timedelta(days=days_ago)).timestamp())


def _play(pct, *, days_ago=1.0, user="Trizzd", watched_status=None, media="episode"):
    """One raw Tautulli history row, in the shape ``get_history`` returns."""
    row = {
        "media_type": media, "date": _epoch(days_ago), "user": user,
        "percent_complete": pct,
        "grandparent_title": "Show", "parent_media_index": 1, "media_index": 4,
        "title": "Some Movie" if media == "movie" else "Ep Title",
    }
    if watched_status is not None:
        row["watched_status"] = watched_status
    return row


# ── 1. the SONARR producer ────────────────────────────────────────────────────
def _sonarr_history(entries, config=None):
    """Drive the real ``_collect_tautulli_episode_history`` over a stubbed API."""
    mgr = SM.__new__(SM)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    mgr.global_cache = None
    mgr.config = {"tautulli": {"url": "x", "port": "1", "api": "k"}}
    mgr.config.update(config or {})

    import scripts.managers.services.tautulli.instances.api as api_mod
    real = api_mod.TautulliAPI

    class _StubAPI:
        base_url = "http://stub"

        def __init__(self, **kwargs):
            pass

        def get_history(self, **kwargs):
            return {"response": {"data": {"data": list(entries)}}}

    api_mod.TautulliAPI = _StubAPI
    try:
        return mgr._collect_tautulli_episode_history(), mgr
    finally:
        api_mod.TautulliAPI = real


def test_sonarr_producer_counts_watches_not_plays():
    hist, mgr = _sonarr_history([_play(2), _play(97, days_ago=0.5)])
    rec = hist[("Show", 1, 4)]
    assert rec["plays"] == 2                    # both plays are seen…
    assert rec["watch_count"] == 1              # …only the completing one is a WATCH
    # threshold-FREE playback facts survive both plays
    assert rec["percent_complete"] == 97
    assert rec["last_watched_at"] is not None


def test_sonarr_producer_a_sample_only_episode_is_not_watched_at_all():
    """The key produces a record with watch_count 0 — it is NOT omitted, so the
    sync recomputes ``is_watched`` to False instead of leaving a stale True."""
    hist, mgr = _sonarr_history([_play(2), _play(9, days_ago=0.5)])
    rec = hist[("Show", 1, 4)]
    assert rec["plays"] == 2 and rec["watch_count"] == 0
    assert rec["percent_complete"] == 9         # the partial-view signal is kept
    assert rec["last_watched_at"] is not None   # …and so is "it was tried"
    assert rec["per_user_watch"]["Trizzd"]["watched"] is False


def test_sonarr_producer_honours_tautullis_own_verdict_over_the_percentage():
    hist, _ = _sonarr_history([_play(12, watched_status=1)])
    assert hist[("Show", 1, 4)]["watch_count"] == 1
    hist, _ = _sonarr_history([_play(99, watched_status=0.5)])
    assert hist[("Show", 1, 4)]["watch_count"] == 0


def test_sonarr_producer_follows_the_configured_bar():
    hist, _ = _sonarr_history([_play(60)], config={"watched_threshold": {"percent": 50}})
    assert hist[("Show", 1, 4)]["watch_count"] == 1
    hist, _ = _sonarr_history([_play(60)])                      # default 85
    assert hist[("Show", 1, 4)]["watch_count"] == 0


def test_sonarr_producer_household_view_is_watched_only():
    """``per_user`` stays "who touched it" (the JIT 'For' column); the household
    gate reads the WATCHED-only view, so it cannot hold disk on a sample."""
    hist, mgr = _sonarr_history([_play(2, user="Aiden"), _play(99, user="Trizzd")])
    rec = hist[("Show", 1, 4)]
    assert set(rec["per_user"]) == {"Aiden", "Trizzd"}
    assert set(mgr._watched_per_user(rec)) == {"Trizzd"}
    # a history dict from an older code path (no per_user_watch) must not resolve
    # to "nobody watched it"
    assert set(mgr._watched_per_user({"per_user": {"Aiden": "x"}})) == {"Aiden"}


# ── 2. the RADARR producer — the SAME definition ──────────────────────────────
def _radarr_watch_map(entries, config=None):
    mgr = RM.__new__(RM)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    mgr.global_cache = None
    mgr.config = {"tautulli": {"url": "x", "port": "1", "api": "k"}}
    mgr.config.update(config or {})

    import scripts.managers.services.tautulli.instances.api as api_mod
    real = api_mod.TautulliAPI

    class _StubAPI:
        base_url = "http://stub"

        def __init__(self, **kwargs):
            pass

        def get_history(self, **kwargs):
            return {"response": {"data": {"data": list(entries)}}}

    api_mod.TautulliAPI = _StubAPI
    try:
        return mgr._fetch_watch_map("standard")
    finally:
        api_mod.TautulliAPI = real


def test_movie_path_uses_the_SAME_definition_as_the_show_path():
    """These two producers each inlined the same threshold-free ``+= 1``; they now
    share one rule, so a movie and an episode cannot disagree about 'watched'."""
    wm = _radarr_watch_map([_play(2, media="movie"), _play(97, days_ago=0.5, media="movie")])
    rec = wm["Some Movie"]
    assert rec["plays"] == 2 and rec["watch_count"] == 1
    assert rec["percent_complete"] == 97 and rec["last_watched_at"] is not None

    wm = _radarr_watch_map([_play(9, media="movie")])
    assert wm["Some Movie"]["watch_count"] == 0          # is_watched will be False
    wm = _radarr_watch_map([_play(9, media="movie", watched_status=1)])
    assert wm["Some Movie"]["watch_count"] == 1          # Tautulli's verdict wins


# ── 3. the CONSEQUENCE: the 3-hour clock does not start on a sample ───────────
def _ep_row(**over):
    row = {
        "episode_file_id": 501, "series_id": 1, "series_title": "Show",
        "season_number": 1, "episode_number": 4,
        "is_pilot": False, "is_watched": True, "next_episode": False,
        "watch_count": 1, "last_watched_at": _iso(30),
        "all_household_watched": True, "household_last_watched_at": None,
        "percent_complete": 100, "marked_for_deletion": False,
        "available_until": None, "keep_policy": None,
        "air_date_utc": _iso(400), "size_bytes": 1_000_000_000,
        "quality_name": "WEBDL-1080p", "resolution": 1080,
    }
    row.update(over)
    return row


def _mgr_for_grace():
    mgr = SM.__new__(SM)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    mgr.global_cache = None
    mgr.config = {"free_space_limit": 100.0, "deletions_consent": True}
    return mgr


def test_a_30_second_sample_does_not_start_the_grace_clock():
    """END TO END: a 2%-complete play 30 days ago. Under the old definition that
    row was ``is_watched=True`` and its 3-hour window expired 29.9 days ago. The
    producer now yields watch_count 0 → is_watched False → the grace pass never
    computes a window for it at all."""
    hist, _ = _sonarr_history([_play(2, days_ago=30)])
    watched = hist[("Show", 1, 4)]["watch_count"] > 0
    assert watched is False

    # A second, un-sampled episode is present so the de-facto-pilot guard (which
    # protects the earliest WATCHED episode) lands on it, not on E04.
    df = pd.DataFrame([
        _ep_row(episode_file_id=500, episode_number=1),
        _ep_row(episode_file_id=501, episode_number=4,
                is_watched=watched, watch_count=hist[("Show", 1, 4)]["watch_count"],
                percent_complete=hist[("Show", 1, 4)]["percent_complete"],
                last_watched_at=hist[("Show", 1, 4)]["last_watched_at"]),
    ])
    out = _mgr_for_grace()._apply_grace_period(df.copy())
    sample = out[out["episode_number"] == 4].iloc[0]
    assert bool(sample["marked_for_deletion"]) is False
    assert sample["available_until"] is None            # no window was ever opened

    # …and the control: the same row at 97% IS marked, so the test can fail.
    df2 = df.copy()
    df2.loc[df2["episode_number"] == 4, ["is_watched", "watch_count", "percent_complete"]] = [True, 1, 97]
    out2 = _mgr_for_grace()._apply_grace_period(df2)
    assert bool(out2[out2["episode_number"] == 4].iloc[0]["marked_for_deletion"]) is True


def test_a_stale_mark_from_the_old_definition_is_cleared_not_acted_on():
    """Five live episode rows and 35 live movie rows are marked_for_deletion on a
    play that no longer counts. 'skip' would have left the mark standing and
    deleted them on the next pass; the guard now CLEARS it."""
    df = pd.DataFrame([
        _ep_row(episode_file_id=500, episode_number=1),
        _ep_row(episode_file_id=501, episode_number=4, is_watched=False, watch_count=0,
                percent_complete=19, marked_for_deletion=True,
                available_until=_iso(29)),
    ])
    out = _mgr_for_grace()._apply_grace_period(df.copy())
    assert bool(out[out["episode_number"] == 4].iloc[0]["marked_for_deletion"]) is False


def test_grace_decisions_clear_an_unwatched_row_in_both_media():
    assert episode_grace_decision(
        is_pilot=False, is_next=False, is_watched=False, has_last_watched=True,
        fid_protected=False, keep_series=False, keep_season_current=False,
        recent_aired=False, household_blocked=False) == "clear"
    assert movie_grace_decision(
        is_franchise_entry=False, fid_franchise_protected=False, keep_protected=False,
        is_watched=False, has_last_watched=True) == "clear"
    # a WATCHED row with no timestamp is still 'skip' — we cannot compute a window,
    # so leave it exactly as it was.
    assert episode_grace_decision(
        is_pilot=False, is_next=False, is_watched=True, has_last_watched=False,
        fid_protected=False, keep_series=False, keep_season_current=False,
        recent_aired=False, household_blocked=False) == "skip"
    assert movie_grace_decision(
        is_franchise_entry=False, fid_franchise_protected=False, keep_protected=False,
        is_watched=True, has_last_watched=False) == "skip"

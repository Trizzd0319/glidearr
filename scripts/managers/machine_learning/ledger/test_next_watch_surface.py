"""ledger/test_next_watch_surface.py — the end-of-run "Next watch" reminder.

``machine_learning/next_watch`` was built, tested, pure — and had ZERO callers. This is
its user-facing consumer, so these tests pin that it (a) actually surfaces owned +
watchlisted + never-played titles, (b) says nothing when there is nothing to say, and
(c) can never take the run down (it renders at the very end of a dry-run summary).
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.ledger.plan_summary import PlanSummary


class _Cache:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, *a, **k):
        return self.data.get(key)

    def set(self, key, value, *a, **k):
        self.data[key] = value


class _Logger:
    def __init__(self):
        self.grids: list = []

    def log_info(self, m):
        pass

    def log_warning(self, m):
        pass

    def log_debug(self, m):
        pass

    def log_error(self, m):
        pass

    def log_table(self, *a, **k):
        pass

    def log_grid(self, cols, rows, **k):
        self.grids.append((cols, rows))


_UNION = [
    {"title": "Owned & Unplayed", "type": "movie", "ids": {"tmdb": 1},
     "watchlisted_by": ["trizzd"], "source": "plex_watchlist"},
    {"title": "Owned but Played", "type": "movie", "ids": {"tmdb": 2},
     "watchlisted_by": ["trizzd"], "source": "plex_watchlist"},
    {"title": "Not Owned", "type": "movie", "ids": {"tmdb": 3},
     "watchlisted_by": ["trizzd", "aiden"], "source": "plex_watchlist"},
]

_MOVIES = pd.DataFrame([
    {"tmdb_id": 1, "title": "Owned & Unplayed", "has_file": True, "is_watched": False,
     "watch_count": 0},
    {"tmdb_id": 2, "title": "Owned but Played", "has_file": True, "is_watched": True,
     "watch_count": 3},
    {"tmdb_id": 9, "title": "Owned, not wanted", "has_file": True, "is_watched": False,
     "watch_count": 0},
])

_EPISODES = pd.DataFrame([
    {"series_id": 7, "series_title": "Wanted Series", "is_watched": False,
     "watchlist_hold": True, "watchlist_hold_by": "trizzd"},
    {"series_id": 8, "series_title": "Started Series", "is_watched": True,
     "watchlist_hold": True, "watchlist_hold_by": "trizzd"},
    {"series_id": 9, "series_title": "Unwanted Series", "is_watched": False,
     "watchlist_hold": False, "watchlist_hold_by": None},
])


def _summary(union=_UNION, frames=(("radarr", "standard", _MOVIES),
                                   ("sonarr", "standard", _EPISODES))):
    ps = PlanSummary(logger=_Logger(), config={},
                     global_cache=_Cache({"plex/watchlist/union": union}))
    ps._iter_frames = lambda: iter(list(frames))
    return ps


def test_surfaces_only_owned_watchlisted_and_never_played():
    rows = _summary().next_watch_rows()
    titles = {r[1] for r in rows}
    assert "Owned & Unplayed" in titles          # owned + asked for + never played
    assert "Wanted Series" in titles             # the show half, via the shield column
    assert "Owned but Played" not in titles      # already played → not a reminder
    assert "Started Series" not in titles        # already started → not a reminder
    assert "Not Owned" not in titles             # unowned watchlist → acquisition's job
    assert "Owned, not wanted" not in titles     # owned but never asked for


def test_the_intent_number_comes_from_the_next_watch_ranker():
    rows = _summary().next_watch_rows()
    movie = next(r for r in rows if r[0] == "movie")
    assert movie[2] == "60"                      # rank_next_watch's solo base
    assert movie[3] == "trizzd"


def test_nothing_to_say_renders_nothing():
    """Empty union AND no shielded series → silence, not an empty table."""
    ps = _summary(union=[], frames=())
    assert ps.next_watch_rows() == []
    assert ps.log_next_watch() == 0
    assert ps.logger.grids == []


def test_the_show_half_is_driven_by_the_shield_column_not_the_union():
    """Deliberate asymmetry, and the honest limit of this surface: ``episode_files`` carries
    no TVDb id to join the Plex union on, so series come from ``watchlist_hold`` — which is
    the same verdict, stamped upstream. An empty union therefore still surfaces them."""
    rows = _summary(union=[]).next_watch_rows()
    assert [r[1] for r in rows] == ["Wanted Series"]


def test_it_renders_one_grid_and_respects_the_limit():
    ps = _summary()
    assert ps.log_next_watch(limit=1) == 1
    (cols, rows), = ps.logger.grids
    assert cols[0] == "Kind" and len(rows) == 1


def test_a_broken_frame_can_never_take_the_run_down():
    ps = _summary()
    ps._iter_frames = lambda: (_ for _ in ()).throw(RuntimeError("parquet exploded"))
    assert ps.log_next_watch() == 0              # logged + swallowed, no raise

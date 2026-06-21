"""Phase 6 — the per-group prefetch walk (_compute_next_episodes).

Guards:
1. Feature OFF → byte-identical per-series behaviour (each started series flagged independently).
2. Feature ON → a saga shares ONE budget walked frontier-first (the next show waits its turn).
3. Feature ON → the next UNSTARTED member of an engaged saga is prefetched once the current is
   exhausted ("finish Loki → Daredevil").

Built to avoid all Sonarr API calls: every episode is already in the df (Parquet) and the season
episode cache is pre-populated, so the walk flags from the in-memory maps only.
"""
from __future__ import annotations

import pandas as pd

import scripts.managers.services.sonarr.cache.episode_files as ef


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


class _SeriesCache:
    def __init__(self, meta): self._meta = meta          # {sid: {id,title,tvdbId,runtime}}
    def iter_all_series(self, instance): return list(self._meta.values())
    def get_series_by_id(self, instance, sid): return self._meta.get(sid, {})


class _SonarrCache:
    def __init__(self, series_cache): self.series = series_cache


class _Cache:
    def __init__(self, d=None): self.d = d or {}
    def get(self, k): return self.d.get(k)


def _df(rows):
    """rows: (sid, title, season, ep, watched, runtime_s)."""
    recs = []
    for (sid, title, s, e, watched, rt) in rows:
        recs.append({
            "series_id": sid, "series_title": title, "season_number": s, "episode_number": e,
            "is_watched": watched, "runtime_seconds": rt, "next_episode": False,
            "keep_policy": None, "last_watched_at": ("2026-06-19T00:00:00Z" if watched else None),
            "certification": None, "watchability_percentile": None,
        })
    return pd.DataFrame(recs)


def _season_ep_cache(df):
    cache = {}
    for r in df.itertuples():
        cache.setdefault(int(r.series_id), {}).setdefault(int(r.season_number), []).append(
            {"episodeNumber": int(r.episode_number), "episodeFileId": None})
    return cache


_META = {1: {"id": 1, "title": "Chicago Fire", "tvdbId": None, "runtime": 60},
         2: {"id": 2, "title": "Chicago P.D.", "tvdbId": None, "runtime": 60}}


def _run(df, *, enabled):
    m = object.__new__(ef.SonarrCacheEpisodeFilesManager)
    # recency_gate off → no cold-skip / no reorder (deterministic); budget binds at 3 eps (3h / 60m).
    m.config = {"acquisition": {"enabled": True, "universe": {"enabled": enabled},
                                "next_episode": {"recency_gate": {"enabled": False}}}}
    m.global_cache = _Cache()
    m.logger = _Logger()
    m.dry_run = True
    m.sonarr_cache = _SonarrCache(_SeriesCache(_META))
    out = m._compute_next_episodes(df.copy(), "standard", {}, season_ep_cache=_season_ep_cache(df))
    return {(int(r.series_id), int(r.season_number), int(r.episode_number))
            for r in out.itertuples() if bool(r.next_episode)}


# Both shows started (E1 watched), 6 owned episodes each.
def _two_started_df():
    rows = []
    for sid, title in ((1, "Chicago Fire"), (2, "Chicago P.D.")):
        for e in range(1, 7):
            rows.append((sid, title, 1, e, e == 1, 3600))   # E1 watched, E2-E6 unwatched
    return _df(rows)


def test_off_is_per_series_byte_identical():
    # Each started series independently flags its next 3 unwatched (budget = 3h / 60m).
    flagged = _run(_two_started_df(), enabled=False)
    assert flagged == {(1, 1, 2), (1, 1, 3), (1, 1, 4),
                       (2, 1, 2), (2, 1, 3), (2, 1, 4)}


def test_on_shares_one_budget_frontier_first():
    # Same library, feature ON: One Chicago shares ONE budget → the frontier (Fire, timeline 0)
    # consumes it; P.D. (timeline 1) waits its turn — NOT flagged this run.
    flagged = _run(_two_started_df(), enabled=True)
    assert flagged == {(1, 1, 2), (1, 1, 3), (1, 1, 4)}          # Fire only
    assert not any(sid == 2 for (sid, _, _) in flagged)          # P.D. waits


def test_on_prefetches_next_unstarted_member_when_current_exhausted():
    # Fire fully watched (no next-up); P.D. UNSTARTED. Engaged saga → the budget flows to the next
    # show: P.D.'s start is prefetched ("finish Loki → Daredevil").
    rows = []
    for e in (1, 2, 3):
        rows.append((1, "Chicago Fire", 1, e, True, 3600))      # Fire: all 3 owned eps watched
    for e in range(1, 7):
        rows.append((2, "Chicago P.D.", 1, e, False, 3600))     # P.D.: owned, unstarted
    flagged = _run(_df(rows), enabled=True)
    assert flagged == {(2, 1, 1), (2, 1, 2), (2, 1, 3)}          # P.D. start, Fire exhausted


def test_off_does_not_prefetch_unstarted_series():
    # Same library as above but feature OFF: P.D. is unstarted → not in the resume frame → the
    # legacy walk never touches it; Fire is fully watched → nothing flagged at all.
    rows = []
    for e in (1, 2, 3):
        rows.append((1, "Chicago Fire", 1, e, True, 3600))
    for e in range(1, 7):
        rows.append((2, "Chicago P.D.", 1, e, False, 3600))
    assert _run(_df(rows), enabled=False) == set()

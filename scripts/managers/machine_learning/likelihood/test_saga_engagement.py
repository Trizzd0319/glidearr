"""saga_engagement — the I/O gatherer for the household, cross-media saga QUALITY credit, plus the
Radarr pre-pass application (caught-up frontier movie → Remux-reaching credit, default-off)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.likelihood.saga_engagement import (
    gather_saga_engagement,
    household_member_count,
    saga_credit_enabled,
)


class _RS:
    def __init__(self): self.calls = []
    def add_rows(self, service, concern, instance, headers, rows, order=None):
        self.calls.append((service, concern, instance, headers, rows, order))


class _GC:
    def __init__(self, d, rs=None):
        self.d = d
        self.key_builder = None          # key_builder None → Sonarr parquets skipped (movies-only path)
        self.run_summary = rs
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


# MCU-ish movies-only saga: 10 → 11 → 12 in timeline order.
_SOURCE = {"universes": {"mcu": {"timeline": True, "items": [
    {"media": "movie", "tmdb": 10, "rank": 0},
    {"media": "movie", "tmdb": 11, "rank": 1},
    {"media": "movie", "tmdb": 12, "rank": 2}]}}}


def _cfg(enabled=True):
    return {"scoring": {"saga_credit": {"enabled": enabled}},
            "radarr_instances": {"standard": {}},
            "rating_groups": {"household": {}}}


def _cache(watched=("10", "11")):
    return _GC({
        "plex/playlists/universe_source": _SOURCE,
        "radarr.movies.standard.full": [{"tmdbId": 10}, {"tmdbId": 11}, {"tmdbId": 12}],
        "tautulli/group/household/tmdb_completions": {
            t: {"pct": 1.0, "threshold": 0.8} for t in watched},
    })


def test_flag_default_off_returns_empty():
    assert saga_credit_enabled({}) is False
    assert gather_saga_engagement(_cache(), _cfg(enabled=False)) == {}


def test_gather_movies_only_computes_caught_up():
    eng = gather_saga_engagement(_cache(watched=("10", "11")), _cfg(True))
    assert eng[("movie", 12)]["caught_up_frac"] == 1.0           # 10 & 11 watched → caught up to 12
    assert eng[("movie", 12)]["saga_watched_frac"] == round(2 / 3, 4)
    assert eng[("movie", 10)]["caught_up_frac"] == 0.0           # first entry, no priors


def test_gather_empty_without_source():
    gc = _GC({})                                                 # no universe source
    assert gather_saga_engagement(gc, _cfg(True)) == {}


def test_household_member_count_from_rating_groups():
    cfg = {"rating_groups": {"household": {"members": ["a", "b", "c"]}, "kids": {"members": ["a", "d"]}}}
    assert household_member_count(cfg, None) == 4                 # distinct union a,b,c,d


def test_household_member_count_unknown_is_zero():
    assert household_member_count({}, None) == 0


# ── Radarr pre-pass application ────────────────────────────────────────────────
def _radarr_mgr(cfg, cache):
    from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager
    m = object.__new__(RadarrSpacePressureManager)
    m.config = cfg
    m.global_cache = cache
    m.logger = _Log()
    return m


# A 6-film saga (tmdb 10..15) with only the FIRST watched → overall depth is low (1/6 ≈ 17%, well
# under the 50% threshold), so the depth signal alone can't reach Remux and the CAUGHT-UP signal is
# isolated: only an entry whose priors are watched gets the frontier boost.
_SOURCE6 = {"universes": {"mcu": {"timeline": True,
                                  "items": [{"media": "movie", "tmdb": 10 + i, "rank": i} for i in range(6)]}}}


def _cache6(watched=("10",)):
    return _GC({
        "plex/playlists/universe_source": _SOURCE6,
        "radarr.movies.standard.full": [{"tmdbId": 10 + i} for i in range(6)],
        "tautulli/group/household/tmdb_completions": {t: {"pct": 1.0, "threshold": 0.8} for t in watched},
    })


def test_radarr_saga_credit_lifts_caught_up_frontier_not_far_member():
    m = _radarr_mgr(_cfg(True), _cache6(watched=("10",)))
    now = pd.Timestamp.now(tz="UTC").isoformat()
    df = pd.DataFrame([
        {"movie_id": 1, "tmdb_id": 11, "date_added": now},   # frontier: its one prior (10) is watched
        {"movie_id": 2, "tmdb_id": 15, "date_added": now},   # far: only 1 of 5 priors watched, shallow
    ])
    out = m._saga_quality_credits(df, pd.to_numeric(df["movie_id"], errors="coerce"), "standard")
    assert out.get(1, 0) >= 3.857       # caught-up frontier, fresh → reaches the Remux gate
    assert out.get(2, 0) < 3.857        # not caught up + shallow depth → no Remux boost


def test_radarr_saga_credit_off_is_byte_identical_noop():
    m = _radarr_mgr(_cfg(enabled=False), _cache6())
    now = pd.Timestamp.now(tz="UTC").isoformat()
    df = pd.DataFrame([{"movie_id": 1, "tmdb_id": 11, "date_added": now}])
    assert m._saga_quality_credits(df, pd.to_numeric(df["movie_id"], errors="coerce"), "standard") == {}


def test_radarr_saga_credit_faded_when_long_owned_unwatched():
    # Caught-up frontier movie, but acquired 400 days ago and never watched → past the grace window,
    # decayed well below the Remux gate.
    m = _radarr_mgr(_cfg(True), _cache6(watched=("10",)))
    old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=400)).isoformat()
    df = pd.DataFrame([{"movie_id": 1, "tmdb_id": 11, "date_added": old}])
    out = m._saga_quality_credits(df, pd.to_numeric(df["movie_id"], errors="coerce"), "standard")
    assert out.get(1, 0) < 3.857


def test_radarr_saga_credit_emits_log_table_and_gui_snapshot():
    rs = _RS()
    cache = _cache6(watched=("10",))
    cache.run_summary = rs
    m = _radarr_mgr(_cfg(True), cache)
    now = pd.Timestamp.now(tz="UTC").isoformat()
    df = pd.DataFrame([{"movie_id": 1, "tmdb_id": 11, "title": "The Frontier", "date_added": now}])
    m._saga_quality_credits(df, pd.to_numeric(df["movie_id"], errors="coerce"), "standard")
    # (1) run-summary table for the log
    assert any(c[0] == "radarr" and c[1] == "Saga credit (caught-up/depth)" for c in rs.calls)
    # (2) offline snapshot the future GUI reads
    snap = cache.get("universe/saga_credit_preview/radarr/standard")
    assert snap and snap["count"] == 1
    it = snap["items"][0]
    assert it["title"] == "The Frontier"
    assert it["saga"] == "Marvel Cinematic Universe"      # saga_display_name('mcu')
    assert it["floor_likelihood"] >= 90                   # the credit alone reaches Remux unwatched
    assert it["caught_up"] == 1.0


# ── Sonarr pre-pass application (cross-media: a watched prior MOVIE lifts a SHOW) ───────
def _sonarr_mgr(cfg, cache):
    from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = cfg
    m.global_cache = cache
    m.logger = _Log()
    return m


# Saga: movie 10 → show (tvdb 500). The show's only prior is the movie.
_SOURCE_TV = {"universes": {"mcu": {"timeline": True, "items": [
    {"media": "movie", "tmdb": 10, "rank": 0},
    {"media": "show", "tvdb": 500, "rank": 1}]}}}


def _cache_tv(watched_movies=("10",)):
    return _GC({
        "plex/playlists/universe_source": _SOURCE_TV,
        "radarr.movies.standard.full": [{"tmdbId": 10}],
        "tautulli/group/household/tmdb_completions": {t: {"pct": 1.0, "threshold": 0.8} for t in watched_movies},
    })


def test_sonarr_saga_credit_lifts_caught_up_show_cross_media():
    # Household watched the prior MOVIE (10); the SHOW (tvdb 500) is now caught-up → its TV quality is
    # boosted toward Remux, proving cross-media (movie watch → show credit).
    m = _sonarr_mgr(_cfg(True), _cache_tv(watched_movies=("10",)))
    now = pd.Timestamp.now(tz="UTC").isoformat()
    df = pd.DataFrame([{"series_id": 7, "date_added": now}, {"series_id": 7, "date_added": now}])
    rows = [{"id": 7, "tvdbId": 500, "title": "Loki"}]
    out = m._saga_quality_credits(df, rows, pd.to_numeric(df["series_id"], errors="coerce"), "standard")
    assert out.get(7, 0) >= 3.857


def test_sonarr_saga_credit_off_is_noop():
    m = _sonarr_mgr(_cfg(enabled=False), _cache_tv())
    now = pd.Timestamp.now(tz="UTC").isoformat()
    df = pd.DataFrame([{"series_id": 7, "date_added": now}])
    assert m._saga_quality_credits(df, [{"id": 7, "tvdbId": 500}],
                                   pd.to_numeric(df["series_id"], errors="coerce"), "standard") == {}

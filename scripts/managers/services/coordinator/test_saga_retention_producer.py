"""SagaRetentionProducerManager — the catch-up retention producer. Covers the pure history
bucketing (watched vs started, title→id, last-activity anchor) and the wired run() that assembles
per_user, calls the brain, and writes lifecycle/saga_gates — including fail-open + dedup paths."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import scripts.managers.services.coordinator.saga_retention_producer as prod

RECENT = int(time.time()) - 86_400          # ~1 day ago (epoch seconds) → never dormant


class _Cache:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _Pum:
    def __init__(self, tracked): self.tracked_users = tracked


class _Registry:
    def __init__(self, pum=None): self._pum = pum
    def get(self, kind, name): return self._pum if name == "PlexUsersManager" else None


def _mgr(config=None, cache=None, registry=None):
    m = object.__new__(prod.SagaRetentionProducerManager)
    m.config = config or {}
    m.global_cache = cache
    m.logger = _Logger()
    m.registry = registry
    m.dry_run = True
    m.sonarr = m.radarr = m.plex = m.tautulli = None
    return m


MOVIE_MAP = {"ironman": 1, "theavengers": 2}
SHOW_MAP = {"loki": 100}
# MCU saga: Iron Man(1) → Avengers(2) → Loki(100), one unified rank axis.
SRC = {"universes": {"mcu": {"timeline": True, "items": [
    {"media": "movie", "tmdb": 1}, {"media": "movie", "tmdb": 2}, {"media": "show", "tvdb": 100}]}}}


# ── pure bucketing ────────────────────────────────────────────────────────────────
def test_bucket_watched_vs_started_and_grace():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    epoch = lambda d: int((now - timedelta(days=d)).timestamp())
    rows = [
        {"user_id": 7, "media_type": "movie", "title": "Iron Man", "percent_complete": 95, "date": epoch(2)},
        {"user_id": 7, "media_type": "movie", "title": "The Avengers", "percent_complete": 20, "date": epoch(1)},  # started, recent
        {"user_id": 7, "media_type": "episode", "grandparent_title": "Loki", "percent_complete": 10, "date": epoch(30)},  # stale start → dropped
    ]
    per_user, last = _mgr()._bucket_history(rows, MOVIE_MAP, SHOW_MAP, now=now, thr=0.8, grace=7)
    assert per_user["7"]["watched"]["movies"] == {1: (now - timedelta(days=2)).isoformat()}
    assert set(per_user["7"]["started"]["movies"]) == {2}      # Avengers started, recent
    assert per_user["7"]["watched"]["shows"] == {} and per_user["7"]["started"]["shows"] == {}  # Loki stale → dropped
    assert last["7"] == now - timedelta(days=1)                # latest activity across all rows


def test_bucket_watched_beats_started_same_title():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    e = int(now.timestamp())
    rows = [{"user_id": 9, "media_type": "movie", "title": "Iron Man", "percent_complete": 30, "date": e},
            {"user_id": 9, "media_type": "movie", "title": "Iron Man", "percent_complete": 99, "date": e}]
    per_user, _ = _mgr()._bucket_history(rows, MOVIE_MAP, SHOW_MAP, now=now, thr=0.8, grace=7)
    assert per_user["9"]["watched"]["movies"] == {1: now.isoformat()} and per_user["9"]["started"]["movies"] == {}


def test_resolve_id_year_fallback():
    assert prod._resolve_id({"bluey": 55}, "Bluey (2018)") == 55      # history owned w/ year, watched w/o
    assert prod._resolve_id({"ironman": 1}, "Unknown Film") is None


# ── wired run() ───────────────────────────────────────────────────────────────────
def _run(extra_cache=None, config_over=None, tracked=None, monkeypatch=None, maps=(MOVIE_MAP, SHOW_MAP)):
    cache = _Cache({"plex/playlists/universe_source": SRC,
                    "tautulli/history/all": [
                        {"user_id": 7, "media_type": "movie", "title": "Iron Man",
                         "percent_complete": 95, "date": RECENT}],
                    **(extra_cache or {})})
    cfg = {"saga_retention": {"enabled": True, "completion_threshold": 0.8,
                              "dormancy_window_days": 90, "expiry_boost_days": 30, **(config_over or {})}}
    reg = _Registry(_Pum(tracked if tracked is not None else [{"safe_user": "Aiden", "tautulli_user_id": 7}]))
    m = _mgr(cfg, cache, reg)
    if monkeypatch is not None:
        monkeypatch.setattr(m, "_title_id_maps", lambda: maps)
    else:
        m._title_id_maps = lambda: maps
    out = m.run()
    return out, cache.get("lifecycle/saga_gates")


def test_run_holds_unreached_members():
    # user 7 watched Iron Man (rank 0) → engaged with mcu → Avengers(2) + Loki(100) held; Iron Man freed.
    out, gates = _run()
    assert gates["movies"] == {2: ["mcu"]} and gates["shows"] == {100: ["mcu"]}
    assert gates["gate_user_count"] == {"mcu": 1} and out == {"sagas": 1, "held": 2}


def test_run_disabled_writes_nothing():
    cache = _Cache({"plex/playlists/universe_source": SRC})
    m = _mgr({"saga_retention": {"enabled": False}}, cache, _Registry())
    assert m.run() == {} and "lifecycle/saga_gates" not in cache.d


def test_run_failopen_empty_source():
    cache = _Cache({"plex/playlists/universe_source": {}, "tautulli/history/all": [{"user_id": 7}]})
    m = _mgr({"saga_retention": {"enabled": True}}, cache, _Registry())
    m._title_id_maps = lambda: (MOVIE_MAP, SHOW_MAP)
    assert m.run() == {"sagas": 0, "held": 0}
    assert cache.get("lifecycle/saga_gates") == prod._EMPTY_GATES     # nothing held (fail-open)


def test_run_exclude_users_accepts_username():
    out, gates = _run(config_over={"exclude_users": ["Aiden"]})        # safe_user → translated to id 7
    assert gates == prod._EMPTY_GATES and out == {"sagas": 0, "held": 0}


def test_run_watchlist_only_holds_prefix():
    # user 7 watched nothing but watchlisted Loki(100, rank 2) → holds the prefix (1,2) + Loki itself.
    out, gates = _run(
        extra_cache={"tautulli/history/all": [],
                     "plex/users/Aiden/watchlist": [{"type": "show", "ids": {"tvdb": 100}}]},
        config_over={"watchlist_hold_policy": "indefinite"})
    assert gates["movies"] == {1: ["mcu"], 2: ["mcu"]} and gates["shows"] == {100: ["mcu"]}


def test_title_id_maps_reads_radarr_cache():
    cache = _Cache({"radarr.movies.standard.full": [
        {"title": "Iron Man", "tmdbId": 1}, {"title": "The Avengers (2012)", "tmdbId": 2}]})
    m = _mgr({}, cache, _Registry())
    movie_map, show_map = m._title_id_maps()
    assert movie_map == {"ironman": 1, "theavengers2012": 2} and show_map == {}

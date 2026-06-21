"""HybridUniverseAcquisitionManager — the Phase-7 universe acquisition capstone. Covers the pure
flatten/dedup/cap (crossover dedup + start-first order + max_per_run) and the wired run() pipeline
(two-flag gate, engaged-saga backfill, owned-skip, movie/show routing, cap, tv-off)."""
from __future__ import annotations

import scripts.managers.services.coordinator.hybrid_universe_acquisition as hua


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _Acq:
    def __init__(self): self.calls = []
    def ensure_owned_and_grab(self, tmdb, **k):
        self.calls.append(("movie", tmdb)); return {"action": "would-add", "title": f"m{tmdb}"}
    def ensure_show_owned_and_grab(self, tvdb, **k):
        self.calls.append(("show", tvdb)); return {"action": "would-add", "title": f"s{tvdb}"}


class _Reg:
    def __init__(self, acq): self._acq = acq
    def get(self, kind, name): return self._acq if name == "AcquisitionManager" else None
    def set_flag(self, f): pass


class _KB:
    base_dir = None                                  # → _sonarr_maps returns empty (no parquet glob)


class _Cache:
    def __init__(self, d): self.d = dict(d); self.key_builder = _KB()
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def _mgr(config, cache, registry, *, dry_run=True):
    m = object.__new__(hua.HybridUniverseAcquisitionManager)
    m.config = config; m.global_cache = cache; m.logger = _Logger(); m.registry = registry
    m.dry_run = dry_run; m.sonarr = m.radarr = m.tautulli = None
    return m


# MCU: Iron Man(1) → Avengers(2) → Loki(show 100)
SRC = {"universes": {"mcu": {"timeline": True, "items": [
    {"media": "movie", "tmdb": 1}, {"media": "movie", "tmdb": 2}, {"media": "show", "tvdb": 100}]}}}

ON = {"acquisition": {"universe": {"enabled": True, "max_per_run": 5}},
      "plex": {"playlists": {"universe_timeline": {"enabled": True}}}}


# ── pure flatten/dedup/cap ──────────────────────────────────────────────────────────
def test_flatten_dedups_crossover_and_orders_start_first():
    plan = {"a": [{"media": "movie", "id": 1, "rank": 0}, {"media": "movie", "id": 2, "rank": 1}],
            "b": [{"media": "movie", "id": 2, "rank": 0}, {"media": "show", "id": 9, "rank": 1}]}
    selected, dropped, flat = hua._flatten_dedup_cap(plan, cap=5)
    ids = [(m["media"], m["id"]) for m in flat]
    assert ids.count(("movie", 2)) == 1                       # crossover film deduped to ONE slot
    assert ids == [("movie", 1), ("movie", 2), ("show", 9)]   # (rank, key) order


def test_flatten_caps_and_keeps_the_dropped_tail():
    plan = {"a": [{"media": "movie", "id": i, "rank": i} for i in range(5)]}
    sel, drop, flat = hua._flatten_dedup_cap(plan, cap=2)
    assert [m["id"] for m in sel] == [0, 1] and len(drop) == 3


# ── wired run() ─────────────────────────────────────────────────────────────────────
def test_run_backfills_engaged_saga(monkeypatch):
    monkeypatch.setattr(hua, "ArrGateway", lambda *a, **k: object())
    acq = _Acq()
    cache = _Cache({"plex/playlists/universe_source": SRC,
                    "radarr.movies.standard.full": [{"tmdbId": 1, "title": "Iron Man"}],   # 1 owned
                    "tautulli/group/household/tmdb_completions": {"1": {"pct": 1.0, "threshold": 0.8}}})
    out = _mgr(ON, cache, _Reg(acq)).run()
    assert out["enabled"] and out["selected"] == 2
    assert ("movie", 2) in acq.calls and ("show", 100) in acq.calls   # unowned members grabbed
    assert ("movie", 1) not in acq.calls                              # owned → not a gap


def test_run_disabled_when_a_flag_is_off():
    cfg = {"acquisition": {"universe": {"enabled": True}},
           "plex": {"playlists": {"universe_timeline": {"enabled": False}}}}
    assert _mgr(cfg, _Cache({}), _Reg(_Acq())).run() == {"enabled": False}


def test_run_noop_without_source():
    assert _mgr(ON, _Cache({}), _Reg(_Acq())).run()["action"] == "noop"


def test_run_caps_grabs_to_max_per_run(monkeypatch):
    monkeypatch.setattr(hua, "ArrGateway", lambda *a, **k: object())
    acq = _Acq()
    src = {"universes": {"mcu": {"timeline": True, "items": [{"media": "movie", "tmdb": i} for i in range(1, 6)]}}}
    cache = _Cache({"plex/playlists/universe_source": src, "radarr.movies.standard.full": [],
                    "tautulli/group/household/tmdb_completions": {"1": {"pct": 1.0, "threshold": 0.8}}})
    cfg = {"acquisition": {"universe": {"enabled": True, "max_per_run": 2}},
           "plex": {"playlists": {"universe_timeline": {"enabled": True}}}}
    out = _mgr(cfg, cache, _Reg(acq)).run()
    assert out["selected"] == 2 and out["dropped"] == 3 and len(acq.calls) == 2


def test_run_tv_off_skips_shows(monkeypatch):
    monkeypatch.setattr(hua, "ArrGateway", lambda *a, **k: object())
    acq = _Acq()
    cache = _Cache({"plex/playlists/universe_source": SRC, "radarr.movies.standard.full": [{"tmdbId": 1}],
                    "tautulli/group/household/tmdb_completions": {"1": {"pct": 1.0, "threshold": 0.8}}})
    cfg = {"acquisition": {"universe": {"enabled": True, "max_per_run": 5, "tv": False}},
           "plex": {"playlists": {"universe_timeline": {"enabled": True}}}}
    _mgr(cfg, cache, _Reg(acq)).run()
    assert ("movie", 2) in acq.calls and ("show", 100) not in acq.calls

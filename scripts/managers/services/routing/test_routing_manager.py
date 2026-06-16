"""Tests for the in-run RoutingManager (the library re-organizer driver). Verifies the three
gates (routing.configured, reorg_mode, relocation consent + dry_run) and that same_instance
issues the correct series/movie editor PUT. A fake instance-manager records GET/PUT calls; the
CSM cache is bypassed so classification is deterministic from genre/language."""
from __future__ import annotations

from scripts.managers.services.routing import RoutingManager


class _Im:
    """Fake instance-manager: serves owned items per instance on GET, records editor PUTs."""

    def __init__(self, items):
        self._items = items            # {instance_name: [arr_obj, ...]}
        self.gets, self.puts = [], []

    def _get_apis(self):
        return {n: object() for n in self._items}

    def _make_request(self, name, endpoint, method="GET", payload=None, fallback=None, **kw):
        if method == "PUT":
            self.puts.append((name, endpoint, payload))
            return {"ok": True}
        self.gets.append((name, endpoint))
        return self._items.get(name, fallback if fallback is not None else [])


class _Mgr:
    def __init__(self, im):
        self.instance_manager = im


_MRF = {"standard": "/m/std", "kids": "/m/kids", "anime": "/m/anime"}
_RF = {"series": "/t/series", "anime": "/t/anime", "kids": "/t/kids"}
_ANIME_MOVIE = {"id": 1, "title": "Akira", "genres": ["Animation"],
                "originalLanguage": {"name": "Japanese"}, "rootFolderPath": "/m/std", "tmdbId": 999}
_ANIME_SHOW = {"id": 7, "title": "Bleach", "genres": ["Anime"], "seriesType": "standard",
               "rootFolderPath": "/t/series", "tmdbId": 7}


def _cfg(reorg_mode="log_only", configured=True, consent=False):
    routing = {"reorg_mode": reorg_mode,
               "movies": {"kids_bucket_enabled": True, "anime_policy": "dedicated"},
               "tv": {"anime_policy": "series_type_plus_folder", "kids_bucket_enabled": True}}
    if configured:
        routing["configured"] = True
    c = {"routing": routing, "movieRootFolders": dict(_MRF), "rootFolders": dict(_RF)}
    if consent:
        c["relocation_consent"] = True
    return c


def _mgr(config, *, movies=None, shows=None, dry_run=False, logger=None):
    rim, sim = _Im(movies or {}), _Im(shows or {})
    m = RoutingManager(config=config, logger=logger, radarr=_Mgr(rim), sonarr=_Mgr(sim), dry_run=dry_run)
    m._movie_ages, m._show_ages = {}, {}        # bypass on-disk CSM cache for determinism
    return m, rim, sim


class _CapLogger:
    """Captures main-log lines (log_info) separately from the dedicated-file sink (log_to_file)."""
    def __init__(self):
        self.info, self.files = [], []
    def log_info(self, m): self.info.append(m)
    def log_warning(self, m): pass
    def log_success(self, m): pass
    def log_to_file(self, category, message, *, reset=False): self.files.append((category, message, reset))


# ── the configured gate ───────────────────────────────────────────────────────
def test_not_configured_does_nothing():
    m, rim, _ = _mgr(_cfg(configured=False), movies={"standard": [_ANIME_MOVIE]})
    m.run()
    assert rim.gets == [] and rim.puts == []          # never even fetches


def test_off_mode_does_nothing():
    m, rim, _ = _mgr(_cfg(reorg_mode="off"), movies={"standard": [_ANIME_MOVIE]})
    m.run()
    assert rim.gets == [] and rim.puts == []


# ── log_only classifies + logs but never moves ────────────────────────────────
def test_log_only_fetches_but_does_not_move():
    m, rim, _ = _mgr(_cfg(reorg_mode="log_only"), movies={"standard": [_ANIME_MOVIE]})
    m.run()
    assert ("standard", "movie") in rim.gets          # it fetched + planned
    assert rim.puts == []                              # but moved nothing


def test_per_title_plan_goes_to_dedicated_file_not_main_log():
    log = _CapLogger()
    m, _, _ = _mgr(_cfg(reorg_mode="log_only"), movies={"standard": [_ANIME_MOVIE]}, logger=log)
    m.run()
    routing_lines = [msg for (cat, msg, _r) in log.files if cat == "routing"]
    assert any("Akira" in msg for msg in routing_lines)        # per-title line → dedicated file
    assert not any("Akira" in s for s in log.info)             # NOT flooding the main log
    assert any("misplaced" in s for s in log.info)             # main log keeps the summary count
    assert any(reset for (_c, _m, reset) in log.files)         # fresh plan each run (reset header)


# ── same_instance moves only with consent + a live (non-dry) run ──────────────
def test_same_instance_with_consent_moves_movie():
    m, rim, _ = _mgr(_cfg(reorg_mode="same_instance", consent=True),
                     movies={"standard": [_ANIME_MOVIE]}, dry_run=False)
    m.run()
    assert len(rim.puts) == 1
    name, ep, payload = rim.puts[0]
    assert ep == "movie/editor" and payload["movieIds"] == [1]
    assert payload["moveFiles"] is True and payload["rootFolderPath"] == "/m/anime"


def test_same_instance_without_consent_does_not_move():
    m, rim, _ = _mgr(_cfg(reorg_mode="same_instance", consent=False),
                     movies={"standard": [_ANIME_MOVIE]}, dry_run=False)
    m.run()
    assert rim.puts == []                              # consent gate blocks the move


def test_same_instance_dry_run_does_not_move():
    m, rim, _ = _mgr(_cfg(reorg_mode="same_instance", consent=True),
                     movies={"standard": [_ANIME_MOVIE]}, dry_run=True)
    m.run()
    assert rim.puts == []                              # dry_run never PUTs


# ── shows: folder move + seriesType correction in one editor call ─────────────
def test_same_instance_moves_show_with_seriestype():
    m, _, sim = _mgr(_cfg(reorg_mode="same_instance", consent=True),
                     shows={"sonarr": [_ANIME_SHOW]}, dry_run=False)
    m.run()
    assert len(sim.puts) == 1
    name, ep, payload = sim.puts[0]
    assert ep == "series/editor" and payload["seriesIds"] == [7]
    assert payload["rootFolderPath"] == "/t/anime" and payload["seriesType"] == "anime"

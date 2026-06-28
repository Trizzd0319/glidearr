"""AcquisitionManager.ensure_owned_and_grab — the targeted single-film acquire the hybrid
universe walk uses. Isolates the BRANCHING (dedup / hasFile / exact-tmdb guard / dry-run /
space-pause / defer) by faking Resolver.prepare + Adder.add; the gateway space/search calls
are real against a fake instance-manager."""
from __future__ import annotations

import scripts.managers.services.acquisition as acq

_DEFERRED_KEY = "acquisition/deferred_search"


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _IM:
    def __init__(self, free=9999, total=10000): self._f, self._t = free, total
    def disk_free_gb(self, inst): return self._f
    def disk_total_gb(self, inst): return self._t


class _GW:
    """Just the surface ensure_owned_and_grab touches (movie record scan + search/monitor)."""
    def __init__(self, im=None, library=None, available=True):
        self.service, self.im, self.available = "radarr", im or _IM(), available
        self._library = library or []
        self.commands, self.puts = [], []

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return "standard"
    def resolve(self, inst): return inst
    def library_items(self, inst): return self._library
    def put(self, inst, ep, payload): self.puts.append((inst, ep, payload)); return {"id": 1}
    def command(self, inst, payload): self.commands.append((inst, payload)); return {"ok": True}


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(config=None, *, dry_run=False, cache=None):
    m = object.__new__(acq.AcquisitionManager)
    m.config = config or {}
    m.logger = _Logger()
    m.dry_run = dry_run
    m.global_cache = cache
    m.radarr = None
    m.sonarr = None
    return m


def _sgw(library=None, im=None, available=True):
    """A Sonarr-flavoured fake gateway (series records, SeriesSearch) for the show twin."""
    g = _GW(im=im, library=library, available=available)
    g.service = "sonarr"
    return g


def _patch(monkeypatch, prepare_result, add_result=None):
    """Fake Resolver/Adder so the test drives ensure_owned_and_grab's branching directly.
    Returns the list of (enriched, search) the Adder was asked to add."""
    monkeypatch.setattr(acq.Resolver, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(acq.Resolver, "prepare", lambda self, cand: dict(prepare_result))
    adds: list = []
    monkeypatch.setattr(acq.Adder, "__init__", lambda self, *a, **k: None)

    def _add(self, e, search=None):
        adds.append((e, search))
        return dict(add_result or {"action": "added", "ok": True, "result": {"id": 999}})
    monkeypatch.setattr(acq.Adder, "add", _add)
    return adds


# ── not in library → add ────────────────────────────────────────────────────
def test_not_in_library_added_with_search(monkeypatch):
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"})
    out = _mgr(cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": _GW()})
    assert out["action"] == "added"
    assert adds and adds[0][1] is True            # ample space → search ON


def test_not_in_library_dry_run_would_add(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"},
           add_result={"action": "would-add", "ok": False})
    out = _mgr(dry_run=True, cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": _GW()})
    assert out["action"] == "would-add"


# ── footgun guard: never add the wrong film on a fuzzy match ──────────────────
def test_tmdb_mismatch_fails_closed(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 999, "instance": "standard", "title": "Wrong"})
    out = _mgr().ensure_owned_and_grab(123, gateways={"radarr": _GW()})
    assert out["action"] == "skipped" and out["reason"] == "tmdb mismatch"


def test_no_lookup_match_skipped(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "no lookup match"})
    out = _mgr().ensure_owned_and_grab(123, gateways={"radarr": _GW()})
    assert out["action"] == "skipped" and out["reason"] == "no lookup match"


# ── already in Radarr ─────────────────────────────────────────────────────────
def test_already_in_library_with_file_is_owned(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    gw = _GW(library=[{"tmdbId": 123, "hasFile": True, "id": 5, "title": "X"}])
    out = _mgr().ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "already-owned" and not gw.commands   # no re-search


def test_already_in_library_no_file_searches(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    gw = _GW(library=[{"tmdbId": 123, "hasFile": False, "monitored": True, "id": 5, "title": "X"}])
    out = _mgr().ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "searched"
    assert gw.commands and gw.commands[0][1] == {"name": "MoviesSearch", "movieIds": [5]}


def test_already_in_library_no_file_dry_run_no_write(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    gw = _GW(library=[{"tmdbId": 123, "hasFile": False, "id": 5, "title": "X"}])
    out = _mgr(dry_run=True).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "would-search" and not gw.commands and not gw.puts


def test_owned_no_file_search_respects_space_band(monkeypatch):
    # The owned-no-file grab is a real search → it must honour the free-space band like a fresh add.
    _patch(monkeypatch, {"skip_reason": "already in library"})
    # full + deletions OFF → pause (no search).
    gw = _GW(im=_IM(free=100, total=8000),
             library=[{"tmdbId": 123, "hasFile": False, "id": 5, "title": "X"}])
    out = _mgr({"free_space_limit": 2000}, cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "paused" and not gw.commands


def test_owned_no_file_defers_under_pressure_when_deletions_armed(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    cache = _Cache()
    cfg = {"free_space_limit": 2000, "deletions_consent": True}   # deletions armed → defer, not search
    gw = _GW(im=_IM(free=100, total=8000),
             library=[{"tmdbId": 123, "hasFile": False, "monitored": True, "id": 5, "title": "X"}])
    out = _mgr(cfg, cache=cache).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "deferred" and not gw.commands           # queued, not grabbed now
    assert cache.get(_DEFERRED_KEY)[0]["arr_id"] == 5


# ── cross-mount dedup: film owned on a DIFFERENT Radarr instance ───────────────
class _MultiGW(_GW):
    """library_items differs per instance, so we can place a film on the 4K mount only."""
    def __init__(self, by_instance):
        super().__init__()
        self._by = by_instance

    def library_items(self, inst): return self._by.get(inst, [])


_TWO_INSTANCES = {"radarr_instances": {"default_instance": "standard", "standard": {}, "uhd": {}}}


def test_fresh_add_dedups_film_owned_on_other_instance(monkeypatch):
    # prepare() only checks the routed (default) instance → "not in library" (skip_reason None),
    # but the film is already owned (hasFile) on the 4K instance → must dedup, never POST a dup add.
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"})
    gw = _MultiGW({"standard": [], "uhd": [{"tmdbId": 123, "hasFile": True, "id": 7, "title": "X"}]})
    out = _mgr(_TWO_INSTANCES, cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "already-owned" and not adds       # found on uhd, never added on default


def test_fresh_add_grabs_owned_no_file_on_other_instance(monkeypatch):
    # Same blind spot but the 4K copy has no file → search it IN PLACE on uhd, never re-add on default.
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"})
    gw = _MultiGW({"standard": [],
                   "uhd": [{"tmdbId": 123, "hasFile": False, "monitored": True, "id": 7, "title": "X"}]})
    out = _mgr(_TWO_INSTANCES, cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "searched" and not adds
    assert gw.commands and gw.commands[0] == ("uhd", {"name": "MoviesSearch", "movieIds": [7]})


# ── space discipline ──────────────────────────────────────────────────────────
def test_paused_when_full_and_deletions_off(monkeypatch):
    # free 100 < U(=2200); deletions not consented → can't reclaim → PAUSE (never strand the add).
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"})
    gw = _GW(im=_IM(free=100, total=8000))
    out = _mgr({"free_space_limit": 2000}, cache=_Cache()).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "paused"


def test_deferred_under_pressure_when_deletions_armed(monkeypatch):
    # free 100 < U but deletions armed (consent + floor) → ADD monitored, search OFF, queue search.
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X"})
    cache = _Cache()
    cfg = {"free_space_limit": 2000, "deletions_consent": True}
    gw = _GW(im=_IM(free=100, total=8000))
    out = _mgr(cfg, cache=cache).ensure_owned_and_grab(123, gateways={"radarr": gw})
    assert out["action"] == "deferred"
    assert adds and adds[0][1] is False                       # search OFF under pressure
    q = cache.get(_DEFERRED_KEY)
    assert q and q[0]["arr_id"] == 999 and q[0]["service"] == "radarr"


# ── FORK-D: rehome_to_standard (add a sub-4K copy on standard, dedup STANDARD-only) ──
def test_rehome_adds_on_standard_even_when_4k_copy_owned(monkeypatch):
    # The SAME tmdb has a 4K copy (hasFile) on the uhd instance. ensure_owned_and_grab would
    # find it (all-instance scan) and return already-owned; rehome_to_standard dedups STANDARD
    # only, so it still adds on standard at the forced sub-4K profile.
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard",
                                "title": "X", "route_category": "standard", "quality_profile": {}})
    gw = _MultiGW({"standard": [], "uhd": [{"tmdbId": 123, "hasFile": True, "id": 7, "title": "X 4K"}]})
    out = _mgr(_TWO_INSTANCES, cache=_Cache()).rehome_to_standard(
        123, std_inst="standard", target_profile_id=8, gateways={"radarr": gw})
    assert out["action"] == "added"               # 4K copy did NOT short-circuit the add
    assert adds and adds[0][1] is True            # searches immediately (no defer → no deadlock)
    enriched = adds[0][0]
    assert enriched["instance"] == "standard"
    assert enriched["quality_profile"]["id"] == 8  # watchability-matched sub-4K profile forced


def test_rehome_dry_run_would_add(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 123, "instance": "standard", "title": "X",
                         "route_category": "standard", "quality_profile": {}},
           add_result={"action": "would-add", "ok": True})
    gw = _MultiGW({"standard": [], "uhd": []})
    out = _mgr(_TWO_INSTANCES, dry_run=True, cache=_Cache()).rehome_to_standard(
        123, std_inst="standard", target_profile_id=8, gateways={"radarr": gw})
    assert out["action"] == "would-add"


def test_rehome_already_on_standard_is_owned(monkeypatch):
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 123})   # prepare must NOT be reached
    gw = _MultiGW({"standard": [{"tmdbId": 123, "hasFile": True, "id": 5, "title": "X"}]})
    out = _mgr(_TWO_INSTANCES, cache=_Cache()).rehome_to_standard(
        123, std_inst="standard", target_profile_id=8, gateways={"radarr": gw})
    assert out["action"] == "already-owned" and not adds


def test_rehome_owned_no_file_on_standard_searches_now(monkeypatch):
    # Already added on standard (prior run) but no file → retune to the sub-4K target + search NOW.
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 123})
    gw = _MultiGW({"standard": [{"tmdbId": 123, "hasFile": False, "monitored": True, "id": 5,
                                 "title": "X", "qualityProfileId": 9}]})   # sat at a 4K profile
    out = _mgr(_TWO_INSTANCES).rehome_to_standard(
        123, std_inst="standard", target_profile_id=8, gateways={"radarr": gw})
    assert out["action"] == "searched"
    assert gw.commands and gw.commands[0][1] == {"name": "MoviesSearch", "movieIds": [5]}
    # the existing fileless record is retuned to the <=1080 target (8) so it can't re-grab a 4K
    assert any(p[1] == "movie/5" and p[2].get("qualityProfileId") == 8 for p in gw.puts)


def test_rehome_tmdb_mismatch_skips(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 999, "instance": "standard", "title": "Wrong",
                         "route_category": "standard", "quality_profile": {}})
    gw = _MultiGW({"standard": [], "uhd": []})
    out = _mgr(_TWO_INSTANCES).rehome_to_standard(
        123, std_inst="standard", target_profile_id=8, gateways={"radarr": gw})
    assert out["action"] == "skipped" and out["reason"] == "tmdb mismatch"


# ── show (Sonarr) twin: ensure_show_owned_and_grab ──────────────────────────────
_TWO_SONARR = {"sonarr_instances": {"default_instance": "standard", "standard": {}, "uhd": {}}}


def test_show_not_in_library_added_with_search(monkeypatch):
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 55, "instance": "standard",
                                "title": "Show", "type": "show"})
    out = _mgr(cache=_Cache()).ensure_show_owned_and_grab(55, gateways={"sonarr": _sgw()})
    assert out["action"] == "added" and adds and adds[0][1] is True


def test_show_already_owned_when_episodes_present(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    gw = _sgw(library=[{"tvdbId": 55, "statistics": {"episodeFileCount": 12}, "id": 5, "title": "Show"}])
    out = _mgr().ensure_show_owned_and_grab(55, gateways={"sonarr": gw})
    assert out["action"] == "already-owned" and not gw.commands       # has episodes → owned


def test_show_in_library_no_episodes_searches(monkeypatch):
    _patch(monkeypatch, {"skip_reason": "already in library"})
    gw = _sgw(library=[{"tvdbId": 55, "statistics": {"episodeFileCount": 0},
                        "monitored": True, "id": 5, "title": "Show"}])
    out = _mgr().ensure_show_owned_and_grab(55, gateways={"sonarr": gw})
    assert out["action"] == "searched"
    assert gw.commands and gw.commands[0][1] == {"name": "SeriesSearch", "seriesId": 5}


def test_show_tvdb_mismatch_fails_closed(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 999, "instance": "standard",
                         "title": "Wrong", "type": "show"})
    out = _mgr().ensure_show_owned_and_grab(55, gateways={"sonarr": _sgw()})
    assert out["action"] == "skipped" and out["reason"] == "tvdb mismatch"


def test_show_dedups_series_owned_on_other_instance(monkeypatch):
    # series owned (has episodes) on the 4K Sonarr instance → already-owned, never re-added on default.
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 55, "instance": "standard",
                                "title": "Show", "type": "show"})
    gw = _MultiGW({"standard": [],
                   "uhd": [{"tvdbId": 55, "statistics": {"episodeFileCount": 3}, "id": 7, "title": "Show"}]})
    gw.service = "sonarr"
    out = _mgr(_TWO_SONARR, cache=_Cache()).ensure_show_owned_and_grab(55, gateways={"sonarr": gw})
    assert out["action"] == "already-owned" and not adds


def test_show_paused_when_full_and_deletions_off(monkeypatch):
    _patch(monkeypatch, {"skip_reason": None, "ext_id": 55, "instance": "standard",
                         "title": "Show", "type": "show"})
    gw = _sgw(im=_IM(free=100, total=8000))
    out = _mgr({"free_space_limit": 2000}, cache=_Cache()).ensure_show_owned_and_grab(55, gateways={"sonarr": gw})
    assert out["action"] == "paused"


def test_show_defers_under_pressure_when_deletions_armed(monkeypatch):
    adds = _patch(monkeypatch, {"skip_reason": None, "ext_id": 55, "instance": "standard",
                                "title": "Show", "type": "show"})
    cache = _Cache()
    gw = _sgw(im=_IM(free=100, total=8000))
    out = _mgr({"free_space_limit": 2000, "deletions_consent": True}, cache=cache).ensure_show_owned_and_grab(
        55, gateways={"sonarr": gw})
    assert out["action"] == "deferred" and adds and adds[0][1] is False
    q = cache.get(_DEFERRED_KEY)
    assert q and q[0]["service"] == "sonarr" and q[0]["type"] == "show"

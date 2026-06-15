"""Tests for AcquisitionManager space-pressure deferral.

Under pressure (free < the band top U) a new-content add is queued for a deferred search
instead of grabbing immediately; once free recovers above U a later run flushes the queue
(triggers MoviesSearch/SeriesSearch). Exercises the real helper methods via a stub manager
(object.__new__) with fake gateways + global_cache.
"""
from __future__ import annotations

import pytest

from scripts.managers.services.acquisition import AcquisitionManager

_KEY = "acquisition/deferred_search"


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _IM:
    def __init__(self, free, total):
        self._free, self._total = free, total

    def disk_free_gb(self, inst):
        return self._free

    def disk_total_gb(self, inst):
        return self._total


class _GW:
    def __init__(self, service, im, available=True, cmd_resp={"ok": True}):
        self.service, self.im, self.available = service, im, available
        self._cmd_resp = cmd_resp          # None simulates a swallowed *arr failure
        self.commands = []

    def command(self, inst, payload):
        self.commands.append((inst, payload))
        return self._cmd_resp


class _Cache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v


def _mgr(config, *, dry_run=False, cache=None):
    m = object.__new__(AcquisitionManager)
    m.config = config
    m.logger = _Logger()
    m.dry_run = dry_run
    m.global_cache = cache
    return m


# free_space_limit 2500 -> T=2500, U=2750 (default 10% headroom)
def test_space_band_returns_band_top_U():
    m = _mgr({"free_space_limit": 2500})
    free, U = m._space_band(_GW("radarr", _IM(3000, 8000)), "standard", {})
    assert free == 3000.0
    assert U == pytest.approx(2750.0)


def test_trigger_search_payloads():
    m = _mgr({})
    gw = _GW("radarr", _IM(0, 0))
    assert m._trigger_search(gw, "standard", {"arr_id": 5, "type": "movie", "title": "M"})
    assert gw.commands == [("standard", {"name": "MoviesSearch", "movieIds": [5]})]
    gw2 = _GW("sonarr", _IM(0, 0))
    assert m._trigger_search(gw2, "720", {"arr_id": 7, "type": "show", "title": "S"})
    assert gw2.commands == [("720", {"name": "SeriesSearch", "seriesId": 7})]


def test_flush_searches_when_free_above_U_and_clears_queue():
    cache = _Cache()
    cache.set(_KEY, [
        {"service": "radarr", "instance": "standard", "arr_id": 5, "title": "M", "type": "movie"},
        {"service": "sonarr", "instance": "720", "arr_id": 7, "title": "S", "type": "show"},
    ])
    m = _mgr({"free_space_limit": 2500}, cache=cache)
    rg, sg = _GW("radarr", _IM(3000, 8000)), _GW("sonarr", _IM(3000, 8000))
    stats = m._flush_deferred({"radarr": rg, "sonarr": sg}, {})
    assert stats["searched"] == 2
    assert cache.get(_KEY) == []                       # cleared
    assert rg.commands == [("standard", {"name": "MoviesSearch", "movieIds": [5]})]
    assert sg.commands == [("720", {"name": "SeriesSearch", "seriesId": 7})]


def test_flush_keeps_items_still_under_pressure():
    cache = _Cache()
    items = [{"service": "radarr", "instance": "standard", "arr_id": 5, "title": "M", "type": "movie"}]
    cache.set(_KEY, list(items))
    m = _mgr({"free_space_limit": 2500}, cache=cache)
    gw = _GW("radarr", _IM(2600, 8000))                # 2600 < U 2750
    stats = m._flush_deferred({"radarr": gw}, {})
    assert stats["searched"] == 0
    assert stats["still_deferred"] == 1
    assert gw.commands == []
    assert cache.get(_KEY) == items                    # untouched


def test_flush_dry_run_does_not_search_or_clear():
    cache = _Cache()
    items = [{"service": "radarr", "instance": "standard", "arr_id": 5, "title": "M", "type": "movie"}]
    cache.set(_KEY, list(items))
    m = _mgr({"free_space_limit": 2500}, dry_run=True, cache=cache)
    gw = _GW("radarr", _IM(3000, 8000))
    stats = m._flush_deferred({"radarr": gw}, {})
    assert stats["searched"] == 1                      # would-search counted
    assert gw.commands == []                           # nothing issued
    assert cache.get(_KEY) == items                    # not cleared in dry_run


def test_flush_empty_queue_is_noop():
    cache = _Cache()
    m = _mgr({"free_space_limit": 2500}, cache=cache)
    stats = m._flush_deferred({"radarr": _GW("radarr", _IM(3000, 8000))}, {})
    assert stats == {"pending": 0, "searched": 0, "abandoned": 0, "still_deferred": 0}


def test_trigger_search_false_on_falsy_command_response():
    # _make_request swallows *arr errors and returns None -> _trigger_search must report
    # failure (not silently succeed).
    m = _mgr({})
    gw = _GW("radarr", _IM(0, 0), cmd_resp=None)
    assert m._trigger_search(gw, "standard", {"arr_id": 5, "type": "movie", "title": "M"}) is False
    assert gw.commands == [("standard", {"name": "MoviesSearch", "movieIds": [5]})]   # it DID try


def test_flush_failed_search_keeps_item_and_counts_attempt():
    cache = _Cache()
    cache.set(_KEY, [{"service": "radarr", "instance": "standard", "arr_id": 5, "title": "M", "type": "movie"}])
    m = _mgr({"free_space_limit": 2500}, cache=cache)
    gw = _GW("radarr", _IM(3000, 8000), cmd_resp=None)   # free >= U, but the command fails
    stats = m._flush_deferred({"radarr": gw}, {})
    assert stats["searched"] == 0 and stats["abandoned"] == 0
    q = cache.get(_KEY)
    assert len(q) == 1 and q[0]["attempts"] == 1         # kept for retry, attempt counted


def test_flush_abandons_after_max_attempts():
    cache = _Cache()
    cache.set(_KEY, [{"service": "radarr", "instance": "standard", "arr_id": 5,
                      "title": "M", "type": "movie", "attempts": 4}])   # one below the cap
    m = _mgr({"free_space_limit": 2500}, cache=cache)
    gw = _GW("radarr", _IM(3000, 8000), cmd_resp=None)
    stats = m._flush_deferred({"radarr": gw}, {})
    assert stats["abandoned"] == 1
    assert cache.get(_KEY) == []                          # dropped after the retry budget


# ── acquisition pause when full + no deletion consent (can't reclaim) ─────────────
def _no_consent_env(monkeypatch):
    for var in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(var, raising=False)


def test_paused_when_full_and_no_deletion_consent(monkeypatch):
    _no_consent_env(monkeypatch)
    m = _mgr({"free_space_limit": 2500})                  # floor set, but no consent
    gw = _GW("radarr", _IM(2000, 8000))                  # 2000 < U(2750)
    assert m._acquisition_paused(gw, "standard", {}) is True


def test_not_paused_with_deletion_consent(monkeypatch):
    _no_consent_env(monkeypatch)
    m = _mgr({"free_space_limit": 2500, "deletions_consent": True})
    gw = _GW("radarr", _IM(2000, 8000))                  # under pressure, but deletion armed → defer
    assert m._acquisition_paused(gw, "standard", {}) is False


def test_not_paused_when_space_ok(monkeypatch):
    _no_consent_env(monkeypatch)
    m = _mgr({"free_space_limit": 2500})
    gw = _GW("radarr", _IM(3000, 8000))                  # 3000 >= U(2750)
    assert m._acquisition_paused(gw, "standard", {}) is False


def test_pause_fails_open_on_unreadable_instance(monkeypatch):
    _no_consent_env(monkeypatch)

    class _BadIM:
        def disk_free_gb(self, inst): raise RuntimeError("unreadable")
        def disk_total_gb(self, inst): return 8000

    m = _mgr({"free_space_limit": 2500})
    assert m._acquisition_paused(_GW("radarr", _BadIM()), "standard", {}) is False

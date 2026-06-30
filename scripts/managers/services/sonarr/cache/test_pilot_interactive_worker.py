"""Tests for the interactive-search pilot worker (_pilot_interactive_worker).

One manual search per stub (GET /release?episodeId=) reveals all availability: the worker sets the
series to the LOWEST tier with results and fires an EpisodeSearch (so Sonarr's quality + custom-format
scoring picks the release — the worker does NOT pick one). When NOTHING is found at any resolution the
show is recorded UNACQUIRABLE in the global_cache ledger. Driven through self.sonarr_api._make_request
(object.__new__ pattern) — no network.
"""
from __future__ import annotations

import threading

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager

LADDER = [(11, 480), (12, 720), (13, 1080), (14, 2160)]
META = {1: {"title": "Show One", "tvdb": 1001}, 2: {"title": "Show Two", "tvdb": 1002}}


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


class _GC:
    def __init__(self, d=None): self.d = dict(d or {})
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


def _rel(res, *, guid, indexer=1, seeders=10):
    return {"guid": guid, "indexerId": indexer, "rejected": False, "seeders": seeders,
            "size": 1000, "quality": {"quality": {"resolution": res}}}


class _FakeApi:
    """Per-episode interactive-search results + per-series profile state; records EpisodeSearch
    commands (episode ids) + PUTs. A release POST would be a bug (the worker must NOT grab by guid)."""
    def __init__(self, releases_by_ep):
        self._rel = releases_by_ep                  # {ep_id: [release, ...]}
        self._pid = {}
        self.searches: list = []                    # episode ids EpisodeSearch'd (CF grabs the release)
        self.grabs: list = []                       # guids POSTed to /release — must stay empty
        self._lock = threading.Lock()

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            if endpoint.startswith("release?episodeId="):
                ep = int(endpoint.split("=", 1)[1])
                return list(self._rel.get(ep, []))
            if endpoint == "release" and method == "POST":
                self.grabs.append(payload.get("guid"))
                return {"id": 1}
            if endpoint == "command" and method == "POST" and (payload or {}).get("name") == "EpisodeSearch":
                self.searches += [int(e) for e in payload.get("episodeIds") or []]
                return {"id": len(self.searches)}
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/")[1])
                return {"id": sid, "qualityProfileId": self._pid.get(sid, 99), "title": f"S{sid}"}
            if endpoint.startswith("series/") and method == "PUT":
                sid = int(endpoint.split("/")[1])
                self._pid[sid] = payload.get("qualityProfileId")
                return payload
            return fallback

    def final_pid(self, sid):
        with self._lock:
            return self._pid.get(sid)


def _mk(api, gc=None):
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.global_cache = gc
    return m


WEEK = None  # cooldown unused by the worker itself (the gate lives in run_pilot_search)


def test_searches_lowest_available_tier_and_defers_release_to_cf():
    # only 1080 + 2160 available → set series to the 1080 tier (13) and fire EpisodeSearch; the
    # worker grabs NOTHING by guid — Sonarr's CF picks the release at that tier.
    api = _FakeApi({901: [_rel(1080, guid="g1080"), _rel(2160, guid="g2160")]})
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2], 0, WEEK)
    assert api.final_pid(1) == 13                     # set to the LOWEST available tier (1080), not 2160
    assert api.searches == [901]                      # EpisodeSearch fired for S01E01
    assert api.grabs == []                            # never grabs a specific release by guid
    assert "1" not in gc.get("sonarr/pilot/unacquirable/inst", {})   # not flagged


def test_empty_results_flag_unacquirable_in_ledger():
    api = _FakeApi({901: []})                         # indexers returned nothing at any resolution
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2, 5], 0, WEEK)
    assert api.searches == [] and api.grabs == []     # nothing searched/grabbed
    ledger = gc.get("sonarr/pilot/unacquirable/inst")
    assert "1" in ledger
    assert ledger["1"]["indexers"] == [1, 2, 5]       # fingerprint captured for the re-check gate
    assert ledger["1"]["title"] == "Show One" and ledger["1"]["flagged_at"]


def test_results_clear_a_prior_unacquirable_flag():
    # series 1 was previously UNACQUIRABLE; a release now exists → searching clears the ledger entry.
    api = _FakeApi({901: [_rel(720, guid="g720")]})
    gc = _GC({"sonarr/pilot/unacquirable/inst": {"1": {"flagged_at": "old", "indexers": [1]}}})
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2], 0, WEEK)
    assert api.searches == [901] and api.final_pid(1) == 12
    assert "1" not in gc.get("sonarr/pilot/unacquirable/inst")        # flag cleared


def test_ledger_merge_preserves_other_entries():
    # an unrelated flagged series (9) must survive a run that only touches series 1.
    api = _FakeApi({901: []})
    gc = _GC({"sonarr/pilot/unacquirable/inst": {"9": {"flagged_at": "x", "indexers": [1]}}})
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1], 0, WEEK)
    ledger = gc.get("sonarr/pilot/unacquirable/inst")
    assert set(ledger) == {"1", "9"}                  # both present — merge, not overwrite


def test_concurrent_mixed_outcomes():
    # two stubs in one pass: one searches (480 floor), one is unacquirable — both land correctly.
    api = _FakeApi({901: [_rel(480, guid="g480")], 902: []})
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901), (2, 902)], LADDER, META, [1], 0, WEEK)
    assert api.searches == [901] and api.final_pid(1) == 11 and api.grabs == []
    assert set(gc.get("sonarr/pilot/unacquirable/inst")) == {"2"}     # only series 2 flagged


# ── floor_res=720 + soft-floor (the "every pilot at 720p" behaviour) ──────────────────────────
def test_floor_720_grabs_lowest_at_or_above_floor():
    # floor_res=720 with 480/720/1080 available → grab the 720 tier (id 12): prefer the floor, never
    # the cheaper 480 (the old lowest-of-everything bug) and not the higher 1080.
    api = _FakeApi({901: [_rel(480, guid="g480"), _rel(720, guid="g720"), _rel(1080, guid="g1080")]})
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2], 720, WEEK)
    assert api.final_pid(1) == 12 and api.searches == [901] and api.grabs == []
    assert "1" not in gc.get("sonarr/pilot/unacquirable/inst", {})


def test_floor_720_steps_up_when_no_720():
    # floor_res=720, only 480/1080 available (no 720) → step UP to the 1080 tier (id 13), not down to 480.
    api = _FakeApi({901: [_rel(480, guid="g480"), _rel(1080, guid="g1080")]})
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2], 720, WEEK)
    assert api.final_pid(1) == 13 and api.searches == [901]


def test_soft_floor_grabs_sub_floor_when_nothing_at_or_above():
    # floor_res=720 but ONLY a 480 release exists → soft-floor (default ON) grabs at the floor tier
    # (id 11) so the SD-only show is still seeded, instead of flagging UNACQUIRABLE.
    api = _FakeApi({901: [_rel(480, guid="g480")]})
    gc = _GC()
    _mk(api, gc)._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1, 2], 720, WEEK)
    assert api.final_pid(1) == 11 and api.searches == [901]
    assert "1" not in gc.get("sonarr/pilot/unacquirable/inst", {})


def test_hard_floor_flags_unacquirable_when_soft_floor_off():
    # soft_floor=False restores the hard floor: a 480-only show under a 720 floor is flagged UNACQUIRABLE.
    api = _FakeApi({901: [_rel(480, guid="g480")]})
    gc = _GC()
    m = _mk(api, gc)
    m.config = {"pilot_interactive": {"soft_floor": False}}
    m._pilot_interactive_worker("inst", [(1, 901)], LADDER, META, [1], 720, WEEK)
    assert api.searches == [] and "1" in gc.get("sonarr/pilot/unacquirable/inst")


# ── Cooperative yield (a long sweep releases the daemon for a higher-priority JIT grab) ──
from scripts.managers.services.sonarr.cache import pilot_interactive as _pi


def test_interactive_search_yields_to_higher_priority(monkeypatch):
    # When should_yield()==True the sweep stops early with yielded=True so the daemon re-enqueues it
    # (id preserved → checkpoint resume) and claims the waiting JIT grab next. How many stubs were
    # already in flight when the check fires is timing-dependent under a no-latency fake; a real
    # multi-hour sweep genuinely stops with work left. 'yielded' is the contract the daemon keys off.
    monkeypatch.setattr(_pi, "_YIELD_CHECK_EVERY", 2)
    items = [(sid, 900 + sid) for sid in range(1, 11)]      # 10 stubs
    api = _FakeApi({900 + sid: [_rel(720, guid=f"g{sid}")] for sid in range(1, 11)})
    meta = {sid: {"title": f"S{sid}", "tvdb": 1000 + sid} for sid in range(1, 11)}

    result = _pi.interactive_pilot_search(
        make_request=api._make_request, logger=_StubLogger(), global_cache=_GC(),
        instance="sonarr", items=items, ladder=LADDER, meta=meta,
        current_indexers=[1], floor_res=0, max_workers=1,
        should_yield=lambda: True,
    )
    assert result["yielded"] is True


def test_interactive_search_no_yield_when_callback_false(monkeypatch):
    monkeypatch.setattr(_pi, "_YIELD_CHECK_EVERY", 2)
    items = [(sid, 900 + sid) for sid in range(1, 7)]
    api = _FakeApi({900 + sid: [_rel(720, guid=f"g{sid}")] for sid in range(1, 7)})
    meta = {sid: {"title": f"S{sid}", "tvdb": 1000 + sid} for sid in range(1, 7)}
    result = _pi.interactive_pilot_search(
        make_request=api._make_request, logger=_StubLogger(), global_cache=_GC(),
        instance="sonarr", items=items, ladder=LADDER, meta=meta,
        current_indexers=[1], floor_res=0, max_workers=2,
        should_yield=lambda: False,
    )
    assert result["yielded"] is False
    assert sorted(api.searches) == [900 + sid for sid in range(1, 7)]   # all processed, none skipped

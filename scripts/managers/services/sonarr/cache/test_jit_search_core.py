"""Tests for the shared JIT step-down core's crash-safety: the inflight-QP store is written before a
flip and cleared after the revert, and revert_inflight_qp restores a series a crash left bumped.

The step-down/over-grab/revert search logic itself is covered by test_jit_search_worker.py (the
in-process worker delegates to this core); here we focus on the new inflight/resume machinery.
"""
from __future__ import annotations

import threading

import scripts.managers.services.sonarr.cache.jit_search as jit


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _StubCache:
    def __init__(self, d=None):
        self.store = dict(d or {})
        self._lock = threading.Lock()
    def get(self, k, default=None):
        with self._lock:
            return self.store.get(k, default)
    def set(self, k, v):
        with self._lock:
            self.store[k] = v


class _Api:
    """Per-series profile state; reflects PUTs; commands instantly 'completed'."""
    def __init__(self, original=5):
        self._orig = original
        self._pid = {}
        self._cmd = 0
        self._lock = threading.Lock()

    def make(self, instance, endpoint, method="GET", payload=None, fallback=None):
        with self._lock:
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/")[1])
                return {"id": sid, "qualityProfileId": self._pid.get(sid, self._orig), "title": f"S{sid}"}
            if endpoint.startswith("series/") and method == "PUT":
                sid = int(endpoint.split("/")[1])
                self._pid[sid] = payload.get("qualityProfileId")
                return payload
            if endpoint == "command" and method == "POST":
                self._cmd += 1
                return {"id": self._cmd}
            if endpoint.startswith("command/"):
                return {"status": "completed"}
            return fallback

    def pid(self, sid):
        with self._lock:
            return self._pid.get(sid, self._orig)


def test_inflight_written_then_cleared_after_revert():
    api = _Api(original=5)
    cache = _StubCache()
    out = jit.jit_step_down_search(
        make_request=api.make, in_queue=lambda inst, ids: set(),   # nothing grabs → full step-down
        logger=_StubLogger(), global_cache=cache, instance="inst",
        items=[(1, [(1080, [(200, 1, 2)], [12, 11])])], max_workers=1,
    )
    assert api.pid(1) == 5                                       # reverted to the pre-flip profile
    assert cache.get(jit.inflight_qp_key("inst")) == {}         # inflight entry cleared after revert
    assert len(out["failed"]) == 1                              # ep 200 never grabbed → retry ledger
    assert cache.get(jit.failed_upgrades_key("inst"))


def test_revert_inflight_qp_restores_stranded_series():
    # Simulate a crash: the inflight store says series 1's true original is 5, but it's stuck at 12.
    api = _Api(original=12)
    cache = _StubCache({jit.inflight_qp_key("inst"): {"1": 5}})
    n = jit.revert_inflight_qp(make_request=api.make, logger=_StubLogger(),
                               global_cache=cache, instance="inst")
    assert n == 1 and api.pid(1) == 5                           # restored to the true original
    assert cache.get(jit.inflight_qp_key("inst")) == {}         # store drained


def test_revert_inflight_qp_noop_when_already_correct():
    api = _Api(original=5)                                       # already at 5
    cache = _StubCache({jit.inflight_qp_key("inst"): {"1": 5}})
    assert jit.revert_inflight_qp(make_request=api.make, logger=_StubLogger(),
                                  global_cache=cache, instance="inst") == 0

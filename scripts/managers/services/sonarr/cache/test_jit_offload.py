"""Tests for spilling LARGE JIT step-down batches to the daemon (_maybe_offload_jit_search).

Mirrors the pilot offload: same daemons.pilot_search gate, threshold counts SERIES, enqueue rollback
on spawn failure. enqueue + the supervisor are monkeypatched so nothing touches disk / spawns.
"""
from __future__ import annotations

from pathlib import Path

from scripts.managers.factories.daemons import pilot_jobs
from scripts.managers.factories.daemons import supervisor as sup
from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


def _mgr(config, dry_run=False):
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = config
    m.dry_run = dry_run
    return m


def _patch(monkeypatch):
    calls = {"enqueued": [], "ensured": 0}
    monkeypatch.setattr(pilot_jobs, "enqueue",
                        lambda inst, job: (calls["enqueued"].append((inst, job))
                                           or Path(f"{inst}__jit.json")))
    monkeypatch.setattr(sup.PilotSearchDaemonSupervisor, "ensure_running",
                        lambda self: calls.__setitem__("ensured", calls["ensured"] + 1) or 1)
    return calls


def _work(n=12):
    # {sid: {tier_res: {"eps": [(ep_id, season, episode)], "step_pids": [pid, ...]}}}
    return {sid: {1080: {"eps": [(900 + sid, 1, 1)], "step_pids": [12, 11]}} for sid in range(1, n + 1)}


_ENABLED = {"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}}


def test_jit_offloads_large_batch(monkeypatch):
    calls = _patch(monkeypatch)
    assert _mgr(_ENABLED)._maybe_offload_jit_search("standard", _work(12)) is True
    assert calls["ensured"] == 1 and len(calls["enqueued"]) == 1
    inst, job = calls["enqueued"][0]
    assert inst == "standard" and job["mode"] == "jit" and len(job["items"]) == 12
    sid0, groups0 = job["items"][0]                       # [[sid, [[tier_res, [[ep,sn,en]], [pids]]]]]
    assert groups0[0][0] == 1080 and groups0[0][1][0] == [901, 1, 1] and groups0[0][2] == [12, 11]


def test_jit_small_batch_stays_in_process(monkeypatch):
    calls = _patch(monkeypatch)
    assert _mgr(_ENABLED)._maybe_offload_jit_search("standard", _work(10)) is False
    assert calls["enqueued"] == [] and calls["ensured"] == 0


def test_jit_disabled_never_offloads(monkeypatch):
    calls = _patch(monkeypatch)
    m = _mgr({"daemons": {"pilot_search": {"enabled": False}}})
    assert m._maybe_offload_jit_search("standard", _work(12)) is False
    assert calls["enqueued"] == []


def test_jit_dry_run_never_offloads(monkeypatch):
    calls = _patch(monkeypatch)
    assert _mgr(_ENABLED, dry_run=True)._maybe_offload_jit_search("standard", _work(12)) is False
    assert calls["enqueued"] == []


def test_jit_rollback_on_ensure_running_failure(monkeypatch):
    removed = []
    monkeypatch.setattr(pilot_jobs, "enqueue", lambda inst, job: Path(f"{inst}__jit.json"))
    monkeypatch.setattr(pilot_jobs, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(sup.PilotSearchDaemonSupervisor, "ensure_running",
                        lambda self: (_ for _ in ()).throw(RuntimeError("spawn failed")))
    assert _mgr(_ENABLED)._maybe_offload_jit_search("standard", _work(12)) is False
    assert removed == [Path("standard__jit.json")]        # rolled back, not left orphaned

"""Tests for spilling LARGE pilot interactive-search batches to the background daemon.

_maybe_offload_pilot_search decides (config gate + threshold + dry-run); run_pilot_search wires it
into the climb dispatch so a > threshold batch is enqueued + the daemon ensured RUNNING instead of
the in-process worker thread (which blocks interpreter exit). enqueue + the supervisor are
monkeypatched so nothing touches disk or spawns a process.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd

from scripts.managers.factories.daemons import pilot_jobs
from scripts.managers.factories.daemons import supervisor as sup
from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


def _mgr(config, dry_run=False):
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = config
    m.dry_run = dry_run
    return m


def _patch_offload(monkeypatch):
    """Capture enqueue + ensure_running without touching disk / spawning a process."""
    calls = {"enqueued": [], "ensured": 0}
    monkeypatch.setattr(pilot_jobs, "enqueue",
                        lambda instance, job: (calls["enqueued"].append((instance, job))
                                               or Path(f"{instance}.json")))
    monkeypatch.setattr(sup.PilotSearchDaemonSupervisor, "ensure_running",
                        lambda self: calls.__setitem__("ensured", calls["ensured"] + 1) or 4321)
    return calls


_ITEMS = [(i, 900 + i) for i in range(1, 13)]          # 12 stubs > default threshold 10
_LADDER = [(11, 480), (12, 1080)]
_META = {i: {"title": f"S{i}", "tvdb": 1000 + i} for i, _ in _ITEMS}
WEEK = timedelta(days=7)


def test_offloads_large_batch_when_enabled(monkeypatch):
    calls = _patch_offload(monkeypatch)
    m = _mgr({"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}})
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [1, 2], 0, WEEK) is True
    assert calls["ensured"] == 1
    assert len(calls["enqueued"]) == 1
    inst, job = calls["enqueued"][0]
    assert inst == "sonarr" and job["mode"] == "interactive"
    assert len(job["items"]) == 12 and job["items"][0] == [1, 901]
    assert job["meta"]["1"]["title"] == "S1"           # meta JSON-keyed by str(sid)


def test_small_batch_stays_in_process(monkeypatch):
    calls = _patch_offload(monkeypatch)
    m = _mgr({"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}})
    small = _ITEMS[:10]                                 # exactly the threshold → in-process
    assert m._maybe_offload_pilot_search("sonarr", small, _LADDER, _META, [], 0, WEEK) is False
    assert calls["enqueued"] == [] and calls["ensured"] == 0


def test_disabled_never_offloads(monkeypatch):
    calls = _patch_offload(monkeypatch)
    m = _mgr({"daemons": {"pilot_search": {"enabled": False}}})
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [], 0, WEEK) is False
    assert calls["enqueued"] == []


def test_dry_run_never_offloads(monkeypatch):
    calls = _patch_offload(monkeypatch)
    m = _mgr({"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}}, dry_run=True)
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [], 0, WEEK) is False
    assert calls["enqueued"] == []


def test_enqueue_failure_falls_back_to_in_process(monkeypatch):
    # An enqueue error must NOT drop the searches — return False so the caller runs the in-process worker.
    def _boom(instance, job):
        raise OSError("disk full")
    monkeypatch.setattr(pilot_jobs, "enqueue", _boom)
    m = _mgr({"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}})
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [], 0, WEEK) is False


def test_offload_rolls_back_enqueue_when_ensure_running_fails(monkeypatch):
    # If the job enqueues but the daemon can't be spawned, the job MUST be removed so we don't both
    # leave an orphan AND run in-process (double-search). Falls back to the in-process worker.
    removed = []
    monkeypatch.setattr(pilot_jobs, "enqueue", lambda inst, job: Path(f"{inst}.json"))
    monkeypatch.setattr(pilot_jobs, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(sup.PilotSearchDaemonSupervisor, "ensure_running",
                        lambda self: (_ for _ in ()).throw(RuntimeError("spawn failed")))
    m = _mgr({"daemons": {"pilot_search": {"enabled": True, "threshold": 10}}})
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [], 0, WEEK) is False
    assert removed == [Path("sonarr.json")]      # the just-enqueued job was rolled back


def test_default_config_enables_offload(monkeypatch):
    # No daemons key at all → pilot_search defaults ON (enabled, threshold 10).
    calls = _patch_offload(monkeypatch)
    m = _mgr({"free_space_limit": 100})
    assert m._maybe_offload_pilot_search("sonarr", _ITEMS, _LADDER, _META, [], 0, WEEK) is True
    assert calls["ensured"] == 1


# ── run_pilot_search wiring (climb default, interactive ON, live) ───────────────────

def _prof(pid, res):
    return {"id": pid, "name": f"P{res}",
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_PROFILES = [_prof(13, 2160), _prof(12, 1080), _prof(11, 720)]


class _Api:
    def __init__(self): self.puts = []
    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "qualityprofile":
            return list(_PROFILES)
        if endpoint == "series" and method == "GET":
            return [{"id": i, "qualityProfileId": 99, "runtime": 60, "title": f"S{i}"} for i in range(1, 13)]
        if endpoint.startswith("series/") and method == "PUT":
            self.puts.append(payload.get("qualityProfileId"))
            return payload
        return fallback


def _run_pilot(config, monkeypatch):
    rows = [{"series_id": i, "series_title": f"S{i}", "is_pilot": True, "episode_file_id": None,
             "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None}
            for i in range(1, 13)]                       # 12 stubs > threshold
    df = pd.DataFrame(rows)
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = _Api()
    m.sonarr_cache = None
    m.global_cache = None
    m.config = config
    m.dry_run = False
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: {}
    m._get_episode_id = lambda *a, **k: 999
    m._prewarm_by_series_episode_cache = lambda *a, **k: 0
    spawned = {"interactive": 0}
    m._spawn_pilot_interactive_worker = lambda *a, **k: spawned.__setitem__("interactive", spawned["interactive"] + 1)
    stats = m.run_pilot_search("inst")
    return stats, spawned


def test_run_pilot_search_offloads_big_batch(monkeypatch):
    calls = _patch_offload(monkeypatch)
    stats, spawned = _run_pilot(
        {"free_space_limit": 100, "daemons": {"pilot_search": {"enabled": True, "threshold": 10}}},
        monkeypatch,
    )
    assert len(calls["enqueued"]) == 1 and calls["ensured"] == 1
    assert spawned["interactive"] == 0                   # in-process worker NOT used
    assert stats["offloaded"] == 12 and stats["searched"] == 12


def test_run_pilot_search_keeps_in_process_when_daemon_disabled(monkeypatch):
    calls = _patch_offload(monkeypatch)
    stats, spawned = _run_pilot(
        {"free_space_limit": 100, "daemons": {"pilot_search": {"enabled": False}}},
        monkeypatch,
    )
    assert calls["enqueued"] == [] and calls["ensured"] == 0
    assert spawned["interactive"] == 1                   # fell back to the in-process worker
    assert stats["offloaded"] == 0 and stats["searched"] == 12

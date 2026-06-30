"""disk_free_bytes fail-open regression — a transient rootfolder connect blip must NOT silently
fall through to inf and disable every space gate for the rest of the run.

Reproduces the 2026-06-30 live-run defect: one `GET /rootfolder` connection blip made the active-watcher
upgrade pass run with "(inf GB free)". The fix retries the read (absorbing a transient blip) and only
falls back to inf on a PERSISTENT failure (logged loudly), while still honouring the legacy
"genuinely no root folders -> inf" contract.
"""
from __future__ import annotations

import scripts.managers.factories.base_instance_manager as bim
from scripts.managers.factories.base_instance_manager import BaseInstanceManager


class _Logger:
    def __init__(self): self.warnings = []
    def log_warning(self, m, *a, **k): self.warnings.append(m)
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mgr(rootfolder_responses, diskspace=None):
    """rootfolder_responses: list consumed one per _make_request('rootfolder') call
    (None simulates a failed fetch returning the fallback)."""
    m = object.__new__(BaseInstanceManager)
    m.logger = _Logger()
    seq = list(rootfolder_responses)
    def fake(instance, endpoint, **kw):
        if endpoint == "rootfolder":
            return seq.pop(0) if seq else None
        if endpoint == "diskspace":
            return diskspace if diskspace is not None else []
        return kw.get("fallback")
    m._make_request = fake
    m._service_name = lambda: "Sonarr"
    return m


def test_transient_blip_recovers_real_value(monkeypatch):
    # first read fails (None), retry succeeds -> the REAL free space, never inf.
    monkeypatch.setattr(bim.time, "sleep", lambda *_: None)
    roots = [{"path": "/data/media/tv", "freeSpace": 500}]
    m = _mgr([None, roots],
             diskspace=[{"path": "/data", "freeSpace": 500}])
    free = m.disk_free_bytes("standard")
    assert free == 500.0
    assert m.logger.warnings == []          # transient blip absorbed silently


def test_persistent_failure_fails_safe_to_inf_and_warns(monkeypatch):
    monkeypatch.setattr(bim.time, "sleep", lambda *_: None)
    m = _mgr([None, None, None])            # all 3 attempts fail
    free = m.disk_free_bytes("standard")
    assert free == float("inf")             # safe fail direction (never delete off a bad read)
    assert any("FAILED" in w for w in m.logger.warnings)   # loud, not silent


def test_genuinely_no_rootfolders_is_inf_without_warning(monkeypatch):
    monkeypatch.setattr(bim.time, "sleep", lambda *_: None)
    m = _mgr([[]])                          # successful fetch, empty list = none configured
    free = m.disk_free_bytes("standard")
    assert free == float("inf")             # legacy "assume sufficient" contract preserved
    assert m.logger.warnings == []          # not a failure -> no warning


def test_normal_read_sums_mount_deduped_free(monkeypatch):
    monkeypatch.setattr(bim.time, "sleep", lambda *_: None)
    roots = [{"path": "/data/media/tv", "freeSpace": 700},
             {"path": "/data/media/anime", "freeSpace": 700}]   # same /data mount
    m = _mgr([roots], diskspace=[{"path": "/data", "freeSpace": 700}])
    assert m.disk_free_bytes("standard") == 700.0               # deduped to one disk

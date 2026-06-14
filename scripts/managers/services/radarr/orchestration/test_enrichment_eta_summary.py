"""Regression test for item D.1: the per-instance Trakt enrichment ETA block is routed into
the consolidated end-of-run summary (one row per service/instance) instead of a per-instance
inline table — and still falls back to the inline table when no collector is present.
"""
from __future__ import annotations

from scripts.managers.services.radarr.orchestration import RadarrOrchestrationManager
from scripts.support.utilities.logger.run_summary import RunSummaryManager


class _Logger:
    def __init__(self): self.tables = 0
    def log_table(self, *a, **k): self.tables += 1
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _GC:
    def __init__(self, rs): self.run_summary = rs


class _Cap:
    def __init__(self): self.grids = []
    def log_grid(self, headers, rows, title="", cap=None):
        self.grids.append((title, list(headers), [list(r) for r in rows]))
    def log_info(self, *a, **k): pass


def _mk(global_cache, logger):
    m = object.__new__(RadarrOrchestrationManager)   # skip __init__/registry/base
    m.config = {}
    m.logger = logger
    m.global_cache = global_cache
    return m


def test_routes_both_instances_into_one_summary_table():
    rs = RunSummaryManager()
    log = _Logger()
    m = _mk(_GC(rs), log)
    m._log_enrichment_eta("standard", [{"hasFile": True}, {"hasFile": True}, {"hasFile": False}])
    m._log_enrichment_eta("ultra", [{"hasFile": True}])

    assert log.tables == 0                       # inline table NOT used when collector present

    cap = _Cap()
    rs.render(cap)
    assert len(cap.grids) == 1                    # one consolidated section (radarr block)
    title, headers, rows = cap.grids[0]
    assert "Trakt enrichment" in title
    assert headers[0] == "Instance"               # service is the block header, not a column
    assert [r[0] for r in rows] == ["standard", "ultra"]
    assert rows[0][1] == "2"                       # standard: 2 owned (hasFile)
    assert rows[1][1] == "1"                       # ultra: 1 owned


def test_falls_back_to_inline_table_without_collector():
    log = _Logger()
    m = _mk(object(), log)                        # global_cache has no run_summary
    m._log_enrichment_eta("standard", [{"hasFile": True}])
    assert log.tables == 1

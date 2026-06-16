"""Regression test for item C: repair_import_untracked must NOT dump a per-folder grid.

A 1.8k-folder library previously logged one grid row per untracked folder (a multi-
thousand-line wall). The grid is replaced by a tqdm progress bar (stderr); only the
checked|triggered|failed summary reaches the log. This guards against the grid being
reintroduced and pins the (unchanged) stat counting.
"""
from __future__ import annotations

from scripts.managers.services.radarr.repair.orphans import RadarrRepairOrphansManager


class _Logger:
    def __init__(self):
        self.grid_calls = 0
        self.summaries: list[str] = []
        self.tables: list = []                         # captured log_table row data
    def log_grid(self, *a, **k):   self.grid_calls += 1
    def log_info(self, msg, *a, **k): self.summaries.append(msg)
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k):   pass
    def log_table(self, headers, data, *a, **k): self.tables.append(data)


def _mk(dry_run, folders, api):
    m = object.__new__(RadarrRepairOrphansManager)   # skip __init__/registry/base
    m.dry_run = dry_run
    m.radarr_api = api
    m.logger = _Logger()
    m._resolve_instance = lambda i: i                # type: ignore[attr-defined]
    m.find_untracked_files = lambda inst: folders    # type: ignore[attr-defined]
    return m


def test_dry_run_no_grid_stats_preserved():
    folders = [{"folder_path": f"/data/media/movies/standard/Movie {i} (2020) {{tmdb-{i}}}"}
               for i in range(50)]
    folders.append({"folder_path": None})            # no path -> counted, then skipped
    m = _mk(True, folders, api=object())
    stats = m.repair_import_untracked("standard")
    assert m.logger.grid_calls == 0                  # the per-folder grid is gone
    assert stats == {"checked": 51, "triggered": 50, "failed": 0}
    # the stats summary moved from a piped log line to a log_table; assert the counts are in it
    _counts = {row[0]: row[1] for tbl in m.logger.tables for row in tbl}
    assert _counts == {"checked": 51, "triggered": 50, "failed": 0}


def test_no_untracked_returns_early():
    m = _mk(True, [], api=object())
    assert m.repair_import_untracked("standard") == {"checked": 0, "triggered": 0, "failed": 0}
    assert m.logger.grid_calls == 0


class _FailAPI:
    def _make_request(self, *a, **k):
        raise RuntimeError("boom")


def test_live_failure_counted_not_gridded():
    folders = [{"folder_path": "/data/media/movies/standard/X (2020)"}]
    m = _mk(False, folders, api=_FailAPI())
    stats = m.repair_import_untracked("standard")
    assert stats == {"checked": 1, "triggered": 0, "failed": 1}
    assert m.logger.grid_calls == 0

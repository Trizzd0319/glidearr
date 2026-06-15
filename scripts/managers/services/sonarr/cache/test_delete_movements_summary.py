"""D.7: per-file episode deletes are recorded into the end-of-run "Deletions & movements"
table (in addition to the live per-file log line, which is intentionally kept for real-time
visibility of destructive actions)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager
from scripts.support.utilities.logger.run_summary import RunSummaryManager


class _L:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _GC:
    def __init__(self, rs): self.run_summary = rs
    def get(self, *a, **k): return None
    def set(self, *a, **k): pass


class _Cap:
    def __init__(self): self.grids = []
    def log_grid(self, headers, rows, title="", cap=None):
        self.grids.append((title, list(headers), [list(r) for r in rows]))
    def log_info(self, *a, **k): pass


def test_sonarr_deletes_route_into_movements_table():
    df = pd.DataFrame([
        dict(episode_file_id=1001, series_id=10, season_number=1, episode_number=1,
             size_bytes=1_000_000_000, series_title="My Show"),
    ])
    rs = RunSummaryManager()
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _L()
    m.config = {"free_space_limit": 100, "deletions_consent": True}   # deletions enabled (consent + floor)
    m.dry_run = True                          # dry-run -> no API, "would delete"
    m.global_cache = _GC(rs)
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    # NOTE: this manager class uses a shared-singleton __new__, so an instance attribute
    # leaks onto the object other tests reuse. Stub the guard, then restore it in finally so
    # the real _build_protected_file_ids is exposed again for sibling tests.
    m._build_protected_file_ids = lambda d, now: set()    # nothing guarded
    try:
        stats = m.delete_selected_episode_files("sonarr", [1001])
    finally:
        del m._build_protected_file_ids
    assert stats["deleted"] == 1

    cap = _Cap()
    rs.render(cap)
    grids = {t: (h, r) for t, h, r in cap.grids}
    assert "Deletions & movements" in grids, list(grids)
    headers, rows = grids["Deletions & movements"]
    assert headers == ["Instance", "Title", "FileId", "Size", "Action"]
    assert rows[0][0] == "sonarr"
    assert rows[0][1] == "My Show"
    assert rows[0][2] == "1001"
    assert rows[0][4] == "would delete"

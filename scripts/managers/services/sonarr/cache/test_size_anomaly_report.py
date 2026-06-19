"""SonarrCacheEpisodeFilesManager.report_size_anomalies — the TV twin of the Radarr size
check; flags wildly-out-of-profile episode files and labels them 'Series SxxExx'."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _RS:
    def __init__(self): self.calls = []
    def add_rows(self, service, concern, instance, headers, rows, order=None):
        self.calls.append((service, concern, instance, headers, rows, order))


class _GC:
    def __init__(self, rs): self.run_summary = rs


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _mgr(config):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = config
    m._rs = _RS()
    m.global_cache = _GC(m._rs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def _edf():
    rows = [{"series_title": f"Show {i}", "season_number": 1, "episode_number": i + 1,
             "quality_name": "Bluray-1080p", "runtime_seconds": 2700, "resolution": 1080,
             "size_bytes": int(3 * 1024 ** 3)} for i in range(8)]
    rows.append({"series_title": "Bloated", "season_number": 1, "episode_number": 2,
                 "quality_name": "Bluray-1080p", "runtime_seconds": 2700, "resolution": 1080,
                 "size_bytes": int(30 * 1024 ** 3)})
    return pd.DataFrame(rows)


def test_report_flags_oversized_episode_with_label():
    m = _mgr({})
    out = m.report_size_anomalies("standard", _edf())
    assert any(r.get("series_title") == "Bloated" and r["verdict"] == "oversized" for r in out)
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern) == ("sonarr", "Size anomalies")
    assert headers[0] == "Episode"
    assert any("Bloated S01E02" in row[0] for row in table)     # formatted 'Series SxxExx'


def test_report_disabled_by_flag():
    m = _mgr({"size_anomaly": {"enabled": False}})
    assert m.report_size_anomalies("standard", _edf()) == []
    assert m._rs.calls == []

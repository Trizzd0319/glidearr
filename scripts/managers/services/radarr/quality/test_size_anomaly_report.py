"""RadarrSpacePressureManager.report_size_anomalies — flags wildly-out-of-profile movie
files and records them in the end-of-run summary (read-only diagnostic)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


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
    m = object.__new__(RadarrSpacePressureManager)
    m.config = config
    m._rs = _RS()
    m.global_cache = _GC(m._rs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def _df():
    rows = [{"title": f"ok {i}", "year": 2009, "quality_name": "Bluray-720p",
             "runtime_minutes": 150, "resolution": 720, "size_bytes": int(7.5 * 1024 ** 3)}
            for i in range(8)]
    rows.append({"title": "Transformers ROTF", "year": 2009, "quality_name": "Bluray-720p",
                 "runtime_minutes": 150, "resolution": 720, "size_bytes": int(45 * 1024 ** 3)})
    return pd.DataFrame(rows)


def test_report_flags_oversized_and_pushes_summary():
    m = _mgr({})
    out = m.report_size_anomalies("standard", _df())
    assert any(r["title"] == "Transformers ROTF" and r["verdict"] == "oversized" for r in out)
    assert m._rs.calls, "should record a run-summary table"
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern, inst) == ("radarr", "Size anomalies", "standard")
    assert headers[0] == "Title" and "Verdict" in headers
    assert any("Transformers" in row[0] for row in table)


def test_report_disabled_by_flag():
    m = _mgr({"size_anomaly": {"enabled": False}})
    assert m.report_size_anomalies("standard", _df()) == []
    assert m._rs.calls == []


def test_report_empty_df_is_noop():
    m = _mgr({})
    assert m.report_size_anomalies("standard", pd.DataFrame()) == []
    assert m._rs.calls == []

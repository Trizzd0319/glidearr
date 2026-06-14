"""Unit tests for RunSummaryManager (D.0 — the end-of-run consolidated report collector),
grouped by service then concern."""
from __future__ import annotations

from scripts.support.utilities.logger.run_summary import RunSummaryManager


class _Grid:
    def __init__(self):
        self.calls = []   # (title, headers, rows)
        self.infos = []   # log_info messages (service headers / banner)
    def log_grid(self, headers, rows, title="", cap=None):
        self.calls.append((title, list(headers), [list(r) for r in rows]))
    def log_info(self, msg="", *a, **k):
        self.infos.append(msg)


def test_empty_render_is_noop():
    g = _Grid()
    rs = RunSummaryManager(logger=g)
    assert rs.has_data() is False
    rs.render(g)
    assert g.calls == [] and g.infos == []


def test_accumulates_with_instance_column_under_service():
    g = _Grid()
    rs = RunSummaryManager(logger=g)
    rs.add_rows("radarr", "Universe quality", "standard",
                ["Title", "Action", "From->To"],
                [["2 Fast 2 Furious", "upgrade", "WEBDL-2160p->Remux-2160p"]])
    rs.add_rows("radarr", "Universe quality", "ultra",
                ["Title", "Action", "From->To"],
                [["Fast Five", "hold", "WEBDL-2160p->WEBDL-2160p"]])
    rs.render(g)
    assert len(g.calls) == 1
    title, headers, rows = g.calls[0]
    assert title == "Universe quality"
    assert headers == ["Instance", "Title", "Action", "From->To"]      # no Service column
    assert rows == [
        ["standard", "2 Fast 2 Furious", "upgrade", "WEBDL-2160p->Remux-2160p"],
        ["ultra", "Fast Five", "hold", "WEBDL-2160p->WEBDL-2160p"],
    ]
    assert g.infos[0] == "===== END-OF-RUN SUMMARY ====="
    assert "--- RADARR ---" in g.infos


def test_empty_rows_noop_and_none_cells_blanked():
    g = _Grid()
    rs = RunSummaryManager(logger=g)
    rs.add_rows("radarr", "S", "standard", ["A", "B"], [])      # no-op
    assert rs.has_data() is False
    rs.add_rows("radarr", "S", "standard", ["A", "B"], [[None, "x"]])
    rs.render(g)
    _, _, rows = g.calls[0]
    assert rows == [["standard", "", "x"]]


def test_row_width_normalised():
    g = _Grid()
    rs = RunSummaryManager(logger=g)
    rs.add_rows("radarr", "S", "i", ["A", "B", "C"], [["1"], ["1", "2", "3", "EXTRA"]])
    rs.render(g)
    _, _, rows = g.calls[0]
    assert rows == [["i", "1", "", ""], ["i", "1", "2", "3"]]


def test_groups_by_service_then_concern_order():
    g = _Grid()
    rs = RunSummaryManager(logger=g)
    # radarr added first, but SONARR renders first (service order); within a service,
    # `order` pins the concern position.
    rs.add_rows("radarr", "Universe", "ultra", ["A"], [["x"]], order=20)
    rs.add_rows("radarr", "Acquisition", "standard", ["A"], [["y"]], order=10)
    rs.add_rows("sonarr", "Acquisition", "sonarr", ["A"], [["z"]], order=10)
    rs.render(g)
    assert [m for m in g.infos if m.startswith("---")] == ["--- SONARR ---", "--- RADARR ---"]
    # sonarr/Acquisition, then radarr/Acquisition(10), radarr/Universe(20)
    assert [c[0] for c in g.calls] == ["Acquisition", "Acquisition", "Universe"]

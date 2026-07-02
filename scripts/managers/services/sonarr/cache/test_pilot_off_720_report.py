"""SonarrCacheEpisodeFilesManager.report_pilots_off_720 -- read-only audit of TV pilots whose on-disk
file is still below 720p. Splits genuine upgrade candidates from HELD ones (a full library that merely
owns a 480p pilot / watched / scored / keep-tagged), records a 'Pilots below 720' table in the run
summary, and changes nothing. Mirrors the report_codec_routing preview's stubbing."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _RS:
    def __init__(self): self.calls = []
    def add_rows(self, service, concern, instance, headers, rows, order=None):
        self.calls.append((service, concern, instance, headers, rows, order))


class _GC:
    def __init__(self, rs): self.run_summary = rs
    def get(self, k): return None


class _Log:
    def __init__(self): self.info = []; self.grids = []
    def log_info(self, msg="", *a, **k): self.info.append(str(msg))
    def log_grid(self, headers, rows, title="", cap=None): self.grids.append((title, headers, rows))
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


def _mgr(config=None):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = config or {}
    m._rs = _RS()
    m.global_cache = _GC(m._rs)
    m.logger = _Log()
    m._resolve_instance = lambda i: i or "standard"
    return m


def _row(**kw):
    base = {"is_pilot": False, "episode_file_id": None, "resolution": None, "series_id": 0,
            "series_title": "?", "is_watched": False, "watchability_score": 0, "keep_policy": None}
    base.update(kw)
    return base


def _has(log, *needles):
    return any(all(n in s for n in needles) for s in log)


def _df():
    return pd.DataFrame([
        # sid 1 — genuine 480p stub, not watched/scored/keep -> UPGRADABLE
        _row(is_pilot=True, episode_file_id=100, resolution=480, series_id=1, series_title="Stub480"),
        # sid 2 — full library (pilot 480 + 3 owned 1080 eps) -> FULL-SERIES (never cap)
        _row(is_pilot=True, episode_file_id=200, resolution=480, series_id=2, series_title="FullLib"),
        _row(episode_file_id=201, resolution=1080, series_id=2, series_title="FullLib"),
        _row(episode_file_id=202, resolution=1080, series_id=2, series_title="FullLib"),
        _row(episode_file_id=203, resolution=1080, series_id=2, series_title="FullLib"),
        # sid 3 — sampled (pilot watched) -> WATCHED
        _row(is_pilot=True, episode_file_id=300, resolution=480, series_id=3, series_title="Sampled",
             is_watched=True),
        # sid 4 — already scored high -> SCORED
        _row(is_pilot=True, episode_file_id=400, resolution=576, series_id=4, series_title="Scored",
             watchability_score=80),
        # sid 5 — keep-tagged -> KEEP
        _row(is_pilot=True, episode_file_id=500, resolution=480, series_id=5, series_title="Kept",
             keep_policy="keep_series"),
        # sid 6 — pilot already at 720 -> NOT in the report
        _row(is_pilot=True, episode_file_id=600, resolution=720, series_id=6, series_title="At720"),
        # sid 7 — stub with NO file (never grabbed) -> NOT in the report (audit is on-disk only)
        _row(is_pilot=True, episode_file_id=None, resolution=None, series_id=7, series_title="NoFile"),
    ])


def test_splits_upgradable_from_held_and_records_summary():
    m = _mgr()
    rows = m.report_pilots_off_720("standard", _df())
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    assert by_status == {"upgradable": 1, "full-series": 1, "watched": 1, "scored": 1, "keep": 1}
    # run-summary table recorded under the right concern + order, upgradable sorted first
    svc, concern, inst, headers, table, order = m._rs.calls[0]
    assert (svc, concern, inst, order) == ("sonarr", "Pilots below 720", "standard", 38)
    assert headers == ["Series", "Res", "Files", "Score", "Status"]
    assert table[0][0].startswith("Stub480") and table[0][-1] == "upgradable"
    assert _has(m.logger.info, "[Pilot720]", "1 upgradable to 720", "4 held")
    assert m.logger.grids and "Pilots below 720" in m.logger.grids[0][0]


def test_full_series_with_480_pilot_is_not_upgradable():
    # the load-bearing guard: a 44-file library that merely owns a 480p pilot must NOT be an upgrade target
    m = _mgr()
    rows = m.report_pilots_off_720("standard", _df())
    fulllib = [r for r in rows if r["series_title"] == "FullLib"][0]
    assert fulllib["status"] == "full-series" and fulllib["owned_files"] == 4


def test_off_by_flag():
    m = _mgr(config={"pilot_interactive": {"report": False}})
    assert m.report_pilots_off_720("standard", _df()) == []
    assert m._rs.calls == [] and m.logger.info == []


def test_no_sub720_pilots_is_quiet():
    m = _mgr()
    df = pd.DataFrame([_row(is_pilot=True, episode_file_id=1, resolution=720, series_id=1, series_title="OK")])
    assert m.report_pilots_off_720("standard", df) == []
    assert m._rs.calls == []
    assert _has(m.logger.info, "[Pilot720]", "no on-disk pilots below 720p")

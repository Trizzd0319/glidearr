"""Tests for the unconfigured-floor operator alert.

When free_space_limit is unset, the space gates default the floor to 25% of the total
drive — alert_unconfigured_floor surfaces that to the operator ONCE per (service,
instance) per process, naming the concrete derived GB floor.
"""
from __future__ import annotations

from scripts.support.utilities import space_floor_alert as sfa
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor


class _Logger:
    def __init__(self):
        self.warnings = []

    def log_warning(self, m, *a, **k):
        self.warnings.append(m)


def setup_function(_fn):
    sfa._WARNED.clear()   # isolate the per-process dedup between tests


def test_no_alert_when_limit_configured():
    lg = _Logger()
    alert_unconfigured_floor({"free_space_limit": 2500}, lg, "Radarr", "standard", 8000.0)
    assert lg.warnings == []


def test_alerts_once_naming_the_derived_floor():
    lg = _Logger()
    alert_unconfigured_floor({}, lg, "Radarr", "standard", 8000.0)
    alert_unconfigured_floor({}, lg, "Radarr", "standard", 8000.0)   # deduped
    assert len(lg.warnings) == 1
    assert "25%" in lg.warnings[0]
    assert "2000" in lg.warnings[0]          # 25% of 8000 GB
    assert "free_space_limit" in lg.warnings[0]


def test_last_resort_wording_when_total_unreadable():
    lg = _Logger()
    alert_unconfigured_floor({}, lg, "Sonarr", "standard", float("inf"))
    assert len(lg.warnings) == 1
    assert "unreadable" in lg.warnings[0].lower()


def test_distinct_service_instance_keys_warn_separately():
    lg = _Logger()
    alert_unconfigured_floor({}, lg, "Radarr", "standard", 8000.0)
    alert_unconfigured_floor({}, lg, "Sonarr", "standard", 8000.0)
    alert_unconfigured_floor({}, lg, "Radarr", "4k", 8000.0)
    assert len(lg.warnings) == 3


def test_noop_without_logger_or_instance():
    alert_unconfigured_floor({}, None, "Radarr", "standard", 8000.0)   # no logger
    lg = _Logger()
    alert_unconfigured_floor({}, lg, "Radarr", None, 8000.0)           # no instance
    assert lg.warnings == []

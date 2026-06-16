"""Unit tests for RadarrSpacePressureManager._downgrade_protect_threshold — the watchability
floor below which movies step down under space pressure. Default = WATCHABILITY_PROTECT_THRESHOLD
(6, only the near-unwatched). With space_pressure_downgrade_before_delete on, it widens to MATCH
the delete ceiling (space_pressure_score_ceiling, default 20) so any deletable title is shrunk to
720p first. A stub manager (object.__new__) bypasses the heavy __init__."""
from __future__ import annotations

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


def _mgr(config):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = config
    return m


def test_default_is_narrow_protect_threshold():
    # No flag → the narrow band: only score < 6 downgrades.
    assert _mgr({})._downgrade_protect_threshold() == 6
    assert _mgr({"space_pressure_downgrade_before_delete": False})._downgrade_protect_threshold() == 6
    assert _mgr(None)._downgrade_protect_threshold() == 6


def test_flag_widens_to_delete_ceiling():
    # Flag on → match the delete band (default ceiling 20).
    assert _mgr({"space_pressure_downgrade_before_delete": True})._downgrade_protect_threshold() == 20


def test_flag_tracks_custom_ceiling():
    # Flag on with a custom ceiling → the downgrade band follows it exactly.
    cfg = {"space_pressure_downgrade_before_delete": True, "space_pressure_score_ceiling": 35}
    assert _mgr(cfg)._downgrade_protect_threshold() == 35


def test_flag_with_malformed_ceiling_falls_back_to_20():
    cfg = {"space_pressure_downgrade_before_delete": True, "space_pressure_score_ceiling": "oops"}
    assert _mgr(cfg)._downgrade_protect_threshold() == 20

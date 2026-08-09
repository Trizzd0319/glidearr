"""
space_targets.py — RE-EXPORT SHIM (ML-migration Step 7a).
================================================================================
The disk-space gating math moved to the brain at
``machine_learning.space.space_targets``. This module re-exports it so every
existing ``from scripts.support.utilities.space_targets import ...`` (radarr/sonarr
space-pressure, the coordinator, anomaly/storage, series orchestration/quality)
keeps working unchanged. Deleted at MIGRATION.md Step 10.

NO WILDCARD — the surface is the explicit list below, so MIGRATION Step 10 can
enumerate exactly what to repoint. (A wildcard re-export lets a caller import any
public name through this module and then break SILENTLY on deletion, with nothing to
grep for; same fix as trakt/movies/scorer.py.) 13 callers — the most of any shim here.

``PRESSURE_FALLBACK_GB`` is GONE, not renamed. It was the last-resort floor used when
BOTH ``free_space_limit`` and the drive's total size were unknown — and four managers
declared their own, disagreeing (25.0 / 25.0 / 1000.0, plus 25.0 under the name
``PRESSURE_THRESHOLD_GB``). The floor now comes from config or from the disk's own size,
and is 0.0 (no floor, no pressure) when neither is available. Anything importing this
name should simply stop: pass nothing and take the default.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space.space_targets import (  # noqa: F401
    DEFAULT_REGRAB_CAP,
    PRESSURE_FALLBACK_FRACTION,
    _cfg_get,
    coordinator_owns_deletion,
    deletions_consented,
    deletions_disabled_reason,
    deletions_enabled,
    downgrade_regrab_cap,
    exhaustive_downgrade,
    space_targets,
)

__all__ = [
    "DEFAULT_REGRAB_CAP",
    "PRESSURE_FALLBACK_FRACTION",
    "_cfg_get",
    "coordinator_owns_deletion",
    "deletions_consented",
    "deletions_disabled_reason",
    "deletions_enabled",
    "downgrade_regrab_cap",
    "exhaustive_downgrade",
    "space_targets",
]

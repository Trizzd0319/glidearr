"""
space_targets.py — RE-EXPORT SHIM (ML-migration Step 7a).
================================================================================
The disk-space gating math moved to the brain at
``machine_learning.space.space_targets``. This module re-exports it so every
existing ``from scripts.support.utilities.space_targets import ...`` (radarr/sonarr
space-pressure, the coordinator, anomaly/storage, series orchestration/quality)
keeps working unchanged. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space.space_targets import *  # noqa: F401,F403
from scripts.managers.machine_learning.space.space_targets import (  # noqa: F401
    PRESSURE_FALLBACK_GB,
    _cfg_get,
    coordinator_owns_deletion,
    deletions_consented,
    deletions_disabled_reason,
    deletions_enabled,
    space_targets,
)

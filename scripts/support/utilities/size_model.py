"""
size_model.py — RE-EXPORT SHIM (ML-migration Step 1).
================================================================================
The real implementation moved to
``scripts.managers.machine_learning.sizing.size_model`` (the brain/decision
layer). This module re-exports it so every existing
``from scripts.support.utilities.size_model import ...`` keeps working unchanged.

There is exactly ONE module now — the calibration-overlay state
(``set_calibration`` / ``get_calibration`` / the overlay used by ``mb_per_min``)
lives in the new module, and these names are the SAME function objects, so state
stays consistent no matter which import path a caller used.

Once every caller imports the new path directly, this shim is deleted
(MIGRATION.md Step 10) and a CI guard forbids the old path.
"""
from __future__ import annotations

from scripts.managers.machine_learning.sizing.size_model import *  # noqa: F401,F403
from scripts.managers.machine_learning.sizing.size_model import (  # noqa: F401
    CALIBRATED_MB_PER_MIN,
    DEFAULT_MB_PER_MIN,
    MAX_MB_PER_MIN,
    MIN_MB_PER_MIN,
    clear_calibration,
    estimate_gb,
    estimate_gb_for_profile,
    get_calibration,
    mb_per_min,
    measured_mb_per_min,
    measured_stats,
    profile_max_quality,
    set_calibration,
)

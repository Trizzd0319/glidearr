"""storage_estimator.py — RE-EXPORT SHIM (ML-migration Step 1b).

Real implementation moved to ``machine_learning/sizing/storage_estimator.py``.
Kept so any future ``from scripts.managers.machine_learning.storage_estimator
import MLStorageForecaster`` keeps resolving. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.sizing.storage_estimator import (  # noqa: F401
    MLStorageForecaster,
)

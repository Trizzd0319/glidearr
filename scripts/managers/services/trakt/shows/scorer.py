"""
trakt/shows/scorer.py — RE-EXPORT SHIM (ML-migration Step 2).
================================================================================
The TV watchability engine moved to the brain layer at
``scripts.managers.machine_learning.scoring.show_scorer``. This module re-exports
it so ``from scripts.managers.services.trakt.shows.scorer import score_show``
(sonarr episode_files) keeps working unchanged. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring.show_scorer import *  # noqa: F401,F403
from scripts.managers.machine_learning.scoring.show_scorer import (  # noqa: F401
    QUALITY_PROFILE_THRESHOLDS,
    score_show,
    score_to_profile,
    score_to_sonarr_profile_id,
)

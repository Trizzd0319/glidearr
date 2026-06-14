"""
trakt/movies/scorer.py — RE-EXPORT SHIM (ML-migration Step 2).
================================================================================
The movie watchability engine moved to the brain layer at
``scripts.managers.machine_learning.scoring.movie_scorer``. This module
re-exports it so every existing ``from scripts.managers.services.trakt.movies.scorer
import score_movie`` (radarr space_pressure / repair, trakt ratings) keeps working
unchanged. There is exactly one implementation now. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring.movie_scorer import *  # noqa: F401,F403
from scripts.managers.machine_learning.scoring.movie_scorer import (  # noqa: F401
    QUALITY_PROFILE_THRESHOLDS,
    _DEVICE_RESOLUTION_CEILING,
    _KIDS_CERTS,
    _TRANSCODE_FRIENDLY_CODECS,
    score_movie,
    score_to_profile,
    score_to_radarr_profile_id,
)

"""
trakt/movies/scorer.py — RE-EXPORT SHIM (ML-migration Step 2).
================================================================================
The movie watchability engine moved to the brain layer at
``scripts.managers.machine_learning.scoring.movie_scorer``. This module
re-exports it so every existing ``from scripts.managers.services.trakt.movies.scorer
import score_movie`` (radarr space_pressure / repair, trakt ratings) keeps working
unchanged. There is exactly one implementation now. Deleted at MIGRATION.md Step 10.

NO WILDCARD — AND THAT IS THE POINT.
This used to be ``from …movie_scorer import *`` plus a partial explicit list. A
wildcard re-export makes the shim's surface UNBOUNDED: a caller could import any
public name in movie_scorer through this module, it would work, and it would break
**silently at Step 10** with nothing to grep for — the name never appears in this
file. Step 10 could not be scoped at all.

The list below is the exact set the wildcard was providing, so this change is
behaviour-identical *today* while making the migration surface finite and
greppable. If a caller imports something not listed here it now fails at IMPORT
time — loudly, immediately, and with the missing name in the traceback — which is
the failure you want, at the time you want it.

Step 10 checklist: every name here must have its callers repointed at
``machine_learning.scoring.movie_scorer`` before this file is removed.
⚠️ The **entire Radarr delete path** imports ``score_movie`` through this shim
(``repair/anomaly.py`` ×4, ``quality/space_pressure.py``), so "delete the shim"
is not a cleanup task — it is a change to the code that removes movie files.
"""
from __future__ import annotations

from scripts.managers.machine_learning.scoring.movie_scorer import (  # noqa: F401
    # — the scorer proper
    score_movie,
    score_to_profile,
    score_to_radarr_profile_id,
    # — tables/constants callers read directly (underscored, but public API here)
    QUALITY_PROFILE_THRESHOLDS,
    _DEVICE_CAPABILITIES,
    _DEVICE_RESOLUTION_CEILING,
    _KIDS_CERTS,
    _TRANSCODE_FRIENDLY_CODECS,
    # — re-exported transitively by the old wildcard. movie_scorer imports these
    #   from scoring/_shared (and people_matrix.build); a caller reaching them
    #   through this shim rather than from _shared directly still worked, so they
    #   are listed to keep the removal of `import *` behaviour-identical.
    INTENT_HALF_LIFE_DAYS,
    INTENT_STALE_FLOOR,
    codec_transcode_prior,
    device_resolution_ceiling,
    normalize_lang,
    person_affinity_score,
    related_graph_affinity,
    route_people,
    select_profile_id,
    user_rating_score,
    watchlist_intent_score,
)

__all__ = [
    "score_movie",
    "score_to_profile",
    "score_to_radarr_profile_id",
    "QUALITY_PROFILE_THRESHOLDS",
    "_DEVICE_CAPABILITIES",
    "_DEVICE_RESOLUTION_CEILING",
    "_KIDS_CERTS",
    "_TRANSCODE_FRIENDLY_CODECS",
    "INTENT_HALF_LIFE_DAYS",
    "INTENT_STALE_FLOOR",
    "codec_transcode_prior",
    "device_resolution_ceiling",
    "normalize_lang",
    "person_affinity_score",
    "related_graph_affinity",
    "route_people",
    "select_profile_id",
    "user_rating_score",
    "watchlist_intent_score",
]


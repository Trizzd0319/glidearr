"""likelihood/quality_ladder.py — score/likelihood -> profile.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: The score->profile thresholds and per-service profile-id resolution (decision half).

PULLS FROM (decision cores to migrate here):
  - trakt/movies/scorer::QUALITY_PROFILE_THRESHOLDS, score_to_profile
  - trakt/movies/scorer::score_to_radarr_profile_id (decision half)
  - trakt/shows/scorer::score_to_sonarr_profile_id (decision half)
  - radarr/quality/selector::_is_valid_profile / get_best_profile_for_instance (decision half)

PUBLIC API (to implement):
  - target_profile_id(likelihood, ranked_profiles, config) -> int

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Service assembles ranked_profiles (FETCH); ladder picks the id.
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

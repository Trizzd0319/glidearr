"""quality_analytics/profile_selector.py — profile scoring.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Score/rank quality profiles for a title (analytics; feeds likelihood/quality_ladder).

PULLS FROM (decision cores to migrate here):
  - machine_learning/profile_selector.py (decision half)

PUBLIC API (to implement):
  - score_profiles(features, profiles) -> list

DEPENDS ON: contracts, likelihood
SERVICE REMAINDER (stays in the service as the thin adapter): Service supplies the profile list (FETCH).
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

"""lifecycle/auto_rater.py — which to auto-rate + what score.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Decide which watched titles to push a Trakt rating for, and the rating value.

PULLS FROM (decision cores to migrate here):
  - trakt/ratings::auto_rate_watched_movies (selection)
  - trakt/ratings::auto_rate_watched_shows (selection)

PUBLIC API (to implement):
  - plan_auto_ratings(features, existing_ratings, config) -> list[tuple[id, rating]]

DEPENDS ON: contracts, scoring
SERVICE REMAINDER (stays in the service as the thin adapter): trakt/ratings keeps the /sync/ratings POST (APPLY).
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

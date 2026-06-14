"""lifecycle/restore_policy.py — restore recovered deletions.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Decide which previously-deleted items to re-acquire on score recovery.

PULLS FROM (decision cores to migrate here):
  - radarr/repair/anomaly::restore_recovered_deletions (decision core)
  - sonarr/cache/episode_files::restore_recovered_episode_deletions (decision core)

PUBLIC API (to implement):
  - plan_restores(restore_set, current_scores, config) -> list[AcquirePlan]

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Service keeps the restore-set read + re-monitor/search APPLY.
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

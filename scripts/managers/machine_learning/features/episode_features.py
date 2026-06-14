"""features/episode_features.py — build EpisodeFeatureRow.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Marshal an episode_files Parquet row into an EpisodeFeatureRow (+ guard inputs).

PULLS FROM (decision cores to migrate here):
  - sonarr/cache/episode_files (per-row field reads in build_delete_candidates etc.)

PUBLIC API (to implement):
  - build_episode_feature_row(parquet_row) -> EpisodeFeatureRow

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Sonarr keeps the Parquet load/save.
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

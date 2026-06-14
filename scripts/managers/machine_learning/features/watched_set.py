"""features/watched_set.py — the owned/household watched-set.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Assemble watched_tmdb_ids + completion_map from Trakt + Tautulli sources.

PULLS FROM (decision cores to migrate here):
  - radarr/orchestration::watched_tmdb_ids assembly (~line 350)
  - radarr/orchestration::completion_map assembly (~line 485)

PUBLIC API (to implement):
  - build_watched_set(trakt_history, tautulli_completions) -> set[int]

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Radarr orchestrator keeps wiring the caches in.
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

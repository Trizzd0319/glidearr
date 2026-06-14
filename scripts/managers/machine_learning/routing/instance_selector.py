"""routing/instance_selector.py — pick the target instance.
==============================================================================
MIGRATION TARGET — pure decision logic (see ../ARCHITECTURE.md).
NO HTTP, NO service imports, NO global_cache writes. Consumes
contracts.* feature rows + config; emits contracts.* plans / scores as
plain data that a service adapter then APPLIES.

PURPOSE: Decide which configured instance an add/route targets.

PULLS FROM (decision cores to migrate here):
  - machine_learning/instance_selector.py (decision half)

PUBLIC API (to implement):
  - select_instance(item, instances, config) -> str

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter): Service keeps instance config FETCH + apply.
"""
from __future__ import annotations

# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).

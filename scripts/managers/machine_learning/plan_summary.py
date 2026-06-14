"""
plan_summary.py — RE-EXPORT SHIM (ML-migration Step 6).
================================================================================
The dry-run plan-ledger roll-up moved to ``machine_learning.ledger.plan_summary``.
This module re-exports it so ``from scripts.managers.machine_learning.plan_summary
import PlanSummary`` (main.py) keeps working unchanged. Deleted at MIGRATION.md
Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.ledger.plan_summary import *  # noqa: F401,F403
from scripts.managers.machine_learning.ledger.plan_summary import PlanSummary  # noqa: F401

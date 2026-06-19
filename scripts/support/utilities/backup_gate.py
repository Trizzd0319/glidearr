"""
backup_gate.py — the one place destructive primitives ask "am I allowed to write?".
================================================================================
A real run (``dry_run=false``) is only permitted to make destructive changes once the
pre-destructive backup pre-flight (``services/backup.ServiceBackupManager``) has ARMED the
gate by creating + validating a loadable backup of every service. If a backup failed, the
gate is DISARMED and the run DEGRADES TO DRY-RUN.

``effective_dry_run(self.dry_run, self.global_cache)`` is what every delete/re-grab/downgrade
primitive should branch on instead of the bare ``self.dry_run``:

  * dry run               → True  (never writes — unchanged behaviour)
  * real run, gate armed  → False (writes, exactly as before)
  * real run, gate DISARMED (backup failed / not run yet for a real destructive op) → True

The gate DEFAULTS TO ARMED when unset, so dry runs, opted-out users
(``backup_before_destructive: false``), and any flow where the pre-flight never ran behave
exactly as they did before this feature — only an EXPLICIT disarm blocks writes.
"""
from __future__ import annotations

from scripts.managers.services.backup import GATE_KEY


def writes_armed(global_cache) -> bool:
    """True when destructive writes are permitted. Only an explicit disarm (a real run whose
    backup pre-flight failed) returns False; an unset gate defaults to armed."""
    try:
        gate = (global_cache.get(GATE_KEY) or {}) if global_cache is not None else {}
    except Exception:
        gate = {}
    return bool(gate.get("armed", True))


def effective_dry_run(dry_run, global_cache) -> bool:
    """The dry-run a destructive primitive should honour: real, but with a disarmed backup gate,
    behaves as dry-run (degrade-to-dry-run safety)."""
    return bool(dry_run) or not writes_armed(global_cache)

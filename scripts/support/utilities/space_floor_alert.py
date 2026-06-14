"""
space_floor_alert.py — operator alert for an UNCONFIGURED disk-space floor.
================================================================================
When ``free_space_limit`` is not set in config.json, every space gate derives its
floor from 25% of the total root-folder space (see
``machine_learning.space.space_targets``). This helper surfaces that decision to the
operator with ONE warning per (service, instance) per process — so they know
reclamation (deletes / downgrades / acquisition gating) is running against a derived
default they didn't choose, and can set ``free_space_limit`` to control it precisely.

Deliberately kept OUT of the brain's pure, log-free ``space_targets`` module: this is
service-layer I/O. The per-process dedup resets naturally on restart, so the operator
is reminded once per run, not spammed by every gate that consults the floor.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space.space_targets import _cfg_get, space_targets

# One alert per "<service>:<instance>" per process.
_WARNED: set[str] = set()


def alert_unconfigured_floor(config, logger, service: str, instance, total_gb) -> None:
    """Warn ONCE that the disk-space floor is being defaulted to 25% of the total drive
    because ``free_space_limit`` is unset. No-op when the limit IS configured, when
    there's no logger/instance, or when already warned for this service+instance."""
    if logger is None or instance is None:
        return
    try:
        fsl = float(_cfg_get(config, "free_space_limit", 0) or 0)
    except (TypeError, ValueError):
        fsl = 0.0
    if fsl > 0:
        return  # operator chose a floor — nothing to alert

    key = f"{service}:{instance}"
    if key in _WARNED:
        return
    _WARNED.add(key)

    T, _ = space_targets(config, total_gb=total_gb)
    try:
        tg = float(total_gb) if total_gb is not None else 0.0
    except (TypeError, ValueError):
        tg = 0.0
    have_total = tg > 0 and tg != float("inf")

    if have_total:
        logger.log_warning(
            f"⚠️ [{service}] '{instance}': free_space_limit is not set — defaulting the "
            f"disk-space floor to 25% of the total root-folder space (~{T:.0f} GB of "
            f"{tg:.0f} GB). Set free_space_limit in config.json to control reclamation "
            f"precisely."
        )
    else:
        logger.log_warning(
            f"⚠️ [{service}] '{instance}': free_space_limit is not set AND the total "
            f"root-folder size is unreadable — using the last-resort {T:.0f} GB floor. "
            f"Set free_space_limit in config.json."
        )

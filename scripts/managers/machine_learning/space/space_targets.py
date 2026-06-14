"""
space_targets.py — the single source of truth for disk-space gating.
================================================================================
RELOCATED into the brain (ML Step 7a) — already pure (config reads only), so it
moved here verbatim; ``scripts/support/utilities/space_targets.py`` is now a
re-export shim (deleted at MIGRATION.md Step 10).

Every space gate (Radarr + Sonarr upgrades, downgrades, deletions, and the
cross-service coordinator) derives its thresholds from the one user setting
``free_space_limit`` instead of scattered hardcoded 25 / 50 / 100 GB constants.

    T = free_space_limit                              # floor to keep free
    U = T * (1 + space_pressure_headroom_ratio)        # top of the pressure band

Rule of thumb:
    free >= U   → comfortable: upgrades/acquisition allowed, no reclamation
    T <= free < U → pressure band: hold steady (deletion loop stops here = hysteresis)
    free <  T   → below floor: downgrade + delete to reclaim back up to U

When ``free_space_limit`` is not set, the floor DEFAULTS TO 25% of the total drive
(``PRESSURE_FALLBACK_FRACTION``) so the gate scales with the disk instead of any
hardcoded GB constant. Callers pass ``total_gb`` (mount-deduped, e.g.
``instance_manager.disk_total_gb``) to enable that. The ``fallback_gb`` constant is
only a last resort for when BOTH ``free_space_limit`` and ``total_gb`` are unknown.

SWEEP COMPLETE: every space gate now passes ``total_gb`` (mount-deduped via
``instance_manager.disk_total_gb``), so the 25%-of-total fallback applies uniformly and
no gate respects a hardcoded GB floor. The per-manager constants that remain
(PRESSURE_THRESHOLD_GB / PRESSURE_FALLBACK_GB 25, MIN_FREE_SPACE_GB 50) are only the
LAST-RESORT ``fallback_gb`` for when ``free_space_limit`` is unset AND the drive's total
size is unreadable. UPGRADE_MIN_FREE_GB (100) and DEFAULT_UPGRADE_GB (50) were deleted.
Two deliberate exceptions, justified inline at their call sites:
  • universe.DEFAULT_DOWNGRADE_GB (10) — a deep low-disk EMERGENCY trigger for the
    never-deleted universe class; intentionally a fixed floor, not total-derived.
  • repair/anomaly.py (fallback_gb=0.0, no total_gb) — a SENTINEL selecting the legacy
    time-based owned-movie prune when no ``free_space_limit`` is configured.
"""
from __future__ import annotations

PRESSURE_FALLBACK_GB = 25.0          # last resort only (total drive unknown too)
PRESSURE_FALLBACK_FRACTION = 0.25    # of total drive when free_space_limit unset


def _cfg_get(config, key, default):
    """Read a key from a ConfigManager OR a plain dict OR None."""
    if config is None:
        return default
    try:
        return config.get(key, default)
    except Exception:
        return default


def space_targets(
    config,
    fallback_gb: float = PRESSURE_FALLBACK_GB,
    *,
    total_gb: "float | None" = None,
) -> tuple[float, float]:
    """Return ``(T, U)`` — the free-space floor and the top of the pressure band.

    ``T = free_space_limit`` when configured. Otherwise ``T`` defaults to
    ``PRESSURE_FALLBACK_FRACTION`` (25%) of ``total_gb`` (the total drive), and only
    if ``total_gb`` is also unknown does it fall back to ``fallback_gb``.
    """
    try:
        T = float(_cfg_get(config, "free_space_limit", 0) or 0)
    except (TypeError, ValueError):
        T = 0.0
    if T <= 0:
        # No configured floor → 25% of the total drive (scales with the disk),
        # falling back to the constant only when the total is also unavailable.
        try:
            tg = float(total_gb) if total_gb is not None else 0.0
        except (TypeError, ValueError):
            tg = 0.0
        T = PRESSURE_FALLBACK_FRACTION * tg if (tg > 0 and tg != float("inf")) else float(fallback_gb)
        return T, T   # no headroom band on the fallback floor (matches prior behaviour)
    try:
        headroom = float(_cfg_get(config, "space_pressure_headroom_ratio", 0.10))
    except (TypeError, ValueError):
        headroom = 0.10
    return T, T * (1.0 + max(0.0, headroom))


def deletions_enabled(config) -> bool:
    """HARD SAFETY GATE for media-file deletion: an operator-set ``free_space_limit``
    (> 0) is required before ANY delete pass may remove a file. With the floor unset
    (0 / missing / unparseable) every deletion path — per-service space-pressure
    deletes, grace-marked file deletes, the stale-owned prune's delete stage, and the
    cross-service coordinator — must SKIP deleting, so an unconfigured install can
    never inadvertently delete media. Downgrades, monitoring, grace MARKING and
    acquisition are unaffected; only the destructive delete APPLY is gated.

    (Deliberately mirrors the ``free_space_limit`` clause of
    ``coordinator_owns_deletion`` — the operator opting into a floor is what arms
    deletion anywhere in the app.)"""
    try:
        return float(_cfg_get(config, "free_space_limit", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def coordinator_owns_deletion(config) -> bool:
    """True when the cross-service space coordinator owns ALL deletion, so the
    per-service legacy delete paths (Radarr space-pressure delete, movie_files
    blanket delete, Sonarr episode delete) must defer — they still MARK candidates,
    but the coordinator does the actual deleting on a unified, ranked movie+TV pool.

    Requires ``space_coordinator_enabled`` AND a configured ``free_space_limit``
    (the coordinator keys off the floor). Defaults to OFF until the coordinator
    ships, so per-service deletion keeps working in the interim.
    """
    if not bool(_cfg_get(config, "space_coordinator_enabled", False)):
        return False
    try:
        return float(_cfg_get(config, "free_space_limit", 0) or 0) > 0
    except (TypeError, ValueError):
        return False

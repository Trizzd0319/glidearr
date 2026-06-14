"""
watch_likelihood.py — RE-EXPORT SHIM (ML-migration Step 4).
================================================================================
The likelihood engine moved to the brain layer at
``scripts.managers.machine_learning.likelihood.watch_likelihood``. This module
re-exports it so every existing
``from scripts.support.utilities.watch_likelihood import ...`` (radarr universe /
space_pressure, sonarr episode_files, advise_watchability, tests) keeps working
unchanged. There is exactly one implementation now. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

from scripts.managers.machine_learning.likelihood.watch_likelihood import *  # noqa: F401,F403
from scripts.managers.machine_learning.likelihood.watch_likelihood import (  # noqa: F401
    _DEFAULTS,
    _DEFAULT_RADARR_LADDER,
    _cfg,
    _cfg_str,
    _get,
    _num,
    affinity_boost,
    explain_likelihood,
    ladder_rank,
    profile_id_for_likelihood,
    radarr_ladder,
    resolution_cap_for_likelihood,
    watch_likelihood,
)

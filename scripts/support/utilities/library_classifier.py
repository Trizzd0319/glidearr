"""
library_classifier.py — RE-EXPORT SHIM (ML-migration Step 5a).
================================================================================
The library classifier moved to the brain layer at
``...machine_learning.classification.library_classifier``. This module re-exports
it so every existing import keeps working unchanged. Dual-import: in-app callers
use the ``scripts.`` prefix (repo root on path); standalone scripts run inside
``scripts/`` (router_show.py / router_movie.py) import it bare, with only
``scripts/`` on path — so try the ``scripts.`` package first and fall back to the
top-level ``managers.`` package. Deleted at MIGRATION.md Step 10.
"""
from __future__ import annotations

try:  # repo root on sys.path (in-app / PYTHONPATH=repo;repo/scripts)
    from scripts.managers.machine_learning.classification.library_classifier import *  # noqa: F401,F403
    from scripts.managers.machine_learning.classification.library_classifier import (  # noqa: F401
        _anime_match, _as_set, _kids_by_genre,
    )
except ImportError:  # only scripts/ on sys.path (standalone router_show/router_movie)
    from managers.machine_learning.classification.library_classifier import *  # noqa: F401,F403
    from managers.machine_learning.classification.library_classifier import (  # noqa: F401
        _anime_match, _as_set, _kids_by_genre,
    )

"""Context bundles — the shared, library-wide inputs a planner needs beyond a
single feature row (affinity maps, space band, free-space reading, config view).
Built by services from caches; passed (read-only) into brain entrypoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AffinityContext:
    """Household + per-user affinity maps derived from Tautulli history."""
    genres: dict = field(default_factory=dict)
    actors: dict = field(default_factory=dict)
    directors: dict = field(default_factory=dict)
    writers: dict = field(default_factory=dict)
    studios: dict = field(default_factory=dict)
    per_user: dict = field(default_factory=dict)
    kids_users: tuple = ()
    adult_users: tuple = ()
    platform_usage: dict = field(default_factory=dict)
    transcode_stats: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpaceContext:
    """The space band + current free space for a space-pressure decision."""
    free_gb: float
    floor_gb: float          # T = free_space_limit
    target_gb: float         # U = T*(1+headroom)
    coordinator_owns_deletion: bool = False

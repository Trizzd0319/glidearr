"""Plans / decisions — the typed output the brain emits and a service APPLIES.

All fields are plain data. A plan never executes anything; the service adapter
turns it into HTTP + a ledger stamp. reclaim_gb is SIGNED: + frees space,
- consumes it (so the dry-run ledger sums to a true net).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityPlan:
    """Upgrade or downgrade a title's quality profile."""
    service: str                      # "movie" | "series"
    item_id: int                      # movie_id / series_id
    file_id: int | None
    target_profile_id: int
    direction: str                    # "upgrade" | "downgrade"
    reason: str
    est_space_gb_signed: float = 0.0  # +freed (downgrade) / -consumed (upgrade)


@dataclass(frozen=True)
class DeletePlan:
    """Delete a specific file (already guard-checked + ranked)."""
    service: str                      # "movie" | "episode"
    file_id: int
    reason: str
    reclaim_gb: float = 0.0           # + (freed)
    restore_key: str | None = None    # id to record in the restore-set


@dataclass(frozen=True)
class DeleteCandidate:
    """A rankable delete candidate fed into the cross-service pool (pre-decision)."""
    service: str                      # "movie" | "episode"
    file_id: int
    tier: int                         # 0 = watched+grace-expired, 1 = unwatched-low
    score: float                      # watchability (lower = delete first)
    critic: float | None              # secondary rank key
    size_gb: float
    title: str | None = None
    item_id: int | None = None        # movie_id / series_id


@dataclass(frozen=True)
class MonitorPlan:
    """Set monitored on/off for a title."""
    service: str
    item_id: int
    monitored: bool
    reason: str


@dataclass(frozen=True)
class GracePlan:
    """Mark/clear a file for grace-period deletion eligibility."""
    file_id: int
    marked_for_deletion: bool
    available_until: str | None
    reason: str


@dataclass(frozen=True)
class AcquirePlan:
    """Acquire (monitor + search) a not-yet-present episode/movie."""
    service: str
    item_id: int                      # series_id / movie_id
    season: int | None = None
    episode: int | None = None
    target_profile_id: int | None = None
    reason: str = ""

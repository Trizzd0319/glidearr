"""
machine_learning/playlists — the pure ordering brain for personalized Plex playlists.
================================================================================
THINKS only. No HTTP, no Plex, no cache, no ratingKey *resolution* — the service
layer (services/plex/playlists) fetches owned items, resolves each to a Plex
ratingKey, attaches a per-user watched flag + a watchability score, and hands the
brain a list of :class:`PlaylistInput`. The brain returns an ordered
:class:`PlaylistPlan`; the service turns that into the (first-ever) Plex write.

The crown-jewel ordering rule (the operator's spec):
  * GROUP items that share a series / franchise / universe so they stay contiguous.
  * WITHIN a group, order by timeline (explicit timeline index if given, else
    chronological); a series is ordered by (season, episode) — NOT air date — so a
    missing/￧out-of-order air date can never surface a later episode before an
    earlier one (the #1 spoiler trap).
  * ACROSS groups (and for standalone items), order by watchability.

Everything here is PURE + deterministic (brain_purity-guarded): same input → same
output, no wall-clock, no randomness, no input-order dependence (every tie has an
explicit deterministic breaker so a golden corpus can pin the result).
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.caps import apply_size_cap
from scripts.managers.machine_learning.playlists.expansion import expand_show
from scripts.managers.machine_learning.playlists.grouping import coverage_stats, group_items
from scripts.managers.machine_learning.playlists.models import (
    PlaylistInput,
    PlaylistItemPlan,
    PlaylistPlan,
)
from scripts.managers.machine_learning.playlists.cert_gate import (
    cert_allowed,
    is_restricted,
    tier_level,
)
from scripts.managers.machine_learning.playlists.ordering import order_items
from scripts.managers.machine_learning.playlists.per_user import tilt_score
from scripts.managers.machine_learning.playlists.spoiler import is_spoiler_safe
from scripts.managers.machine_learning.playlists.timeline import order_within_group

__all__ = [
    "PlaylistInput", "PlaylistItemPlan", "PlaylistPlan",
    "order_items", "group_items", "coverage_stats",
    "order_within_group", "is_spoiler_safe", "expand_show", "apply_size_cap", "tilt_score",
    "cert_allowed", "tier_level", "is_restricted",
]

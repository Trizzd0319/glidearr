"""
playlists/models.py — the brain's playlist contracts (plain frozen data).
================================================================================
``PlaylistInput`` is what the SERVICE builds (one per owned/acquired item it could
put in a user's playlist) and the brain consumes. ``PlaylistItemPlan`` /
``PlaylistPlan`` are what the brain emits and the service APPLIES. No field is an
action; the brain never executes anything (mirrors contracts/plans.py).

``rating_key`` is OPAQUE to the brain — it is the Plex item handle the service
already resolved; the brain only carries it through to the plan and uses it as the
final, always-present deterministic tie-breaker. The brain NEVER resolves it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Group kinds, broadest → narrowest. The broadest kind present in a connected
# component names the group (a universe that swept in a franchise is a "universe").
UNIVERSE = "universe"
FRANCHISE = "franchise"
SERIES = "series"
STANDALONE = "standalone"

# Precedence for naming a mixed component (lower = broader = wins the label).
_KIND_RANK = {UNIVERSE: 0, FRANCHISE: 1, SERIES: 2, STANDALONE: 3}


@dataclass(frozen=True)
class PlaylistInput:
    """One candidate item for a user's playlist (already owned + ratingKey-resolved).

    The service supplies every field; the brain treats them as pure data. ``score``
    is the watchability the cross-group ordering ranks on (household, or per-user
    once the operator opts into the personal re-rank). ``watched`` is per-USER (the
    brain drops watched items — Hard-req #5). Grouping/timeline fields are nullable
    and degrade gracefully when a tag/date is absent.
    """
    rating_key: str                       # opaque Plex item handle (carried through)
    medium: str                           # "movie" | "episode"
    title: str = ""
    score: float | None = None            # watchability (higher = rank earlier)
    watched: bool = False                 # per-user watched → dropped from the plan

    # ── grouping affinities (any shared one binds two items into a group) ──────
    universes: tuple[str, ...] = ()       # normalized universe labels (e.g. "mcu")
    franchise: str | None = None          # franchise/collection key (movies or Kometa TV)
    series_id: int | None = None          # Sonarr series id (episode grouping + s/e order)

    # ── timeline / chronology (within-group order) ────────────────────────────
    timeline_index: int | None = None     # explicit curated order; wins over dates
    season: int | None = None
    episode: int | None = None
    air_date: str | None = None           # ISO-8601 (episodes)
    release_date: str | None = None       # ISO-8601 — service picks THEATRICAL-priority
    year: int | None = None               # last-resort chrono when no date present

    # ── safety / display ──────────────────────────────────────────────────────
    cert: str | None = None               # content rating (kids cert-gating, later PR)
    is_special: bool = False              # season 0 / special → group tail


@dataclass(frozen=True)
class PlaylistItemPlan:
    """One ordered slot in the emitted playlist."""
    rating_key: str
    ordinal: int                          # 0-based final position
    group_key: str                        # resolved group identity (preview/debug)
    group_kind: str                       # universe | franchise | series | standalone
    score: float | None                   # the score used to rank this item's group
    reason: str                           # short human rationale for the preview grid


@dataclass(frozen=True)
class PlaylistPlan:
    """The brain's full answer for one playlist (one Plex playlist object)."""
    family: str                           # "up_next" | "fresh" | "saga" | ...
    items: tuple[PlaylistItemPlan, ...] = ()
    considered: int = 0                   # candidates seen (pre watched-filter)
    dropped_watched: int = 0              # removed because the user already watched
    truncated: int = 0                    # dropped by the size cap
    coverage: dict = field(default_factory=dict)  # group_kind → count (degradation signal)

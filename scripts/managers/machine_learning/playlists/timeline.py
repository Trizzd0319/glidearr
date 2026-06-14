"""
playlists/timeline.py — order items WITHIN a group by timeline / chronology.
================================================================================
Spoiler-safety is the #1 trust requirement, so the ordering is deliberately NOT a
flat "sort everything by air date":

  * A SERIES is ordered by (season, episode) — the authoritative within-series order.
    Air date is NEVER the primary key for episodes of one series, because a missing
    or out-of-order ``air_date`` would otherwise float a later episode ahead of an
    earlier one (red-team: NULL date → tail would put S1E2 before a date-less S1E1).
  * Each series, and each movie, is a TRACK. Tracks are then interleaved by their
    lead chronological date (theatrical/air), so a franchise reads in saga order
    (Film 1 → Film 2 → [Show season as a block] → Film 3) without ever splitting a
    series across a film (which would be both confusing and spoiler-prone).
  * An explicit ``timeline_index`` (a future curated saga order) overrides dates.
  * Specials (season 0 / ``is_special``) sink to the track tail.
  * Missing dates sink to the GROUP tail (never above a dated entry).

Pure + deterministic: every comparison ends in (title.casefold(), rating_key) so
there is no dependence on input order.
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput

_INF = float("inf")


def _parse_date(s: str | None) -> int | None:
    """ISO-8601 'YYYY-MM-DD...' → comparable int YYYYMMDD. None/unparseable → None."""
    if not s or not isinstance(s, str):
        return None
    head = s.strip()[:10]                      # tolerate full ISO timestamps
    parts = head.split("-")
    if len(parts) < 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return y * 10000 + m * 100 + d


def chrono_value(it: PlaylistInput) -> float:
    """Absolute real-date sort value shared across media (so a film and an episode
    interleave on one axis). Episode → air_date; movie → release_date (the service
    supplies theatrical-priority). Falls back to ``year``; nothing → +inf (tail)."""
    primary = it.air_date if it.medium == "episode" else it.release_date
    val = _parse_date(primary)
    if val is not None:
        return float(val)
    if it.year is not None:
        return float(it.year * 10000)
    return _INF


def _num(x: int | None) -> float:
    return float(x) if x is not None else _INF


def _episode_sort_key(it: PlaylistInput):
    """Within a single series: (season, episode) is authoritative; specials last."""
    special = 1 if (it.is_special or it.season == 0) else 0
    return (special, _num(it.season), _num(it.episode), it.title.casefold(), it.rating_key)


def _tracks(members: list[PlaylistInput]) -> list[list[PlaylistInput]]:
    """Partition a group into tracks: one per series_id (its episodes), and one per
    movie / un-serialized item. Tracks keep a series atomic so it never splits across
    a film."""
    by_series: dict[int, list[PlaylistInput]] = {}
    singles: list[list[PlaylistInput]] = []
    for it in members:
        if it.medium == "episode" and it.series_id is not None:
            by_series.setdefault(it.series_id, []).append(it)
        else:
            singles.append([it])
    tracks = [sorted(eps, key=_episode_sort_key) for eps in by_series.values()]
    tracks.extend(singles)
    return tracks


def _track_lead_key(track: list[PlaylistInput]):
    """Order tracks within a group. Explicit timeline index wins (curated saga order);
    else the track's earliest real date; missing → tail. Deterministic tie-break."""
    head = track[0]
    indices = [t.timeline_index for t in track if t.timeline_index is not None]
    if indices:
        return (0, float(min(indices)), head.title.casefold(), head.rating_key)
    lead_date = min(chrono_value(t) for t in track)
    return (1, lead_date, head.title.casefold(), head.rating_key)


def order_within_group(members: list[PlaylistInput]) -> list[PlaylistInput]:
    """Return the members of one group in spoiler-safe timeline order."""
    if len(members) <= 1:
        return list(members)
    tracks = _tracks(members)
    tracks.sort(key=_track_lead_key)
    return [it for track in tracks for it in track]

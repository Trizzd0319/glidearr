"""
playlists/expansion.py — represent an acquired SHOW as its episodes (capped).
================================================================================
Plex playlists hold playable items (movies + episodes), never a show object, so a
"show acquired" must be expanded to episodes. This is the single biggest blast
radius (a 20-season library could explode a playlist), so expansion is ALWAYS
capped. Pure: the service supplies the owned episode rows; the brain only picks +
orders them. (The service's full-episode inventory is a separate data-layer
prerequisite; this function is correct the moment those rows exist.)
"""
from __future__ import annotations

from scripts.managers.machine_learning.playlists.models import PlaylistInput
from scripts.managers.machine_learning.playlists.timeline import _episode_sort_key

NEXT_UNWATCHED = "next_unwatched_n"
FULL_SERIES = "full_series"


def expand_show(episodes: list[PlaylistInput], *, mode: str = NEXT_UNWATCHED,
                cap: int = 25, include_specials: bool = False) -> list[PlaylistInput]:
    """Pick the episodes of ONE owned series to place in a playlist.

    ``next_unwatched_n`` → the earliest ``cap`` UNWATCHED episodes in (season,
    episode) order (the natural "continue the show" set). ``full_series`` → all
    episodes in order, still capped. Specials are excluded unless asked. Always
    returns at most ``cap`` items so one show can never dominate the playlist.
    """
    eps = list(episodes)
    if not include_specials:
        eps = [e for e in eps if not (e.is_special or e.season == 0)]
    eps.sort(key=_episode_sort_key)
    if mode == NEXT_UNWATCHED:
        eps = [e for e in eps if not e.watched]
    cap = max(0, int(cap))
    return eps[:cap]

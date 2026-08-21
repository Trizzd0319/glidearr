"""plex/playlists/watched_state.py — Plex's OWN watched state, per profile.

WHY THIS EXISTS. Every watched-set in this repo comes from
`builder._watched_for`, which reads Tautulli. Tautulli monitors SESSIONS: it
learns about playback because a stream started and stopped. Ticking an item
watched in the Plex UI opens no session, writes no history row, and is therefore
INVISIBLE to every builder here.

The consequences are not limited to cadence, which is how the gap surfaced:

  * Hidden Gems, whose contract is "owned and UNPLAYED", keeps offering titles
    the household has already ticked off
  * Up Next keeps pointing at an episode somebody marked seen
  * a list a viewer has visibly cleared keeps looking full

Plex knows all of it - `viewCount` is on the item, per user, and it is set by
BOTH a real play and a manual mark. This module reads that.

SCOPED BY SECTION, FILTERED SERVER-SIDE. `get_section_all` accepts a per-user
``token`` and arbitrary query params, so `unwatched=0` asks PMS for the WATCHED
set directly and it is filtered before it crosses the wire. The obvious
alternative - batched `/library/metadata/{rk,rk,...}` - was rejected on
inspection: `PlexAPI.get_pms_metadata` takes NO token, so every answer would be
the OWNER's watch state attributed to somebody else, marking a child's list
watched because an adult saw the film. A per-item endpoint that cannot be scoped
to a user is unusable for a per-user question.

PER-USER TOKEN, ALWAYS. `viewCount` is per account. A profile with no usable
token returns EMPTY, never the owner's set.

FAILS TOWARD "NOT WATCHED". Any error, any missing field, any unparseable
response yields an empty set. The caller then sees a plan as un-stale and defers
a rebuild, which costs one cycle. The opposite default would report items watched
that were not, and rebuild a list out from under somebody mid-way through it.
"""
from __future__ import annotations

from scripts.managers.services.plex._common import metadata_items

#: Items per page when walking a section's watched set.
_PAGE = 500

#: Hard ceiling on pages per section, so a pathological library cannot turn a
#: cadence check into an unbounded crawl. A profile past this is reported as
#: partial rather than silently truncated.
_MAX_PAGES = 20


def watched_in_section(plex_api, section_key, *, token, plex_type=None,
                       logger=None) -> set:
    """``{rating_key}`` this profile has WATCHED in one section.

    Watched means PMS's own ``unwatched=0`` filter, which is true for a completed
    play AND for a manual mark - the whole reason this module exists. Partial
    progress is NOT watched: somebody twenty minutes into a film has not finished
    it, and draining a list from under them is the failure this avoids.
    """
    if not plex_api or not token or section_key in (None, ""):
        return set()
    out: set = set()
    for page in range(_MAX_PAGES):
        try:
            resp = plex_api.get_section_all(
                section_key, plex_type=plex_type, start=page * _PAGE, size=_PAGE,
                token=token, extra_params={"unwatched": 0})
        except Exception as exc:
            if logger is not None and hasattr(logger, "log_debug"):
                logger.log_debug(f"[WatchedState] section {section_key} page {page} "
                                 f"failed: {type(exc).__name__}: {exc}")
            break
        items = [d for d in metadata_items(resp) if isinstance(d, dict)]
        if not items:
            break
        for d in items:
            rk = d.get("ratingKey")
            if rk not in (None, ""):
                out.add(str(rk))
        if len(items) < _PAGE:
            break
    return out


def watched_for_profile(plex_api, sections, *, token, logger=None) -> set:
    """Union of :func:`watched_in_section` across ``sections``.

    ``sections`` is ``[(section_key, plex_type), ...]`` - the caller supplies it
    because only the caller knows which libraries a profile can even see.
    """
    out: set = set()
    for key, ptype in (sections or ()):
        out |= watched_in_section(plex_api, key, token=token, plex_type=ptype,
                                  logger=logger)
    return out


def combined_watched(tautulli_watched, plex_watched) -> set:
    """The union of what Tautulli SAW and what Plex KNOWS.

    Union, not either alone, because each covers a case the other misses:
    Tautulli holds history for items since removed and re-added (a new ratingKey
    Plex has no view count for), while Plex holds marks that never produced a
    session. Neither is a superset of the other.
    """
    return {str(x) for x in (tautulli_watched or ())} | {str(x) for x in (plex_watched or ())}

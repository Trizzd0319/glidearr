"""
plex/_common.py — schema-tolerant parsing shared by the per-user fetchers.
================================================================================
Every UNSTABLE Plex response (Discover watchlist, plex.tv, onDeck) is parsed with
``.get(...)`` only — never indexed — so a contract drift soft-empties (``[]``)
instead of raising (DESIGN §6.3). One ``metadata_items`` / ``parse_item`` pair keeps
that discipline uniform across watchlist / on_deck / ratings / collections.
"""
from __future__ import annotations

_TYPE = {"1": "movie", "2": "show", "movie": "movie", "show": "show",
         "episode": "episode", "season": "show", "4": "episode"}


def anon_label(title, tier: str, index: int) -> str:
    """De-identified profile handle for LOGS: ``'{initial} - {tier} {n}'`` (e.g. ``'W - little_kid
    1'``). A profile NAME is PII (like a token/PIN) and shouldn't land in a shareable run log, but
    the operator still needs to act ("which profile do I age-gate?") and tell two apart. So we log
    the first INITIAL + the resolved age tier + a stable 1-based number — enough to recognise your
    own household, nothing a stranger can use. ``tier`` is the resolved tier (or ``'unknown'``)."""
    initial = (str(title or "?").strip()[:1] or "?").upper()
    return f"{initial} - {tier} {index}"


def metadata_items(resp) -> list:
    """The ``Metadata`` list from a MediaContainer response (or ``[]``)."""
    if not isinstance(resp, dict):
        return []
    mc = resp.get("MediaContainer", resp)
    if not isinstance(mc, dict):
        return []
    items = mc.get("Metadata") or mc.get("Video") or mc.get("Directory") or []
    return items if isinstance(items, list) else []


def total_size(resp) -> int:
    """Grand ``totalSize`` for the paging early-stop, or 0 when absent.

    Deliberately does NOT fall back to ``size`` — ``size`` is the count of items in
    the CURRENT page (== the page cap on a full page), so treating it as the grand
    total would early-stop after page 1 and silently truncate a multi-page result
    (the UNSTABLE Discover watchlist is exactly where ``totalSize`` may be absent).
    Returning 0 makes the callers fall through to the safe ``if not items: break``
    empty-page terminator (bounded by their _MAX_PAGES ceiling)."""
    if not isinstance(resp, dict):
        return 0
    mc = resp.get("MediaContainer", resp)
    if not isinstance(mc, dict):
        return 0
    try:
        return int(mc.get("totalSize") or 0)
    except (TypeError, ValueError):
        return 0


def parse_item(item: dict) -> dict:
    """Normalize one Metadata entry to a fetcher-friendly shape. Tolerant of every
    missing field."""
    if not isinstance(item, dict):
        item = {}
    raw_type = str(item.get("type", "")).lower()
    return {
        "rating_key": item.get("ratingKey") or item.get("ratingkey"),
        "guid": item.get("guid") or "",
        "guids": item.get("Guid") or item.get("guids") or [],
        "title": item.get("title") or "",
        "year": item.get("year"),
        "type": _TYPE.get(raw_type, "movie" if raw_type in ("", "movie") else raw_type),
        "user_rating": item.get("userRating"),
        "view_offset_ms": item.get("viewOffset"),
        "duration_ms": item.get("duration"),
    }


def excluded_section_titles(config) -> set:
    """Lowercased set of Plex library TITLES to SKIP in the owned-inventory scans —
    config ``plex.exclude_sections`` (list of titles; default empty).

    Lets you point UMTK/TSSK-style "Coming Soon" placeholder libraries at Plex WITHOUT
    their unreleased tmdbs/tvdbs entering ``plex/{movies,episodes}/owned_inventory``.
    Without this, a placeholder for an unreleased title resolves the title to "owned",
    which silently suppresses its real acquisition (universe_acquisition) and can surface
    a seconds-long placeholder in playlists / the anniversary shelf. The scanners select
    sections by TYPE (movie/show), so a dedicated Coming Soon library — itself a movie/show
    library — is otherwise scanned like any other; this is the by-name escape hatch.

    Default empty → every section scanned, byte-identical to prior behaviour. Tolerant of
    a bare string (treated as a one-element list) and of blank/whitespace entries."""
    plex_cfg = (config.get("plex", {}) if config else {}) or {}
    raw = plex_cfg.get("exclude_sections") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(t).strip().lower() for t in raw if str(t).strip()}

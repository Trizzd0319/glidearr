"""
services/_intent_index.py — the ONE service-side gather for Group-A5 watchlist intent.
================================================================================
The Radarr score pass (``radarr/quality/space_pressure._build_score_map``) and the Sonarr
score pass (``sonarr/cache/episode_files._build_show_score_map``) both need the same
household forward-intent index, and both need it to hash identically into their score
memos. Two copies of this gather would drift the moment one of them learned about a new
feed, so there is exactly one.

This module owns only the I/O and the identity resolution — reading cache keys, mapping
watchlister NAMES onto last-activity timestamps, and bridging MAL's id-less entries onto
library ids (``services/mal/id_bridge``). The folding, grading and expiry live in the brain
(``machine_learning/next_watch.build_intent_index``,
``scoring/_shared.watchlist_intent_score`` / ``intent_hold_active``).

    index, cap = gather_intent_index(global_cache, config)
    #   index = {"movies": {tmdb: entry}, "shows": {tvdb: entry}}, entry =
    #           {"sources": (...), "members": (...), "dated": {feed: iso}, "anchor": iso}

Fail-OPEN by construction: any missing/unreadable cache key contributes nothing, and an
entirely empty index forces ``cap`` to 0.0 → A5 is byte-identical and no shield is stamped.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from scripts.managers.machine_learning.next_watch import build_intent_index
from scripts.managers.services.mal.id_bridge import resolve_mal_id_map

_UNION_KEY = "plex/watchlist/union"
_HISTORY_KEY = "tautulli/history/all"


def _norm_member(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def member_last_activity(history) -> dict:
    """``{normalised_member: last_play_iso}`` from the cached Tautulli history.

    The shield's expiry anchor. Built from ``tautulli/history/all`` (one cached key, ~900
    rows here) rather than the per-user partitions so the score pass needs no user-manager
    registry lookup and no extra I/O.

    SHARED ACCOUNTS: Tautulli reports this household's kids account as the single friendly
    name "Aiden / Raina" while the Plex watchlist attributes entries to "Aiden". Each
    history name therefore contributes its whole normalised form AND each of its
    slash/ampersand-separated parts, so either spelling resolves to the same last-activity
    timestamp. A part never OVERWRITES a real account of that exact name — exact keys are
    written last and win."""
    exact: dict = {}
    parts: dict = {}
    for row in (history or []):
        if not isinstance(row, dict):
            continue
        name = row.get("user") or row.get("friendly_name")
        raw = row.get("date")
        if not name or raw in (None, ""):
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        key = _norm_member(name)
        if not key:
            continue
        if ts > exact.get(key, 0.0):
            exact[key] = ts
        for piece in re.split(r"[/&,+]| and ", str(name)):
            pk = _norm_member(piece)
            if pk and pk != key and ts > parts.get(pk, 0.0):
                parts[pk] = ts
    merged = {**parts, **exact}
    return {k: datetime.fromtimestamp(v, tz=timezone.utc).isoformat() for k, v in merged.items()}


def _union_with_normalised_members(union) -> list:
    """The Plex union with ``watchlisted_by`` normalised, so the member keys the index
    counts are the same keys :func:`member_last_activity` produces."""
    out: list = []
    for item in (union or []):
        if not isinstance(item, dict):
            continue
        who = [_norm_member(m) for m in (item.get("watchlisted_by") or []) if m]
        out.append({**item, "watchlisted_by": [m for m in who if m]})
    return out


def gather_intent_index(global_cache, config, logger=None) -> dict:
    """Read every cached forward-intent feed and fold it into the scorer-facing index.

    Returns ``{"movies": {...}, "shows": {...}}`` — empty dicts when nothing is cached,
    which ``_shared.resolve_intent_inputs`` turns into a 0.0 cap (A5 inert). Never raises:
    the score pass must not fail because a watchlist could not be read.

    DELIBERATELY NOT MEMOISED, even though ``refresh_scores`` calls it twice (once to
    score, once to stamp the delete shield). An instance-scoped memo was tried and pinned
    a manager to the watchlist it saw first — which is exactly the staleness the whole
    memo-invalidation half of this feature exists to prevent, and it is not worth the few
    milliseconds: every input is already served from the in-memory cache layer
    (factories/cache/memory), so a second call re-walks ~260 union entries and ~900
    history rows and touches no disk."""
    if global_cache is None:
        return {"movies": {}, "shows": {}}

    def _get(key, default=None):
        try:
            return global_cache.get(key)
        except Exception:
            return default

    union = _get(_UNION_KEY) or []
    anchors = member_last_activity(_get(_HISTORY_KEY) or [])

    # The Trakt account belongs to ONE household member. Resolving it means "Trizzd
    # watchlisted it on Plex AND on Trakt" counts as one member (correct — one human),
    # not two. ``trakt.household_member`` is that mapping; the Trakt username is only the
    # FALLBACK, and it is the wrong answer whenever the two do not spell the same.
    #
    # THIS HOUSEHOLD IS THAT CASE, AND BOTH FAILURES WERE SILENT. Trakt's handle is
    # "BuckITrizzd"; Plex and Tautulli both call the same human "Trizzd". Left on the
    # fallback the Trakt (and MAL) member normalised to "buckitrizzd", which is nobody:
    #   * every Trakt/MAL entry counted as a SECOND watchlister on any title also on the
    #     Plex watchlist — +0.12 of the cap for account sprawl (4.80 → 5.76 at cap 8) on
    #     6 movies and 2 shows here; and
    #   * "buckitrizzd" has no Tautulli activity, so a Trakt-ONLY or MAL-ONLY title got
    #     ``anchor: None`` and ``intent_hold_active`` failed CLOSED — the delete shield
    #     could never fire for exactly the population it was built for (94 movies + 43
    #     shows on this install, including all four MAL-only entries).
    # See services/test_intent_identity for both, pinned.
    trakt_cfg = ((config or {}).get("trakt", {}) or {}) if config else {}
    trakt_user = trakt_cfg.get("username") or "default"
    trakt_member = _norm_member(trakt_cfg.get("household_member") or trakt_user)

    trakt_shows = _get(f"trakt/{trakt_user}/watchlist/shows") or []
    trakt_movies = _get(f"trakt/{trakt_user}/watchlist/movies") or []

    # MAL plan-to-watch, bridged onto library ids by an exact normalized-title match and
    # cached under ``mal/{user}/id_map`` (see services/mal/id_bridge — including why the
    # Trakt title-search fallback is deliberately NOT on this path). Attributed to
    # ``trakt_member`` on purpose: one human keeping a Plex watchlist, a Trakt watchlist
    # AND a MAL list is ONE person asking, and three members would walk a solo title from
    # 0.60 to full cap on account sprawl alone.
    mal_map = resolve_mal_id_map(global_cache, config, logger=logger)

    try:
        index = build_intent_index(
            plex_union=_union_with_normalised_members(union),
            trakt_shows=trakt_shows,
            trakt_movies=trakt_movies,
            mal_shows=mal_map.get("shows"),
            mal_movies=mal_map.get("movies"),
            member_anchors=anchors,
            trakt_member=trakt_member,
        )
    except Exception as e:                      # pragma: no cover — defensive
        if logger:
            logger.log_debug(f"[Intent] index build failed ({e}) — A5 inert this run.")
        return {"movies": {}, "shows": {}}

    if logger and (index["movies"] or index["shows"]):
        logger.log_debug(
            f"[Intent] watchlist index: {len(index['movies'])} movie(s), "
            f"{len(index['shows'])} show(s) from {len(union)} Plex union entries + "
            f"{len(trakt_shows)} Trakt show(s) + {len(trakt_movies)} Trakt movie(s) + "
            f"{len(mal_map.get('shows') or [])} MAL show(s) + "
            f"{len(mal_map.get('movies') or [])} MAL movie(s).")
    return index


def intent_memo_fingerprint(index) -> list:
    """A stable, compact digest of the intent index for the two score-memo CONTEXT hashes.

    CRITICAL, and the thing that silently breaks if skipped: the watchlist union is a
    per-PASS household input that appears in NEITHER memo's per-row key — Radarr hashes the
    parquet row, Sonarr hashes an explicit ``_SCORE_COLS`` list, and the watchlist is in
    neither. Without this in the context hash, adding a title to your watchlist would never
    invalidate its memoized score and A5 would appear to do nothing.

    Digests the SCORE-RELEVANT fields only (sources / member count / listing dates /
    anchor), so a cosmetic change to the union — a re-ordered ``watchlisted_by``, a retitled
    entry — does not force a needless full rescore."""
    out: list = []
    for media in ("movies", "shows"):
        for key in sorted((index or {}).get(media) or {}):
            ent = index[media][key] or {}
            out.append([media, key, list(ent.get("sources") or ()),
                        len(ent.get("members") or ()),
                        sorted((ent.get("dated") or {}).items()),
                        ent.get("anchor")])
    return out

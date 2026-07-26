"""
universe_membership.py — tagless universe membership for the Radarr universe QUALITY pass.
================================================================================
The universe quality manager (`quality/universe.py`) historically activated only when the
operator hand-created ``keep-universe*`` tags in Radarr — public-release users won't tag,
so the pass sat inactive. Universe knowledge already exists TAGLESS in three places this
module reads instead:

  1. TMDB collections — every Radarr movie dict carries ``collection`` (``title`` on
     Radarr v4/v5, ``name`` on v3; both are read — v4+ payloads have NO ``name`` key,
     which is why the Parquet ``collection_name`` column can be empty even when the
     library is full of collections).
  2. Learned franchise maps — the playlist builders persist Kometa-taught franchises at
     ``plex/playlists/kometa_franchises`` (Kometa as a one-time teacher). The show pass
     writes ``shows`` (tvdb ids); the movie builder's ``_plex_collection_order`` learns
     each recognised Plex UNIVERSE collection's owned membership and persists it as the
     entries' ``movies`` list (tmdb ids, via ``_persist_kometa_movie_franchises``) —
     which :func:`gather_derived_maps` reads here.
  3. MDBList universe lists — the fetched saga lists cached at
     ``plex/playlists/universe_source`` (movies keyed by tmdb, in saga order).

Modes (top-level config key ``universe_membership``):
  * ``"tags"``    (DEFAULT) — byte-identical to historical behavior: membership comes
                  ONLY from Radarr tags (classification.keep_policy). No new logs.
  * ``"derived"`` — tags are IGNORED for membership; the derived sources above decide.
                  ``keep-universe*`` tags are still honored for PROTECTION (the
                  never-delete pin), and keep/keep-movie tags keep their policies.
  * ``"hybrid"``  — tags win where present; derived sources fill untagged movies.
                  The intended release mode.

PRECEDENCE (per movie): explicit *arr tags > TMDB collection > learned franchise maps >
MDBList universe lists.

POLICY INVARIANT (Robert's rule, enforced structurally + tested): only an explicit
``keep-universe*`` TAG can yield ``keep=True`` / ``keep_policy="keep_universe"`` (never
deleted; quality-change only). DERIVED membership is ALWAYS bare-universe semantics —
``keep_policy="universe"``: grouping, saga credit, the universe quality ladder and
downgrade-first eligibility, but deletable as an absolute last resort. Auto-derived data
must never permanently protect content.

Naming: derived labels are normalized consistently with the existing saga naming —
trailing " Collection" stripped, whitespace collapsed, casefolded, placeholders
(PLACEHOLDER_AFFINITY: "universe"/"franchise"/…) dropped so they can never fuse
unrelated films into one bogus group (a placeholder label falls through to the next
source). Multi-universe membership pipe-joins sorted keys ("dc|mcu"), matching
``classification.keep_policy``.

PURE except :func:`gather_derived_maps` (the one global_cache reader, best-effort).
"""
from __future__ import annotations

import re

from scripts.managers.machine_learning.classification.keep_policy import build_keep_policy_map
from scripts.managers.machine_learning.playlists.models import PLACEHOLDER_AFFINITY

MODE_TAGS    = "tags"
MODE_DERIVED = "derived"
MODE_HYBRID  = "hybrid"
VALID_MODES  = (MODE_TAGS, MODE_DERIVED, MODE_HYBRID)

SOURCE_TAG        = "tag"
SOURCE_COLLECTION = "collection"
SOURCE_FRANCHISE  = "franchise"
SOURCE_MDBLIST    = "mdblist"

# Cache keys of the tagless universe knowledge (written by the plex playlist builders).
UNIVERSE_SOURCE_KEY  = "plex/playlists/universe_source"
KOMETA_FRANCHISE_KEY = "plex/playlists/kometa_franchises"


def membership_counts_key(instance: str) -> str:
    """global_cache key for the per-instance membership counts blob the audit (and a
    future GUI) reads — written by RadarrCacheMovieFilesManager on refresh in
    derived/hybrid modes. Single source of truth for writer + readers."""
    return f"radarr/{instance}/universe_membership"


def membership_mode(config) -> str:
    """The effective membership mode from the top-level ``universe_membership`` config
    key. Unknown/absent values fall back to ``"tags"`` (the byte-identical default) —
    fail SAFE, never fail ACTIVE."""
    try:
        raw = str((config or {}).get("universe_membership", MODE_TAGS) or MODE_TAGS)
    except Exception:
        return MODE_TAGS
    raw = raw.strip().lower()
    return raw if raw in VALID_MODES else MODE_TAGS


_WS = re.compile(r"\s+")
_COLLECTION_SUFFIX = " collection"


def normalize_universe_label(name) -> "str | None":
    """A collection title / franchise key → a grouping token consistent with the
    existing saga naming: trailing " Collection" stripped ('The Conjuring Collection'
    → 'the conjuring'), whitespace collapsed, casefolded. Placeholder labels
    (PLACEHOLDER_AFFINITY — the junk bare "universe"/"franchise"/"nan"/…) → None so
    they never fuse unrelated films into one bogus group. Short saga keys ('mcu',
    'one chicago') pass through unchanged."""
    s = str(name or "").strip()
    if not s:
        return None
    if s.casefold().endswith(_COLLECTION_SUFFIX):
        s = s[: -len(_COLLECTION_SUFFIX)].strip()
    s = _WS.sub(" ", s).casefold()
    return s if s and s not in PLACEHOLDER_AFFINITY else None


def _tmdb_int(movie: dict) -> "int | None":
    t = movie.get("tmdbId", movie.get("tmdb_id"))
    try:
        return int(t)
    except (TypeError, ValueError):
        return None


def _movie_collection_label(movie: dict, tmdb_collections=None) -> "str | None":
    """The movie's normalized TMDB-collection label. Radarr v3 payloads call the field
    ``collection.name``; v4/v5 call it ``collection.title`` — read both. Falls back to
    an optional ``tmdb_collections`` map ({tmdb_id: collection name}, e.g. rebuilt from
    a Parquet) when the movie dict itself carries none."""
    coll = movie.get("collection") or {}
    raw = coll.get("title") or coll.get("name") if isinstance(coll, dict) else None
    if not raw and tmdb_collections:
        t = _tmdb_int(movie)
        if t is not None:
            raw = tmdb_collections.get(t)
    return normalize_universe_label(raw)


def _map_labels(mapping, tmdb: "int | None") -> "str | None":
    """Normalized, sorted, pipe-joined labels for ``tmdb`` from a
    ``{tmdb_id: key | iterable-of-keys}`` map; None when absent/placeholder-only."""
    if tmdb is None or not mapping:
        return None
    raw = mapping.get(tmdb)
    if raw is None:
        return None
    keys = raw if isinstance(raw, (set, frozenset, list, tuple)) else [raw]
    cleaned = sorted({lbl for k in keys if (lbl := normalize_universe_label(k)) is not None})
    return "|".join(cleaned) if cleaned else None


def _derived_membership(movie, *, tmdb_collections=None, franchise_maps=None,
                        mdblist_maps=None) -> "tuple[str | None, str | None]":
    """``(universe_name, source)`` from the TAGLESS sources in precedence order —
    TMDB collection > learned franchise maps > MDBList lists; ``(None, None)`` when no
    source knows the movie. A placeholder-normalized label falls through to the next
    source rather than producing a junk group."""
    label = _movie_collection_label(movie, tmdb_collections)
    if label is not None:
        return label, SOURCE_COLLECTION
    tmdb = _tmdb_int(movie)
    label = _map_labels(franchise_maps, tmdb)
    if label is not None:
        return label, SOURCE_FRANCHISE
    label = _map_labels(mdblist_maps, tmdb)
    if label is not None:
        return label, SOURCE_MDBLIST
    return None, None


def _compose(tag_policy, tag_name, derived_name, derived_src, mode) -> "tuple[str | None, str | None, str | None, bool]":
    """The per-movie mode adjudication → ``(policy, universe_name, source, keep)``.

    Rules (see module docstring):
      * ``keep=True`` ⟺ the TAG layer resolved ``keep_universe`` — the ONLY path.
      * tags mode  → tag layer verbatim (byte-identical to build_keep_policy_map).
      * hybrid     → any tag-resolved universe membership wins whole; derived fills
                     movies with no tag-resolved universe_name (policy fills only when
                     the tag layer resolved NO policy → bare "universe"; keep/keep-movie
                     rows keep their policy but may gain a derived name for grouping).
      * derived    → derived sources decide membership. ``keep-universe*`` tags keep the
                     PROTECTION pin (policy stays keep_universe; the tag-derived name is
                     kept only when nothing derived covers the movie). A bare "universe"
                     tag is ignored for membership. keep/keep-movie policies unaffected.
    """
    keep_tag = tag_policy == "keep_universe"
    if mode == MODE_TAGS:
        src = SOURCE_TAG if tag_name is not None else None
        return tag_policy, tag_name, src, keep_tag

    if mode == MODE_HYBRID:
        if tag_name is not None:                       # tags win where present
            return tag_policy, tag_name, SOURCE_TAG, keep_tag
        if derived_name is not None:                   # derived fills the gaps
            policy = tag_policy if tag_policy is not None else "universe"
            return policy, derived_name, derived_src, False
        return tag_policy, None, None, keep_tag        # nothing knows this movie

    # MODE_DERIVED — tags ignored for membership; keep-universe pin still honored.
    if keep_tag:
        if derived_name is not None:
            return "keep_universe", derived_name, derived_src, True
        return "keep_universe", tag_name, SOURCE_TAG, True
    if derived_name is not None:
        policy = tag_policy if tag_policy in ("keep_forever", "keep_movie") else "universe"
        return policy, derived_name, derived_src, False
    # No derived source: a bare "universe"/hint tag does NOT grant membership here.
    policy = tag_policy if tag_policy in ("keep_forever", "keep_movie") else None
    return policy, None, None, False


def resolve_universe_membership(movie: dict, *, tags_map=None, tmdb_collections=None,
                                franchise_maps=None, mdblist_maps=None,
                                mode: str = MODE_TAGS) -> dict:
    """Resolve ONE movie's universe membership → ``{"universe_name", "source", "keep",
    "policy"}``.

    ``tags_map`` is the Radarr ``{tag_id: label}`` catalogue (the tag layer delegates to
    ``classification.keep_policy.build_keep_policy_map`` so tag semantics are identical
    to the Parquet refresh). ``tmdb_collections``/``franchise_maps``/``mdblist_maps``
    are the derived sources (see :func:`_derived_membership`).

    PRECEDENCE: explicit tags (the only source that can yield ``keep=True``) > TMDB
    collection > learned franchise maps > MDBList. Derived membership is ALWAYS
    ``keep=False`` (bare-universe semantics — deletable as last resort)."""
    mode = mode if mode in VALID_MODES else MODE_TAGS
    policy_map, name_map = build_keep_policy_map([movie], tags_map or {})
    mid = movie.get("id")
    tag_policy, tag_name = policy_map.get(mid), name_map.get(mid)
    derived_name, derived_src = (None, None)
    if mode != MODE_TAGS:
        derived_name, derived_src = _derived_membership(
            movie, tmdb_collections=tmdb_collections,
            franchise_maps=franchise_maps, mdblist_maps=mdblist_maps)
    policy, name, source, keep = _compose(tag_policy, tag_name, derived_name, derived_src, mode)
    return {"universe_name": name, "source": source, "keep": keep, "policy": policy}


def build_membership_maps(movies, tag_label_map, *, mode: str = MODE_TAGS,
                          tmdb_collections=None, franchise_maps=None, mdblist_maps=None):
    """The LIST-form twin of :func:`resolve_universe_membership` for the Parquet
    refresh → ``(policy_map, universe_name_map, counts)``.

    ``mode="tags"`` delegates the whole list VERBATIM to
    ``classification.keep_policy.build_keep_policy_map`` — byte-identical maps by
    construction (same function, same arguments). Other modes run the tag layer once,
    then adjudicate per movie via the same :func:`_compose` core the single-movie
    resolver uses.

    ``counts``: {"mode", per-source membership counts ("tag"/"collection"/"franchise"/
    "mdblist"), "members" (rows with a universe_name), "keep_universe" (tag-pinned,
    never deleted), "bare_universe" (deletable last resort)} — the numbers behind the
    one-line per-instance log and the audit."""
    mode = mode if mode in VALID_MODES else MODE_TAGS
    tag_policy_map, tag_name_map = build_keep_policy_map(movies, tag_label_map)
    if mode == MODE_TAGS:
        policy_map, universe_name_map = tag_policy_map, tag_name_map
        counts = {"mode": mode, SOURCE_TAG: 0, SOURCE_COLLECTION: 0, SOURCE_FRANCHISE: 0,
                  SOURCE_MDBLIST: 0, "members": 0, "keep_universe": 0, "bare_universe": 0}
        for mid, name in universe_name_map.items():
            if name is not None:
                counts[SOURCE_TAG] += 1
                counts["members"] += 1
        for pol in policy_map.values():
            if pol == "keep_universe":
                counts["keep_universe"] += 1
            elif pol == "universe":
                counts["bare_universe"] += 1
        return policy_map, universe_name_map, counts

    policy_map: dict = {}
    universe_name_map: dict = {}
    counts = {"mode": mode, SOURCE_TAG: 0, SOURCE_COLLECTION: 0, SOURCE_FRANCHISE: 0,
              SOURCE_MDBLIST: 0, "members": 0, "keep_universe": 0, "bare_universe": 0}
    for movie in movies:
        mid = movie.get("id")
        derived_name, derived_src = _derived_membership(
            movie, tmdb_collections=tmdb_collections,
            franchise_maps=franchise_maps, mdblist_maps=mdblist_maps)
        policy, name, source, _keep = _compose(
            tag_policy_map.get(mid), tag_name_map.get(mid), derived_name, derived_src, mode)
        policy_map[mid] = policy
        universe_name_map[mid] = name
        if name is not None and source is not None:
            counts[source] = counts.get(source, 0) + 1
            counts["members"] += 1
        if policy == "keep_universe":
            counts["keep_universe"] += 1
        elif policy == "universe":
            counts["bare_universe"] += 1
    return policy_map, universe_name_map, counts


def gather_derived_maps(global_cache) -> dict:
    """Read the TAGLESS universe knowledge that already exists in the cache →
    ``{"franchise_maps": {tmdb: set(keys)}, "mdblist_maps": {tmdb: set(keys)}}``.

    * mdblist: ``plex/playlists/universe_source`` via ``universe_order.saga_member_sets``
      (full, ownership-independent membership — the same reader the catch-up retention
      gate and saga credit use). Show-only entries (``tvfran:*``) contribute nothing to
      the movie maps by construction.
    * franchise: ``plex/playlists/kometa_franchises`` — reads the OPTIONAL ``movies``
      list on each learned entry. The movie playlist builder's ``_plex_collection_order``
      persists these (owned members of the operator's recognised Plex universe
      collections, tmdb ids); the show pass writes only ``shows``, which contributes
      nothing here. Installs with no Plex universe collections leave this map empty.

    Best-effort: any missing cache / import failure degrades to empty maps (the
    resolver then falls back to TMDB collections alone), never raises."""
    fran: dict = {}
    mdb: dict = {}
    if global_cache is None:
        return {"franchise_maps": fran, "mdblist_maps": mdb}
    try:
        # Lazy import (mirrors space_pressure's saga readers): keeps this module's
        # import cost tiny and avoids any service-package import cycles.
        from scripts.managers.services.plex.playlists.universe_order import saga_member_sets
        source = global_cache.get(UNIVERSE_SOURCE_KEY) or {}
        for key, sets in saga_member_sets(source).items():
            for tmdb in (sets.get("movies") or {}):
                try:
                    mdb.setdefault(int(tmdb), set()).add(str(key))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    try:
        learned = global_cache.get(KOMETA_FRANCHISE_KEY) or {}
        for key, ent in (learned.items() if isinstance(learned, dict) else []):
            for tmdb in ((ent or {}).get("movies") or []) if isinstance(ent, dict) else []:
                try:
                    fran.setdefault(int(tmdb), set()).add(str(key))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    return {"franchise_maps": fran, "mdblist_maps": mdb}

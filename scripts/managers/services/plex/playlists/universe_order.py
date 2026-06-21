"""
plex/playlists/universe_order.py — saga (timeline) order + TV franchise grouping, derived
from Kometa Plex universe collections (read IN COLLECTION ORDER) with a curated TV fallback.
================================================================================
The playlist brain orders a universe/franchise block by ``timeline_index`` (which OVERRIDES
release/air date) and groups TV siblings by ``franchise``. Neither is populated by default —
this module produces the maps the resolvers consume:

  • movies  → ``{tmdb_id: position}``      (fed to ``movie_inputs(universe_order=…)``)
  • TV      → ``{series_id: franchise}`` + ``{series_id: position}``
              (fed to ``tv_inputs(franchise_by_series=…, series_timeline=…)``)

Two sources, merged with Kometa winning:
  1. KOMETA — the operator's "Universe Collections" in Plex. We match a collection's title to
     a universe key (``UNIVERSE_COLLECTION_NAMES``), then read its children in collection order
     (Kometa preserves the IMDb/mdblist list order it was built from = the saga order). Covers
     the film universes the operator already maintains.
  2. CURATED — ``CURATED_TV_FRANCHISES``: sibling TV shows NOT expressible as a single Kometa
     film-universe collection (One Chicago, Law & Order, Doctor Who, …). Editable; keyed by
     series TITLE (a trailing ``(year)`` is ignored) so it resolves against any install.

IMPORTANT — Kometa "universe" vs "franchise" (verified from a real meta.log + parquet):
Kometa runs TWO collection defaults. (a) ``universe`` (mcu, star, trek, …) is IMDb/mdblist-
sourced with a CUSTOM order ⇒ this is the only source of in-universe TIMELINE order, and the
only thing this module reads for ``timeline_index``. (b) ``franchise`` is per-TMDB-collection
with ``collection_order: release`` and tags BOTH Radarr (``item_radarr_tag: <<key>>, franchise``)
AND Sonarr (``item_sonarr_tag``). For MOVIES the franchise ``<<key>>`` IS the ``collection_tmdb_id``
glidearr already groups on (e.g. 2 Fast 2 Furious → tag ``9485`` == its collection id) and is
release-ordered, so franchise collections add NOTHING for movies — we never read them. For TV,
franchise/universe collections also exist as SHOW-library Plex collections; reading those gives
sibling-show GROUPING (franchise token = collection title), but — being release-ordered — only
universe (custom-order) show collections carry a saga ``timeline_index``; a franchise show
collection contributes grouping only (air-date handles its order). (glidearr does not cache
Sonarr series tags today, so the Plex show collection is the read path, not the Sonarr tag.)

PURE — no I/O. The manager fetches collections + ordered children + the owned inventories and
passes plain dicts/lists here; everything below is deterministic + unit-testable.
"""
from __future__ import annotations

import re
import unicodedata

from scripts.managers.services.plex.playlists.movie_resolver import _coerce_int, _coll_key

# Kometa "Universe Collections" standard mapping (collection display name → universe key, the
# same key Radarr tags as ``universe_name``). Match a Plex collection title against this to
# learn which universe it represents. Casefold keys; extend if the operator renames a universe.
UNIVERSE_COLLECTION_NAMES = {
    "alien / predator": "avp",
    "arrowverse": "arrow",
    "view askewniverse": "askew",
    "conjuring universe": "conjuring",
    "dc animated universe": "dca",
    "dc universe": "dcu",
    "fast & furious": "fast",
    "in association with marvel": "marvel",
    "marvel cinematic universe": "mcu",
    "middle earth": "middle",
    "rocky / creed": "rocky",
    "star trek": "trek",
    "star wars universe": "star",
    "mummy universe": "mummy",
    "wizarding world": "wizard",
    "x-men universe": "xmen",
}

# Curated TV franchises that span SEPARATE Sonarr series and aren't in the Kometa film-universe
# config. Each value is the sibling shows in saga / debut order → their list position becomes the
# ``timeline_index`` that orders the series blocks. Matched by normalized series TITLE (a trailing
# "(year)" suffix is ignored). EDIT THIS to add franchises or fix a title for your library.
CURATED_TV_FRANCHISES: dict[str, list[str]] = {
    "one chicago": ["Chicago Fire", "Chicago P.D.", "Chicago Med", "Chicago Justice"],
    "law & order": [
        "Law & Order", "Law & Order: Special Victims Unit",
        "Law & Order: Criminal Intent", "Law & Order: Trial by Jury",
        "Law & Order: LA", "Law & Order: Organized Crime",
    ],
    "fbi": ["FBI", "FBI: Most Wanted", "FBI: International"],
    "ncis": ["NCIS", "NCIS: Los Angeles", "NCIS: New Orleans", "NCIS: Hawai'i", "NCIS: Sydney"],
    "doctor who": ["Doctor Who"],   # classic + revival usually one Sonarr series each; both match
}

# ── Layer 1: same-name TV-franchise clustering (runtime, owned inventory) ─────────────
# Auto-groups SAME-named sibling shows from the owned Sonarr titles alone (no baked list, no
# network) — the cross-named families (Grey's↔Station 19) come from the generated catalog (Layer 2).
# Two signals: (a) a shared SUBTITLE STEM — "X" / "X: Sub" (Law & Order, NCIS, CSI, 9-1-1, Star Trek);
# (b) a distinctive shared LEADING TOKEN for the no-subtitle class (Chicago Fire/Med/P.D.). Kept
# SEPARATE from ``_norm`` (whose year-strip semantics are locked by tests). PURE.
_SUBTITLE_DELIMS = re.compile(r"\s*[:–—]\s+|\s+-\s+")   # "X: Sub" (no space before colon) | " – " | " — " | " - " (hyphen needs BOTH spaces so 'Spider-Man'/'9-1-1' survive)
_LEAD_STOPWORDS = {"the", "a", "an", "american", "new", "young", "untitled", "los", "la"}
# Same leading token / stem but DIFFERENT franchise → never merge (regional remakes etc.). Covers
# both the stem form ('theoffice') and the leading-token form ('office') so either title shape is safe.
_FRANCHISE_DENY: set = {"theoffice", "office", "shameless", "skins", "thebridge", "beinghuman"}


def _stem_norm(s) -> str:
    """The SUBTITLE STEM key: accent-folded, lowercased head BEFORE the first subtitle delimiter,
    stripped to ``[a-z0-9]`` — so 'Law & Order: SVU'→'laworder', '9-1-1: Lone Star'→'911',
    "NCIS: Hawai'i"→'ncis'. A bare 'Chicago Fire' (no delimiter) →'chicagofire' (see _lead_token)."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    head = _SUBTITLE_DELIMS.split(s, 1)[0]
    return re.sub(r"[^a-z0-9]", "", head)


def _lead_token(s) -> "str | None":
    """The first distinctive word (skips a stop-word lead, needs ≥3 chars) — the franchise marker for
    the no-subtitle sibling class: 'Chicago Fire'/'Chicago Med'→'chicago'. None if nothing distinctive."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for w in re.findall(r"[a-z0-9]+", s):
        if len(w) >= 3 and w not in _LEAD_STOPWORDS:
            return w
    return None


def stem_franchise_clusters(series_rows, *, deny=None) -> dict:
    """Owned Sonarr series → ``{franchise_key: [tvdb…]}`` for SAME-named families of ≥2 owned members.
    ``series_rows`` is ``[{title, tvdbId|tvdb, id?}]``. A series joins a cluster by shared subtitle
    STEM first; the remaining singletons then cluster by shared distinctive LEADING TOKEN (the Chicago
    case). De-duped by tvdb; clusters of one are dropped; DENY keys never form a cluster. PURE — no
    I/O, deterministic (members in input order). The cross-named catalog (Layer 2) overlays this."""
    denyset = set(_FRANCHISE_DENY) | set(deny or ())
    rows = []
    for r in (series_rows or []):
        tv = _coerce_int(r.get("tvdbId") if "tvdbId" in r else r.get("tvdb"))
        title = r.get("title") or r.get("series_title")
        if tv is not None and title:
            rows.append((title, tv))

    by_stem: dict = {}
    for title, tv in rows:
        key = _stem_norm(title)
        if key and key not in denyset:
            by_stem.setdefault(key, [])
            if tv not in by_stem[key]:
                by_stem[key].append(tv)
    clustered = {tv for members in by_stem.values() if len(members) >= 2 for tv in members}

    by_lead: dict = {}
    for title, tv in rows:
        if tv in clustered:                       # already in a subtitle-stem cluster
            continue
        lead = _lead_token(title)
        if lead and lead not in denyset:
            by_lead.setdefault(lead, [])
            if tv not in by_lead[lead]:
                by_lead[lead].append(tv)

    out: dict = {}
    for key, members in by_stem.items():
        if len(members) >= 2:
            out[f"tvfran:{key}"] = list(members)
    for lead, members in by_lead.items():
        if len(members) >= 2:
            out.setdefault(f"tvfran:{lead}", list(members))
    return out


def tv_franchise_universes(owned_series_rows, catalog, *, engaged_tvdbs=None,
                           deny_tvdbs=None, cluster_same_stem=True) -> dict:
    """Synthetic ``tvfran:`` universe-source entries — the SINGLE seam that makes discovered TV
    franchises participate in playlist grouping, catch-up retention AND acquisition. Merges Layer-2
    (the baked/generated tvdb-keyed ``catalog`` — cross-named families + the migrated curated
    same-name ones) with Layer-1 (owned same-stem clusters, :func:`stem_franchise_clusters`),
    namespaced ``tvfran:<key>``. Deconflicted: a catalog franchise's full membership SUPERSEDES the
    owned-only Layer-1 cluster of the same family, and a family a film universe (``deny_tvdbs`` —
    Arrowverse, Star Trek…) already groups is dropped — so no family is ever grouped under two keys.

    ``owned_series_rows`` — ``[{title|series_title, tvdbId|tvdb|series_tvdb_id, year?,
    tvdb_first_aired?}]``. Each emitted value is shaped EXACTLY like :func:`split_list_media` output
    so every existing consumer reads it unchanged::

        {"tvfran:laworder": {"timeline": True, "movies": [],
                             "shows": [tvdb…],                              # debut-ordered
                             "items": [{"media": "show", "tvdb": tvdb, "rank": i}…]}}

    ``timeline`` is **True**, NOT False: :func:`unified_universe_order` SKIPS non-timeline universes
    (so the acquisition backfill would never see a franchise's gaps), and ``build_universe_maps``
    only stamps a series order for timeline universes — both of which we want. Members are ordered by
    debut (``tvdb_first_aired`` → ``year`` → stable input order, undated last) so the saga rank the
    retention watchlist-prefix scoping relies on is defensible. ``shows`` and ``items`` share that
    order. ``deny_tvdbs`` = tvdbs a film universe already groups. PURE — no I/O; ``catalog={}`` makes
    the Layer-2 branch a no-op (Layer-1 clustering still runs)."""
    clusters = stem_franchise_clusters(owned_series_rows) if cluster_same_stem else {}

    # Debut + first-seen index per tvdb (for member ordering). Undated rows → stable input order.
    debut: dict = {}
    first_index: dict = {}
    for i, r in enumerate(owned_series_rows or []):
        tv = _coerce_int(r.get("tvdbId") if "tvdbId" in r
                         else (r.get("tvdb") if "tvdb" in r else r.get("series_tvdb_id")))
        if tv is None:
            continue
        first_index.setdefault(tv, i)
        d = r.get("tvdb_first_aired") or r.get("year")
        if d is not None and tv not in debut:
            debut[tv] = str(d)

    def _debut_ordered(members):
        # (undated last, debut asc, original order) — a stable, defensible debut order.
        return sorted(members, key=lambda tv: (tv not in debut, debut.get(tv, ""), first_index.get(tv, 0)))

    def _entry(ordered):
        return {"timeline": True, "movies": [], "shows": list(ordered),
                "items": [{"media": "show", "tvdb": tv, "rank": i} for i, tv in enumerate(ordered)]}

    # The household ENGAGEMENT scope for catalog franchises: tvdbs it OWNS (from the rows) PLUS any
    # extra engaged tvdbs the caller supplies — watchlisted shows (intent to watch), which may be
    # UNOWNED. (``watched`` ⊆ ``owned`` for TV: the watched signal is derived from the owned episode
    # parquet, so an owned-or-watchlisted scope already covers watched-or-owned-or-watchlisted.)
    scope = set(first_index)
    for x in (engaged_tvdbs or ()):
        xi = _coerce_int(x)
        if xi is not None:
            scope.add(xi)
    deny = {di for di in (_coerce_int(x) for x in (deny_tvdbs or ())) if di is not None}

    out: dict = {}
    catalog_shows: set = set()                                # tvdbs a catalog franchise already claims

    # Layer-2 catalog FIRST (canonical, full membership). Cross-named families the same-stem clusterer
    # can't derive (Grey's↔Station 19, Buffy↔Angel) AND the migrated curated same-name families (One
    # Chicago, Law & Order, NCIS) — now tvdb-keyed so they reach retention + acquisition, not just the
    # playlist. Emitted in FULL (incl. UNOWNED siblings, so acquisition backfills them start-first) but
    # ONLY when the household is ENGAGED — owns OR has watchlisted ≥1 member — and NOT when a film
    # universe (``deny_tvdbs`` — Arrowverse, Star Trek…) already groups the family (no double-grouping).
    for key, entry in (catalog or {}).items():
        k = key if str(key).startswith("tvfran:") else f"tvfran:{key}"
        raw = entry.get("shows") if isinstance(entry, dict) else entry
        shows = [t for t in (_coerce_int(tv) for tv in (raw or [])) if t is not None]
        if not shows or not any(s in scope for s in shows):
            continue
        if deny and any(s in deny for s in shows):           # film universe already covers it
            continue
        if any(s in catalog_shows for s in shows):           # another catalog franchise already claims a
            continue                                         # member → first-wins (floor before generated)
        out[k] = _entry(shows)
        catalog_shows.update(shows)

    # Layer-1 owned-stem clusters — SKIP any whose members a catalog franchise already covers (the
    # catalog's full membership SUPERSEDES the owned-only auto-cluster, so a family is never grouped
    # under two keys) or that a film universe covers.
    for key, members in clusters.items():
        if any(m in catalog_shows for m in members) or (deny and any(m in deny for m in members)):
            continue
        out[key] = _entry(_debut_ordered(members))
    return out

# Bundled list DEFINITIONS — Kometa's own universe→list map (the IMDb/mdblist lists it builds
# from), copied here so glidearr can fetch the SAME source ITSELF and is no longer reliant on a
# Kometa run having created the Plex collections. This is the only STATIC piece (changes only when
# a NEW universe appears); per-FILM updates come from re-fetching the list contents at runtime.
# ``timeline`` marks lists whose order is true in-universe chronology (→ drives timeline_index);
# flip to False for a list that's merely release-ordered (→ grouping only, dates order it). Extend
# or override per install via ``plex.playlists.universe_lists`` in config.json (no image rebuild).
UNIVERSE_LISTS: dict[str, dict] = {
    # Public mdblist lists (numeric id or user/slug), fetched IN LIST ORDER and content-verified
    # against the live API. The prior IMDb-list refs (ls…) 404 — mdblist exposes no IMDb-list
    # endpoint — and the Kometa ``external/<n>`` numbers resolved to unrelated lists (anime, art
    # films), so every ref here was re-sourced. Override/extend per install via
    # ``plex.playlists.universe_lists`` in config.json (no image rebuild).
    "mcu":       {"id": 117444, "timeline": True},                 # MCU (timeline order)
    "star":      {"id": 119979, "timeline": True},                 # Star Wars: the Skywalker Saga
    "trek":      {"id": 102138, "timeline": True},                 # Star Trek (chronological)
    "xmen":      {"id": 92827,  "timeline": True},                 # X-Men universe
    "dcu":       {"id": 49433,  "timeline": True},                 # DC Extended Universe
    "arrow":     {"id": 140366, "timeline": True},                 # Arrowverse (TV)
    "avp":       {"id": 101434, "timeline": True},                 # Alien vs Predator
    "conjuring": {"id": 68164,  "timeline": True},                 # The Conjuring universe
    "fast":      {"id": 76743,  "timeline": True},                 # Fast & Furious
    "dca":       {"mdblist": "johnfawkes/dca", "timeline": True},  # DC Animated
    "middle":    {"id": 120168, "timeline": True},                 # Middle-earth (LOTR + Hobbit)
    "mummy":     {"id": 16827,  "timeline": True},                 # The Mummy / Scorpion King
    "rocky":     {"id": 49530,  "timeline": True},                 # Rocky & Creed
    "askew":     {"id": 80700,  "timeline": True},                 # View Askewniverse
    "wizard":    {"id": 159768, "timeline": True},                 # Wizarding World
}


def universe_lists(config_overrides: dict | None = None) -> dict:
    """The effective ``{universe_key: {imdb|mdblist|id, timeline}}`` map: bundled defaults MERGED
    with ``plex.playlists.universe_lists`` from config.json, so an operator adds a NEW universe (or
    re-points/disables one) with a single config line in the appdata volume — no image rebuild."""
    return {**UNIVERSE_LISTS, **(config_overrides or {})}


def split_list_media(items, timeline) -> dict:
    """A ``client.list_items()`` result → ``{"timeline": bool, "movies": [tmdb…], "shows": [tvdb…],
    "items": [{media, tmdb|tvdb, rank}…]}`` IN LIST ORDER.

    ``movies``/``shows`` are the legacy per-media views (unchanged). ``items`` is the UNIFIED
    single-axis view: films AND shows share one ``rank`` (their position in the source saga list),
    so a universe's movies and episodes can be interleaved on ONE timeline (MCU: …film… →
    WandaVision → Loki → …film…). ``rank`` reflects whatever order ``list_items`` produced.
    Purely additive — existing consumers (build_universe_maps, the resolvers) read movies/shows."""
    movies, shows, unified = [], [], []
    for it in (items or []):
        media = it.get("media")
        if media == "movie" and it.get("tmdb") is not None:
            movies.append(it["tmdb"])
            unified.append({"media": "movie", "tmdb": it["tmdb"], "rank": len(unified)})
        elif media == "show" and it.get("tvdb") is not None:
            shows.append(it["tvdb"])
            unified.append({"media": "show", "tvdb": it["tvdb"], "rank": len(unified)})
    return {"timeline": bool(timeline), "movies": movies, "shows": shows, "items": unified}


def is_stale(fetched_at, now_ordinal, ttl_days) -> bool:
    """True when a universe's cached contents are older than ``ttl_days`` (or never fetched).
    ``fetched_at``/``now_ordinal`` are Gregorian ordinals (``date.toordinal()``)."""
    if fetched_at is None:
        return True
    try:
        return (int(now_ordinal) - int(fetched_at)) >= max(0, int(ttl_days))
    except (TypeError, ValueError):
        return True


def build_universe_maps(source, owned_movie_tmdbs, owned_tvdb_to_sid):
    """The cached universe ``source`` (``{"universes": {key: {timeline, movies, shows}}}``)
    intersected with what the operator OWNS → the maps the resolvers consume:

        (movie_membership ``{tmdb: set(keys)}``, movie_order ``{tmdb: pos}``,
         series_franchise ``{series_id: key}``, series_timeline ``{series_id: pos}``)

    MEMBERSHIP is always produced — it drives universe GROUPING straight from the list, with NO
    Kometa ``universe_name`` tag required. ORDER maps only for ``timeline`` universes. Positions
    are dense 0-based over OWNED survivors IN LIST ORDER, so a newly-acquired film auto-slots at
    its list position. A title in two universes keeps ALL membership keys (so they bridge) and its
    FIRST universe's position."""
    owned_m = set(owned_movie_tmdbs or ())
    tvdb_sid = owned_tvdb_to_sid or {}
    movie_membership: dict = {}
    movie_order: dict = {}
    series_franchise: dict = {}
    series_timeline: dict = {}
    for key, data in ((source or {}).get("universes") or {}).items():
        timeline = bool(data.get("timeline", True))
        for pos, tmdb in enumerate(t for t in (data.get("movies") or []) if t in owned_m):
            movie_membership.setdefault(tmdb, set()).add(key)
            if timeline:
                movie_order.setdefault(tmdb, pos)
        seen, pos = set(), 0
        for tvdb in (data.get("shows") or []):
            sid = tvdb_sid.get(tvdb)
            if sid is None or sid in seen:
                continue
            seen.add(sid)
            series_franchise.setdefault(sid, key)
            if timeline:
                series_timeline.setdefault(sid, pos)
            pos += 1
    return movie_membership, movie_order, series_franchise, series_timeline


def unified_universe_order(source, owned_movie_tmdbs, owned_tvdb_to_sid, *, include_unowned=False):
    """A universe's films + shows on ONE saga axis: ``{universe_key: [{media, id, rank, owned}…]}``,
    densely re-ranked over the source's unified ``items`` list (:func:`split_list_media`). This is
    the cross-media order the per-media :func:`build_universe_maps` can't express (it numbers movies
    and shows on independent axes).

    ``id`` is source-native — ``tmdb`` for a movie, ``tvdb`` for a show — so the caller resolves
    owned→ratingKey/series_id (playlist) or acquires by id (Radarr by tmdb / Sonarr by tvdb). A
    movie is ``owned`` when its tmdb is in ``owned_movie_tmdbs``; a show when its tvdb is in
    ``owned_tvdb_to_sid``. ``include_unowned`` False (playlist) → only owned survivors; True
    (acquisition) → keeps the saga's gaps flagged ``owned=False`` so a walk sees what to grab next,
    in order. Only ``timeline`` universes get an order; a stale source lacking ``items`` yields none
    (caller falls back to per-media). De-duped by (media, id), first-wins. PURE."""
    owned_m = {t for t in (owned_movie_tmdbs or set()) if t is not None}
    owned_tv = set((owned_tvdb_to_sid or {}).keys())
    out: dict = {}
    for key, data in ((source or {}).get("universes") or {}).items():
        if not (isinstance(data, dict) and data.get("timeline")):
            continue
        seq: list = []
        seen: set = set()
        for it in data.get("items") or []:
            media = it.get("media")
            if media == "movie" and it.get("tmdb") is not None:
                ident, owned = ("movie", it["tmdb"]), it["tmdb"] in owned_m
            elif media == "show" and it.get("tvdb") is not None:
                ident, owned = ("show", it["tvdb"]), it["tvdb"] in owned_tv
            else:
                continue
            if (not owned and not include_unowned) or ident in seen:
                continue
            seen.add(ident)
            seq.append({"media": ident[0], "id": ident[1], "rank": len(seq), "owned": owned})
        if seq:
            out[key] = seq
    return out


def universe_acquire_plan(unified_order, watched_movie_tmdbs, watched_show_tvdbs):
    """For each ENGAGED universe, the UNOWNED members to acquire IN SAGA ORDER (ascending rank —
    the START first), so a saga fills from its beginning at higher priority than its middle.

    A universe is ENGAGED when the household has WATCHED ≥1 of its members (extend-only: never
    cold-start a saga nobody has touched). Once engaged ANYWHERE, the whole saga's gaps are
    acquire candidates, earliest-first — so watching Clone Wars (mid-saga) pulls Episodes I–III
    (the start) ahead of the rest, and watching Birds of Prey pulls Arrow up front. The actively
    watched show's CONTINUATION is left to the per-series next-episode walk; this only adds the
    start-first backfill of the other members.

    ``unified_order`` is :func:`unified_universe_order` with ``include_unowned=True``
    ({key: [{media,id,rank,owned}…]}). ``watched_*`` are the household-watched ids keyed like the
    unified items (movie→tmdb, show→tvdb). Returns ``{key: [{media,id,rank}…]}`` — unowned members
    of engaged universes, rank-ascending (acquire priority). PURE."""
    wm = set(watched_movie_tmdbs or set())
    ws = set(watched_show_tvdbs or set())
    out: dict = {}
    for key, members in (unified_order or {}).items():
        engaged = any(
            (m["media"] == "movie" and m["id"] in wm) or (m["media"] == "show" and m["id"] in ws)
            for m in members)
        if not engaged:
            continue
        gaps = [{"media": m["media"], "id": m["id"], "rank": m["rank"]}
                for m in members if not m.get("owned")]
        if gaps:
            out[key] = gaps                          # already rank-ascending: start before middle
    return out


def saga_member_sets(source) -> dict:
    """The cached universe ``source`` → ``{key: {"movies": {tmdb: rank}, "shows": {tvdb: rank}}}`` —
    the FULL, OWNERSHIP-INDEPENDENT membership of every saga, each member carrying its saga RANK
    (position in the unified cross-media ``items`` list when present, else movies-then-shows). Unlike
    :func:`build_universe_maps` (owned-only) this keeps EVERY listed member, so the catch-up
    retention gate counts engagement off a since-deleted or never-owned title, and the ranks let a
    watchlist-only hold scope to the PREFIX up to the watchlisted title. Includes timeline AND
    release-ordered universes (this is membership, not order). De-duped by id, first-wins. PURE."""
    out: dict = {}
    for key, data in ((source or {}).get("universes") or {}).items():
        if not isinstance(data, dict):
            continue
        movies: dict = {}
        shows: dict = {}
        items = data.get("items")
        if items:                                    # unified cross-media order → one shared rank axis
            rank = 0
            for it in items:
                media = it.get("media")
                if media == "movie" and it.get("tmdb") is not None:
                    movies.setdefault(it["tmdb"], rank); rank += 1
                elif media == "show" and it.get("tvdb") is not None:
                    shows.setdefault(it["tvdb"], rank); rank += 1
        else:                                        # legacy per-media lists → movies then shows
            for r, t in enumerate(data.get("movies") or []):
                if t is not None:
                    movies.setdefault(t, r)
            base = len(movies)
            for r, v in enumerate(data.get("shows") or []):
                if v is not None:
                    shows.setdefault(v, base + r)
        if movies or shows:
            out[key] = {"movies": movies, "shows": shows}
    return out


_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)\s*$")


def _norm(s) -> str:
    """Normalize a title for matching: drop a trailing ``(YYYY)``, strip, casefold."""
    return _YEAR_SUFFIX.sub("", str(s or "").strip()).casefold()


def collection_universe_key(title) -> str | None:
    """A Plex collection title → its universe key (``"mcu"`` …) or None if it isn't a known
    universe collection. Comparison is exact (after strip+casefold) against the registry."""
    return UNIVERSE_COLLECTION_NAMES.get(str(title or "").strip().casefold())


# Reverse of UNIVERSE_COLLECTION_NAMES (key -> a Title-Cased display name), built once. Used by
# the acquisition logs to print 'Marvel Cinematic Universe' instead of the bare 'mcu' key.
_UNIVERSE_KEY_TO_NAME = {v: k.title() for k, v in UNIVERSE_COLLECTION_NAMES.items()}


def saga_display_name(key) -> str:
    """A saga KEY → a human label for logs: 'mcu' -> 'Marvel Cinematic Universe' (reverse of
    ``UNIVERSE_COLLECTION_NAMES``), 'one chicago' -> 'One Chicago', 'tvfran:ncis' -> 'Ncis' (the
    auto-clustered TV-family prefix is stripped). An unmapped key is Title-Cased as-is. PURE."""
    k = str(key or "").strip()
    if not k:
        return ""
    if k in _UNIVERSE_KEY_TO_NAME:
        return _UNIVERSE_KEY_TO_NAME[k]
    if k.startswith("tvfran:"):
        k = k.split(":", 1)[1]
    return k.replace("_", " ").title()


def saga_membership_index(source) -> dict:
    """The cached universe ``source`` → ``{("movie", tmdb): [keys], ("show", tvdb): [keys]}`` — a
    REVERSE index from a title's native id to the saga key(s) it belongs to, so an acquisition add
    can be attributed to its saga(s). Built from :func:`saga_member_sets` (full, ownership-independent
    membership), so a recommendation add that happens to be an MCU film is recognisable. Ids are
    coerced to int (source ids are ints; lookups coerce too). PURE — no I/O."""
    out: dict = {}
    for key, sets in saga_member_sets(source).items():
        for tmdb in (sets.get("movies") or {}):
            try:
                out.setdefault(("movie", int(tmdb)), []).append(key)
            except (TypeError, ValueError):
                continue
        for tvdb in (sets.get("shows") or {}):
            try:
                out.setdefault(("show", int(tvdb)), []).append(key)
            except (TypeError, ValueError):
                continue
    return out


def movie_universe_keys(owned_movies) -> dict:
    """``{tmdb_id: set(universe_keys)}`` from each owned movie's Radarr ``universe_name``
    (pipe-split, placeholders dropped + casefolded — the SAME cleaning the resolver groups on).
    Lets the order producer stamp a saga position on a movie ONLY from the collection whose
    universe the movie actually belongs to, so a film in the MCU Plex collection but tagged only
    ``xmen`` never inherits an MCU index into its xmen group (the tags/collection-disagree case)."""
    out: dict = {}
    for mv in owned_movies or []:
        tmdb = _coerce_int(mv.get("tmdb_id"))
        if tmdb is None:
            continue
        keys = {k.lower() for raw in str(mv.get("universe_name") or "").split("|")
                if (k := _coll_key(raw)) is not None}
        if keys:
            out[tmdb] = keys
    return out


def movie_order_from_children(ordered_child_rks, rk_to_tmdb, allowed_tmdbs=None) -> dict:
    """A collection's children (Plex ratingKeys, IN COLLECTION ORDER) → ``{tmdb_id: position}``
    for the ones the operator OWNS (present in the inverted movie inventory). Positions are
    dense 0-based over the owned survivors, so an un-owned gap doesn't leave a hole. First
    occurrence wins (a duplicate ratingKey keeps its earliest position).

    ``allowed_tmdbs`` (optional set): when given, ONLY movies in it get a position — pass the
    set of owned tmdbs whose ``universe_name`` includes THIS collection's universe so a saga
    index is never stamped from a universe the movie doesn't belong to (review finding)."""
    inv = rk_to_tmdb or {}
    out: dict = {}
    pos = 0
    for rk in ordered_child_rks or []:
        tmdb = inv.get(str(rk))
        if tmdb is None or tmdb in out:
            continue
        if allowed_tmdbs is not None and tmdb not in allowed_tmdbs:
            continue
        out[tmdb] = pos
        pos += 1
    return out


def merge_movie_orders(orders) -> dict:
    """Merge per-collection ``{tmdb: pos}`` maps into one. Positions only matter WITHIN a
    universe group (each collection is one universe), so a flat merge is safe; on the rare
    overlap (a film in two universes) the later collection wins."""
    merged: dict = {}
    for o in orders or []:
        merged.update(o or {})
    return merged


def tv_franchise_maps(owned_series, curated: dict | None = None):
    """``owned_series``: iterable of ``(series_id, series_title)``. Returns
    ``({series_id: franchise}, {series_id: position})`` for series whose title matches the
    curated franchise map. Position is the show's index in its franchise's saga list, so the
    series blocks order by saga; an unmatched series is simply absent (per-series fallback)."""
    table = curated if curated is not None else CURATED_TV_FRANCHISES
    by_title: dict = {}
    for fran, titles in table.items():
        for pos, t in enumerate(titles):
            by_title.setdefault(_norm(t), (fran, pos))     # first definition wins on dup title
    franchise: dict = {}
    timeline: dict = {}
    for sid, title in owned_series or []:
        hit = by_title.get(_norm(title))
        if hit is not None:
            franchise[sid], timeline[sid] = hit
    return franchise, timeline


def tv_group_maps(owned_series, universe_source, tvdb_to_sid, *, curated=None):
    """The canonical ``({series_id: group_token}, {series_id: timeline_index})`` for TV, merged
    from the curated TV-franchise map + the fetched universe lists — the SINGLE producer both the
    playlist builder and the acquisition prefetch consume, so the two layers agree on grouping and
    order.

    ``owned_series``: iterable of ``(series_id, series_title)``. ``tvdb_to_sid``:
    ``{tvdb_id: series_id}``. ``universe_source``: the cached mdblist source dict
    (``{"universes": {key: {timeline, shows, ...}}}``).

    GROUPING — a series joins a group if a curated franchise title matches OR a universe list
    contains its tvdb (the LIST wins on conflict). ORDER — ``timeline_index`` is used whenever it
    is AVAILABLE: a curated TV franchise's saga position (One Chicago Fire=0, P.D.=1, …) OR a
    timeline-flagged universe's position. A series the LIST grouped WITHOUT a timeline (a
    release-ordered universe collection) drops any timeline → that group falls back to AIR DATE.
    So the walk's rule is simply: order by ``timeline_index`` if present, else air date — which is
    what :func:`...next_episode_planner.group_key_for_series` keys off. PURE."""
    c_fran, c_time = tv_franchise_maps(owned_series, curated)             # curated grouping + order
    _, _, l_fran, l_time = build_universe_maps(universe_source or {}, set(), tvdb_to_sid or {})
    # Precedence: a real mdblist universe list still wins over a curated name, but a SYNTHETIC
    # same-name cluster (``tvfran:…``, auto-derived from owned titles) YIELDS to the hand-named
    # curated franchise — so "One Chicago" / "Law & Order" keep their curated label + saga order in
    # the playlist even though the same family is also discovered by clustering (which still drives
    # acquisition + retention through the universe source). Phase 3 migrates curated into the catalog.
    franchise = dict(c_fran)
    for sid, key in l_fran.items():
        if sid not in franchise or not str(key).startswith("tvfran:"):
            franchise[sid] = key                                         # real list overrides; synthetic does not
    timeline = dict(c_time)                                               # curated saga positions…
    for sid, key in l_fran.items():                                       # …then the list source, but
        if franchise.get(sid) != key:                                    # only where the LIST grouping won
            continue                                                     # (curated kept this series → keep its order)
        if sid in l_time:
            timeline[sid] = l_time[sid]                                   # timeline universe → keep
        else:
            timeline.pop(sid, None)                                       # release-ordered → air date
    return franchise, timeline


def tv_group_maps_from_series(series_rows, universe_source, *, curated=None):
    """:func:`tv_group_maps` for callers holding RAW Sonarr series rows — dicts with ``id`` /
    ``title`` / ``tvdbId`` (e.g. the acquisition layer reading the series cache). Splits the rows
    into the ``(series_id, title)`` items + ``{tvdb: series_id}`` map :func:`tv_group_maps` needs,
    then delegates — so the acquisition prefetch and the playlist builder share ONE producer of
    grouping + order. First-seen wins on duplicate series_id / tvdb. PURE."""
    seen: dict = {}
    tvdb_to_sid: dict = {}
    for s in series_rows or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if sid is None:
            continue
        seen.setdefault(sid, s.get("title") or "")
        tv = s.get("tvdbId")
        try:
            tv = int(tv) if tv is not None else None
        except (TypeError, ValueError):
            tv = None
        if tv is not None:
            tvdb_to_sid.setdefault(tv, sid)
    return tv_group_maps(list(seen.items()), universe_source, tvdb_to_sid, curated=curated)


def series_order_from_children(ordered_child_rks, show_rk_to_series, franchise,
                               *, with_timeline: bool = True):
    """A Kometa show-library collection's children (SHOW ratingKeys, in collection order) →
    ``({series_id: franchise}, {series_id: position})``. ``show_rk_to_series`` maps a show's
    Plex ratingKey → owned Sonarr ``series_id`` (built by the manager from the episode
    inventory's ``grandparent_rating_key``). Owned survivors get dense 0-based positions.

    ``with_timeline`` distinguishes the two Kometa show-collection kinds: a UNIVERSE collection
    has a CUSTOM (saga) order ⇒ ``True`` (default), so the positions become ``timeline_index``.
    A FRANCHISE collection is ``collection_order: release`` ⇒ pass ``False`` to contribute
    GROUPING ONLY (empty timeline map), letting the brain order those series by air date."""
    inv = show_rk_to_series or {}
    fran_map: dict = {}
    time_map: dict = {}
    pos = 0
    for rk in ordered_child_rks or []:
        sid = inv.get(str(rk))
        if sid is not None and sid not in fran_map:
            fran_map[sid] = franchise
            if with_timeline:
                time_map[sid] = pos
            pos += 1
    return fran_map, time_map

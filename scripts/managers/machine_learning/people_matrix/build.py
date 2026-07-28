"""people_matrix/build.py — person↔media co-occurrence index (pure).
================================================================================
Turns the enrich-daemon's per-title credits dicts (which already carry a stable
``id = tmdb_person_id`` on every cast/crew member — see ``enrich_daemon.normalise_people``)
into a searchable people graph: an inverted ``person -> {titles}`` index plus a
role-segmented ``title -> {role: [person_id]}`` forward map. From those, the
"find every film with Scarlett Johansson AND Robert Downey Jr." query is a set
intersection over the inverted index — NO N×N matrix is ever materialised (at
~10-20k people that is 100-400M cells; the intersection is O(min(list))).

PURE: stdlib only — no I/O, no service imports, no global_cache. A service manager
reads the daemon people buckets (via ``TraktMovie/ShowCacheManager.get_people``) and
passes the decoded ``{cast, crew}`` dicts here, keyed by ``(medium, ext_id)``; the
returned structures are what the manager caches and the scorer/candidate layers read.

The role routing here MIRRORS ``factories/daemons/bucket_merge.flatten_trakt_people``
so the matrix classifies a person into the same bucket the display columns do — only
the captured field differs (``id`` here vs ``name`` there).
"""
from __future__ import annotations

from collections import defaultdict

# Role buckets, mirroring flatten_trakt_people's crew classification + the Group-B
# cast/crew split. Keys are the role names the forward map and person-affinity use.
# ``cinematographers`` / ``editors`` have no counterpart in flatten_trakt_people (the
# daemon's display columns stop at composers), but BOTH exist as first-class
# ``role_type`` values in the Radarr relational credits table and as
# ``cinematographer_names`` / ``editor_names`` columns on movie_files — so the graph
# carries every credited role the library actually records, not a subset.
ROLES: tuple[str, ...] = ("cast", "directors", "writers", "composers", "producers",
                          "cinematographers", "editors")

# Role weights for person-affinity (mirror the Group-B cap ratios: leads + director
# carry the signal; the below-the-line crafts are progressively weaker). SINGLE source
# for both the aggregation (affinity.genre_affinity.aggregate_person_affinity) and the
# scoring term (scoring._shared.person_affinity_score), so the two never drift.
#
# The ordering is the claim being made: a household re-watches a DIRECTOR's or a LEAD's
# work far more reliably than an EDITOR's. Editors sit lowest because editing credits
# are both the most numerous per title and the least predictive of a re-watch; a
# cinematographer's look is more identifiable, so DPs sit a rung above.
PERSON_ROLE_WEIGHTS: dict[str, float] = {
    "cast": 1.0, "directors": 1.0, "writers": 0.6, "composers": 0.4, "producers": 0.3,
    "cinematographers": 0.3, "editors": 0.2,
}

# Billing-order decay for CAST only: the n-th billed actor contributes
# ``1 / (1 + PERSON_BILLING_DECAY * n)`` of the cast role weight (rank 0 = 1.00,
# rank 1 = 0.80, rank 2 = 0.67 … rank 9 = 0.31). Crew roles are unordered — a film has
# one director, and "second credited writer" carries no billing semantics — so the
# decay is deliberately NOT applied to them.
#
# Why decay at all: without it the tenth-billed bit-part actor of a beloved film counts
# exactly as much as its star, which floods the affinity vector with people the
# household has no opinion about. The forward map already stores cast in billing order
# (route_people sorts by ``order`` before de-duping), so position IS the rank.
PERSON_BILLING_DECAY: float = 0.25

# Which forward-map roles carry billing semantics.
BILLED_ROLES: frozenset[str] = frozenset({"cast"})

# Radarr relational-credits ``role_type`` → forward-map role bucket. The relational
# table (radarr/<inst>/relational/movie_person_relations.parquet) is the ONLY library
# source that carries a stable ``person_tmdb_id`` AND a ``billing_order`` for all seven
# credited roles, which is why it — not the pipe-joined ``*_names`` columns on
# movie_files — is what the matrix is built from. C4 is id-keyed on purpose (immune to
# "Scarlett Johansson" vs alias drift); a name-keyed graph could not feed it at all.
RELATION_ROLE_TYPES: dict[str, str] = {
    "actor": "cast",
    "director": "directors",
    "writer": "writers",
    "composer": "composers",
    "producer": "producers",
    "cinematographer": "cinematographers",
    "editor": "editors",
}


def billing_weight(rank: int, *, decay: float = PERSON_BILLING_DECAY) -> float:
    """Multiplier for the ``rank``-th billed cast member (0 = top-billed).

    ``1 / (1 + decay * rank)`` — a gentle hyperbolic falloff rather than a cliff, so a
    third lead still counts substantially while a tenth-billed cameo does not dominate.
    Negative / non-int ranks fall back to 1.0 (treated as unbilled)."""
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return 1.0
    if r <= 0:
        return 1.0
    return 1.0 / (1.0 + max(0.0, float(decay)) * r)


def _ordered_unique_ids(members) -> list[int]:
    """tmdb person ids from credit members, in order, de-duped, dropping non-int /
    missing ids (a member whose source lacked a tmdb person id simply doesn't enter
    the graph — it still appears in the name-based display columns)."""
    out: list[int] = []
    for m in members:
        pid = m.get("id")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid not in out:
            out.append(pid)
    return out


def _route_members(credits: dict, *, cast_limit: int = 10) -> dict[str, list[dict]]:
    """Classify daemon credits ``{cast:[{name,id,order}], crew:[{name,id,job,department}]}``
    into role buckets of MEMBER dicts (names + ids intact). The SHARED step behind both
    :func:`route_people` (which keeps ids) and :func:`route_people_names` (which keeps
    names), so the crew-classification branch logic exists ONCE and the two never drift.
    Crew branches mirror ``flatten_trakt_people``; cast is capped at ``cast_limit`` after
    sorting by billing ``order`` (same as the display columns)."""
    credits = credits or {}
    cast = credits.get("cast") or []
    crew = credits.get("crew") or []

    cast_sorted = sorted(cast, key=lambda c: c.get("order", 9999))[:cast_limit]
    directors, producers, writers, composers = [], [], [], []
    cinematographers, editors = [], []
    for m in crew:
        job = m.get("job") or ""
        dept = (m.get("department") or "").lower()
        if job == "Director" or dept == "directing":
            directors.append(m)
        elif dept == "production" and "producer" in job.lower():
            producers.append(m)
        elif dept == "writing" or job in ("Screenplay", "Story", "Writer"):
            writers.append(m)
        elif job == "Original Music Composer":
            composers.append(m)
        # Two roles flatten_trakt_people does not model (it stops at composers) but the
        # relational credits table and movie_files DO record. Matched by department so
        # the localised job titles TMDB uses ("Director of Photography", "Cinematography")
        # all land in one bucket.
        elif dept == "camera" or job in ("Director of Photography", "Cinematography"):
            cinematographers.append(m)
        elif dept == "editing" or job == "Editor":
            editors.append(m)
    return {"cast": cast_sorted, "directors": directors,
            "writers": writers, "composers": composers, "producers": producers,
            "cinematographers": cinematographers, "editors": editors}


def route_people(credits: dict, *, cast_limit: int = 10) -> dict[str, list[int]]:
    """Daemon credits → ``{role: [tmdb_person_id]}`` for every role in :data:`ROLES`
    (ids only — the minimal forward-map payload). Crew classification + the cast cap come
    from :func:`_route_members`."""
    return {role: _ordered_unique_ids(members)
            for role, members in _route_members(credits, cast_limit=cast_limit).items()}


def route_people_names(credits: dict, *, cast_limit: int = 10) -> dict[int, str]:
    """The ``{tmdb_person_id: name}`` pairs for exactly the members :func:`route_people`
    admits to the graph (same cast cap + crew classification), captured BEFORE the id-only
    routing drops the name. The persisted union of these (see :func:`build_index`) is the
    id→name lookup the forward map can't carry — INFRASTRUCTURE for resolving a person id
    to a label; it is NOT consumed by any scorer (which is purely id-keyed) or logger."""
    out: dict[int, str] = {}
    for members in _route_members(credits, cast_limit=cast_limit).values():
        for m in members:
            pid, name = m.get("id"), m.get("name")
            if isinstance(pid, int) and not isinstance(pid, bool) and name:
                out.setdefault(pid, str(name))
    return out


def invert_forward(media_people_fwd: dict) -> dict:
    """Rebuild the ``{tmdb_person_id: set[(medium, ext_id)]}`` inverted index from a
    forward map ``{(medium, ext_id): {role: [pid]}}``. Lets a cached forward map (the
    minimal persisted artifact) regenerate the inverted index on load without storing
    it redundantly."""
    person_index: dict[int, set] = defaultdict(set)
    for key, roles in media_people_fwd.items():
        for pids in roles.values():
            for pid in pids:
                person_index[pid].add(key)
    return dict(person_index)


def build_index(media_people: dict, *, cast_limit: int = 10):
    """Build the people graph from ``{(medium, ext_id): credits_dict}``.

    ``medium`` is ``"movie"``/``"show"`` and ``ext_id`` is the tmdb_id (movie) /
    tvdb_id (show); the ``(medium, ext_id)`` tuple namespaces the two id-spaces so a
    movie tmdb 603 and a show tvdb 603 never collide.

    Returns ``(person_index, media_people_fwd, names)``:
      * ``person_index``     — ``{tmdb_person_id: set[(medium, ext_id)]}`` inverted index
      * ``media_people_fwd`` — ``{(medium, ext_id): {role: [tmdb_person_id]}}`` forward map
      * ``names``            — ``{tmdb_person_id: name}`` flat id→name lookup (infra only;
        the union of every title's :func:`route_people_names`; not read by the scorers)
    """
    media_people_fwd = {
        key: route_people(credits, cast_limit=cast_limit)
        for key, credits in media_people.items()
    }
    names: dict[int, str] = {}
    for credits in media_people.values():
        for pid, name in route_people_names(credits, cast_limit=cast_limit).items():
            names.setdefault(pid, name)
    return invert_forward(media_people_fwd), media_people_fwd, names


def forward_from_relations(records, *, cast_limit: int = 10) -> dict:
    """Build the forward map ``{(medium, ext_id): {role: [tmdb_person_id]}}`` directly
    from RELATIONAL credit records — the Radarr
    ``relational/movie_person_relations.parquet`` rows.

    ``records`` is any iterable of mappings carrying at least::

        {"tmdb_id": <title id>, "person_tmdb_id": <person id>,
         "role_type": "actor"|"director"|"writer"|"composer"|"producer"|
                      "cinematographer"|"editor",
         "billing_order": <float|None>,   # cast only; None/NaN sorts last
         "medium": "movie"}               # optional, defaults to "movie"

    Why this exists alongside :func:`build_index`: the daemon people buckets are ~31k
    individually-gzipped JSON files whose crew payload has no cinematographer/editor
    department for a large share of titles, while the relational table is ONE parquet
    per instance that already carries every credited role with stable person ids and a
    real billing order. Same output shape, same role vocabulary — so both sources feed
    the identical downstream (``invert_forward`` → ``aggregate_person_affinity`` → C4).

    Cast is emitted in billing order and truncated to ``cast_limit`` (matching
    :func:`route_people`); crew roles keep first-seen order. Rows with a missing /
    non-integer person id, or an unmapped ``role_type``, are skipped. PURE."""
    # role -> list[(billing_order, pid)] so cast can be sorted before truncation
    staged: dict = defaultdict(lambda: defaultdict(list))
    for rec in records or ():
        try:
            role = RELATION_ROLE_TYPES.get(str(rec.get("role_type") or "").strip().lower())
            if not role:
                continue
            pid = rec.get("person_tmdb_id")
            ext = rec.get("tmdb_id")
            if pid is None or ext is None:
                continue
            pid = int(pid)
            ext = int(ext)
        except (TypeError, ValueError):
            continue
        order = rec.get("billing_order")
        try:
            order = float(order)
            if order != order:            # NaN
                order = 9999.0
        except (TypeError, ValueError):
            order = 9999.0
        medium = str(rec.get("medium") or "movie")
        staged[(medium, ext)][role].append((order, pid))

    out: dict = {}
    for key, roles in staged.items():
        resolved: dict[str, list[int]] = {r: [] for r in ROLES}
        for role, pairs in roles.items():
            if role in BILLED_ROLES:
                pairs = sorted(pairs, key=lambda p: p[0])
            seen: list[int] = []
            for _, pid in pairs:
                if pid not in seen:
                    seen.append(pid)
            resolved[role] = seen[:cast_limit] if role in BILLED_ROLES else seen
        out[key] = resolved
    return out


def merge_forward(*maps: dict) -> dict:
    """Merge several forward maps into one. Later maps win on a duplicate
    ``(medium, ext_id)`` key ONLY when they actually carry people for it — a title
    present in two Radarr instances must not lose its credits to whichever instance's
    table happened to be read last with an empty role set. Pure."""
    out: dict = {}
    for m in maps:
        for key, roles in (m or {}).items():
            if key not in out or (any(roles.values()) and not any(out[key].values())):
                out[key] = roles
    return out


def serialize_forward(media_people_fwd: dict) -> dict:
    """Forward map → JSON-safe dict: the ``(medium, ext_id)`` tuple key becomes the
    string ``"medium:ext_id"`` (medium has no colon, so the split is unambiguous).
    Pure — the service manager gzips the result; :func:`deserialize_forward` inverts it."""
    return {f"{m}:{e}": roles for (m, e), roles in media_people_fwd.items()}


def deserialize_forward(d: dict) -> dict:
    """Inverse of :func:`serialize_forward` — JSON dict → ``{(medium, ext_id): {role: [pid]}}``
    with ext_ids and person ids coerced back to int."""
    out: dict[tuple, dict[str, list[int]]] = {}
    for k, roles in (d or {}).items():
        medium, ext = k.split(":", 1)
        out[(medium, int(ext))] = {r: [int(p) for p in (v or [])] for r, v in (roles or {}).items()}
    return out


def serialize_names(names: dict) -> dict:
    """``{person_id: name}`` → JSON-safe ``{str(person_id): name}``. Pure — the service
    manager gzips the result; :func:`deserialize_names` inverts it."""
    return {str(pid): name for pid, name in (names or {}).items()}


def deserialize_names(d: dict) -> dict:
    """Inverse of :func:`serialize_names` — JSON dict → ``{int(person_id): name}``,
    dropping any key that won't coerce to int."""
    out: dict[int, str] = {}
    for k, name in (d or {}).items():
        try:
            out[int(k)] = name
        except (TypeError, ValueError):
            continue
    return out


def co_occurring(person_index: dict, pids) -> dict:
    """Titles featuring ANY of the query ``pids``, ranked by how many DISTINCT query
    persons appear: ``{(medium, ext_id): n_matched}``. Derived on demand from the
    inverted index (no materialised matrix). Persons absent from the index contribute
    nothing. Use :func:`films_with_all` for the strict "all of them" (AND) answer."""
    counts: dict = defaultdict(int)
    for pid in {p for p in pids}:
        for key in person_index.get(pid, ()):  # empty for an unknown person
            counts[key] += 1
    return dict(counts)


def films_with_all(person_index: dict, pids) -> set:
    """The strict AND query: titles where EVERY distinct query person appears — the
    literal "films with ScarJo AND RDJ". Empty if any query person is absent from the
    index (they appear in zero titles, so the conjunction is empty)."""
    distinct = {p for p in pids}
    if not distinct:
        return set()
    counts = co_occurring(person_index, distinct)
    return {key for key, n in counts.items() if n == len(distinct)}

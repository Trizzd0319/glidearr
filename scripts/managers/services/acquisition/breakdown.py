"""
breakdown.py — the acquisition "why was this elevated" breakdown, as DATA first.
================================================================================
``AcquisitionManager._log_elevation_breakdown`` used to print a free-form 4-5 line
prose stanza per title. This module replaces that with ONE canonical record per
acted-on title (:func:`build_records`) plus a set of renderers that project those
records into the boxed log tables (:func:`render`). The same records are the frame
the website generator reads — there is exactly one source of truth, and every
legend table below is a pure aggregation of it.

Why records-first
-----------------
The prose repeated two kinds of RUN-CONSTANT context on every title: the household
genre affinity weight (``adventure(1.00)`` is 1.00 for every candidate, always) and
the quality-profile rationale (3 distinct sentences, ~110 chars each, printed once
per row). Both are emitted ONCE here as legends keyed off the per-title rows, which
is where the per-title line reduction comes from — the per-title information content
is unchanged, and the signal-contribution table below is strictly NEW (it was only
ever visible as a ``log_debug`` ``matrix=`` dump).

P-C hazard (absent vs empty) — READ BEFORE EDITING
--------------------------------------------------
``scorer.score`` only sets ``evidence["people"]`` when the candidate resolves in the
people-matrix forward map (``scorer.py``: ``people_ev`` stays None otherwise). So:

    people_scored=False  -> the title is not in the people-matrix; the cast/crew
                            signal was NEVER COMPUTED for it.
    people_scored=True,
    people_matched=0     -> it WAS computed and found no household favourites.

The old prose distinguished these by whether the ``cast/crew:`` line existed at all.
A table cell cannot: a blank/0 cell silently asserts "we checked and found none".
``people_scored`` carries the distinction explicitly and MUST NOT be collapsed into
``people_matched == 0``. Same rule for every matrix signal: ``None`` means the signal
was absent from the matrix (and therefore excluded from the score's denominator),
which is NOT the same as a scored zero. :func:`to_dataframe` pins nullable dtypes so
the distinction survives the pandas boundary too.

Pure module: no I/O, no manager/state access, stdlib only (pandas imported lazily and
optionally in :func:`to_dataframe`). ASCII-only cell text — the cp1252 console/log sink
cannot encode <= / ... / em-dash, and ``_box_table`` pads with NBSP.
"""
from __future__ import annotations

# Canonical column order for the breakdown frame. The website generator and
# to_dataframe() both key off this list, so append-only: never reorder or rename
# without bumping SCHEMA_VERSION and updating the site's column map.
# v2: appended space_charge_gb / space_pool (the byte-budget price levied on a
# funded candidate and the pool it drew from; None when the budget is off or
# the candidate passed uncharged).
SCHEMA_VERSION = 2

SCHEMA: tuple = (
    # identity
    "title", "ext_id", "type", "instance", "route_category", "is_anime",
    # outcome
    "score", "decision", "expected_size_gb", "size_unit", "saga_names",
    "space_charge_gb", "space_pool",
    # profile choice
    "profile_id", "profile_name", "profile_reason", "profile_ref",
    # evidence (raw drivers, straight off scorer evidence)
    "source_feed", "source_label", "rating10", "votes", "year",
    "genres", "genre_names", "genre_top_weight", "genre_count",
    "people_scored", "people_matched", "people_affinity",
    # score decomposition (matrix value 0-100 per signal; None == signal ABSENT)
    "sig_genre_affinity", "sig_source", "sig_trakt_rating",
    "sig_popularity", "sig_recency", "sig_people_affinity",
    # weighted contribution in SCORE POINTS (sums to `score`); None where absent
    "con_genre_affinity", "con_source", "con_trakt_rating",
    "con_popularity", "con_recency", "con_people_affinity",
    "signals_present", "weight_denominator",
)

# Matrix keys in display order, paired with their short column label.
_SIGNALS: tuple = (
    ("genre_affinity", "Genre"),
    ("source", "Source"),
    ("trakt_rating", "Rating"),
    ("popularity", "Popular"),
    ("recency", "Recent"),
    ("people_affinity", "Cast"),
)

_NOT_SCORED = "not scored"   # people_scored=False sentinel (never "0")
_ABSENT = "-"                # a matrix signal absent from this candidate


def _fmt_votes(v) -> str:
    """412000 -> '412K', 1500000 -> '1.5M', None/0 -> '-'. Table-cell variant of the
    manager's ``_fmt_votes`` (no ' votes' suffix — the column header carries that)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return _ABSENT
    if v <= 0:
        return _ABSENT
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{round(v / 1000)}K"
    return str(int(v))


def _fmt_feed(feed) -> str:
    """'trakt_watchlist' -> 'Trakt watchlist'. Mirrors the manager's static helper so the
    rendered label is identical to the prose it replaces."""
    if not feed:
        return ""
    head, _, tail = str(feed).partition("_")
    svc = {"trakt": "Trakt", "plex": "Plex", "mal": "MAL"}.get(head, head.title())
    return f"{svc} {tail.replace('_', ' ')}".strip()


def _short_feed(feed) -> str:
    """A compact fixed-width-friendly feed label for the table: 'Trakt watchlist' ->
    'Trakt WL', 'MAL suggestions' -> 'MAL sugg'. Falls back to the long form."""
    table = {
        "trakt_watchlist": "Trakt WL", "plex_watchlist": "Plex WL",
        "mal_plantowatch": "MAL PTW", "trakt_recommendations": "Trakt rec",
        "mal_suggestions": "MAL sugg", "mal_seasonal": "MAL seas",
        "people_cooccurrence": "co-cast", "plex_playlist": "Plex list",
        "plex_hubs": "Plex hubs",
    }
    return table.get(str(feed or ""), _fmt_feed(feed) or _ABSENT)


def _num(v, nd=1):
    """Format a float for a numeric column, or the absent sentinel. Kept numeric-looking
    so ``_box_table``'s ``_looks_numeric`` right-aligns the column when no cell is absent."""
    if v is None:
        return _ABSENT
    return f"{float(v):.{nd}f}"


def build_records(elevated, weights, *, saga_key=None) -> list:
    """One canonical dict per acted-on title, in the order they were acted on.

    ``elevated``  — the manager's post-add candidate dicts (score, matrix, evidence,
                    quality_profile, profile_reason, instance, decision, ...).
    ``weights``   — the live scorer weight map (``scorer._weights``); needed to turn
                    matrix values into contribution POINTS that sum back to ``score``.
    ``saga_key``  — optional callable(e) -> list[str] of saga display names.

    Every returned dict has exactly the keys in :data:`SCHEMA`. Absent signals stay
    ``None`` (never coerced to 0) — see the P-C note in the module docstring.
    """
    records = []
    profile_refs: dict = {}
    for e in elevated or []:
        ev = e.get("evidence") or {}
        matrix = e.get("matrix") or {}
        qp = e.get("quality_profile") or {}

        # Profile rationale is deduped to a numbered ref; the full sentence is emitted
        # once in the profile legend instead of once per row.
        pname = qp.get("name")
        preason = e.get("profile_reason")
        pkey = (pname, preason)
        if pkey not in profile_refs:
            profile_refs[pkey] = len(profile_refs) + 1
        pref = profile_refs[pkey] if (pname or preason) else None

        # Dynamic denominator: _weighted() divides by the sum of weights of the signals
        # actually PRESENT, so two candidates carrying different signal sets are not
        # scored on the same basis. Surfacing it makes that comparability gap visible.
        den = 0.0
        for key in weights:
            if matrix.get(key) is not None:
                den += float(weights[key])

        mg = ev.get("matched_genres") or []
        ppl = ev.get("people")

        rec = {
            "title": str(e.get("title") or e.get("ext_id") or ""),
            "ext_id": e.get("ext_id"),
            "type": e.get("type"),
            "instance": e.get("instance"),
            "route_category": e.get("route_category"),
            "is_anime": bool(e.get("route_category") == "anime" or e.get("is_anime")),
            "score": e.get("score"),
            "decision": e.get("decision"),
            "expected_size_gb": e.get("expected_size_gb"),
            "size_unit": e.get("size_unit"),
            "space_charge_gb": e.get("space_charge_gb"),
            "space_pool": e.get("space_pool"),
            "saga_names": list(e.get("saga_names") or (saga_key(e) if saga_key else []) or []),
            "profile_id": qp.get("id"),
            "profile_name": pname,
            "profile_reason": preason,
            "profile_ref": pref,
            "source_feed": ev.get("source_feed"),
            "source_label": _short_feed(ev.get("source_feed")),
            "rating10": ev.get("rating10"),
            "votes": ev.get("votes"),
            "year": ev.get("year"),
            # genres carries the (name, weight) pairs so the genre legend is a pure
            # aggregation of this frame — no second source of household weights.
            "genres": [{"name": g, "weight": float(w)} for g, w in mg],
            "genre_names": [g for g, _w in mg],
            "genre_top_weight": (max(float(w) for _g, w in mg) if mg else None),
            "genre_count": len(mg),
            # P-C: absent vs empty. Do NOT collapse these three into two.
            "people_scored": ppl is not None,
            "people_matched": (ppl or {}).get("matched") if ppl is not None else None,
            "people_affinity": (ppl or {}).get("score") if ppl is not None else None,
            "signals_present": sorted(k for k, _l in _SIGNALS if matrix.get(k) is not None),
            "weight_denominator": round(den, 4) if den else None,
        }
        for key, _label in _SIGNALS:
            val = matrix.get(key)
            rec[f"sig_{key}"] = val
            rec[f"con_{key}"] = (
                round(float(weights.get(key, 0.0)) * float(val) / den, 2)
                if (val is not None and den) else None
            )
        records.append(rec)
    return records


# ── Legends: every one of these is an aggregation of `records` ────────────────────

def genre_legend(records) -> list:
    """[(genre, weight), ...] desc — the household genre affinity, deduped out of the
    per-title rows. Identical for every candidate by construction (``scorer._affinity``
    normalises one household-wide map), so it belongs in a legend, not on 25 rows."""
    seen: dict = {}
    for r in records:
        for g in r.get("genres") or []:
            seen.setdefault(g["name"], g["weight"])
    return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))


def profile_legend(records) -> list:
    """[(ref, profile_name, reason, row_count), ...] — one entry per distinct
    (profile, reason) pair, so a ~110-char rationale is printed once, not per row."""
    agg: dict = {}
    for r in records:
        ref = r.get("profile_ref")
        if ref is None:
            continue
        key = (ref, r.get("profile_name"), r.get("profile_reason"))
        agg[key] = agg.get(key, 0) + 1
    return [(k[0], k[1], k[2], n) for k, n in sorted(agg.items(), key=lambda kv: kv[0][0])]


def signal_legend(records, weights) -> list:
    """[(label, key, weight, n_present, n_absent), ...] — how many acted-on titles
    actually carried each scoring signal. A signal missing on a whole cohort (e.g.
    cast/crew on every MAL-sourced title) shows up here as a lopsided present/absent
    split, which is the thing the dynamic denominator quietly hides in the score."""
    out = []
    total = len(records)
    for key, label in _SIGNALS:
        present = sum(1 for r in records if r.get(f"sig_{key}") is not None)
        out.append((label, key, float(weights.get(key, 0.0)), present, total - present))
    return out


def cohort_summary(records) -> list:
    """[(source_label, n, n_show, n_movie, mean_score, pct_people_scored), ...] — the
    per-source cohort roll-up. This is what makes a shared movie/show add budget legible:
    if one medium is taking every slot, it is one row apart here."""
    by: dict = {}
    for r in records:
        by.setdefault(r.get("source_label") or _ABSENT, []).append(r)
    rows = []
    for src, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        scores = [r["score"] for r in rs if isinstance(r.get("score"), (int, float))]
        scored = sum(1 for r in rs if r.get("people_scored"))
        rows.append((
            src, len(rs),
            sum(1 for r in rs if r.get("type") == "show"),
            sum(1 for r in rs if r.get("type") == "movie"),
            round(sum(scores) / len(scores), 1) if scores else None,
            f"{round(100 * scored / len(rs))}%",
        ))
    return rows


# ── Renderers ─────────────────────────────────────────────────────────────────────

def _genres_cell(rec, n: int = 3) -> str:
    names = rec.get("genre_names") or []
    if not names:
        return "none matched"
    head = ", ".join(names[:n])
    return head + (f" +{len(names) - n}" if len(names) > n else "")


def _people_cell(rec) -> str:
    """P-C-safe rendering: 'not scored' (never computed) is visually distinct from
    '0 (0.0)' (computed, no household favourites on the title)."""
    if not rec.get("people_scored"):
        return _NOT_SCORED
    return f"{rec.get('people_matched')} ({_num(rec.get('people_affinity'))})"


def has_saga(records) -> bool:
    return any(r.get("saga_names") for r in records)


def evidence_rows(records, with_saga: bool = False) -> list:
    """``Prof`` is the join back to the profile rationale key; ``Saga`` is only emitted
    when at least one acted-on title belongs to one (mirrors the decisions table, which
    omits the column entirely on an unbuilt universe index)."""
    rows = []
    for r in records:
        row = [
            r["title"][:34], r.get("type") or _ABSENT, r.get("score"),
            r.get("source_label") or _ABSENT, _num(r.get("rating10")),
            _fmt_votes(r.get("votes")), r.get("year") if r.get("year") else _ABSENT,
            _people_cell(r), _genres_cell(r),
            f"({r['profile_ref']})" if r.get("profile_ref") else _ABSENT,
        ]
        if with_saga:
            row.append(", ".join(r.get("saga_names") or []) or _ABSENT)
        rows.append(row)
    return rows


def contribution_rows(records) -> list:
    """Score decomposition: each cell is that signal's contribution in SCORE POINTS.
    The signal columns sum to Score (+/- rounding), so the table is self-checking."""
    rows = []
    for r in records:
        row = [r["title"][:34], r.get("score")]
        for key, _label in _SIGNALS:
            row.append(_num(r.get(f"con_{key}"), 1))
        row.append(len(r.get("signals_present") or []))
        row.append(_num(r.get("weight_denominator"), 2))
        rows.append(row)
    return rows


def profile_rows(records) -> list:
    return [[f"({ref})", name or _ABSENT, (reason or _ABSENT), n]
            for ref, name, reason, n in profile_legend(records)]


def genre_rows(records, per_row: int = 4) -> list:
    """The genre legend laid out ``per_row`` (genre, weight) pairs across so a 14-genre
    household map is 4 lines, not 14."""
    flat = [c for name, w in genre_legend(records) for c in (name, f"{w:.2f}")]
    width = per_row * 2
    rows = [flat[i:i + width] for i in range(0, len(flat), width)]
    if rows:
        rows[-1] += [""] * (width - len(rows[-1]))
    return rows


def signal_rows(records, weights) -> list:
    return [[label, key, _num(w, 2), present, absent,
             "all" if absent == 0 else ("none" if present == 0 else "partial")]
            for label, key, w, present, absent in signal_legend(records, weights)]


def taste_rows(taste) -> list:
    """The household taste profile as rows. A role the metadata source never supplies
    comes back empty and is omitted (same contract as the prose it replaces)."""
    order = [("genres", "Top genres"), ("directors", "Top directors"),
             ("actors", "Top cast"), ("writers", "Top writers"),
             ("composers", "Top composers"), ("producers", "Top producers")]
    return [[label, ", ".join(taste.get(key) or [])]
            for key, label in order if (taste or {}).get(key)]


def render(records, taste, weights, logger) -> None:
    """Emit the whole breakdown as boxed tables. No-op on empty ``records``."""
    if not records:
        return
    n = len(records)
    shows = sum(1 for r in records if r.get("type") == "show")
    movies = sum(1 for r in records if r.get("type") == "movie")

    saga = has_saga(records)
    headers = ["Title", "Type", "Score", "Source", "Rating", "Votes", "Year", "Cast/crew",
               "Genres matched", "Prof"]
    if saga:
        headers.append("Saga")
    logger.log_table(
        headers,
        evidence_rows(records, with_saga=saga),
        title=f"[Acquisition] elevation evidence - {n} acted on ({shows} show / {movies} movie)",
        caption="The raw drivers behind each score. Cast/crew = household-favourite people on "
                "the title (people-affinity). 'not scored' means the title is absent from the "
                "people-matrix so the signal was never computed - it is NOT a scored zero. "
                "Genre weights are household-wide; see the affinity legend. 'Prof' joins to the "
                "profile rationale key.",
    )

    logger.log_table(
        ["Title", "Score", *[label for _k, label in _SIGNALS], "Sigs", "Denom"],
        contribution_rows(records),
        title="[Acquisition] score decomposition (contribution in score points)",
        caption="Each signal column is weight x value / denominator, so the signal columns sum "
                "to Score. '-' means the signal was ABSENT from that candidate's matrix and was "
                "excluded from its denominator. 'Denom' is that dynamic denominator: two rows "
                "with different denominators were not scored on the same basis.",
    )

    logger.log_table(
        ["Ref", "Profile", "Why this profile", "Rows"],
        profile_rows(records),
        title="[Acquisition] profile rationale key",
        caption="Referenced by (n) from the Prof column of the evidence table above. Each "
                "rationale is emitted once here instead of repeated on every row.",
    )

    logger.log_table(
        ["Source", "N", "Show", "Movie", "Mean score", "People scored"],
        cohort_summary(records),
        title="[Acquisition] cohort roll-up by source",
        caption="How the run's add budget actually split. 'People scored' is the share of that "
                "cohort carrying a cast/crew signal at all - a low value means those titles were "
                "ranked on fewer signals than the rest.",
    )

    logger.log_table(
        ["Signal", "Matrix key", "Weight", "Present", "Absent", "Coverage"],
        signal_rows(records, weights),
        title="[Acquisition] scoring signal coverage",
        caption="Which signals were available across the acted-on titles. An absent signal is "
                "dropped from that candidate's denominator rather than scored as zero.",
    )

    grows = genre_rows(records)
    if grows:
        logger.log_table(
            ["Genre", "Aff", "Genre", "Aff", "Genre", "Aff", "Genre", "Aff"],
            grows,
            title="[Acquisition] household genre affinity (0-1)",
            caption="Derived once from household watch history and identical for every "
                    "candidate, so it is printed here rather than repeated per title.",
        )

    trows = taste_rows(taste or {})
    if trows:
        logger.log_table(
            ["Role", "Household favourites (top 5)"], trows,
            title="[Acquisition] household taste profile",
            caption="What the cast/crew affinity signal is measured against. A candidate's own "
                    "credits are not reachable (the people-matrix is id-only), so this names the "
                    "household side of the comparison.",
        )


# ── Frame export ──────────────────────────────────────────────────────────────────

def to_dataframe(records):
    """The single breakdown frame the website generator reads: one row per acted-on
    title, columns exactly :data:`SCHEMA`, in that order. Returns a pandas DataFrame.

    List-valued columns (``genres``, ``genre_names``, ``saga_names``, ``signals_present``)
    are kept as objects rather than flattened — every legend table above is a pure
    aggregation of this frame, and flattening them would break that. ``genres`` holds
    ``{"name", "weight"}`` dicts so the genre legend needs no second source.

    Raises ImportError when pandas is unavailable; callers that only need the log
    tables should use :func:`render` and never touch this.
    """
    import pandas as pd
    df = pd.DataFrame(list(records or []), columns=list(SCHEMA))
    # P-C guard at the frame boundary. A plain int column holding None becomes float64
    # NaN, and the first ``fillna(0)`` in a site template would silently reassert
    # "we checked and found zero". Nullable dtypes keep absent as <NA> so that coercion
    # has to be written on purpose, and `people_scored` stays the authoritative flag.
    nullable_int = ["people_matched", "year", "ext_id", "profile_id", "profile_ref",
                    "genre_count"]
    nullable_float = ["score", "rating10", "votes", "people_affinity",
                      "expected_size_gb", "genre_top_weight", "weight_denominator",
                      "space_charge_gb"]
    nullable_float += [f"sig_{k}" for k, _l in _SIGNALS] + [f"con_{k}" for k, _l in _SIGNALS]
    for col in nullable_int:
        if col in df.columns:
            df[col] = pd.array(df[col], dtype="Int64")
    for col in nullable_float:
        if col in df.columns:
            df[col] = pd.array(df[col], dtype="Float64")
    if "people_scored" in df.columns:
        df["people_scored"] = df["people_scored"].astype("boolean")
    df.attrs["schema_version"] = SCHEMA_VERSION
    return df


def to_payload(records, taste, weights) -> dict:
    """JSON-safe payload for the global cache / website: the frame plus the run-level
    context that is NOT derivable from it (the household taste profile is household-wide,
    not per-title, so it cannot be aggregated back out of the rows)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "columns": list(SCHEMA),
        "rows": [{k: r.get(k) for k in SCHEMA} for r in (records or [])],
        "taste_profile": dict(taste or {}),
        "weights": {k: float(v) for k, v in (weights or {}).items()},
    }

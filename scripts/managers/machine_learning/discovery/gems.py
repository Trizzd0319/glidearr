"""discovery/gems.py — the "Hidden Gems" pipeline: TASTE-ONLY scoring, candidate selection,
diversity caps, and the 30-day outcome join. PURE — no I/O, no manager graph, no wall clock
(every clock-dependent function takes its ``now`` as an argument, so a frozen clock is a
plain parameter in tests).

WHY THIS EXISTS
---------------
Most installs arrive via Kometa with a large OWNED backlog nobody is working through. The
shipped surfaces do not help with it:

  * ``Up Next`` / ``The Long Glide`` / ``Touch & Go`` are CONTINUATION-driven — they rank what
    the household already watches to the top, so a never-touched title can never surface;
  * ``plex/discovery`` ("This Week in History") is CALENDAR-driven — a title has to have an
    anniversary this week to appear at all.

Nothing says "you own this, you have never played it, and it matches your taste". That is
this module. It is also the highest-value ML surface in the product: an OWNED title we
recommend and then observe being played (or not) is a REAL prospective label with no
counterfactual problem — unlike an acquisition of an unowned title, where "would they have
watched it if we hadn't grabbed it?" is unanswerable.

────────────────────────────────────────────────────────────────────────────────
1. THE TASTE-ONLY SCORE — and why Group A must be excluded
────────────────────────────────────────────────────────────────────────────────
The persisted ``watchability_score`` (see ``scoring/SCORING_GROUPS.md``) blends two very
different things:

  * ENGAGEMENT (GROUP A: keep-tag / completion / rewatch count / the household's own rating) —
    "have they already engaged with THIS title?";
  * TASTE (GROUP B affinity: actor/director/writer/genre/studio; GROUP C: collection /
    universe / related-graph / person-affinity; GROUP F: critic consensus + popularity) —
    "does this title look like the things they engage with?".

For a NEVER-WATCHED title Group A is structurally ~zero (no completion, no rewatch, no user
rating), so ranking gems by the blended score means ranking them by *the noise left over
after the dominant term went to zero* — and, worse, any gem that DOES carry Group A points
carries them because it is already a household favourite. Sorting by the blended score would
therefore surface "titles you have already curated" instead of "titles you have never
touched", i.e. it would just re-rank favourites. So the gem score sums ONLY the taste groups
and renormalises to 0-100 against the maximum achievable taste-only total.

Group-by-group, with the reason each is in or out:

  A1/A2/A3/A4  OUT — engagement. A2/A3/A4 are structurally zero on a never-watched title;
                     A1 (``keep_forever`` / ``keep_universe`` tag) is explicit CURATION, not
                     taste inference — letting a keep-tag worth +15 into a 75-point scale
                     would let one tag alone dominate the shelf with titles the operator has
                     already told us they care about.
  A5           IN  — watchlist intent, and the one Group-A exception. Unlike A1-A4 it says
                     nothing about engagement with THIS title: it is a forward statement
                     about something nobody has played, which is exactly this shelf's
                     population. "You own this, you asked for it, you have never started
                     it" is the highest-value reminder the surface can produce, so it is
                     admitted (Robert's call, overriding the recommendation to exclude it
                     alongside the rest of Group A). Its 8.0 cap is a ninth of the
                     denominator, so it re-ranks rather than dominates, and admitting it
                     cannot leak a PLAYED title onto the shelf — ``gem_candidates`` drops
                     those on the ``seen(row)`` predicate before taste is computed at all.
  B1..B5       IN  — the affinity core. "Does this title's cast/crew/genre/studio look like
                     what this household actually watches?" is precisely the gem question.
  C1..C4       IN  — collection completeness, universe siblings, Trakt related-graph and
                     person-affinity. NOTE these are computed from engagement with OTHER
                     titles ("you finished 3 of the 4 films in this collection"), never with
                     this one, so they stay taste signals on a never-watched title.
  D1/D2/D3     OUT — device / playback FIT. Per SCORING_GROUPS.md §2, D1 and D3 depend only
                     on the household's platform usage and the target resolution, not on the
                     title, so they are near-constant across the candidate pool: they cannot
                     re-rank anything, they would only inflate the denominator and compress
                     the taste signal's dynamic range.
  E1/E2/E3     OUT — audience alignment (kids-cert x kids-affinity, library routing). The
                     per-profile AGE GATE already decides who may see what, from the same
                     certification data; scoring it again would double-count the age axis
                     inside a number that is supposed to mean "taste".
  F1/F2        IN  — critic consensus + popularity. The "is it any good / did anyone notice
                     it" prior that keeps a shelf of never-watched titles from filling up
                     with library filler.
  F3           OUT — recency is a CALENDAR signal, not taste, and it is already the
                     ``Fresh Arrivals`` playlist's whole job. Letting it in would tilt a
                     HIDDEN-gems shelf toward brand-new acquisitions.
  G1..G4       IN, as PENALTIES — language mismatch, abandoned, critically panned, and not
                     yet available all still apply. A gem you cannot play is not a gem, and a
                     title the household abandoned at 10% is not undiscovered.

Signals are matched by their GROUP CODE (``B4``), not by the full breakdown key, because the
movie and show scorers spell the same slot differently (``B5_studio_affinity`` vs
``B5_network_affinity``). Matching on the code makes this module medium-agnostic.

────────────────────────────────────────────────────────────────────────────────
2. THE MEASUREMENT LOOP
────────────────────────────────────────────────────────────────────────────────
Every published pick is recorded as a recommendation EVENT (see
``machine_learning/labels/recommendations.py`` for the ledger). :func:`classify_outcome` then
joins those events against what the profile actually played:

    HIT      the profile played it within ``window_days`` (default 30) of ``recommended_at``
    MISS     ``window_days`` elapsed with no play
    PENDING  the window has not closed yet — NOT a miss, and never counted as one

A pick is offered for its WHOLE window, not for one run: the shelf HOLDS open picks (see
``apply_diversity_caps(held=…)``) and only tops up the free slots. Otherwise the household
would get a single day to act on each pick, the rendered playlist would churn completely
between runs, and the resulting labels would measure nothing.

:func:`hit_rate_summary` aggregates matured picks only and annotates the result with a
confidence label drawn from the SAME vocabulary the thresholds module uses, so a 4-pick
sample reads "directional" instead of masquerading as a measurement.
"""
from __future__ import annotations

import json
import math
import re

from scripts.managers.machine_learning.thresholds.derive import (
    CONFIDENCE_NONE,
    CONFIDENCE_TIERS,
)

# ── signal-group taxonomy ─────────────────────────────────────────────────────
# The NOMINAL per-signal maximum of every TASTE signal (the coded caps in
# scoring/movie_scorer.py + show_scorer.py). Keyed by GROUP CODE so the movie/show spelling
# difference (B5_studio_affinity vs B5_network_affinity) cannot cause a silent miss.
#
# C4 is listed at 4.0 — its runtime cap (``person_affinity_cap``) defaults to 0.0, so with the
# default config the denominator is deliberately 4 points conservative and no title can reach a
# literal 100. That is the right trade: the denominator is a CONSTANT either way, so stored
# taste scores stay comparable across runs and across a flip of that knob. (Reaching 100 would
# anyway require maxing every taste signal at once — perfect cast AND director AND writer AND
# genre AND studio affinity, a >=8.5 critic average, a >=75%-watched collection …)
#
# A5 (watchlist intent) is listed at its shipped cap of 8.0 and is the ONE Group-A signal on
# the taste side. Robert's call, overriding the recommendation to exclude it, and his
# reasoning is right: "owned + watchlisted + never played" is the single highest-value
# reminder this shelf can produce, and it is the only Group-A signal that is NOT engagement
# with THIS title — it is a forward statement about a title nobody has touched. The
# never-played filter is enforced by ``gem_candidates``'s ``seen(row)`` predicate BEFORE the
# taste score is computed, so admitting A5 cannot put a played title back on the shelf.
TASTE_CAPS = {
    "A5": 8.0,                                                   # GROUP A — EXPLICIT intent (see above)
    "B1": 8.0, "B2": 6.0, "B3": 4.0, "B4": 4.0, "B5": 3.0,       # GROUP B — affinity
    "C1": 8.0, "C2": 4.0, "C3": 4.0, "C4": 4.0,                  # GROUP C — collection/universe/graph
    "F1": 20.0, "F2": 2.0,                                       # GROUP F — critic consensus + popularity
}

#: Penalties that still apply to a gem (all negative in the breakdown).
PENALTY_CODES = ("G1", "G2", "G3", "G4")

#: Deliberately EXCLUDED (documented one-by-one in the module docstring). NOTE A5 is NOT
#: here — it is the one Group-A signal that belongs on the taste side; see TASTE_CAPS.
EXCLUDED_CODES = ("A1", "A2", "A3", "A4",        # engagement — would re-rank favourites
                  "D1", "D2", "D3", "D4",        # device/playback fit — not TASTE. v1's
                                                 # D1-D3 were household-constant; v2's D4 is
                                                 # a real (negative) transcode-risk signal,
                                                 # but it describes the FILE we happen to
                                                 # hold, not whether the household would
                                                 # enjoy the title — and a shelf that
                                                 # demoted a beloved film for having DTS
                                                 # audio would be obviously wrong.
                  "E1", "E2", "E3",              # audience alignment — the age gate's job
                  "F3")                          # recency — Fresh Arrivals' job

#: The denominator the raw taste sum is renormalised against (75.0 with the caps above —
#: DERIVED, never hardcoded, so admitting or dropping a signal re-anchors the scale in one
#: place. It was 67.0 before A5 joined).
TASTE_MAX = sum(TASTE_CAPS.values())

# "sig_B4_genre_affinity" / "B4_genre_affinity" -> "B4". The snapshot parquet prefixes every
# signal with "sig_"; the raw ``watchability_breakdown`` JSON does not. Both are accepted.
_CODE_RE = re.compile(r"^(?:sig_)?([A-G][1-9])(?:_|$)")

# ── outcome vocabulary ────────────────────────────────────────────────────────
OUTCOME_HIT = "hit"
OUTCOME_MISS = "miss"
OUTCOME_PENDING = "pending"

#: Robert's spec: a pick has 30 days to be played before it counts as a miss.
DEFAULT_WINDOW_DAYS = 30
_DAY_SECONDS = 86400.0

# Confidence ladder for the hit rate. The LABEL VOCABULARY is imported from the thresholds
# module so the two surfaces can never drift apart; only the floors differ, because the two
# quantities need very different sample sizes: thresholds/derive.py fits an isotonic
# score->P(watch) CURVE (>=100 positives before "usable"), whereas this is a single hit-rate
# proportion, whose standard error at n=30 is already ~9pp. Below 30 matured picks the number
# is "directional" — order suggestive, magnitude noise.
_CONFIDENCE_LABELS = tuple(label for _floor, label in CONFIDENCE_TIERS)
GEM_CONFIDENCE_FLOORS = (300, 100, 30, 1)
GEM_CONFIDENCE_TIERS = tuple(zip(GEM_CONFIDENCE_FLOORS, _CONFIDENCE_LABELS))

#: How deep into the billing order a cast member still counts as a "credited person" for the
#: per-person diversity cap. The parquet stores the top-10 billed cast; capping on all ten
#: would let a single bit-part actor block unrelated picks, so only the leads count.
PERSON_BILLING_DEPTH = 3


# ── small pure helpers ────────────────────────────────────────────────────────

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def signal_code(key):
    """``'sig_B4_genre_affinity'`` / ``'B4_genre_affinity'`` -> ``'B4'``; ``None`` for a
    meta key (``_total_raw``) or anything that is not a scorer signal."""
    m = _CODE_RE.match(str(key))
    return m.group(1) if m else None


def parse_breakdown(breakdown):
    """A scorer breakdown (dict, or the JSON string persisted in the ``watchability_breakdown``
    Parquet column) -> ``{group_code: float}``, or ``None`` when it is missing / unparseable /
    not a scorer breakdown at all.

    ``None`` (not ``0.0``) is the answer for "no breakdown", because a title we cannot explain
    must be EXCLUDED from the shelf and counted, never silently ranked last with a zero — the
    two mean completely different things (unknown taste vs zero taste)."""
    if breakdown is None or _is_nan(breakdown):
        return None
    if isinstance(breakdown, (bytes, bytearray)):
        try:
            breakdown = breakdown.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return None
    if isinstance(breakdown, str):
        if not breakdown.strip():
            return None
        try:
            breakdown = json.loads(breakdown)
        except (ValueError, TypeError):
            return None
    if not isinstance(breakdown, dict):
        return None
    out: dict = {}
    for k, v in breakdown.items():
        code = signal_code(k)
        if code is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out[code] = f
    return out or None            # a dict with no recognised signals is not a breakdown


def taste_score(breakdown, *, caps=None):
    """TASTE-ONLY 0-100 score for one persisted breakdown, or ``None`` when the breakdown is
    missing/unparseable (the caller excludes and counts it).

    Sums the taste signals in ``caps`` (default :data:`TASTE_CAPS` — GROUPS B, C and F1/F2),
    applies the GROUP-G penalties, and renormalises against ``sum(caps.values())``. GROUP A
    (engagement), D (device fit), E (audience alignment) and F3 (recency) are excluded — see
    the module docstring for the per-group reasoning.

    Clamped to [0, 100]: the scorer's ``affinity_boost`` multiplies the Group-B caps, so a
    boosted household CAN exceed the nominal denominator; heavy penalties can drive the raw
    sum negative. Both clamp rather than distort the scale."""
    codes = parse_breakdown(breakdown)
    if codes is None:
        return None
    caps = caps or TASTE_CAPS
    total = float(sum(caps.values())) or 1.0
    raw = sum(codes.get(c, 0.0) for c in caps)
    raw += sum(codes.get(c, 0.0) for c in PENALTY_CODES)
    return round(max(0.0, min(100.0, 100.0 * raw / total)), 2)


# ── candidate selection ───────────────────────────────────────────────────────

def _names(cell) -> list:
    """A pipe-separated parquet name cell (``'Ridley Scott|Tony Scott'``), a real list, or
    ``None`` -> a clean list of names."""
    if cell is None or _is_nan(cell):
        return []
    if isinstance(cell, str):
        return [p.strip() for p in cell.split("|") if p.strip()]
    try:
        return [str(x).strip() for x in cell if str(x).strip()]
    except TypeError:
        return []


def franchise_name(row):
    """The saga a movie belongs to, AS WRITTEN (``'The Matrix Collection'``) — its TMDB
    collection, else its first universe label. ``None`` for a true standalone. This is the
    display half; :func:`franchise_key` is the matching half."""
    for cell in (row.get("collection_name"), row.get("universe_name")):
        if cell is None or _is_nan(cell):
            continue
        name = str(cell).split("|")[0].strip().strip('"').strip()
        if name:
            return re.sub(r"\s+", " ", name)
    return None


def franchise_key(row):
    """The saga BUCKET for the per-franchise diversity cap. Normalised (case, spacing, a
    trailing "Collection") so ``'The Matrix Collection'`` and ``'the matrix'`` are one bucket.
    ``None`` for a true standalone (which is therefore never capped)."""
    name = franchise_name(row)
    if not name:
        return None
    return re.sub(r"\s+collection$", "", name.lower()) or None


def credited_people(row, *, depth: int = PERSON_BILLING_DEPTH) -> list:
    """The people a pick is "by" for the per-person diversity cap: every credited director
    plus the top-``depth`` BILLED cast (``cast_names`` is stored in billing order). Lower-cased
    and de-duplicated, order preserved."""
    out: list = []
    seen: set = set()
    for name in _names(row.get("director_names")) + _names(row.get("cast_names"))[:max(0, depth)]:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _blank_stats() -> dict:
    return {"considered": 0, "no_id": 0, "not_owned": 0, "unreachable": 0, "watched": 0,
            "excluded": 0, "age_gated": 0, "no_breakdown": 0, "eligible": 0}


def gem_candidates(rows, *, seen=None, excluded_ids=(), age_ok=None, reachable=None, caps=None):
    """``(ranked_candidates, stats)`` — the OWNED + NEVER-WATCHED-BY-THIS-PROFILE pool, ranked
    by taste score descending. PURE.

    ``rows``        owned-movie rows (the ``movie_files`` projection: tmdb_id, title, year,
                    has_file, certification, collection_name/universe_name, cast/director
                    names, watchability_breakdown).
    ``seen(row)``   per-PROFILE watched predicate. Per-user, NOT household: a film dad
                    finished is still a gem for the kid, so this is the same per-user Tautulli
                    finished-set the playlist builders join on. ``None`` -> nothing watched
                    (fail-OPEN, matching the sibling builders' behaviour for an unmatched
                    profile).
    ``excluded_ids`` tmdb ids to keep off the shelf — the ids already in this profile's Up
                    Next / other plans (never double-surface), plus the ids recommended
                    recently and still inside their measurement window (a pick gets its full
                    window to be played before it is offered again).
    ``age_ok(row)`` per-profile age gate (fail-CLOSED for restricted profiles). ``None`` ->
                    no gating (an adult profile).
    ``reachable(row)`` per-profile LIBRARY reachability: the title resolves to a Plex ratingKey
                    in a section this profile was actually shared. Applied here rather than
                    after the diversity caps so an unreachable title never consumes a shelf
                    slot or a franchise/person budget. ``None`` -> everything reachable.

    ``stats`` counts every exclusion reason, so an empty shelf is always explainable."""
    seen = seen or (lambda r: False)
    age_ok = age_ok or (lambda r: True)
    reachable = reachable or (lambda r: True)
    excluded = {t for t in (_to_int(x) for x in (excluded_ids or ())) if t is not None}
    stats = _blank_stats()
    out: list = []
    for row in rows or []:
        stats["considered"] += 1
        tmdb = _to_int(row.get("tmdb_id"))
        if tmdb is None:
            stats["no_id"] += 1
            continue
        if row.get("has_file") is False:            # absent column == owned (the loader pre-filters)
            stats["not_owned"] += 1
            continue
        if not reachable(row):
            stats["unreachable"] += 1
            continue
        if seen(row):
            stats["watched"] += 1
            continue
        if tmdb in excluded:
            stats["excluded"] += 1
            continue
        if not age_ok(row):
            stats["age_gated"] += 1
            continue
        taste = taste_score(row.get("watchability_breakdown"), caps=caps)
        if taste is None:
            stats["no_breakdown"] += 1
            continue
        out.append({
            "tmdb_id": tmdb,
            "title": row.get("title"),
            "year": _to_int(row.get("year")),
            "taste_score": taste,
            "certification": row.get("certification"),
            "franchise": franchise_key(row),                # the cap's matching bucket
            "franchise_label": franchise_name(row),         # …as written, for the log mirror
            "people": credited_people(row),
        })
    stats["eligible"] = len(out)
    # Deterministic order: taste desc, then title, then id — so two runs over the same library
    # publish the same shelf (the recommendation ledger depends on that stability).
    out.sort(key=lambda c: (-c["taste_score"], str(c.get("title") or "").lower(), c["tmdb_id"]))
    return out, stats


# ── diversity caps ────────────────────────────────────────────────────────────

def apply_diversity_caps(ranked, *, size, max_per_franchise=2, max_per_person=3, held=()):
    """``(picks, stats)`` — walk the ranked pool best-first and fill the shelf up to ``size``,
    never letting one saga or one person own it. PURE.

    ``max_per_franchise`` picks may share a collection/universe (default 2) and
    ``max_per_person`` may share a credited lead/director (default 3); a candidate that would
    breach either cap is SKIPPED and the walk continues, so the slot goes to the next-best
    title instead of being lost. ``<= 0`` disables a cap. A standalone (no franchise) is never
    franchise-capped; a title with no credits is never person-capped.

    ``held`` are picks ALREADY on the shelf from a previous run whose measurement window is
    still open. They LEAD the result in their existing order, consume shelf slots, and count
    against both caps — so a shelf that is topping itself up cannot drift past the caps one
    top-up at a time. ``stats["held"]`` reports how many there were.

    Every pick gains ``rank`` (0-based, its position on the published shelf); ranks are
    assigned across held + new, so the returned list IS the shelf in order."""
    held = list(held or ())[:max(0, int(size))]
    picks: list = list(held)
    stats = {"ranked": len(ranked or []), "capped_franchise": 0, "capped_person": 0,
             "picked": 0, "size": int(size), "held": len(held)}
    by_franchise: dict = {}
    by_person: dict = {}
    for c in held:                                  # held picks pre-charge both budgets
        fr = c.get("franchise")
        if fr is not None:
            by_franchise[fr] = by_franchise.get(fr, 0) + 1
        for p in (c.get("people") or []):
            by_person[p] = by_person.get(p, 0) + 1
    for c in ranked or []:
        if len(picks) >= size:
            break
        fr = c.get("franchise")
        if max_per_franchise > 0 and fr is not None and by_franchise.get(fr, 0) >= max_per_franchise:
            stats["capped_franchise"] += 1
            continue
        people = list(c.get("people") or [])
        if max_per_person > 0 and any(by_person.get(p, 0) >= max_per_person for p in people):
            stats["capped_person"] += 1
            continue
        if fr is not None:
            by_franchise[fr] = by_franchise.get(fr, 0) + 1
        for p in people:
            by_person[p] = by_person.get(p, 0) + 1
        picks.append(c)
    # Rank is stamped LAST, across held + new alike, so it always reads as "position on the
    # published shelf" (the field the recommendation ledger records and reports on).
    picks = [{**c, "rank": i} for i, c in enumerate(picks)]
    stats["picked"] = len(picks)
    return picks, stats


# ── the 30-day outcome join ───────────────────────────────────────────────────

def classify_outcome(recommended_ts, first_play_ts, now_ts, *,
                     window_days: int = DEFAULT_WINDOW_DAYS):
    """``(outcome, days_to_watch)`` for ONE published pick. PURE — the clock is an argument.

    * :data:`OUTCOME_HIT`     — the profile played it at/after ``recommended_ts`` and within
      ``window_days``; ``days_to_watch`` is how long it took (2dp).
    * :data:`OUTCOME_MISS`    — the window has fully elapsed with no qualifying play.
    * :data:`OUTCOME_PENDING` — the window is still open. Deliberately NOT a miss: counting
      open picks as failures would drag every freshly-published shelf's hit rate to zero.

    A play STRICTLY BEFORE ``recommended_ts`` never counts (it cannot have been caused by a
    recommendation that had not happened yet) — the gem pool is never-watched, so this only
    fires on a stale/replayed ledger row, and it fails toward "not a hit"."""
    try:
        rec = float(recommended_ts)
        now = float(now_ts)
    except (TypeError, ValueError):
        return OUTCOME_PENDING, None
    deadline = rec + max(0, int(window_days)) * _DAY_SECONDS
    if first_play_ts is not None:
        try:
            play = float(first_play_ts)
        except (TypeError, ValueError):
            play = None
        if play is not None and rec <= play <= deadline:
            return OUTCOME_HIT, round((play - rec) / _DAY_SECONDS, 2)
    if now >= deadline:
        return OUTCOME_MISS, None
    return OUTCOME_PENDING, None


def hit_rate_confidence(n_matured) -> str:
    """How seriously to take a hit rate computed over ``n_matured`` picks.

    ``none`` (nothing matured) -> ``directional`` (<30: order suggestive, magnitude noise) ->
    ``usable`` (>=30) -> ``stable`` (>=100) -> ``magnitude-grade`` (>=300). The LABELS are the
    thresholds module's (imported, so they cannot drift); the FLOORS are this surface's — a
    hit-rate proportion stabilises far sooner than an isotonic calibration curve."""
    n = _to_int(n_matured) or 0
    if n <= 0:
        return CONFIDENCE_NONE
    for floor, label in GEM_CONFIDENCE_TIERS:
        if n >= floor:
            return label
    return CONFIDENCE_NONE


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(float(vals[mid]), 2)
    return round((float(vals[mid - 1]) + float(vals[mid])) / 2.0, 2)


def median_taste(picks):
    """Median taste score across a shelf — the one number in the run-log summary that says how
    strong the picks actually were. ``None`` for an empty shelf."""
    return _median([p.get("taste_score") for p in (picks or [])])


def hit_rate_summary(outcomes, *, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Aggregate ``(outcome, days_to_watch)`` pairs into the run's measurement line. PURE.

    ``hit_rate`` is over MATURED picks only (hits + misses) — pending picks are excluded from
    both numerator and denominator, never counted as failures — and is ``None`` until
    something matures. ``median_days_to_watch`` covers hits only. ``confidence`` annotates the
    sample size (:func:`hit_rate_confidence`); at fewer than 30 matured picks it reads
    "directional" and the number should be read as a direction, not a measurement."""
    hits = misses = pending = 0
    days: list = []
    for entry in outcomes or []:
        if isinstance(entry, dict):
            outcome, d = entry.get("outcome"), entry.get("days_to_watch")
        else:
            outcome, d = (list(entry) + [None])[:2]
        if outcome == OUTCOME_HIT:
            hits += 1
            days.append(d)
        elif outcome == OUTCOME_MISS:
            misses += 1
        else:
            pending += 1
    matured = hits + misses
    return {
        "published": hits + misses + pending,
        "hits": hits,
        "misses": misses,
        "pending": pending,
        "matured": matured,
        "hit_rate": (round(hits / matured, 4) if matured else None),
        "median_days_to_watch": _median(days),
        "confidence": hit_rate_confidence(matured),
        "window_days": int(window_days),
    }

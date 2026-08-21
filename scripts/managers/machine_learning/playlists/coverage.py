"""coverage.py — what the household watched that we never offered.

Every measurement in this package is POSITIVE-ONLY. `outcomes.py` asks whether a
family got a play; `discovery.py` asks whether a surfaced pick was finished; the
Hidden Gems ledger asks whether a held pick matured into a watch. All three ask
the same question - *did they take what we offered* - and none can answer *what
did they want that we never showed them*.

That blind spot is not a small one. A family can score 100% on the two things it
offered while the household watched forty films it never mentioned. Hit rate
cannot see that. Nothing in this repo currently can.

    watched something we offered   -> the list worked
    watched something else         -> WE OFFERED THE WRONG THINGS   <- this module
    watched nothing                -> dormant, no information

WHY THIS IS MEASURABLE WHEN ATTRIBUTION IS NOT. Telling "played FROM the Tonight
playlist" apart from "searched for the same title" is impossible - Tautulli
records no referrer, which is why `provenance.exclusivity` exists to report how
ambiguous the attribution is. But "this item was in NO curated plan at all"
needs no referrer. It is a set-membership test, and it is exact.

THE THREE-WAY COMPARISON IS THE WHOLE DESIGN.

Comparing what they chose against what we offered is not enough, because an
attribute can be over-represented in their choices simply because it is common.
Comparing against the LIBRARY separates two failures that demand opposite
responses:

    high in MISSED, low in OFFERED, library HAS supply   -> BLIND SPOT
        we own this and never surface it. A recommendation bug. Free to fix.

    high in MISSED, low in OFFERED, library is THIN      -> SUPPLY GAP
        they reach for something we barely own. An acquisition signal.
        Recommending harder cannot fix it; there is nothing to recommend.

Collapsing those two into one number would send every finding to the wrong team.

ON CALLING THIS "ML". It is interpretable statistics - lift with smoothing and a
support floor - and that is a deliberate ceiling, not a shortcut. The corpus is
979 plays over 197 days across three profiles with real volume (717 / 177 / 60).
A model with genuine capacity would fit six people's habits tightly and you would
never see it happen. `backtest.py` moved DEFAULT_DAY_THRESHOLD 0.28 -> 0.16 on
this data, a 4.5x improvement, and that was a ONE-PARAMETER sweep on a
walk-forward split. The honest next step is to name a pattern, not to fit one.
A model earns its place when a blind spot is stable across profiles and months
and still resists a rule - and you will know, because you will be able to
describe it in a sentence first.

PURE. No I/O, no manager, no config. Plays + offered sets + attributes in,
ranked findings out.
"""
from __future__ import annotations

import math

VERDICT_BLIND_SPOT = "blind_spot"
VERDICT_SUPPLY_GAP = "supply_gap"
VERDICT_COVERED = "covered"

#: Times an attribute must appear among the MISSED items before it is reported.
#: Below this, lift is arithmetic on noise: one film with an unusual genre
#: produces an infinite ratio against an offered set that never carries it.
DEFAULT_MIN_SUPPORT = 3

#: Additive (Laplace) smoothing on both sides of the ratio. Without it any
#: attribute absent from the offered set divides by zero and sorts to the top
#: forever, which is exactly the least reliable finding masquerading as the most
#: important one.
DEFAULT_SMOOTHING = 1.0

#: Lift at or above which an attribute is worth reporting. 1.0 is parity; 2.0
#: means it is twice as prevalent in what they chose as in what we showed.
DEFAULT_MIN_LIFT = 2.0

#: Library share below which a blind spot is re-read as a SUPPLY gap. If we own
#: almost none of it, the failure is upstream of the recommender.
DEFAULT_THIN_SUPPLY = 0.02

#: Matured sample below which magnitudes should not be trusted. Mirrors the
#: language the Hidden Gems outcome table already uses: read the ORDER, not the
#: number.
DIRECTIONAL_BELOW = 30


def _norm(v) -> str:
    return " ".join(str(v or "").strip().casefold().split())


def _ids_of(entity_of_play, row) -> list:
    """A play's candidate identities as a LIST, whatever shape the resolver returns.

    ``plex/playlists/identity.build_resolver`` returns several - an episode play
    answers both ``tvdb:<series>:<s>:<e>`` and ``tvdb:<series>`` - because
    surfaces record at the level they RECOMMEND at. Coverage has to accept the
    same shapes `discovery.play_keys` does, or a show placement satisfied by an
    episode would read as a MISS here while reading as a hit there, and the two
    measurements would disagree about the same watch.
    """
    eids = entity_of_play(row)
    if eids in (None, ""):
        return []
    if isinstance(eids, (str, bytes)):
        return [str(eids)]
    return [str(e) for e in eids if e not in (None, "")]


def coverage(plays, offered, *, entity_of_play, min_pct=None) -> dict:
    """``{watched, covered, missed, coverage_rate, missed_ids}``.

    ``offered`` is the set of entity ids this profile was shown. Two fidelities,
    and the caller decides which it can afford:

      SNAPSHOT   a flat set from the plans currently on disk. Available TODAY,
                 and anachronistic - it asks "is what they watched in the list we
                 are showing NOW", not "was it in the list that day". Good enough
                 to find a systematic blind spot; useless for a trend.
      TEMPORAL   ``{entity_id: earliest_offered_ts}`` from the recommendation
                 ledger. Exact, and only as deep as the ledger, which starts
                 empty (`GLD-PLY-26`).

    A dict is treated as temporal and a play only counts as covered when it
    landed AT OR AFTER the offer. Anything else credits a list for a watch that
    preceded it - the same causality error `provenance` guards against.

    ``min_pct`` filters to plays that actually got watched, so a 2% bounce is not
    counted as a thing the household wanted.
    """
    temporal = isinstance(offered, dict)
    offered_set = {str(k) for k in (offered or ())}

    watched = covered = 0
    missed_ids: dict = {}
    for p in (plays or ()):
        if not isinstance(p, dict):
            continue
        if min_pct is not None:
            pct = p.get("percent_complete")
            try:
                if pct is None or float(pct) < float(min_pct):
                    continue
            except (TypeError, ValueError):
                continue
        eids = _ids_of(entity_of_play, p)
        if not eids:
            continue                      # unidentifiable: not evidence either way
        watched += 1
        hit = next((e for e in eids if e in offered_set), None)
        if hit is not None:
            if not temporal:
                covered += 1
                continue
            ts, offered_ts = p.get("date"), offered.get(hit)
            try:
                if offered_ts is None or float(ts) >= float(offered_ts):
                    covered += 1
                    continue
            except (TypeError, ValueError):
                covered += 1
                continue
        # MISSED: recorded under the most specific identity available, so the
        # attribute lookup has the best chance of resolving it.
        missed_ids[eids[0]] = missed_ids.get(eids[0], 0) + 1

    return {"watched": watched, "covered": covered, "missed": watched - covered,
            "coverage_rate": (covered / watched) if watched else None,
            "missed_ids": missed_ids, "fidelity": "temporal" if temporal else "snapshot"}


def _dist(ids, attributes_of) -> tuple:
    """``({attribute: count}, total_items)`` over a set of entity ids."""
    counts: dict = {}
    n = 0
    for eid in (ids or ()):
        attrs = attributes_of(str(eid)) or ()
        seen = {_norm(a) for a in attrs if _norm(a)}
        if not seen:
            continue
        n += 1
        for a in seen:
            counts[a] = counts.get(a, 0) + 1
    return counts, n


def attribute_lift(missed_ids, offered_ids, library_ids, *, attributes_of,
                   min_support: int = DEFAULT_MIN_SUPPORT,
                   smoothing: float = DEFAULT_SMOOTHING,
                   min_lift: float = DEFAULT_MIN_LIFT,
                   thin_supply: float = DEFAULT_THIN_SUPPLY) -> list:
    """Attributes over-represented in what they CHOSE versus what we SHOWED.

    Returns rows sorted by lift, descending::

        {attribute, missed_n, missed_p, offered_p, library_p, lift, verdict,
         confidence}

    ``lift`` is ``p(a|missed) / p(a|offered)`` with additive smoothing on both
    sides. Above 1.0 the attribute is more common in their choices than in our
    offers; the smoothing means an attribute we never offer produces a large but
    FINITE lift that a bigger real signal can still outrank.

    ``verdict`` splits the two failures the three-way comparison exists to
    separate - see the module docstring. An attribute the library barely holds is
    a SUPPLY_GAP however badly we under-offer it, because recommending harder
    cannot conjure files that are not there.
    """
    m_counts, m_n = _dist(missed_ids, attributes_of)
    o_counts, o_n = _dist(offered_ids, attributes_of)
    l_counts, l_n = _dist(library_ids, attributes_of)
    if not m_n:
        return []
    if not o_n:
        # NOTHING WAS OFFERED. Lift is undefined, not large: there is no offered
        # distribution to be over-represented against. An earlier version set
        # ``o_p = smoothing`` here, i.e. a probability of 1.0 - asserting we showed
        # every attribute ALL of the time, the exact inverse of the truth, which
        # drove every lift below the reporting threshold and returned nothing at
        # the moment the failure was total.
        #
        # The finding in this case is the COVERAGE number (0%), and `diagnose`
        # says so. Returning [] here is correct and deliberate.
        return []

    rows = []
    for attr, m_c in m_counts.items():
        if m_c < int(min_support):
            continue
        m_p = (m_c + smoothing) / (m_n + smoothing * 2)
        o_p = (o_counts.get(attr, 0) + smoothing) / (o_n + smoothing * 2)
        l_p = (l_counts.get(attr, 0) / l_n) if l_n else None
        lift = m_p / o_p
        if lift < float(min_lift):
            continue
        if l_p is not None and l_p < float(thin_supply):
            verdict = VERDICT_SUPPLY_GAP
        else:
            verdict = VERDICT_BLIND_SPOT
        rows.append({
            "attribute": attr, "missed_n": m_c,
            "missed_p": round(m_p, 4), "offered_p": round(o_p, 4),
            "library_p": None if l_p is None else round(l_p, 4),
            "lift": round(lift, 2), "verdict": verdict,
            "confidence": "directional" if m_n < DIRECTIONAL_BELOW else "sample",
        })
    rows.sort(key=lambda r: (-r["lift"], -r["missed_n"], r["attribute"]))
    return rows


def diagnose(plays, offered, library_ids, *, entity_of_play, attributes_of,
             min_pct=None, **lift_kw) -> dict:
    """``{coverage, findings, summary, usable}`` — the whole read for one profile.

    ``usable`` is False when the numbers exist but cannot answer the question the
    module was built for. Two cases, and BOTH have been hit on live data:

      nothing offered     no curated list was shown at all
      nothing covered     placements exist, but NO play post-dates any of them

    The second is the one that misleads. Coverage is TEMPORAL - a play counts as
    covered only if it landed at or after the offer - so a ledger recorded today
    against months of prior history reads 0% by construction. The missed set is
    then EVERYTHING watched, and the lift table stops describing a blind spot and
    starts describing the household's taste. Observed live 2026-08-10: 618
    placements across six profiles, all dated that morning, every profile
    reporting `0% coverage` with a confident-looking `genre:crime 5.52x`.

    That reads like a diagnosis and is not one, so `summary` says so explicitly
    and `findings` is EMPTIED. Returning a plausible ranked table nobody can act
    on is worse than returning nothing - it invites exactly the over-reading it
    cannot support.
    """
    cov = coverage(plays, offered, entity_of_play=entity_of_play, min_pct=min_pct)
    offered_ids = set(offered.keys()) if isinstance(offered, dict) else set(offered or ())
    findings = attribute_lift(cov["missed_ids"].keys(), offered_ids, library_ids,
                              attributes_of=attributes_of, **lift_kw)
    rate = cov["coverage_rate"]

    if rate is None:
        return {"coverage": cov, "findings": [], "usable": False,
                "summary": "no identifiable plays — nothing to measure"}

    if not offered_ids:
        # Distinct from "nothing stood out": we showed them NOTHING. The gap is
        # total, and an attribute breakdown would only describe their taste.
        return {"coverage": cov, "findings": [], "usable": False,
                "summary": (f"{rate:.0%} coverage — no curated list was offered at all, "
                            f"so there is no attribute comparison to make")}

    if cov["covered"] == 0 and cov["fidelity"] == "temporal":
        # The ledger is younger than the history. Not a finding, a start date.
        return {"coverage": cov, "findings": [], "usable": False,
                "summary": (f"0% coverage — {len(offered_ids)} placement(s) recorded, but NO "
                            f"play post-dates any of them. The ledger starts now; this "
                            f"becomes measurable as the household watches. Not a blind spot.")}

    top = findings[0] if findings else None
    summary = (f"{rate:.0%} of what they watched was on a list we showed"
               + (f" · biggest gap: {top['attribute']} "
                  f"({top['lift']}x, {top['verdict'].replace('_', ' ')}"
                  + (f", {top['confidence']}" if top['confidence'] == 'directional' else "")
                  + ")" if top else " · no attribute stands out"))
    return {"coverage": cov, "findings": findings, "usable": True, "summary": summary}

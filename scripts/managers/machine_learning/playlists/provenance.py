"""provenance.py — exactly what we put where, for which day, and what happened.

`outcomes.py` keeps a per-FAMILY tally. This keeps the ROW it is derived from:
one entry per (item, container, generation). That is the difference between
"Tonight scored 0.41 this month" and "we put S07E03 into playlist 9001 for
Tuesday the 11th, and it was watched at 21:40 on Wednesday the 12th, 1.03 days
late, and it was also sitting in Up Next at the time".

WHAT THIS DOES NOT SOLVE. Tautulli records no referrer - nothing says which list
they clicked. Recording the container ratingKey improves PROVENANCE (what we
offered, where, when) and not CAUSATION (what they used). Any entry whose item
was in more than one container at play time stays genuinely ambiguous, and
`exclusivity` exists to say how much of the data is in that state rather than
letting an inferred number pass for a measured one.

WHAT IT DOES SOLVE:

* **Signed offsets, not in/out of a bucket.** A show watched consistently one
  day late is a SCHEDULING error worth fixing; one watched at random offsets is
  a RANKING error. Attribution in outcomes.py is deliberately binary on the day,
  which cannot tell those apart - this can.
* **Evidence for the strict rule.** `grace_curve` reports what a grace window
  WOULD have bought. Attribution no longer has a grace knob and should not get
  one back, but the curve says how much credit strictness is giving up, and
  whether Tonight is systematically one day off rather than randomly wrong.
* **Write verification.** `confirmed` records whether the item was actually
  observed in the container, so an intended-but-failed write is not later
  counted as a recommendation the household ignored.

PURE. Caller loads the ledger, appends entries, supplies plays, persists.
"""
from __future__ import annotations

#: Entries older than this are pruned. A year of daily lists across a dozen
#: families is ~40k rows, which is fine as JSON but not worth keeping forever;
#: the aggregate in outcomes.py is the long-term memory.
DEFAULT_RETENTION_DAYS = 120

_DAY = 86400.0


def empty_ledger() -> dict:
    return {"entries": [], "updated_at": None}


def record(ledger, *, item_rk, container_rk, container_kind, family,
           profile=None, target_date=None, generated_at=None,
           item_key=None, confirmed=None) -> dict:
    """Append one placement.

    ``target_date`` is a unix ts for the day the item was CHOSEN FOR - midnight
    local of that day is the natural value. It is per-ENTRY, not per-run,
    because a list built a week ahead has a different target per day and a
    single run-level target would smear them together.

    ``confirmed`` is tri-state on purpose: True (seen in the container), False
    (write failed), None (not checked). None must not be read as False - an
    unverified placement is not a failed one, and counting it as such would
    invent misses (P-C).
    """
    ledger = dict(ledger or empty_ledger())
    ledger.setdefault("entries", [])
    ledger["entries"].append({
        "item_rk": str(item_rk),
        "item_key": item_key,
        "container_rk": str(container_rk) if container_rk is not None else None,
        "container_kind": container_kind,
        "family": family,
        "profile": profile,
        "target_date": target_date,
        "generated_at": generated_at,
        "confirmed": confirmed,
        "watched_at": None,
    })
    ledger["updated_at"] = generated_at
    return ledger


def resolve(ledger, plays, *, now=None, retention_days=DEFAULT_RETENTION_DAYS) -> tuple:
    """Match plays onto open entries. Returns ``(ledger, resolved)``.

    ``plays`` are ``{"item_rk", "date", "profile"}``. A play matches an entry
    when the item and profile agree and the play is NOT BEFORE the entry was
    generated - a watch that predates the recommendation cannot have been caused
    by it, and crediting it would let a list take credit for a habit it merely
    described.

    ``resolved`` carries a signed ``offset_days``: negative = watched early,
    0 = on the target day, positive = late.
    """
    ledger = dict(ledger or empty_ledger())
    entries = list(ledger.get("entries") or [])

    by_item: dict = {}
    for p in (plays or ()):
        if not isinstance(p, dict) or p.get("item_rk") is None:
            continue
        by_item.setdefault((str(p["item_rk"]), p.get("profile")), []).append(p)

    resolved = []
    for e in entries:
        if e.get("watched_at") is None:
            cands = by_item.get((e["item_rk"], e.get("profile"))) or []
            gen = e.get("generated_at")
            hits = [p for p in cands
                    if gen is None or _num(p.get("date")) >= _num(gen)]
            if hits:
                e["watched_at"] = min(_num(p.get("date")) for p in hits)
        if e.get("watched_at") is not None and e.get("target_date") is not None:
            off = (_num(e["watched_at"]) - _num(e["target_date"])) / _DAY
            e["offset_days"] = off
            resolved.append(dict(e))

    if retention_days and now is not None:
        cutoff = _num(now) - float(retention_days) * _DAY
        entries = [e for e in entries
                   if e.get("generated_at") is None or _num(e["generated_at"]) >= cutoff]
    ledger["entries"] = entries
    ledger["updated_at"] = now
    return ledger, resolved


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def jitter_histogram(resolved, family=None) -> dict:
    """``{whole_days_late: count}`` - the shape of the household's slippage.

    Floored to whole days so a play at 00:30 the next morning reads as +0 if it
    is still inside the target day locally, which is what a household means by
    "we watched it Tuesday night".
    """
    out: dict = {}
    for e in resolved or ():
        if family and e.get("family") != family:
            continue
        d = int(e.get("offset_days", 0) // 1)
        out[d] = out.get(d, 0) + 1
    return out


def grace_curve(resolved, family=None, *, max_grace: int = 7) -> list:
    """``[(grace_days, hit_rate, hits, total)]`` - what strictness costs.

    DIAGNOSTIC ONLY. Attribution in outcomes.py is binary on the target day and
    has no grace knob, deliberately. This does not set anything; it reports what
    a grace window WOULD have bought, which answers two questions the binary rule
    cannot: how much credit strict scoring is giving up, and whether a family is
    systematically one day off (a scheduling bug, fixable) rather than randomly
    wrong (a ranking bug, a different fix).
    """
    offs = [e.get("offset_days", 0.0) for e in (resolved or ())
            if not family or e.get("family") == family]
    total = len(offs)
    rows = []
    for g in range(0, int(max_grace) + 1):
        hits = sum(1 for o in offs if abs(o) < (g + 1))
        rows.append((g, (hits / total) if total else None, hits, total))
    return rows


def exclusivity(ledger, *, window_days: float = 1.0) -> dict:
    """``{"exclusive", "shared", "rate"}`` - how much of the data is unambiguous.

    An item placed in only ONE container around a given time can be attributed
    with confidence; one sitting in Tonight and Up Next at once cannot. This
    does not resolve the ambiguity, it MEASURES it - so a hit rate derived from
    the ambiguous half can be discounted honestly rather than quoted flat.
    """
    seen: dict = {}
    for e in (ledger or {}).get("entries") or ():
        gen = _num(e.get("generated_at"))
        bucket = int(gen // (window_days * _DAY)) if gen else 0
        seen.setdefault((e["item_rk"], e.get("profile"), bucket), set()).add(e.get("family"))
    excl = sum(1 for fams in seen.values() if len(fams) == 1)
    shared = len(seen) - excl
    total = len(seen)
    return {"exclusive": excl, "shared": shared,
            "rate": (excl / total) if total else None}


def by_container(ledger, container_rk) -> list:
    """Every placement into one Plex object, newest first.

    Keyed on the container's ratingKey rather than the family name because a
    recreate mints a NEW ratingKey for the same family - so a family's history
    can span several containers, and only this view shows where the seam is.
    """
    rows = [e for e in (ledger or {}).get("entries") or ()
            if str(e.get("container_rk")) == str(container_rk)]
    return sorted(rows, key=lambda e: -_num(e.get("generated_at")))

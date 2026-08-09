"""outcomes.py — did the thing we surfaced actually get watched?

Every family glidearr publishes (Up Next, Tonight, Fresh Arrivals, Hidden Gems,
each mood list, each collection) is a RECOMMENDATION, and until now nothing
recorded whether any of them worked. `engagement.py` measures this for saga
groups inside the per-user builder; this does it per FAMILY, across both the
per-user playlists and the account-wide shelves, so the two are comparable.

THE MEASUREMENT. On each run a family surfaces a set of ratingKeys. On a later
run, some of those appear in the profile's watched set. The hit rate is
    engaged / surfaced
over a window. That is a direct answer to "is Tonight working", and it is the
signal that lets the ranking be tuned against evidence instead of taste.

TWO TRAPS THIS AVOIDS, both learned from engagement.py:

* A LOW HIT RATE IS NOT FAILURE IF NOBODY WATCHED ANYTHING. A quiet fortnight
  makes every family look broken. So activity is recorded alongside, and a
  window with less than `min_activity` total watches is marked DORMANT and
  excluded from the rate rather than counted as a miss.
* ONE PLAY IS CREDITED TO EXACTLY ONE FAMILY. The same episode can sit in Up
  Next and Tonight at once; counting it for both would let two families each
  claim a 100% hit rate off a single play. The loser records it as `shared`, so
  the overlap itself stays visible rather than being silently dropped.

PRECEDENCE, in order:
  1. a SCHEDULED family that hit its target day (see SCHEDULED_FAMILIES);
  2. otherwise the family that surfaced the item EARLIEST.

Rule 1 exists because Up Next is a standing list - an item usually sits in it
for days before Tonight picks it up for a specific evening - so ranking on
earliest-surfaced alone handed Up Next the credit even on Tonight's own target
day, which inverts the whole point.

PURE. The caller loads prior state, supplies the surfaced sets and the watched
set, and persists the result.
"""
from __future__ import annotations

#: Runs of history kept per family. Enough to see a trend, small enough that the
#: artifact stays cheap to load every run.
MAX_HISTORY = 60

#: Total watches in a window below which it is DORMANT and scores nothing.
DEFAULT_MIN_ACTIVITY = 3

#: Families that target a specific DAY rather than standing open. A scheduled
#: family only claims a play that lands on its target day; otherwise credit
#: falls through to whichever continuous family also surfaced the item.
#:
#: THIS IS AN INFERENCE, NOT A MEASUREMENT. Tautulli records no referrer on a
#: history row - there is no field saying which list a play came from. So "they
#: watched it from Up Next" cannot be read off the data. The rule below is the
#: honest substitute: a list that aimed at Tuesday and got watched on Tuesday
#: gets the credit; a list that aimed at Tuesday and got watched on Friday
#: MISSED ITS SLOT, and the standing list that was also offering it did the work.
SCHEDULED_FAMILIES = {"tonight"}

# NO GRACE. A scheduled family is credited if and only if the play lands on the
# LOCAL CALENDAR DAY it was built for. There is deliberately no knob to soften
# this, because softening it destroys the thing the measurement is for.
#
# THE REASONING. Tonight's claim is not "you will watch this" - it is "you will
# watch this ON TUESDAY". A Wednesday play means the show was right and the DAY
# was wrong, and that is precisely the error the weekday model needs to see. Give
# it partial credit and a model that is systematically one day off scores well
# and never corrects. Strict scoring makes a wrong day indistinguishable from a
# wrong show at the family level, which is what forces the day prediction to
# actually get good.
#
# The play is not lost: it falls through to whichever CONTINUOUS family also
# surfaced the item, which is the honest reading anyway - the list that was
# standing open on Wednesday is the one that did the work.
#
# Calendar day, not a rolling 24h: "the day it was generated for" is what a
# household means, and a rolling window would credit a Monday 23:50 play to a
# Tuesday list.
#
# This pairs with habits.DEFAULT_JITTER_SIGMA_DAYS = 0.0. They are ONE decision:
# a model that spreads a play across neighbouring days must be scored across
# those days too, or it is penalised for doing exactly what it was told.


def empty_outcomes() -> dict:
    """Canonical empty shape, so a first run and a loaded one look identical."""
    return {"families": {}, "updated_at": None, "watched_count": 0}


def _fam(state: dict, family: str) -> dict:
    return state["families"].setdefault(family, {
        "surfaced_total": 0, "engaged_total": 0, "shared_total": 0,
        "dormant_windows": 0, "missed_total": 0, "runs": 0,
        "history": [], "pending": {},
    })


def record_surfaced(state, family: str, rating_keys, *, now=None,
                    target_day=None) -> dict:
    """Note what ``family`` put in front of the household this run.

    Stored as ``pending`` - a key is not a hit or a miss yet, it is an open
    question. It resolves on a later call to :func:`measure`, whenever the item
    is watched or the window closes.

    ``target_day`` (0=Mon..6=Sun) is what a SCHEDULED family was built for.
    Continuous families leave it None.
    """
    state = dict(state or empty_outcomes())
    state.setdefault("families", {})
    f = _fam(state, family)
    f["runs"] = int(f.get("runs") or 0) + 1
    pending = dict(f.get("pending") or {})
    for rk in (rating_keys or ()):
        # Keep the FIRST time we showed it, plus the day it was aimed at. Both
        # are needed: the timestamp orders competing claims within a rank, the
        # target day decides whether a scheduled family may claim at all.
        if str(rk) not in pending:
            pending[str(rk)] = {"first": now, "target_day": target_day}
    f["pending"] = pending
    f["surfaced_total"] = int(f.get("surfaced_total") or 0) + len(rating_keys or ())
    state["updated_at"] = now
    return state


def _entry(v):
    """Pending values are dicts; tolerate the older bare-timestamp shape."""
    if isinstance(v, dict):
        return v.get("first"), v.get("target_day")
    return v, None


def _on_target_day(target_day, watched_day) -> bool:
    """Did the play land on the exact local day this family was built for?

    A scheduled family with NO target day recorded claims normally - absent is
    not the same as missed, and refusing credit for un-targeted surfacings would
    silently zero a family that simply predates the field (P-C).
    """
    if target_day is None or watched_day is None:
        return True
    return int(target_day) % 7 == int(watched_day) % 7


def _winner(claim, rk):
    c = claim.get(rk)
    return c[2] if c else None


def measure(state, watched_keys, *, now=None, min_activity=DEFAULT_MIN_ACTIVITY,
            watched_day=None, scheduled=None) -> tuple:
    """Resolve every family's pending set against ``watched_keys``.

    Returns ``(state, report)``. ``report`` is ``{family: {...}}`` with the
    window's engaged / shared / missed counts and the running hit rate.

    ``watched_day`` is the local weekday (0=Mon..6=Sun) the plays happened on;
    it is what a scheduled family's target is tested against.

    A watched item is credited to exactly ONE family, by the precedence in the
    module docstring: scheduled-on-its-day first, then earliest-surfaced.
    """
    state = dict(state or empty_outcomes())
    state.setdefault("families", {})
    watched = {str(k) for k in (watched_keys or ())}

    grew = max(0, len(watched) - int(state.get("watched_count") or 0))
    dormant = grew < int(min_activity)

    sched = SCHEDULED_FAMILIES if scheduled is None else set(scheduled)
    claim: dict = {}                      # rk -> (rank, first_seen, family)
    for family, f in state["families"].items():
        for rk, raw in (f.get("pending") or {}).items():
            if rk not in watched:
                continue
            first_seen, target_day = _entry(raw)
            # A scheduled family that missed its day is not a candidate at all,
            # so the play falls through to a continuous family that also
            # surfaced it. That is the "watched it a day late from Up Next"
            # case: Tonight aimed at Tuesday and missed; Up Next was standing
            # open and did the work.
            on_day = _on_target_day(target_day, watched_day)
            if family in sched and not on_day:
                continue
            # PRECEDENCE. Up Next is a STANDING list: an item is usually sitting
            # in it for days before Tonight picks it up for a specific evening.
            # Ranking on first-surfaced alone therefore handed Up Next the credit
            # even on Tonight's own target day, which inverts the rule. So a
            # scheduled family that HIT its day outranks every continuous family
            # regardless of surfacing order; ties inside a rank still fall to
            # earliest-surfaced.
            rank = 0 if (family in sched and on_day) else 1
            # None sorts as "unknown, treat as oldest" so an un-timestamped
            # surfacing never steals credit from a dated one.
            key = (float("inf") if first_seen is None else float(first_seen))
            prior = claim.get(rk)
            if prior is None or (rank, key) < (prior[0], prior[1]):
                claim[rk] = (rank, key, family)

    report: dict = {}
    for family, f in state["families"].items():
        pending = dict(f.get("pending") or {})

        # A scheduled family that aimed at the wrong day records MISSED and
        # nothing else. It is NOT also "shared": shared means another family was
        # offering the same thing and got there first, whereas this family was
        # disqualified for picking the wrong evening. Counting one play in both
        # buckets would make a day error look partly like a competition loss and
        # hide the signal the strict rule exists to expose.
        missed = [rk for rk in pending if rk in watched
                  and family in sched
                  and not _on_target_day(_entry(pending[rk])[1], watched_day)]
        _miss = set(missed)
        hits = [rk for rk in pending
                if rk in watched and rk not in _miss and _winner(claim, rk) == family]
        shared = [rk for rk in pending
                  if rk in watched and rk not in _miss and _winner(claim, rk) != family]

        if dormant:
            f["dormant_windows"] = int(f.get("dormant_windows") or 0) + 1
        else:
            f["engaged_total"] = int(f.get("engaged_total") or 0) + len(hits)
            f["shared_total"] = int(f.get("shared_total") or 0) + len(shared)
            if missed:
                f["missed_total"] = int(f.get("missed_total") or 0) + len(missed)

        for rk in hits + shared + missed:
            pending.pop(rk, None)             # resolved either way
        f["pending"] = pending

        surfaced = int(f.get("surfaced_total") or 0)
        engaged = int(f.get("engaged_total") or 0)
        rate = (engaged / surfaced) if surfaced else None
        entry = {"engaged": len(hits), "shared": len(shared),
                 "missed_day": len(missed), "pending": len(pending),
                 "dormant": dormant, "surfaced_total": surfaced,
                 "engaged_total": engaged,
                 "missed_total": int(f.get("missed_total") or 0),
                 "hit_rate": rate, "activity": grew}
        hist = list(f.get("history") or [])
        hist.append({"at": now, "engaged": len(hits), "shared": len(shared),
                     "missed_day": len(missed), "dormant": dormant,
                     "activity": grew})
        f["history"] = hist[-MAX_HISTORY:]
        report[family] = entry

    state["watched_count"] = len(watched)
    state["updated_at"] = now
    return state, report


def leaderboard(report: dict) -> list:
    """``[(family, hit_rate, engaged_total, surfaced_total)]`` best first.

    Families with no resolved surfacing yet sort LAST rather than at 0.0 - a
    family that has never had the chance to be watched is not a bad
    recommender, and ranking it as one would bury a new shelf permanently.
    """
    rows = []
    for family, r in (report or {}).items():
        rows.append((family, r.get("hit_rate"),
                     r.get("engaged_total", 0), r.get("surfaced_total", 0)))
    return sorted(rows, key=lambda t: (t[1] is None, -(t[1] or 0.0), str(t[0])))

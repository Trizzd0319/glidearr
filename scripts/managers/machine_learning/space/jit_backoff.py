"""
jit_backoff.py — send a repeatedly-fruitless series to the BACK of the JIT queue.
================================================================================
THE MEASUREMENT THAT BUILT THIS (2026-08-21 daemon window, 298 minutes):

    step-downs: 146      grabs: 14      exhausted: 16      reverts: 10
    => ~112 seconds per profile-flip-and-search

    Johnny Bravo      37 steps   0 grabs
    Attack on Titan   22 steps   0 grabs
    Ted Lasso         21 steps   0 grabs
    Abbott & Costello 18 steps   0 grabs
    TMNT              18 steps   0 grabs

**116 of 146 step-downs went to five series that grabbed nothing**, and every one
of them ended `queued for retry next run`. ``_reconcile_failed_jit`` then reset
their flags and DELETED the ledger key, so the next run began with no memory that
any of it had happened. Same five series, same 116 flips, indefinitely.

WHY A QUEUE AND NOT A COOLDOWN
------------------------------
``legacy_regrab`` benches a file for 14 days. ``GLD-SON-02`` is the record of that
going wrong: 814 of 881 files sat benched on evidence never gathered, because a
disabled or rate-limited indexer looks EXACTLY like "no release exists".

The same ambiguity is live right now. Those five titles are old, obscure or anime
-- precisely the content that lives on the 13 torrent/anime indexers currently
DISABLED on this deployment. Bench them and they stay benched after the indexers
come back, because nothing re-examines a benched title.

So this module ORDERS, it does not EXCLUDE:

* a demoted series is still attempted, just LAST, after fresh candidates have had
  the run's budget;
* the moment supply changes it grabs, and :func:`record_success` clears its counter
  with no operator action;
* nothing is ever permanently ineligible, so there is no benched population to
  audit, un-bench, or forget about.

Degrading the ORDER fails safe. Degrading ELIGIBILITY does not.

Pure module: ledger dicts in, ordering out. No I/O, stdlib only.
"""
from __future__ import annotations

# ── Policy constants ─────────────────────────────────────────────────────────
# NOT CONFIGURABLE, deliberately. These are a correctness fix, not a preference:
# without them a series that cannot be satisfied consumes a run's search budget
# forever. A knob here would be a knob to re-enable the defect, and an operator
# who set it wrong would see no error -- only a daemon that quietly takes three
# hours again.
#
# The reason a wrong value cannot do real harm is that both limits DEGRADE
# ORDERING, never eligibility, and the miss counter RESETS on any grab: a
# productive ladder is never truncated, and a demoted series is always attempted
# again. So the cost of a bad constant is bounded by "a bit more or less search
# per run" -- not worth a config surface, an onboarding leaf and a schema entry.

# Consecutive full-ladder exhaustions before a series is demoted at all. 1 =
# demote as soon as it fails once; the first failure is still the cheapest
# possible evidence that supply is missing.
DEMOTE_AFTER = 1

# Max consecutive misses INSIDE one series' ladder before the walk stops. This is
# the limit that actually buys the time back: measured 2026-08-21, one series
# walked 37 profiles at ~112s each and grabbed nothing. The TAIL of a ladder is
# where releases are rarest, so the marginal profile is both the least likely to
# grab and exactly as expensive as the first.
MAX_CONSECUTIVE_MISSES = 4

# A series with N exhaustions is attempted every Nth run, capped here. A SOFT
# cooldown -- it slows re-attempts without ever making one impossible.
ATTEMPT_EVERY_N_RUNS_CAP = 6

LEDGER_KEY_TMPL = "sonarr/{instance}/jit/backoff"
RUN_SEQ_KEY_TMPL = "sonarr/{instance}/jit/run_seq"


def ledger_key(instance: str) -> str:
    return LEDGER_KEY_TMPL.format(instance=instance)


def run_seq_key(instance: str) -> str:
    """Monotonic per-instance run counter backing the soft cooldown.

    A COUNT rather than a timestamp on purpose: the schedule is "every Nth run",
    not "every N hours". Wall-clock would make the cooldown depend on how often the
    operator happens to run Glidearr -- twice an hour would bench a series for
    minutes, once a week for months -- when what is being rationed is a share of
    each run's finite search budget.
    """
    return RUN_SEQ_KEY_TMPL.format(instance=instance)


def _entry(led, sid) -> dict:
    e = (led or {}).get(str(sid))
    return e if isinstance(e, dict) else {}


def exhaustions(led, sid) -> int:
    """Consecutive full-ladder exhaustions for one series. 0 when unknown -- an
    unseen series must sort with the fresh ones, never with the demoted."""
    try:
        return max(0, int(_entry(led, sid).get("exhaustions") or 0))
    except (TypeError, ValueError):
        return 0


def record_exhaustion(led, sid, *, run_seq: int) -> dict:
    """A NEW ledger with this series' counter incremented. Returns a copy; the
    caller persists it, so a crash mid-pass cannot leave a half-written ledger."""
    out = dict(led or {})
    e = dict(_entry(led, sid))
    e["exhaustions"] = exhaustions(led, sid) + 1
    e["last_run"] = int(run_seq)
    out[str(sid)] = e
    return out


def record_success(led, sid) -> dict:
    """Clear a series' demotion. Called on ANY successful grab.

    This is what makes the queue self-healing: when a disabled indexer is
    re-enabled, or a release finally appears, the series grabs once and returns to
    full priority on its own. No un-benching, no operator step, and no stale
    benched population accumulating unnoticed.
    """
    out = dict(led or {})
    out.pop(str(sid), None)
    return out


def due(led, sid, *, run_seq: int) -> bool:
    """Is this series due for an attempt on this run?

    A series with N exhaustions is attempted every Nth run, capped by
    :data:`ATTEMPT_EVERY_N_RUNS_CAP`. Deliberately NOT a hard gate: the cap bounds
    how slow a re-attempt can get, so even the most-demoted series is tried
    regularly.
    """
    n = exhaustions(led, sid)
    if n < DEMOTE_AFTER:
        return True
    every = min(n, ATTEMPT_EVERY_N_RUNS_CAP)
    if every <= 1:
        return True
    last = _entry(led, sid).get("last_run")
    try:
        last = int(last)
    except (TypeError, ValueError):
        return True                    # never recorded => treat as due
    return (int(run_seq) - last) >= every


def order_series(sids, led, *, run_seq: int) -> tuple:
    """``(attempt_now, deferred)`` -- the run's series order, fresh first.

    Within each bucket the caller's INCOMING order is preserved, so whatever
    next-up priority produced the list still decides ties. This adds a demotion
    axis; it does not replace the existing one.
    """
    now, later = [], []
    for sid in (sids or []):
        (now if due(led, sid, run_seq=run_seq) else later).append(sid)
    now.sort(key=lambda s: exhaustions(led, s))          # stable: ties keep caller order
    return now, later


def prune(led, *, keep_sids) -> dict:
    """Drop entries for series no longer in the library, so the ledger cannot grow
    without bound. Called with the CURRENT candidate universe, not one run's slice,
    or a series merely absent tonight would lose its history."""
    keep = {str(s) for s in (keep_sids or [])}
    return {k: v for k, v in (led or {}).items() if k in keep}


def summarise(led) -> dict:
    """``{demoted, worst_sid, worst_n}`` for the run summary -- a demotion nobody
    can see is the same defect this session has hit repeatedly (``GLD-ACQS-20``,
    ``GLD-SON-24``): an outcome counted and then omitted from the line an operator
    actually reads."""
    if not isinstance(led, dict) or not led:
        return {"demoted": 0, "worst_sid": None, "worst_n": 0}
    pairs = [(k, exhaustions(led, k)) for k in led]
    pairs = [(k, n) for k, n in pairs if n >= DEMOTE_AFTER]
    if not pairs:
        return {"demoted": 0, "worst_sid": None, "worst_n": 0}
    worst = max(pairs, key=lambda x: x[1])
    return {"demoted": len(pairs), "worst_sid": worst[0], "worst_n": worst[1]}

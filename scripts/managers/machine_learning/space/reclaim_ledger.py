"""
reclaim_ledger.py — one shared, run-scoped record of space every pass has PLANNED to free.
================================================================================
THE PROBLEM. Every space pass reads the same free-space figure from the same shared
mount and then plans its full deficit independently. Measured on a live run:

    standard  922.8 GB free, band top 5500  ->  planned  396 GB
    ultra     922.8 GB free, band top 5500  ->  planned  205 GB
    Sonarr TV 922.8 GB free, band top 5500  ->  planned  476 GB

Three passes, ~1077 GB of planned reclaim, one 922 GB pool, and not one of them knew
the others had already committed to part of it. They were not cooperating on a
deficit; they were racing the same number. Under live deletion that over-reclaims:
each pass shrinks or evicts enough for the WHOLE gap, so the household ends up well
above the band top with quality given away for nothing.

THE FIX IS A TREE, NOT A LOCK. Passes stay independent and ordered; each simply adds
what earlier passes have already planned to its effective free space:

    effective_free = free_gb + planned_reclaim_gb(cache)
    need_gb        = max(0, band_top - effective_free)

So the first pass sees the full deficit, the second sees what is left, and a pass
that arrives after the gap is closed plans nothing at all. The last pass in the chain
is deletion, which therefore only ever sees the deficit that shrinking could not
cover — the codebase's stated policy ("deletion is the true last resort",
"EVERYTHING shrinks before anything is deleted") made arithmetic rather than
sequencing.

This is the same idea as the in-pass ``inflight_regrab_gb`` subtraction — "never
downgrade against phantom headroom" — lifted from within one pass to across all of
them.

PROJECTED, NOT REALIZED. A pass records what it PLANNED, not what landed. That is the
point: the replacements have not imported yet, and a later pass must not re-plan
space an earlier one has already committed to freeing. Realized reclaim shows up on
its own as free space grows between runs.

RUN-SCOPED. ``reset_planned_reclaim`` is called once at the start of a run; entries
are keyed by pass so a re-entrant pass overwrites rather than double-counts.
"""
from __future__ import annotations

RECLAIM_LEDGER_KEY = "space/planned_reclaim"


def reset_planned_reclaim(cache) -> None:
    """Clear the ledger. Call ONCE at the start of a run, before any space pass.

    Not calling it is a stale-state bug in the dangerous direction: last run's planned
    reclaim would be added to this run's free space, so every pass would believe the
    deficit is already covered and plan nothing. A run that reclaims nothing is safe
    but useless, and it would look exactly like a healthy disk.
    """
    if cache is None:
        return
    try:
        cache.set(RECLAIM_LEDGER_KEY, {})
    except Exception:
        pass


def record_planned_reclaim(cache, pass_key: str, gb: float) -> None:
    """Record that *pass_key* plans to free *gb*. Keyed, so a pass that runs twice
    overwrites its own entry instead of double-counting itself."""
    if cache is None or not pass_key:
        return
    try:
        gb = float(gb or 0.0)
    except (TypeError, ValueError):
        return
    if gb <= 0:
        return
    try:
        led = cache.get(RECLAIM_LEDGER_KEY)
        led = dict(led) if isinstance(led, dict) else {}
        led[str(pass_key)] = gb
        cache.set(RECLAIM_LEDGER_KEY, led)
    except Exception:
        pass


def planned_reclaim_gb(cache, exclude: str = "") -> float:
    """Total GB every earlier pass has planned to free this run.

    ``exclude`` drops one pass's own entry, so a re-entrant pass does not credit itself
    with space it has not freed. Returns 0.0 on any failure — an unreadable ledger must
    make a pass plan its FULL deficit (it may over-plan, which the apply loop's own
    stop-at-band-top still bounds) rather than plan nothing at all.
    """
    if cache is None:
        return 0.0
    try:
        led = cache.get(RECLAIM_LEDGER_KEY)
    except Exception:
        return 0.0
    if not isinstance(led, dict):
        return 0.0
    total = 0.0
    for k, v in led.items():
        if exclude and str(k) == str(exclude):
            continue
        try:
            total += float(v or 0.0)
        except (TypeError, ValueError):
            continue
    return max(0.0, total)


def planned_reclaim_breakdown(cache) -> dict:
    """``{pass_key: gb}`` as recorded — for the end-of-run summary, so an operator can see
    WHICH pass claimed which part of the deficit rather than one opaque total."""
    if cache is None:
        return {}
    try:
        led = cache.get(RECLAIM_LEDGER_KEY)
    except Exception:
        return {}
    return dict(led) if isinstance(led, dict) else {}

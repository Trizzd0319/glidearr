"""
ledger/pending_plan.py — planned actions for titles that have NO Parquet row yet.
================================================================================
``decision_ledger.stamp`` writes ``planned_action`` / ``plan_reason`` /
``plan_reclaim_gb`` onto a Parquet ROW, and ``plan_summary`` rolls those columns
up into the end-of-run "Change plan" grid and "Dry-run plan ledger".

That works for every pass which acts on media the library already holds —
downgrades, upgrades, deletes, JIT next-up grabs. It structurally CANNOT work for
acquisition: a title being added to Radarr/Sonarr for the first time has no row
to stamp, because it is not in the library yet.

The consequence was measured on the 2026-08-20 19:23 run. The grid, whose own
title reads *"Change plan - every planned action this run"*, reported::

    acquire   19   -25.2 GB      (all Sonarr next-up episodes)
    TOTAL    292  +242.7 GB      <- reads as a run that FREES space

while the acquisition pass in the same run reported ``97 funded (~312 GB)``.
Not one of those 97 appears — there is no ``> movies`` sub-row under ``acquire``
at all. An operator reading the ledger would conclude the run nets +242 GB free;
the true figure is roughly 70 GB consumed. A summary that omits the largest flow
in the run is worse than no summary, because it is trusted.

This module is the rowless half: passes that create NEW media record their
planned bytes here, and ``plan_summary`` folds them in beside the row-derived
ones so both land in one table with one TOTAL.

Sign convention is inherited verbatim from ``decision_ledger``: **+GiB freed,
-GiB consumed**. An acquisition is therefore negative. Getting this backwards
would flip the TOTAL's meaning, so ``record_pending`` refuses an unsigned zero
rather than guessing.

Pure module: no I/O beyond the injected cache, stdlib only.
"""
from __future__ import annotations

PENDING_PLAN_KEY = "ledger/pending_plan"


def reset_pending(cache) -> None:
    """Clear the pending ledger. Call ONCE at the start of a run, before any pass.

    Same discipline, and the same hazard, as ``space/reclaim_ledger``: without a
    reset, last run's planned acquisitions would be folded into this run's totals.
    That is the dangerous direction here — the grid would over-report consumption,
    an operator would believe the array is filling faster than it is, and the
    natural reaction is to delete media that did not need deleting.
    """
    if cache is None:
        return
    try:
        cache.set(PENDING_PLAN_KEY, {})
    except Exception:
        pass


def _row_key(service, instance, action, ext_id, title) -> str:
    """Identity for one planned action. ``ext_id`` when present, else the title —
    keyed so a re-entrant pass OVERWRITES its own entry instead of double-counting,
    which is exactly why ``reclaim_ledger`` keys by pass rather than appending."""
    ident = str(ext_id) if ext_id not in (None, "") else str(title or "?")
    return f"{service}:{instance}:{action}:{ident}"


def record_pending(cache, *, service: str, instance: str, action: str,
                   title: str, gb, reason: str = "", ext_id=None) -> None:
    """Record a planned action on media with no Parquet row.

    ``gb`` follows ``decision_ledger``'s convention: **+GiB freed, -GiB consumed**,
    so an acquisition passes a NEGATIVE value. A value that will not parse is
    dropped rather than stored as 0 — a silent zero would understate the run's
    consumption in the one table an operator uses to sanity-check it (P-C).
    """
    if cache is None or not action:
        return
    try:
        gb_f = float(gb)
    except (TypeError, ValueError):
        return
    try:
        led = cache.get(PENDING_PLAN_KEY)
        led = dict(led) if isinstance(led, dict) else {}
        led[_row_key(service, instance, action, ext_id, title)] = {
            "service": str(service), "instance": str(instance),
            "action": str(action), "title": str(title or "")[:80],
            "gb": round(gb_f, 2), "reason": str(reason or "")[:60],
        }
        cache.set(PENDING_PLAN_KEY, led)
    except Exception:
        pass


def pending_rows(cache) -> list:
    """Every recorded pending action, as a list of dicts. Empty on any failure:
    a missing pending ledger must not break the summary that reads it."""
    if cache is None:
        return []
    try:
        led = cache.get(PENDING_PLAN_KEY)
    except Exception:
        return []
    if not isinstance(led, dict):
        return []
    return [v for v in led.values() if isinstance(v, dict)]


def fold_into(agg: dict, detail: dict, rows) -> int:
    """Merge pending rows into ``plan_summary.summarize``'s two accumulators,
    in place, using the identical shapes it builds from Parquet:

    * ``agg[action]           = [count, gb]``
    * ``detail[action][svc][inst] = [count, gb]``

    Returns the number of rows folded. Both structures are updated from the SAME
    iteration so a subtotal can never disagree with its parent row -- the property
    ``summarize`` deliberately preserves by populating both in one groupby pass.
    """
    n = 0
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        action = str(r.get("action") or "")
        if not action:
            continue
        try:
            gb = float(r.get("gb") or 0.0)
        except (TypeError, ValueError):
            gb = 0.0
        a = agg.setdefault(action, [0, 0.0])
        a[0] += 1
        a[1] += gb
        d = (detail.setdefault(action, {})
                   .setdefault(str(r.get("service") or "?"), {})
                   .setdefault(str(r.get("instance") or "?"), [0, 0.0]))
        d[0] += 1
        d[1] += gb
        n += 1
    return n

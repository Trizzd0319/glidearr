"""
plex/playlists/readiness.py — diagnose TV-playlist readiness + degradation messaging.
================================================================================
This ships to other operators whose servers WON'T all hit 100% join coverage, and
whose watchability scores depend on the background enrichment daemon. So every
less-than-ideal outcome must produce a clear, actionable, NON-silent message — we
never leave anyone staring at an empty or wrong-looking playlist wondering why.

Two themes the operator must always understand:
  * COVERAGE — how much of the owned library could be matched to Plex (legacy TV
    agents without TVDB ids resolve poorly); we still build with whatever matched.
  * ENRICHMENT — watchability ordering needs per-series ratings from the enrichment
    daemon. Until it finishes, playlists fall back to air-date order; we say so
    explicitly AND reassure that full ordering resumes automatically when it's done.

Pure: the manager gathers the signals (resolution_stats cache, config, the daemon
supervisor's liveness) and passes scalars; this only classifies + phrases.
"""
from __future__ import annotations

LOW_COVERAGE_PCT = 80.0        # below this, the join is too weak for a complete experience
PARTIAL_COVERAGE_PCT = 95.0    # 80–95 is fine, just note the skipped few
SCORE_PENDING_FRACTION = 0.5   # ≥ this share of series unscored ⇒ clearly enrichment-pending


def diagnose_tv_readiness(*, inventory_present: bool, resolution_pct, max_pages_hit: bool,
                          series_total: int, series_scored: int,
                          daemon_enabled: bool, daemon_running: bool) -> dict:
    """Classify the TV-playlist build state. Returns
    ``{"can_build": bool, "notes": [{"level","code","message"}, ...]}``.

    ``can_build`` is False ONLY when there is literally nothing to build yet (no
    inventory); every other degradation still builds and just adds a note. Notes are
    ordered most-actionable first; the manager logs each one."""
    if not inventory_present:
        return {"can_build": False, "notes": [{
            "level": "warn", "code": "no_inventory",
            "message": ("Owned-episode inventory not built yet — set "
                        "plex.episodes.enabled=true and run once; TV playlists will "
                        "populate on the next run."),
        }]}

    notes: list[dict] = []

    # ── coverage ────────────────────────────────────────────────────────────
    pct = float(resolution_pct) if resolution_pct is not None else 0.0
    if pct < LOW_COVERAGE_PCT:
        notes.append({"level": "warn", "code": "low_coverage", "message": (
            f"Only {pct:.0f}% of owned episodes match a Plex item, so TV playlists will "
            f"be incomplete. This usually means a legacy Plex TV agent that doesn't expose "
            f"TVDB ids — refresh or switch the library's agent to improve coverage. "
            f"Matched episodes are still included.")})
    elif pct < PARTIAL_COVERAGE_PCT:
        notes.append({"level": "info", "code": "partial_coverage", "message": (
            f"{pct:.0f}% episode coverage — a few episodes can't be matched to Plex and "
            f"are skipped.")})

    if max_pages_hit:
        notes.append({"level": "warn", "code": "scan_truncated", "message": (
            "A show section exceeded the scan limit, so some episodes weren't indexed — "
            "TV playlists may be partial until per-series scanning is added.")})

    # ── enrichment (watchability ordering) ──────────────────────────────────
    unscored = max(0, series_total - series_scored)
    if series_total > 0 and unscored / series_total >= SCORE_PENDING_FRACTION:
        if daemon_enabled:
            where = " (running now)" if daemon_running else ""
            notes.append({"level": "info", "code": "scores_pending_enrichment", "message": (
                f"{unscored}/{series_total} series don't have a watchability rating yet — "
                f"the enrichment daemon{where} is still enriching show ratings. Playlists "
                f"are ordered by air date for now and will AUTOMATICALLY resume full "
                f"watchability ordering once enrichment finishes — no action needed.")})
        else:
            notes.append({"level": "info", "code": "scores_no_daemon", "message": (
                f"{unscored}/{series_total} series have no watchability rating — enable the "
                f"enrichment daemon (daemons.enrich) so TV playlists can order by "
                f"watchability; using air date for now.")})

    return {"can_build": True, "notes": notes}


def headline(diagnosis: dict) -> str:
    """One-line summary for a run log: the first warn (if any), else the first note,
    else an all-clear."""
    notes = diagnosis.get("notes") or []
    if not diagnosis.get("can_build"):
        return notes[0]["message"] if notes else "TV playlists cannot build yet."
    warns = [n for n in notes if n.get("level") == "warn"]
    if warns:
        return warns[0]["message"]
    if notes:
        return notes[0]["message"]
    return "TV playlists ready (full coverage, watchability-ordered)."

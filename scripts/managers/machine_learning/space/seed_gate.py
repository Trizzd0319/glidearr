"""
seed_gate.py — should this step-down happen YET? (GLD-RST-06)
================================================================================
PURE decision logic, no I/O. Given (a) what the grab record says about a file and
(b) what the torrent client currently reports for it, decide whether stepping the
file down now is worth doing.

WHY THIS EXISTS. Under the TRaSH layout the download dir and the media root share a
filesystem and *arr HARDLINKS on import, so a seeding torrent's data and the library
file are two links to ONE inode. `DELETE episodefile/{fid}` unlinks the library
path; qbit keeps its own link; the inode survives; the delete frees ZERO bytes.
The step-down then grabs a smaller replacement — which is REAL new consumption.

    net effect of a step-down on a still-seeding torrent = −(replacement size)

GLD-RST-05 made the ledger stop counting those phantom bytes as freed. It did not
stop the pass from performing the action. This gate does: an operation whose only
certain outcome is spending space should not run just because a threshold said the
library is too big.

WHAT WE WAIT FOR — AND WHY IT IS NOT A SEED-CRITERIA CALCULATION. The obvious
implementation reads seedRatio/seedTime off the indexer and recomputes whether the
obligation is met. That reimplements, badly, a decision Sonarr already makes: with
Completed Download Handling → Remove enabled, Sonarr removes the torrent (and the
download-side link) once the indexer's seedCriteria are satisfied. So the question
"has it met its obligation?" has a far more reliable answer available:

    IS THE INFOHASH STILL IN THE CLIENT?

Gone  -> the download-side link is gone, the library file is the LAST link, and
         deleting it frees the bytes for real. Net positive. Proceed.
Present -> two links remain. Net negative. Defer.

That is one boolean, sourced from the component that actually owns the truth,
instead of a duplicate ladder that can drift out of agreement with Sonarr's
(precedent: ENHANCEMENTS §8 P-E, duplicate implementations).

FAIL-OPEN VS FAIL-CLOSED. Deferring wrongly costs a delayed reclaim. Proceeding
wrongly costs real disk on a library already under pressure, and the operator sees
"reclaimed" in the ledger while free space falls. The asymmetry is not close, so an
UNKNOWN torrent state defers by default (`assume_pinned_when_unknown`). This mirrors
bin_forecast's standing rule that uncertainty may only make the system LESS
aggressive, never more.

THE DEFERRAL'S OWN FAILURE MODE. If seed criteria are never satisfied — unset
`seedCriteria`, Remove Completed Downloads switched off, a tracker requiring
indefinite seeding — the torrent never leaves the client and the file defers
FOREVER. A guard with no bound is a leak, so `max_defer_days` releases a file that
has been waiting too long (it proceeds, and GLD-RST-05 still books the reclaim as
pinned rather than free), and `warn_on_unbounded_seeding` names the condition out
loud instead of letting the pass quietly shrink its own working set every run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Returned as the second element of the decision tuple; stable strings so callers
# can aggregate reasons without parsing prose.
REASON_NOT_TORRENT = "not-torrent"
REASON_RELEASED = "client-released"
REASON_SEEDING = "still-seeding"
REASON_UNKNOWN_ASSUMED = "client-unknown-assumed-pinned"
REASON_UNKNOWN_ALLOWED = "client-unknown-allowed"
REASON_DEFER_EXPIRED = "defer-window-expired"
REASON_DISABLED = "gate-disabled"

_DATA_GONE_STATES = (
    # qBittorrent `state` values where a record exists but the DATA does not, so no
    # second hardlink is holding the inode open.
    "missingfiles", "error", "unknown",
)


def seed_config(config) -> dict:
    """The ``seeding`` block with defaults applied.

    Absent or malformed config yields the SAFE posture (gate on, defer on,
    unknown-means-pinned) rather than the permissive one: a config typo must not
    silently re-enable the net-negative behaviour this gate exists to stop.
    """
    raw = {}
    try:
        got = config.get("seeding") if config is not None else None
        if isinstance(got, dict):
            raw = got
    except (AttributeError, TypeError):
        raw = {}

    def _b(key, default):
        val = raw.get(key, default)
        return default if val is None else bool(val)

    def _n(key, default):
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return val if val >= 0 else float(default)

    return {
        "enabled": _b("enabled", True),
        "defer_stepdown_while_pinned": _b("defer_stepdown_while_pinned", True),
        "min_seed_time_hours": _n("min_seed_time_hours", 0),
        "min_seed_ratio": _n("min_seed_ratio", 0.0),
        "assume_pinned_when_unknown": _b("assume_pinned_when_unknown", True),
        "max_defer_days": _n("max_defer_days", 14),
        "warn_on_unbounded_seeding": _b("warn_on_unbounded_seeding", True),
    }


def obligation_shortfall(torrent, cfg) -> "str | None":
    """Why the client is holding a torrent that has NOT met the operator's floor, or None.

    ADVISORY ONLY — deliberately NOT consulted by should_defer_stepdown, and the
    reason is worth stating because the opposite is the obvious thing to build.

    A stricter Glidearr floor cannot change the deferral decision in either
    direction. It cannot release a file EARLY: while the client holds the torrent it
    holds a hardlink, so the bytes are on disk regardless of what any threshold
    says. It cannot hold a file LONGER either: once Sonarr's completed-download
    handling removes the torrent at the INDEXER's seedCriteria, the link is gone and
    the delete is net positive whether or not our floor was reached. Physical link
    presence dominates both knobs completely.

    Wiring them into the gate anyway would have produced exactly the failure this
    codebase already tracks as P-A: a value computed on every candidate, appearing
    to gate the decision, consuming nothing. So they do the one job they CAN do —
    telling the operator that Sonarr is set to release torrents sooner than they
    intended, which is a seedCriteria problem to fix in Sonarr, not here.
    """
    if not torrent:
        return None
    want_h = cfg.get("min_seed_time_hours") or 0
    want_r = cfg.get("min_seed_ratio") or 0
    if not want_h and not want_r:
        return None
    short = []
    if want_h:
        try:
            have = float(torrent.get("seeding_time") or 0) / 3600.0
            if have < want_h:
                short.append(f"seeded {have:.1f}h of {want_h:.0f}h")
        except (TypeError, ValueError):
            short.append("seed time unreadable")
    if want_r:
        try:
            have = float(torrent.get("ratio") or 0)
            if have < want_r:
                short.append(f"ratio {have:.2f} of {want_r:.2f}")
        except (TypeError, ValueError):
            short.append("ratio unreadable")
    return "; ".join(short) or None


def _defer_expired(first_deferred_at, cfg, now=None) -> bool:
    """True once a file has been deferred longer than ``max_defer_days``.

    0 disables the escape hatch (defer indefinitely) — allowed, but it is the
    configuration in which an unbounded seed silently removes a file from the
    reclaim pool forever, which is what warn_on_unbounded_seeding reports.
    """
    days = cfg.get("max_defer_days") or 0
    if not days or not first_deferred_at:
        return False
    try:
        seen = datetime.fromisoformat(str(first_deferred_at))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) - seen >= timedelta(days=float(days))


def should_defer_stepdown(push, torrent, config, *, first_deferred_at=None, now=None):
    """``(defer: bool, reason: str)`` for one candidate episode file.

    push     — the archived push descriptor (``info_hash`` present ⇒ torrent-sourced)
    torrent  — the client's record for that infohash, or None when the client says
               it does not have it, or the sentinel ``{}``/``"unknown"`` when the
               client could not be reached at all. The distinction matters: "the
               client says no" is an ANSWER (release it), "the client did not
               answer" is not (assume pinned).
    """
    cfg = seed_config(config)
    if not cfg["enabled"] or not cfg["defer_stepdown_while_pinned"]:
        return False, REASON_DISABLED

    info_hash = (push or {}).get("info_hash")
    if not info_hash:
        # Usenet, or a grab we have no record for. Nothing else holds a link, so the
        # unlink frees the bytes and the step-down is net positive as designed.
        return False, REASON_NOT_TORRENT

    if torrent == "unknown":
        if cfg["assume_pinned_when_unknown"]:
            return True, REASON_UNKNOWN_ASSUMED
        return False, REASON_UNKNOWN_ALLOWED

    if not torrent:
        # Client answered and does not have it: Sonarr's completed-download handling
        # already removed it after seedCriteria were met. Library link is the last one.
        return False, REASON_RELEASED

    if _defer_expired(first_deferred_at, cfg, now=now):
        return False, REASON_DEFER_EXPIRED

    # PRESENCE IS THE WHOLE ANSWER. Every state below `paused`/`stopped` still means
    # the client holds the file, so the hardlink is intact and the unlink frees
    # nothing — a paused torrent pins bytes exactly as hard as an uploading one.
    # The narrow exception is a record whose DATA the client has lost (missingFiles,
    # error): there is no second link left, so the step-down is net positive again.
    state = str(torrent.get("state") or "").lower()
    if state in ("missingfiles", "error", "unknown"):
        return False, REASON_RELEASED

    return True, REASON_SEEDING


def unbounded_seeding(torrent, cfg) -> bool:
    """True when a held torrent has no configured stopping condition at all.

    A torrent with ratio_limit and seeding_time_limit both at the client's "no
    limit" sentinel (-1, or -2 meaning 'use global' when no global is set) will seed
    until an operator intervenes, so every file hardlinked to it is permanently
    outside the reclaim pool. Worth naming: it looks exactly like a working system
    right up until free space stops responding to step-downs.
    """
    if not torrent or not cfg.get("warn_on_unbounded_seeding"):
        return False
    try:
        ratio_cap = float(torrent.get("ratio_limit", -1))
        time_cap = float(torrent.get("seeding_time_limit", -1))
    except (TypeError, ValueError):
        return False
    return ratio_cap < 0 and time_cap < 0

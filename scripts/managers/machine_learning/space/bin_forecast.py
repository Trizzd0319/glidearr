"""bin_forecast.py — space the *arr RECYCLE BIN is about to give back.

NAMED "bin", NOT "recycle", on purpose. "Recycle" already means something else
here: ``_recycle_to_fund_acquisition`` deletes OWNED episodes to fund new grabs.
This module is about the *arr's holding area for files it has ALREADY deleted -
the opposite direction, and nothing to do with acquisition. Two unrelated
concepts under one word is how somebody later reads the wrong docstring and
believes it. Pairs with ``bin_verify.py``.

The problem this exists for, observed live on 2026-08-08. At 20:28 the pressure
pipeline read 1546.6 GB free against a 3500 GB floor, declared CRITICAL, and
planned an exhaustive step-down admitting **523 titles**. Overnight the operator
dropped the *arr recycle-bin retention from 7 days to 1, the bin cleaned out, and
free space went to 8.25 TB. The deficit the pipeline was reclaiming against had
never been a shortage of disk - it was a week of already-deleted files waiting to
age out of a holding area.

A recycle bin makes free space SAWTOOTH. Every upgrade parks the release it
replaced in the bin, where it keeps occupying disk for the retention period and
then vanishes all at once. Sampling that curve at a trough and reclaiming against
it destroys real media to solve a problem that was going to solve itself.

    free_now                     what the disk reports
  + reclaim_within(horizon)      bin contents whose retention expires first
  = effective_free               what to make a DELETION decision against

THE ASYMMETRY IS THE WHOLE SAFETY PROPERTY, and it is not negotiable:

    pending reclaim may only make the system LESS aggressive, NEVER more.

It may DEFER a deletion or a step-down. It must never authorise an acquisition,
raise a quality target, or justify a grab. The failure modes are not symmetric:
being wrong while deferring costs one run's delay and the deletion happens next
pass; being wrong while acquiring fills a disk that had no room, which is the
condition the floor exists to prevent. A forecast is evidence about the future,
and only the reversible direction may be spent on it.

WHAT IT IS BUILT FROM. glidearr runs on Windows and cannot stat the Unraid share,
so the bin is not read directly. It is RECONSTRUCTED from the *arr's own delete
history - every file the *arr removed is what went into the bin, and history
carries the timestamp and the size. That makes the forecast an ESTIMATE and it is
labelled as one everywhere: files can be pruned from the bin by hand, the bin can
be disabled, and a delete performed outside the *arr never appears.

Because it is an estimate, it is deliberately CONSERVATIVE at every choice:
unknown size counts as zero, unknown retention disables the forecast entirely,
and only entries whose retention has ALREADY expired or expires inside the
horizon are counted.

── STAGED: DIRECT BIN ACCESS WHEN THIS RUNS AS AN UNRAID CONTAINER ────────────
(`GLD-SPA-01`. Written now, while the reasoning is fresh, because the swap is
cheap ONLY if the interface is designed for it before the container exists.)

The history reconstruction above is a WORKAROUND for one fact: glidearr runs on
Windows and cannot stat `/mnt/user/data/.Recycle.Bin`. In a container bound to
`/data` the bin is a plain directory, and every estimate here becomes a
measurement:

    estimate (today)                    direct scan (in-container)
    history says a file was deleted     the file is THERE, or it is not
    size from the history event         size from the inode
    expiry = event_ts + retention       expiry = mtime + retention
    hand-pruned files invisible         absent from the listing, correctly
    non-*arr deletions invisible        present in the listing, correctly

THE SWAP IS A SOURCE CHANGE, NOT A REWRITE, and that is deliberate.
:func:`pending_entries` returns ``[{ts, expires_at, bytes}]`` and NOTHING above
it knows where those came from - a directory walk produces exactly that shape
from mtime + st_size + retention. So the container work is: add
``pending_entries_from_disk(bin_path, retention_days, now)`` with the same return
contract, and pick between them at the call site. ``reclaim_within``,
``bin_total`` and ``effective_free_gb`` do not change at all.

RUN BOTH FOR A WHILE BEFORE TRUSTING EITHER. The DELTA between the reconstruction
and the scan is itself the measurement nobody has: it says how wrong the estimate
was, on this household, in GB. If they agree closely the estimate can stay the
fallback for non-container installs with real confidence; if they diverge, the
direction of the error tells you whether the estimator was optimistic (dangerous
- it deferred reclaims it should not have) or pessimistic (harmless).

WHAT DIRECT ACCESS ALSO UNLOCKS, and the order to do it in:
  1. the VERIFIER (`GLD-SPA-02`) - a binned file is only redundant if its
     replacement actually imported. If the upgrade failed, the bin copy is the
     ONLY copy and reclaiming it is data loss. Buildable TODAY from
     `has_file` + `movieFileId` + the import/delete history pairing; it does not
     need the container, and nothing else here is safe without it.
  2. per-item purge (`GLD-SPA-03`) - only meaningful once (1) can say a specific
     file is redundant. There is no *arr API for removing ONE bin entry; the bin
     is a directory the *arr does not index, so this is an unlink, which is
     exactly why it must be gated behind the verifier AND `deletions_consented`.
  3. early reclaim via the *arr (`GLD-SPA-04`) - CONFIRMED to exist: "Clean Up
     Recycle Bin" is in Radarr's `/api/v3/system/task` list, so
     `POST /api/v3/command {"name": "CleanUpRecycleBin"}` is the trigger (Radarr
     command names are the task name with spaces stripped, as in
     `Rss Sync` -> `RssSync`).

     BUT IT ENFORCES THE RETENTION PERIOD RATHER THAN BYPASSING IT. Running it
     early against a 7-day bin holding 2-day-old files frees exactly nothing -
     it deletes what is already eligible, which the scheduled run would have
     taken anyway. Pulling space FORWARD means lowering
     `recycleBinCleanupDays` via `PUT /api/v3/config/mediamanagement`, running
     the task, then restoring the setting: a blunt instrument that drops the
     safety net for EVERY binned file in order to reclaim one, and leaves the
     retention lowered if the restore fails. Least valuable of the three, and
     the most likely to be regretted.

     This is also, incidentally, exactly what happened on 2026-08-08: retention
     was changed 7d -> 1d, the SCHEDULED task ran, and 5.6 TiB of
     already-deleted files left at once. No deletion path in glidearr was
     involved - `audit.log` shows zero deletes - and nothing the household still
     had was lost.

── HOW TO REACH THOSE ENDPOINTS (traced 2026-08-09) ────────────────────────
glidearr does NOT own a Radarr HTTP client. `radarr/api/{client,auth}.py` are
0-byte placeholders with orphaned `cpython-310` bytecode beside them - always
empty, never used, safe to delete. The real client is the third-party `arrapi`
package: `RadarrInstanceManager._api_class` returns `arrapi.RadarrAPI`, and
`radarr_api` on the Radarr manager is an alias for that instance manager.

`arrapi` (1.4.14) has NO config or command methods - `raws/radarr.py` covers
movies, exclusions and tags only. But `raws/base.py` exposes generic
`_get` / `_put` / `_post` / `_delete` against the `/api/v3` base, reachable as
`api._raw`. So the three calls this needs require no new HTTP client:

    raw = instance_manager.get_radarr_api(name)._raw
    cfg = raw._get("config/mediamanagement")          # read recycleBinCleanupDays
    raw._put("config/mediamanagement", json=cfg)      # write it back, modified
    raw._post("command", json={"name": "CleanUpRecycleBin"})

THE UNDERSCORE IS A WARNING, NOT A FORMALITY. These are a third-party library's
private methods; a minor `arrapi` bump can rename them without notice. Any caller
must `getattr`-probe and degrade to "cannot adjust retention" rather than raise -
and the degraded path must leave retention UNCHANGED, never half-changed.

WHICH IS THE REAL HAZARD, and it is not the API surface:

    PUT retention = 1      <- safety net lowered
    POST CleanUpRecycleBin
         ^ process dies
    PUT retention = 7      <- never runs

A `try/finally` does NOT cover this. It survives an exception, not a killed
process, a reboot, or a timeout on the restore call itself. The bin would sit at
1 day silently and permanently, and nobody would find out until the next time
they needed to recover something.

So the sequence MUST persist its intent BEFORE it acts:

    1. read current retention
    2. WRITE {"restore_to": 7, "at": ts} to cache   <- before touching anything
    3. PUT retention = N
    4. POST CleanUpRecycleBin
    5. PUT retention = 7
    6. clear the marker

and every run must check for a stranded marker at START-UP and restore it. That
turns a permanent silent failure into a self-healing one, and it is the only part
of `GLD-SPA-04` that is not optional.

The asymmetry below survives all of it unchanged: even with a perfect directory
listing, reclaimable space is space that is not free YET.

PURE. No I/O, no manager, no config object. History rows + retention in, GB out.
"""
from __future__ import annotations

_DAY = 86400.0
_GB = 1024.0 ** 3

#: Delete-event types across Radarr and Sonarr whose files land in the recycle
#: bin. An upgrade's replaced file is the dominant source by volume - it is the
#: routine churn that accumulates into a week-deep trough.
DELETE_EVENTS = frozenset({
    "movieFileDeleted", "episodeFileDeleted", "deletedFiles",
    "movieFileRenamed", "downloadFolderImported",
})

#: Only these reasons put a file in the BIN. An import that renamed a file in
#: place did not delete anything, and counting it would inflate the forecast with
#: space that was never going to come back.
BIN_REASONS = frozenset({"upgrade", "deleted", "manual", "missingFromDisk"})

#: How far ahead a reclaim is worth counting, when the caller does not say. One
#: day, because the pressure pipeline runs at least daily: anything expiring
#: further out will be seen by a later run with better information, and counting
#: it now means acting on a forecast two runs deep.
DEFAULT_HORIZON_HOURS = 24.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None                      # NaN-safe


def _event_size(row) -> float:
    """Bytes this delete freed into the bin, or 0.0 when unknown.

    ZERO, not a guess. An unknown size must not contribute to a forecast that
    can defer a deletion - the conservative direction is to under-count pending
    reclaim, which at worst reclaims space we did not need to.
    """
    data = (row or {}).get("data") or {}
    for key in ("size", "fileSize", "droppedSize", "importedSize"):
        for src in (data, row or {}):
            v = _num((src or {}).get(key))
            if v is not None and v > 0:
                return v
    return 0.0


def _event_ts(row):
    """Unix seconds for a history row's ``date``, or None."""
    v = (row or {}).get("date")
    if v is None:
        return None
    num = _num(v)
    if num is not None and not isinstance(v, str):
        return num
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def is_bin_entry(row) -> bool:
    """True when this history row put a file in the recycle bin."""
    if not isinstance(row, dict):
        return False
    if str(row.get("eventType") or "") not in DELETE_EVENTS:
        return False
    data = row.get("data") or {}
    reason = str(data.get("reason") or data.get("deleteReason") or "").strip()
    # An ABSENT reason is admitted; the event type already says a file was
    # deleted, and most Radarr delete events carry no reason at all. A reason
    # that is present and NOT a bin reason is excluded.
    return (not reason) or reason in BIN_REASONS


def pending_entries(history, *, retention_days, now):
    """``[{ts, expires_at, bytes}]`` for files believed to still be in the bin.

    THE SEAM FOR DIRECT BIN ACCESS (`GLD-SPA-01`). This return contract is the
    only thing the rest of the module knows about the bin, so an in-container
    ``pending_entries_from_disk`` producing the same shape from mtime + st_size
    substitutes here and nothing above changes. See the module docstring.

    An entry is still in the bin when it was deleted less than ``retention_days``
    ago. Anything older has already been cleaned and its space is ALREADY in the
    free figure - counting it again would double-count and inflate the forecast.
    """
    if retention_days is None:
        return []
    try:
        retention = float(retention_days)
    except (TypeError, ValueError):
        return []
    if retention <= 0:
        return []
    out = []
    for row in (history or ()):
        if not is_bin_entry(row):
            continue
        ts = _event_ts(row)
        if ts is None:
            continue
        expires = ts + retention * _DAY
        if expires <= now:
            continue                     # already cleaned; in `free` already
        size = _event_size(row)
        if size <= 0:
            continue                     # unknown size contributes nothing
        out.append({"ts": ts, "expires_at": expires, "bytes": size,
                    # Carried so `effective_free_gb` can intersect against
                    # bin_verify's verdicts, which key on the DELETED file's id.
                    "file_id": _binned_file_id(row)})
    return out


def _binned_file_id(row):
    """The file id this delete removed, or None. Mirrors ``bin_verify``'s reader
    so the two modules agree on what identifies a bin entry (P-E)."""
    data = (row or {}).get("data") or {}
    for key in ("movieFileId", "movie_file_id", "episodeFileId", "fileId"):
        for src in (data, row or {}):
            v = (src or {}).get(key)
            if v not in (None, ""):
                return str(v)
    return None


def reclaim_within(history, *, retention_days, now,
                   horizon_hours: float = DEFAULT_HORIZON_HOURS) -> float:
    """GB the bin will free within ``horizon_hours``. 0.0 when unknowable.

    Returns 0.0 - never a guess - when retention is unknown, because the entire
    point is to avoid acting on a number nobody established. A forecast of zero
    degrades the caller to today's behaviour exactly.
    """
    cutoff = now + max(0.0, float(horizon_hours)) * 3600.0
    total = sum(e["bytes"] for e in pending_entries(
        history, retention_days=retention_days, now=now) if e["expires_at"] <= cutoff)
    return total / _GB


def bin_total(history, *, retention_days, now) -> float:
    """GB currently held by the bin, whenever it expires. Reporting only.

    Deliberately NOT the number a decision is made against: most of it may sit
    for days. It exists so a log can say how much of a shortfall is real disk
    pressure and how much is a holding area, which is the distinction that was
    invisible when 523 titles were admitted for step-down.
    """
    return sum(e["bytes"] for e in pending_entries(
        history, retention_days=retention_days, now=now)) / _GB


def effective_free_gb(free_gb, history, *, retention_days, now,
                      horizon_hours: float = DEFAULT_HORIZON_HOURS,
                      verdicts=None) -> dict:
    """``{free_gb, reclaim_gb, effective_gb, bin_total_gb, withheld_gb,
    horizon_hours, estimated, verified}``.

    ``effective_gb`` is what a DELETION decision should be made against. It must
    never be fed to an acquisition gate - see the module docstring; the asymmetry
    is the safety property, not a stylistic preference.

    ``verdicts`` is ``bin_verify.verify_all(...)`` output. Supply it and the
    reclaim counts ONLY entries whose replacement provably landed; omit it and
    every deleted file counts, which is honest arithmetic about disk and says
    nothing about safety. The difference lands in ``withheld_gb``, which is the
    number worth logging: it measures what the verifier is buying.

    A caller that intends to PURGE must pass verdicts. A caller that only wants
    to know whether to DEFER need not - deferring on an over-optimistic forecast
    costs one run, and the run after it will have better information.
    """
    base = _num(free_gb) or 0.0
    entries = pending_entries(history, retention_days=retention_days, now=now)
    cutoff = now + max(0.0, float(horizon_hours)) * 3600.0

    safe_ids = None
    if verdicts is not None:
        safe_ids = {str(v.get("binned_file_id")) for v in verdicts
                    if v.get("verdict") == "redundant" and v.get("binned_file_id")}

    reclaim = withheld = 0.0
    for e in entries:
        if e["expires_at"] > cutoff:
            continue
        if safe_ids is not None and str(e.get("file_id")) not in safe_ids:
            withheld += e["bytes"]
            continue
        reclaim += e["bytes"]

    total_bin = sum(e["bytes"] for e in entries)
    return {
        "free_gb": base,
        "reclaim_gb": reclaim / _GB,
        "effective_gb": base + reclaim / _GB,
        "bin_total_gb": total_bin / _GB,
        "withheld_gb": withheld / _GB,
        "horizon_hours": float(horizon_hours),
        "estimated": not (retention_days and total_bin > 0),
        "verified": verdicts is not None,
    }

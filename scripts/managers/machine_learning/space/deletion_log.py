"""space/deletion_log.py — the permanent record of what was destroyed (pure).
==============================================================================
PURE MODULE (see ../ARCHITECTURE.md). NO HTTP, NO service imports, NO
global_cache writes. Records in, lines out. The caller owns the file handle.

WHY THIS IS NOT A LOG THAT ROTATES
──────────────────────────────────────────────────────────────────────────────
``RUN_LOG_ARTIFACTS`` exists for PLANS. ``default.log``, ``routing.log`` and
``acquisition/decisions.log`` are all recomputed every run, so run N+1's copy
supersedes run N's — ``decision_log`` says it outright: a stale decision log read
against a fresh summary is worse than none.

A deletion is not a plan, it is an EVENT. It is never recomputed, and the record
is the only evidence it happened. At ``RUN_LOG_BACKUPS = 5`` on a nightly
cadence, rotating this would expire the answer to "what did I lose, and how do I
get it back" in six days. So this file is APPEND-ONLY and deliberately absent
from ``RUN_LOG_ARTIFACTS``.

Growth is bounded by behaviour rather than by rotation, which is why omitting it
is safe here and would not have been for ``decisions.log``: that file costs ~1,000
lines EVERY run regardless of outcome, while this one is written only when
something is actually destroyed. Most runs append zero bytes.

WHY A RUN ID AND NOT A ``-N`` SUFFIX
──────────────────────────────────────────────────────────────────────────────
Rotation actively destroys cross-referenceability: today's ``default-2.log`` is
tomorrow's ``default-3.log``, so a deletion row pointing at a ``-N`` file is wrong
within 24 hours. ``run_id`` is a stable UTC stamp in the format already used by
``plex/watchlist/snapshot/<id>.json``. The orchestrator stamps the SAME id into
``default.log`` at start, so a row here can be traced back to its run after
rotation has scrambled every suffix.

ONE WRITER, NOT TWO
──────────────────────────────────────────────────────────────────────────────
The human-readable view is RENDERED from these records (:func:`render`), never
written alongside them. Two writers emitting the same facts is P-E, and a
deletion record and its own summary drifting apart is precisely the shape that
pattern describes.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1

#: Dispositions a row can carry. Only ``deleted`` means bytes left the disk.
#: The others exist because "we decided to delete and did not" is exactly the
#: state the 2026-08-23 run was in (266 rows marked, consent withheld) and it was
#: invisible: no artifact anywhere said WHICH 266.
DISPOSITIONS = ("deleted", "stepped-down", "upgraded", "upgrade-abandoned",
                "would-delete", "marked-not-consented", "guarded", "failed")

#: Dispositions that describe something that HAPPENED. Append-only, never deduped,
#: never rewritten: a file deleted twice is genuinely two events.
#:
#: ``stepped-down`` belongs here and is arguably the MOST important of them. A
#: delete is recoverable — `match_release` re-acquires it from the recorded release
#: identity. A step-down destroys QUALITY irreversibly: the Remux original is gone
#: and the 720p replacement cannot be undone, because until `GLD-DEL-05` nothing
#: recorded what the original was. On 2026-08-24 four movies lost 83.9 GB of Remux
#: masters whose only trace was four `default.log` lines that rotate away in five
#: runs, plus recycle-bin files on a 24-hour timer.
EVENT_DISPOSITIONS = ("deleted", "stepped-down", "upgraded", "upgrade-abandoned", "failed")

#: Directions a quality transition can move, for churn detection. An ``upgraded`` row
#: is emitted at RECONCILIATION rather than at trigger (`GLD-DEL-13`): both upgrade
#: paths fire a SEARCH and the *arr swaps asynchronously, so at trigger time we know
#: only what is being replaced. By reconciliation the replacement has been observed,
#: which is what lets ``replaced_by`` carry a measured size instead of a projection.
_DIRECTION = {"upgraded": "up", "stepped-down": "down"}

#: Dispositions that describe a STATE rather than an event — "these are queued",
#: "these would go". Nothing was destroyed, so re-appending an unchanged state every
#: night is pure accumulation: 271 rows became 24,390 in ninety runs while describing
#: the same 271 files, and the earliest copies were frozen with fields that were
#: missing at the time and have since been fixed.
#:
#: This is the same plan-vs-event split that governs ``RUN_LOG_ARTIFACTS`` — applied
#: correctly this time. State is a PLAN: recomputed every run, so run N+1 supersedes
#: run N and the file should be REWRITTEN. Only events earn the append-only archive.
STATE_DISPOSITIONS = ("would-delete", "marked-not-consented", "guarded")


def split_by_kind(records):
    """``(events, states)`` — rows destined for the archive vs the pending snapshot.

    An unrecognised disposition is treated as an EVENT. That is the safe direction:
    an unknown row lands in the append-only file where nothing can overwrite it,
    rather than in the one that gets rewritten every run.
    """
    events, states = [], []
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        (states if r.get("disposition") in STATE_DISPOSITIONS else events).append(r)
    return events, states

#: Media roots, longest first so ``/data/media/tv`` cannot shadow a longer root.
#: The segment AFTER the root is the library class.
_MEDIA_ROOTS = ("/data/media/", "/mnt/user/data/media/")

_TS_RE = re.compile(r"[^0-9TZ]")


def _native(value):
    """A numpy/pandas scalar as its Python equivalent; anything else unchanged.

    WHY THIS IS NOT COSMETIC. The delete paths read parquet, so a ``series_id`` is
    an ``np.int64`` and a ``score`` is an ``np.int64``. ``np.int64`` is NOT JSON
    serialisable, so ``json.dumps(..., default=str)`` silently rendered them as
    STRINGS — ``"17209"``, ``"50"`` — while ``np.float64`` (a real ``float``
    subclass) came through as a number. The archive would have been permanently
    mistyped, and inconsistently so, which is worse than uniformly wrong: any later
    ``score < 20`` query over the file would compare ints against strings and
    quietly return nothing.

    Caught by replaying the real 266-row marked set through the real call path
    rather than by reading the code.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()                 # numpy scalar -> int / float / bool
        except Exception:
            return value
    return value


def new_run_id(now=None) -> str:
    """``20260823T042426Z`` — a stable, sortable id for one orchestrator run.

    Deliberately NOT the rotation suffix: ``-N`` shifts every run, so anything
    referencing it is wrong by the next night."""
    dt = now or datetime.now(timezone.utc)
    return _TS_RE.sub("", dt.strftime("%Y%m%dT%H%M%SZ"))


def library_class(path) -> "str | None":
    """The library class a file physically sat in, read off its own path.

    ``/data/media/tv/documentaries/Show (2020)/...`` -> ``"documentaries"``.

    Read from the PATH rather than asked of ``classification.library_classifier``
    on purpose. The classifier answers "where should this live", which is an
    opinion that can differ from reality — it differed 7,645 times on the
    2026-08-23 reclassification. A forensic record wants what was TRUE.

    ⚠️ It is only as true as the path it is given. Callers MUST also store the raw
    path (:func:`deletion_record` does), so a class derived from a stale cache can
    be recomputed later instead of being silently believed.

    Returns None rather than guessing when the path is not under a known root —
    an unknown class is honest, and a wrong one contaminates every later query."""
    p = str(path or "").replace("\\", "/")
    if not p:
        return None
    for root in sorted(_MEDIA_ROOTS, key=len, reverse=True):
        if root in p:
            rest = p.split(root, 1)[1].strip("/").split("/")
            # rest[0] is the media kind (tv / movies); rest[1] is the class.
            if len(rest) >= 2 and rest[1]:
                return rest[1]
            return None
    return None


def deletion_record(*, run_id, media, instance, title, disposition="deleted",
                    season=None, episode=None, year=None, path=None,
                    file_id=None, series_id=None, tmdb_id=None, tvdb_id=None,
                    quality_profile_id=None, quality_profile_name=None,
                    quality_name=None, resolution=None, size_bytes=None,
                    score=None, reason=None, source=None, release=None,
                    push=None, deleted_at=None, replaced_by=None, **extra) -> dict:
    """One deletion, normalised. Every field beyond the five required ones is
    optional and simply absent when the call site does not know it.

    That optionality is the whole design. The six delete paths know different
    things — the recycle path has no score, the coordinator path has no tier, a
    seeded historical row has no reason at all — and a schema that demanded a
    uniform shape would either lie with defaults or refuse the row. Absent is
    recorded as absent (**P-C**).

    ``release`` is a ``restore_policy.release_record`` (what the FILE was) and
    ``push`` is a redacted ``release/push`` descriptor (how to ask for it back).
    Neither is required; both are what make a row actionable rather than merely
    informative."""
    rec = {
        "v": SCHEMA_VERSION,
        "run_id": run_id,
        "deleted_at": deleted_at or datetime.now(timezone.utc).isoformat(),
        "media": media,
        "instance": instance,
        "title": _native(title),
        "disposition": disposition,
    }
    cls = library_class(path)
    optional = {
        "season": season, "episode": episode, "year": year,
        "path": path, "class": cls,
        "file_id": file_id, "series_id": series_id,
        "tmdb_id": tmdb_id, "tvdb_id": tvdb_id,
        "pid": quality_profile_id, "profile": quality_profile_name,
        "quality_name": quality_name, "resolution": resolution,
        "size_bytes": size_bytes, "score": score,
        "reason": reason, "source": source,
        "release": release or None, "push": push or None,
        # What took its place, on a `stepped-down` row: {title, size_bytes,
        # quality_name}. Absent on a plain delete, where nothing replaces the file.
        "replaced_by": replaced_by or None,
    }
    for k, v in optional.items():
        v = _native(v)
        if v is None or (isinstance(v, float) and v != v):
            continue
        rec[k] = v
    for k, v in extra.items():
        v = _native(v)
        if v is not None:
            rec.setdefault(k, v)
    return rec


def to_jsonl(records) -> list:
    """Records -> newline-delimited JSON, one object per line.

    ``sort_keys`` so a diff between two runs shows changed VALUES rather than
    reordered keys, and ``ensure_ascii=False`` so a title stays readable to a
    human grepping the file."""
    out = []
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        try:
            out.append(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            continue
    return out


def parse_jsonl(lines) -> list:
    """The inverse, tolerating a torn final line.

    An append-only file that was being written when the process died ends in a
    partial object. Skipping it is correct: the rest of the file is intact and
    refusing to read 40,000 good rows because of one bad tail would make the
    archive useless exactly when it is being consulted."""
    out = []
    for line in (lines or []):
        s = str(line or "").strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def seed_records(ledger, *, run_id, instance, media="episode",
                 source="seed", disposition="deleted", titles=None) -> list:
    """Historical rows recovered from a ``deleted_episodes`` / ``stepdown_releases``
    ledger, so the archive does not start empty.

    Both ledgers share one entry shape by construction (``GLD-RST-02`` says so
    explicitly), which is why one seeder reads both. v1 entries carry no
    ``releases`` map and still seed — they yield WHAT was deleted and WHEN, which
    is more than the archive would otherwise have.

    Seeded rows are marked ``source="seed"`` and carry no ``reason``: the ledger
    never recorded one, and inventing a plausible reason for a historical
    deletion would put a guess in a file whose entire value is that it does not
    guess.

    ``titles`` is an optional ``{series_id: title}`` map — the ledger is keyed by
    id alone, and ``series:17267`` is not something a human can act on eighteen
    months later. Supplying the map turns it into ``The Jetsons``. Absent or
    unmatched ids keep the ``series:<id>`` form rather than being dropped: an
    unresolvable id is still a real deletion."""
    out = []
    if not isinstance(ledger, dict):
        return out
    names = {str(k): v for k, v in (titles or {}).items() if v}
    for sid, entry in ledger.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        releases = entry.get("releases") if isinstance(entry.get("releases"), dict) else {}
        for pair in (entry.get("episodes") or []):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            try:
                sn, en = int(pair[0]), int(pair[1])
            except (TypeError, ValueError):
                continue
            rel = releases.get(f"S{sn:02d}E{en:02d}")
            rel = dict(rel) if isinstance(rel, dict) else None
            push = rel.pop("push", None) if rel else None
            out.append(deletion_record(
                run_id=run_id, media=media, instance=instance,
                title=(names.get(str(sid))
                       or (rel or {}).get("scene_name")
                       or f"series:{sid}"),
                season=sn, episode=en, series_id=sid,
                quality_name=(rel or {}).get("quality_name"),
                resolution=(rel or {}).get("resolution"),
                size_bytes=(rel or {}).get("size_bytes"),
                release=rel or None, push=push,
                deleted_at=ts, source=source, disposition=disposition))
    return out


def churn_key(rec) -> "str | None":
    """A stable per-title key for churn grouping, or None.

    Prefers the external id (``tmdb_id`` / ``tvdb_id``) over the title, because a
    title string changes with metadata refreshes and would split one asset's history
    into two."""
    if not isinstance(rec, dict):
        return None
    for k in ("tmdb_id", "tvdb_id", "series_id"):
        v = rec.get(k)
        if v is not None:
            return f"{k}:{v}"
    t = rec.get("title")
    return f"title:{t}" if t else None


def detect_churn(records, *, window_days=30, min_flips=2, now=None) -> list:
    """Titles that reversed quality direction repeatedly — GLD-DEL-06.

    A title upgraded, then stepped down under pressure, then upgraded again is
    burning bandwidth and thrashing the library, and NEITHER existing log shows it:
    the upgrade side recorded nothing at all, and the archive only ever saw one half.
    The pathological form is already in this register — Edge of Tomorrow deleted six
    times in one day on 2026-08-08 — and it surfaced in a post-mortem rather than on
    the day, because nothing counted per-title reversals.

    A FLIP is a direction CHANGE, not an event: three consecutive step-downs are one
    descent, not churn. Counting events instead would flag every ordinary multi-stage
    reclaim as pathological and the signal would be ignored within a week.

    Returns ``[{key, title, flips, events, first, last}]`` sorted worst-first.
    Never raises; malformed rows are skipped.
    """
    from datetime import timedelta
    cutoff = None
    if window_days is not None:
        base = now or datetime.now(timezone.utc)
        cutoff = (base - timedelta(days=float(window_days))).isoformat()

    groups: dict = {}
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        d = _DIRECTION.get(r.get("disposition"))
        if not d:
            continue
        ts = str(r.get("deleted_at") or "")
        if cutoff and ts and ts < cutoff:
            continue
        k = churn_key(r)
        if not k:
            continue
        groups.setdefault(k, []).append((ts, d, r.get("title")))

    out = []
    for k, evs in groups.items():
        evs.sort(key=lambda e: e[0])
        flips = sum(1 for a, b in zip(evs, evs[1:]) if a[1] != b[1])
        if flips >= int(min_flips):
            out.append({"key": k, "title": evs[-1][2], "flips": flips,
                        "events": len(evs), "first": evs[0][0], "last": evs[-1][0]})
    out.sort(key=lambda x: (-x["flips"], -x["events"]))
    return out


def space_ledger(records) -> dict:
    """``{reclaimed_bytes, spent_bytes, net_bytes, by_disposition}`` — GLD-DEL-06.

    The system meticulously logged the RECLAIM (``freed_now_gb``) and never the
    SPEND. An upgrade from 2 GB to 8 GB consumes 6 GB, and that consumption is what
    later produces the pressure that triggers step-downs and deletions — so the two
    halves belong in one number.

    ``spent_bytes`` is a PROJECTION on upgrade rows, because an upgrade is recorded
    as intent and the replacement size is not known until the *arr imports it. On a
    step-down the numbers are real: the original is gone and ``replaced_by`` carries
    what was grabbed.
    """
    out = {"reclaimed_bytes": 0, "spent_bytes": 0, "net_bytes": 0, "by_disposition": {}}
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        disp = r.get("disposition")
        try:
            sz = int(r.get("size_bytes") or 0)
        except (TypeError, ValueError):
            sz = 0
        rb = r.get("replaced_by")
        # isinstance, not `or {}` — a non-empty STRING is truthy and passes that
        # guard, then AttributeErrors on .get. Caught by the hostile-input sweep.
        rb = rb if isinstance(rb, dict) else {}
        try:
            new = int(rb.get("size_bytes") or 0)
        except (TypeError, ValueError):
            new = 0
        d = out["by_disposition"].setdefault(disp, {"n": 0, "bytes": 0})
        d["n"] += 1
        d["bytes"] += sz
        if disp in ("deleted", "stepped-down"):
            out["reclaimed_bytes"] += sz
            out["spent_bytes"] += new
        elif disp == "upgraded":
            # REAL by the time this row exists: `upgrade_events` emits at
            # RECONCILIATION, when the replacement has actually been observed, so the
            # spend is measured rather than projected.
            out["spent_bytes"] += max(0, new - sz) if new else 0
    out["net_bytes"] = out["reclaimed_bytes"] - out["spent_bytes"]
    return out


#: Drift gates for `parquet_drift` — GLD-DEL-08. Operator-set 2026-08-24.
#:
#: SCALED, not flat. A flat gate cannot work across library sizes because the two
#: kinds of gate break in OPPOSITE directions as the library grows:
#:
#:   5 files      →   4.2% of a 120-file library (sane)
#:                →  0.03% of a 15,548-file library (fires on one mid-run grab)
#:   5% of size   →   5 GiB on a small library (sane)
#:                → 609 GiB on a 12.2 TiB library (blind to ~780 missing files)
#:
#: So one becomes hair-trigger and the other goes blind at the same scale.
#:
#: WHY √n AND NOT log. Counting noise scales as √n (Poisson): that IS the natural
#: fluctuation band for "how many discrete things did I expect to see". A log gate
#: grows too slowly and compresses the wrong end — at n=120 it hands out ~21% of the
#: library as tolerance while still being hair-trigger at n=120,000.
#:
#:   files      120 →  11 (9.2%)      15,548 → 125 (0.80%)
#:              800 →  29 (3.6%)      40,000 → 200 (0.50%)
#:            4,000 →  64 (1.6%)     120,000 → 347 (0.29%)
#:
#: `DRIFT_K = 1` is the one-sigma band (operator-set). k=2 roughly doubles every
#: figure; start at 1, since the gap that motivated this clears the k=1 gate by 39x
#: and there is ample headroom to loosen rather than tighten later.
DRIFT_K = 1.0

#: Below ~25 files √n collapses and single-file events dominate, so the gate never
#: goes under this regardless of scale.
DRIFT_FLOOR_FILES = 5


def drift_tolerances(arr_file_count, arr_size_bytes, *, k=DRIFT_K,
                     floor=DRIFT_FLOOR_FILES) -> dict:
    """``{files, bytes, mean_file_bytes}`` — the scaled gates for one library.

    The SIZE gate is DERIVED from the count gate (``C files × mean file size``)
    rather than being an independent percentage. Two independent gates measure
    different things and one always fires first — on a 12.2 TiB library the 5%
    size gate was 609 GiB while the 5-file gate was 0.03%, so size was decorative.
    Deriving it means both express the same severity in different units: for that
    library the size gate becomes ~100 GiB (0.80%), six times tighter and aligned
    with the count gate instead of fighting it.

    Falls back to a count-only gate when the mean file size is unknowable.
    """
    try:
        n = float(arr_file_count)
        n = n if n == n and n > 0 else 0.0
    except (TypeError, ValueError):
        n = 0.0
    files = max(int(floor), math.ceil(float(k) * math.sqrt(n))) if n else int(floor)
    try:
        total = float(arr_size_bytes)
        mean = (total / n) if (n and total == total and total > 0) else None
    except (TypeError, ValueError):
        mean = None
    return {"files": files,
            "bytes": int(files * mean) if mean else None,
            "mean_file_bytes": int(mean) if mean else None}


def intersection_drift(parquet_files, arr_files, *, k=DRIFT_K,
                       floor=DRIFT_FLOOR_FILES) -> dict:
    """Is the parquet WRONG about the files it claims to know? — GLD-DEL-08.

    ⚠️ MEASURES THE INTERSECTION, NOT TOTALS. The first cut of this compared the
    parquet's totals against the *arr's whole library and would have breached every
    night forever. The parquet is a WORKING SET by construction, not a mirror:
    ``sync_from_tautulli`` adds rows from watch history, ``_ingest_inventory_tv``
    gives a series nobody has watched only a PILOT row, and ``_ingest_cold_inventory``
    caps at ``max_series_per_run``. Measured 2026-08-24: **97.6% of series (13,065 of
    13,382) hold exactly one row**, and the parquet carried 10,711 of Sonarr's 15,548
    files. That 35% gap is DESIGN. No rebuild closes it, and a detector that fires on
    it nightly is one the operator turns off.

    What IS a defect is the parquet being wrong about a file it DOES claim:

      * ``orphaned``       — a ``file_id`` the parquet holds and the *arr does not.
                             The row describes a file that no longer exists, and
                             every space decision that reads it is counting bytes
                             that are already gone. This is the shape of the BBT S3
                             ``files=6`` gap: deleted outside glidearr, still owned.
      * ``size_mismatch``  — both sides hold the id and disagree on ``size_bytes``.
                             The file was replaced (upgrade, step-down, re-grab) and
                             the parquet kept the old figure, so reclaim projections
                             built on it are wrong in an unknown direction.

    Deliberately NOT flagged: files the *arr has that the parquet does not. That is
    the by-design gap above, and conflating it with a defect is what made the first
    version useless.

    Both inputs are ``{file_id: size_bytes}``. The *arr side should be built from the
    per-series episodefile/moviefile caches the sync already holds, so this costs no
    extra API calls.

    Returns FULL ``orphaned_ids`` / ``mismatch_ids`` — they are a repair work list,
    not a log line, and a caller that logs them must slice them itself.

    Tolerance scales √n over the INTERSECTION — the population actually being
    checked — not over the library. Never raises.
    """
    out = {"checked": 0, "orphaned": 0, "size_mismatch": 0, "breach": False,
           "tolerance": int(floor), "orphaned_ids": [], "mismatch_ids": [],
           "reasons": []}
    if not isinstance(parquet_files, dict) or not isinstance(arr_files, dict):
        out["reasons"].append("unmeasurable: inputs must be {file_id: size_bytes}")
        return out
    if not parquet_files:
        out["reasons"].append("nothing to check: the parquet claims no files")
        return out
    if not arr_files:
        # An empty *arr side is a failed read, not a library with no files. Asserting
        # every row orphaned here would condemn the whole parquet on one bad fetch.
        out["reasons"].append("unmeasurable: the *arr side returned no files at all")
        return out

    orphaned, mismatch = [], []
    for fid, psize in parquet_files.items():
        if fid is None:
            continue
        out["checked"] += 1
        if fid not in arr_files:
            orphaned.append(fid)
            continue
        try:
            a, p = int(arr_files[fid] or 0), int(psize or 0)
        except (TypeError, ValueError):
            continue
        if a != p and a > 0 and p > 0:
            mismatch.append(fid)

    out["orphaned"] = len(orphaned)
    out["size_mismatch"] = len(mismatch)
    # FULL lists — these are a WORK list, not a log line. Capping them here was a
    # real defect: the Sonarr caller drove its re-sync off `orphaned_ids`, so a
    # 194-orphan breach repaired only the 9 series covered by the first 50 ids and
    # then reported PERSISTENT (194->140) when the partial repair had actually
    # cleared 28% and a full one would likely have closed it. Truncation belongs at
    # the DISPLAY layer; callers logging these must slice them themselves.
    out["orphaned_ids"] = sorted(orphaned)
    out["mismatch_ids"] = sorted(mismatch)
    tol = drift_tolerances(out["checked"], None, k=k, floor=floor)["files"]
    out["tolerance"] = tol

    if out["orphaned"] > tol:
        out["breach"] = True
        out["reasons"].append(
            f"{out['orphaned']:,} orphaned row(s) > gate {tol:,} "
            f"(√{out['checked']:,}) — files the parquet owns that the *arr does not")
    if out["size_mismatch"] > tol:
        out["breach"] = True
        out["reasons"].append(
            f"{out['size_mismatch']:,} size mismatch(es) > gate {tol:,} "
            f"— rows whose byte count no longer matches the *arr")
    return out


def coverage(parquet_file_count, arr_file_count) -> dict:
    """``{parquet, arr, ratio}`` — a GAUGE, never a gate — GLD-DEL-08.

    How much of the library the working set currently holds. Worth watching (a
    sudden collapse would be real) but it must never breach: 65% is the designed
    steady state, not a fault. Kept separate from :func:`intersection_drift` so the
    two can never be confused again.
    """
    try:
        p, a = float(parquet_file_count), float(arr_file_count)
        return {"parquet": int(p), "arr": int(a),
                "ratio": (p / a) if a > 0 else None}
    except (TypeError, ValueError):
        return {"parquet": None, "arr": None, "ratio": None}


def drift_after_rebuild(before, after) -> str:
    """``closed`` | ``improved`` | ``persistent`` | ``worse`` — GLD-DEL-08.

    The SECOND measurement is the one that means something. A first breach only says
    the parquet and the *arr disagree about files the parquet claims; it cannot
    distinguish a stale cache from one that keeps going stale. Re-syncing the
    affected series and re-measuring separates them:

      * ``closed``     — it was staleness; the resync fixed it
      * ``persistent`` — the rows keep diverging, so something is writing them wrong
                         or the *arr itself has lost them. This must NOT trigger
                         another resync: repeating an expensive pass every run to
                         reach the same answer is how a detector becomes noise the
                         operator disables.
      * ``worse``      — the resync lost ground; a failed resync, not a library fact.
    """
    try:
        if not before.get("breach") or not after.get("breach"):
            return "closed"
        b = (before.get("orphaned") or 0) + (before.get("size_mismatch") or 0)
        a = (after.get("orphaned") or 0) + (after.get("size_mismatch") or 0)
        if a > b:
            return "worse"
        if a < b * 0.5:
            return "improved"
        return "persistent"
    except Exception:
        return "persistent"


#: How long an upgrade intent stays on the worklist before it is called abandoned.
#: Deliberately generous: a 20 GB Remux can queue behind other downloads for many
#: hours, and calling it abandoned early would archive a failure that had not
#: happened yet. The cost of waiting is one cheap re-check per pass; the cost of
#: declaring too early is a false record in an append-only file.
UPGRADE_TERMINAL_HOURS = 48.0


def upgrade_key(series_id=None, season=None, episode=None, *, movie_id=None) -> "str | None":
    """``"17209:S01E02"`` or ``"movie:812"`` — the stable worklist key.

    Keyed on the ASSET, not the file id, because the file id is precisely the thing
    about to change: an upgrade deletes the old file and imports a new one with a
    brand-new id. Keying on what SURVIVES the upgrade is what makes "did it land?"
    answerable at all.

    A movie has no season/episode, so it gets its own namespace rather than being
    forced into the TV shape — and the ``movie:`` prefix means a Sonarr and a Radarr
    worklist can never collide on a shared id.
    """
    if movie_id is not None:
        try:
            return f"movie:{int(movie_id)}"
        except (TypeError, ValueError):
            return None
    try:
        return f"{int(series_id)}:S{int(season):02d}E{int(episode):02d}"
    except (TypeError, ValueError):
        return None


def upgrade_intent(*, series_id=None, season=None, episode=None, file_id,
                   movie_id=None, size_bytes=None, quality_name=None,
                   title=None, media=None, at=None) -> "dict | None":
    """One pending upgrade, recorded at the moment the search is triggered — GLD-DEL-10.

    ``file_id`` is the file the asset owns RIGHT NOW. That is the entire mechanism:
    when the upgrade lands, the *arr's id will be different, and the difference is the
    proof. Recording it is what turns an untracked fire-and-forget search into
    something that can be reconciled.

    Pass ``movie_id`` for Radarr or ``series_id``/``season``/``episode`` for Sonarr.
    Returns None when the asset cannot be keyed — an unkeyable intent is worse than
    none, because it would sit on the worklist forever and never resolve.
    """
    key = upgrade_key(series_id, season, episode, movie_id=movie_id)
    if key is None:
        return None
    rec = {"key": key,
           "media": media or ("movie" if movie_id is not None else "episode"),
           "at": at or datetime.now(timezone.utc).isoformat()}
    if movie_id is not None:
        rec["movie_id"] = int(movie_id)
    else:
        rec["series_id"] = int(series_id)
        rec["season"] = int(season)
        rec["episode"] = int(episode)
    try:
        rec["from_file_id"] = int(file_id) if file_id is not None else None
    except (TypeError, ValueError):
        rec["from_file_id"] = None
    for k, v in (("from_size_bytes", size_bytes), ("from_quality", quality_name),
                 ("title", title)):
        v = _native(v)
        if v is not None and not (isinstance(v, float) and v != v):
            rec[k] = v
    return rec


def merge_upgrade_intents(ledger, intents) -> dict:
    """Add *intents* to the pending worklist, newest wins per episode.

    A second upgrade triggered on an episode still pending from the first REPLACES
    it: the older intent's ``from_file_id`` is stale the moment the newer search is
    fired, and keeping both would resolve the same episode twice.
    """
    out = dict(ledger) if isinstance(ledger, dict) else {}
    for i in (intents or []):
        if isinstance(i, dict) and i.get("key"):
            out[i["key"]] = i
    return out


def reconcile_upgrades(ledger, observed, *, now=None,
                       terminal_hours=UPGRADE_TERMINAL_HOURS) -> dict:
    """Resolve pending upgrades against what the *arr holds NOW — GLD-DEL-10.

    *observed* is ``{key: current_file_id_or_None}``, freshly read. A key ABSENT from
    *observed* means "not looked at this pass" and is left pending; a key present with
    ``None`` means the *arr says the episode owns no file at all.

    ⚠️ THOSE TWO MUST NOT BE CONFLATED (**P-C**). Absent is ignorance, ``None`` is an
    answer. Treating a series we failed to fetch as "the file is gone" would archive
    phantom orphans and re-point rows against nothing — the same shape as
    `_get_episode_files` returning ``[]`` for both empty and failed (`GLD-DEL-09`).

    Four outcomes:

      * ``fulfilled``  — the id CHANGED. The upgrade landed; the caller re-points the
                         parquet row at the new file, which is what stops this
                         upgrade from becoming tomorrow's orphan.
      * ``orphaned``   — the *arr now reports NO file. The old one is gone and nothing
                         replaced it, so the parquet row is a dead pointer.
      * ``pending``    — id unchanged and inside the window. Usenet queues; this is
                         the normal state for hours, not a problem.
      * ``abandoned``  — id unchanged past ``terminal_hours``. The grab never landed.

    Never raises.
    """
    from datetime import timedelta
    base = now or datetime.now(timezone.utc)
    cutoff = (base - timedelta(hours=float(terminal_hours))).isoformat()
    out = {"fulfilled": [], "orphaned": [], "abandoned": [], "pending": {}}
    if not isinstance(ledger, dict):
        return out
    obs = observed if isinstance(observed, dict) else {}

    for key, rec in ledger.items():
        if not isinstance(rec, dict):
            continue
        if key not in obs:                       # not examined this pass — ignorance
            out["pending"][key] = rec
            continue
        cur = obs[key]
        try:
            cur = int(cur) if cur is not None else None
        except (TypeError, ValueError):
            out["pending"][key] = rec            # unreadable is not an answer either
            continue
        was = rec.get("from_file_id")
        if cur is None:
            out["orphaned"].append({**rec, "observed_file_id": None})
        elif was is None or cur != was:
            out["fulfilled"].append({**rec, "observed_file_id": cur})
        elif str(rec.get("at") or "") < cutoff:
            out["abandoned"].append({**rec, "observed_file_id": cur})
        else:
            out["pending"][key] = rec
    return out


def upgrade_events(result, *, run_id, instance, observed_files=None) -> list:
    """Archive rows for a reconciliation outcome — GLD-DEL-13.

    Reconciliation resolved the worklist SILENTLY: a landed upgrade left no event at
    all, so `detect_churn` only ever saw the step-down half and could never register a
    direction change, and `space_ledger` counted reclaim without the spend that caused
    the pressure in the first place.

    ⚠️ ``replaced_by`` here is REAL, not projected. At trigger time the *arr had not
    picked a release yet, so an intent could only say what was being replaced. By
    reconciliation the new file EXISTS and *observed_files* (``{file_id: {size,
    quality_name}}``) carries what actually landed — which is why emitting at this
    point rather than at trigger is what makes the spend figure trustworthy.

    Only ``fulfilled`` and ``abandoned`` produce rows. ``pending`` has not happened
    yet, and ``orphaned`` is handled by the purge, which owns that row's fate.
    """
    out = []
    if not isinstance(result, dict):
        return out
    obs = observed_files if isinstance(observed_files, dict) else {}

    for rec in (result.get("fulfilled") or []):
        if not isinstance(rec, dict):
            continue
        new = obs.get(rec.get("observed_file_id")) or {}
        out.append(deletion_record(
            run_id=run_id, media=rec.get("media") or "episode", instance=instance,
            title=rec.get("title") or rec.get("key"),
            disposition="upgraded",
            season=rec.get("season"), episode=rec.get("episode"),
            series_id=rec.get("series_id"),
            file_id=rec.get("from_file_id"),
            size_bytes=rec.get("from_size_bytes"),
            quality_name=rec.get("from_quality"),
            reason=(f"upgrade landed: {rec.get('from_quality') or '?'} → "
                    f"{new.get('quality_name') or '?'}"),
            source="upgrade",
            replaced_by={k: v for k, v in
                         (("file_id", rec.get("observed_file_id")),
                          ("size_bytes", new.get("size_bytes")),
                          ("quality_name", new.get("quality_name")),
                          ("state", "imported")) if v is not None},
            deleted_at=rec.get("at")))

    for rec in (result.get("abandoned") or []):
        if not isinstance(rec, dict):
            continue
        out.append(deletion_record(
            run_id=run_id, media=rec.get("media") or "episode", instance=instance,
            title=rec.get("title") or rec.get("key"),
            disposition="upgrade-abandoned",
            season=rec.get("season"), episode=rec.get("episode"),
            series_id=rec.get("series_id"),
            file_id=rec.get("from_file_id"),
            size_bytes=rec.get("from_size_bytes"),
            quality_name=rec.get("from_quality"),
            reason=(f"upgrade never landed within {UPGRADE_TERMINAL_HOURS:.0f}h — "
                    f"file unchanged"),
            source="upgrade",
            deleted_at=rec.get("at")))
    return out


def _gb(n):
    try:
        return f"{int(n) / (1024 ** 3):.2f} GB"
    except (TypeError, ValueError):
        return "?"


def _ident(r):
    se = ""
    if r.get("season") is not None and r.get("episode") is not None:
        try:
            se = f" S{int(r['season']):02d}E{int(r['episode']):02d}"
        except (TypeError, ValueError):
            se = ""
    elif r.get("year"):
        se = f" ({r['year']})"
    return f"{r.get('title', '?')}{se}"


def render(records, *, run_id=None) -> list:
    """The human view, RENDERED from the records — never written alongside them.

    Grouped by disposition because the question a reader arrives with is almost
    always "what actually went" or "what was held back and why", and those are
    different questions with different follow-ups."""
    rows = [r for r in (records or []) if isinstance(r, dict)]
    if run_id is not None:
        rows = [r for r in rows if r.get("run_id") == run_id]
    lines = ["==== deletions ====" if run_id is None else f"==== deletions | run {run_id} ===="]
    if not rows:
        lines.append("nothing deleted")
        return lines

    total = sum(int(r.get("size_bytes") or 0) for r in rows if r.get("disposition") == "deleted")
    lines.append(f"{len(rows)} row(s) | reclaimed {_gb(total)}")
    for disp in DISPOSITIONS:
        group = [r for r in rows if r.get("disposition") == disp]
        if not group:
            continue
        gb = sum(int(r.get("size_bytes") or 0) for r in group)
        lines.append("")
        lines.append(f"-- {disp} ({len(group)}, {_gb(gb)}) --")
        group.sort(key=lambda r: -(int(r.get("size_bytes") or 0)))
        for r in group:
            bits = [f"[{r.get('media', '?')}]", r.get("instance") or "?"]
            if r.get("class"):
                bits.append(f"class={r['class']}")
            if r.get("pid") is not None:
                bits.append(f"pid={r['pid']}")
            if r.get("quality_name"):
                bits.append(str(r["quality_name"]))
            if r.get("score") is not None:
                bits.append(f"score={r['score']}")
            bits.append(_gb(r.get("size_bytes")))
            tail = f" — {r['reason']}" if r.get("reason") else ""
            handle = ""
            rb = r.get("replaced_by")
            if isinstance(rb, dict) and rb:
                handle = f"  → {rb.get('quality_name') or '?'} {_gb(rb.get('size_bytes'))}"
            elif (r.get("push") or {}).get("info_hash"):
                handle = "  [magnet]"
            elif (r.get("push") or {}).get("download_url"):
                handle = "  [url]"
            elif r.get("release"):
                handle = "  [identity]"
            lines.append(f"  {_ident(r)} | {' '.join(bits)}{tail}{handle}")
    return lines

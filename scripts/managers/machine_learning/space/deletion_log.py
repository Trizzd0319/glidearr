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
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1

#: Dispositions a row can carry. Only ``deleted`` means bytes left the disk.
#: The others exist because "we decided to delete and did not" is exactly the
#: state the 2026-08-23 run was in (266 rows marked, consent withheld) and it was
#: invisible: no artifact anywhere said WHICH 266.
DISPOSITIONS = ("deleted", "stepped-down", "would-delete", "marked-not-consented", "guarded", "failed")

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
EVENT_DISPOSITIONS = ("deleted", "stepped-down", "failed")

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
            if r.get("replaced_by"):
                rb = r["replaced_by"]
                handle = f"  → {rb.get('quality_name') or '?'} {_gb(rb.get('size_bytes'))}"
            elif (r.get("push") or {}).get("info_hash"):
                handle = "  [magnet]"
            elif (r.get("push") or {}).get("download_url"):
                handle = "  [url]"
            elif r.get("release"):
                handle = "  [identity]"
            lines.append(f"  {_ident(r)} | {' '.join(bits)}{tail}{handle}")
    return lines

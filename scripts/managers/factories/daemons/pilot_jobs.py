"""
pilot_jobs.py — the on-disk job queue shared by ``run_pilot_search`` (producer) and
``pilot_search_daemon.py`` (consumer).
================================================================================
A pilot-search job is a small JSON file describing a batch of stub pilots to search
out-of-process: ``{instance, mode, items[[sid, ep_id]...], ladder, meta, ...}`` (see
``SonarrCacheEpisodeFilesManager._maybe_offload_pilot_search`` for the producer side).

Queue model
-----------
* ``queue/<instance>.json`` — at most ONE pending job per instance; a fresh enqueue
  OVERWRITES it (atomic temp + replace), so a run that re-derives the full due-stub
  list simply supersedes the previous pending batch instead of piling up.
* ``processing/<instance>.<pid>.<ts>.json`` — a claimed job. The daemon renames the
  queue file here so a second poll can't re-claim it, then deletes it on completion.
* On daemon start, any ORPHANED processing files (a crash mid-search) are moved back
  to the queue so the batch resumes — "searches resume across runs".

Pure stdlib (json / os / pathlib / tempfile) so both the lightweight producer and the
daemon import it without pulling in heavy dependencies. All paths come from
``daemon_paths`` so producer and consumer can never disagree about where the queue lives.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from scripts.managers.factories.daemons.daemon_paths import (
    PILOT_PROCESSING_DIR,
    PILOT_QUEUE_DIR,
)

# NB: '.' is deliberately NOT in the allow-list — the processing filename is
# "<slug>.<pid>.<ts>.json" and ``requeue_orphans`` recovers the instance via split('.', 1)[0],
# so a dot inside the slug would misattribute an orphan to the wrong instance's queue.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _slug(instance: str) -> str:
    """Filesystem-safe SINGLE-segment stem for an instance name (no dots — see ``_SAFE``)."""
    s = _SAFE.sub("_", str(instance)).strip("_-")
    return s or "default"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pilotjob_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)          # atomic overwrite of any existing pending job
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _queue_path(instance: str, mode: str) -> Path:
    """One pending slot per (instance, MODE) — so a pilot 'interactive' job and a 'jit' job for the
    same instance never clobber each other, while a re-enqueue of the SAME (instance, mode) supersedes
    the prior batch (newest wins)."""
    return PILOT_QUEUE_DIR / f"{_slug(instance)}__{_slug(mode)}.json"


# Claim priority by job mode (lower = claimed first). A time-sensitive JIT next-up grab is claimed
# BEFORE a bulk 'interactive' pilot sweep — a sweep can carry thousands of stubs and run for hours,
# so without this it would starve a freshly-enqueued JIT grab queued behind it. Unknown modes sit
# between JIT and the bulk sweep. Within one priority tier, oldest-first (mtime) order is preserved.
_MODE_PRIORITY = {"jit": 0, "legacy_regrab": 1, "interactive": 2}
_DEFAULT_MODE_PRIORITY = 1


def _mode_of(qpath: Path) -> str:
    """The job mode encoded in a ``<instance>__<mode>.json`` queue filename. ``rsplit`` so an
    instance whose slug itself contains '__' still resolves the trailing mode correctly; '' when
    the stem has no '__' separator."""
    stem = qpath.stem
    return stem.rsplit("__", 1)[-1] if "__" in stem else ""


def _priority_of(qpath: Path) -> int:
    return _MODE_PRIORITY.get(_mode_of(qpath), _DEFAULT_MODE_PRIORITY)


def has_higher_priority_pending(mode: str) -> bool:
    """True when a PENDING queue job outranks ``mode`` (strictly lower priority number). Lets a
    long-running bulk job (the interactive pilot sweep) cooperatively YIELD the daemon to a
    freshly-enqueued JIT grab instead of blocking it for the rest of the sweep."""
    try:
        mine = _MODE_PRIORITY.get(mode, _DEFAULT_MODE_PRIORITY)
        for qpath in PILOT_QUEUE_DIR.glob("*.json"):
            if _priority_of(qpath) < mine:
                return True
    except OSError:
        return False
    return False


def requeue(job: dict) -> "Path | None":
    """Re-enqueue a job that COOPERATIVELY YIELDED mid-run so it resumes later, PRESERVING its
    ``enqueued_at`` (the per-stub resume checkpoint is keyed on that id, so preserving it keeps the
    already-done stubs skipped on resume — ``enqueue`` would stamp a new id and re-run everything).
    Newest-wins: if a fresher batch already occupies the (instance, mode) slot, it supersedes and we
    do NOT clobber it. Returns the queue path, or None when superseded / malformed."""
    instance = job.get("instance")
    mode = str(job.get("mode") or "interactive")
    if not instance:
        return None
    path = _queue_path(instance, mode)
    if path.exists():
        return None                        # a fresher batch already supersedes this resume
    try:
        _atomic_write_json(path, job)      # writes the job AS-IS (keeps job['enqueued_at'])
        return path
    except Exception:
        return None


def enqueue(instance: str, job: dict) -> Path:
    """Write/overwrite the pending job for (instance, job['mode']) and return its path."""
    job = dict(job)
    job.setdefault("instance", instance)
    job["enqueued_at"] = time.time()
    path = _queue_path(instance, str(job.get("mode") or "interactive"))
    _atomic_write_json(path, job)
    return path


def _load_json(path: Path) -> dict | None:
    try:
        if path.stat().st_size == 0:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return None
    except json.JSONDecodeError:
        return None


def claim_next() -> tuple[Path, dict] | None:
    """Atomically claim the oldest pending job: rename ``queue/<x>.json`` into
    ``processing/`` (so another poll can't re-claim it) and return (processing_path, job).
    Returns None when the queue is empty. A claim that loses the rename race or a corrupt
    file is skipped (the corrupt one is removed)."""
    try:
        # JIT-priority first, then oldest-first within a tier (see _MODE_PRIORITY): a bulk pilot
        # sweep can never be claimed ahead of a pending time-sensitive JIT grab.
        pending = sorted(PILOT_QUEUE_DIR.glob("*.json"),
                         key=lambda p: (_priority_of(p), p.stat().st_mtime))
    except OSError:
        return None
    for qpath in pending:
        dest = PILOT_PROCESSING_DIR / f"{qpath.stem}.{os.getpid()}.{int(time.time() * 1000)}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(qpath, dest)            # claim — fails if another process took it first
        except OSError:
            continue
        job = _load_json(dest)
        if job is None:
            try:
                dest.unlink()                  # corrupt/empty claim — drop it
            except OSError:
                pass
            continue
        return dest, job
    return None


def complete(processing_path: Path) -> None:
    """Delete a finished job's processing file."""
    try:
        Path(processing_path).unlink(missing_ok=True)
    except OSError:
        pass


def remove(queue_path: Path) -> None:
    """Delete a still-PENDING queue file. Used to ROLL BACK an enqueue when the producer fails to
    ensure the daemon is running, so it doesn't leave an orphan job AND run the batch in-process
    (a double-search). No-op if the file is already gone (e.g. the daemon just claimed it)."""
    try:
        Path(queue_path).unlink(missing_ok=True)
    except OSError:
        pass


def requeue_orphans() -> int:
    """Move any leftover ``processing/`` files back to the queue (a crash mid-search) so the
    batch resumes. The newest per instance wins if a fresh queue file already exists — that
    queue file is a superset of due stubs, so we DROP the older orphan rather than clobber it.
    Returns the number of orphans re-queued. Called once on daemon start."""
    requeued = 0
    try:
        orphans = list(PILOT_PROCESSING_DIR.glob("*.json"))
    except OSError:
        return 0
    for opath in orphans:
        # Recover (instance, mode) from the job CONTENT, not the filename — robust to any chars.
        job = _load_json(opath)
        instance = str((job or {}).get("instance") or "")
        if not job or not instance:
            try:
                opath.unlink()                 # corrupt/instance-less orphan — drop it
            except OSError:
                pass
            continue
        qpath = _queue_path(instance, str(job.get("mode") or "interactive"))
        try:
            if qpath.exists():
                opath.unlink()                 # a newer pending batch already supersedes this
            else:
                qpath.parent.mkdir(parents=True, exist_ok=True)
                os.replace(opath, qpath)
                requeued += 1
        except OSError:
            pass
    return requeued


def pending_count() -> int:
    """Number of pending (queued) + in-flight (processing) jobs — used by the idle-exit check."""
    try:
        q = len(list(PILOT_QUEUE_DIR.glob("*.json")))
    except OSError:
        q = 0
    try:
        p = len(list(PILOT_PROCESSING_DIR.glob("*.json")))
    except OSError:
        p = 0
    return q + p

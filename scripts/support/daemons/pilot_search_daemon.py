"""
pilot_search_daemon.py
======================
Standalone background pilot-search daemon.

Drains Sonarr "pilot interactive search" batches OUT of the main run process. When a run
finds more stub pilots to search than ``daemons.pilot_search.threshold`` (default 10),
``run_pilot_search`` writes the batch to a JSON job file and ensures THIS daemon is running
instead of starting the in-process worker. The in-process worker is a NON-daemon thread, so a
massive spree (the live logs show a single run handing it ~9,000 stubs) blocks interpreter exit
until every indexer search finishes — hanging the whole run for hours. Offloading lets the run
exit immediately while the searches grind out-of-band, resuming across runs if interrupted.

Each claimed job runs ONE Sonarr interactive search per stub via the SAME shared core
(``sonarr/cache/pilot_interactive.interactive_pilot_search``) the in-process worker uses — set
the series to the lowest tier with results + fire an EpisodeSearch (Sonarr's quality + custom-
format scoring grabs the release), or flag UNACQUIRABLE when nothing is found. The UNACQUIRABLE
ledger is written to the exact same global-cache key the run reads.

Usage
-----
    python scripts/support/daemons/pilot_search_daemon.py            # run until idle, draining jobs
    python scripts/support/daemons/pilot_search_daemon.py --status   # print queue + daemon status, then exit (no work)
    python scripts/support/daemons/pilot_search_daemon.py --once     # claim+process one job, then exit
    python scripts/support/daemons/pilot_search_daemon.py --dry-run  # log claimed jobs, no searches/writes
    python scripts/support/daemons/pilot_search_daemon.py --verbose  # debug logging
    python scripts/support/daemons/pilot_search_daemon.py --force    # start even if another instance is up

Normally you do NOT launch this by hand — main.py / run_pilot_search (re)spawn it via
PilotSearchDaemonSupervisor when ``daemons.pilot_search.enabled`` is set.

Paths / tuning are single-sourced in managers/factories/daemons/daemon_paths.py; the job queue
contract lives in managers/factories/daemons/pilot_jobs.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# UTF-8 safe console (never crash / mojibake on cp1252), mirrors enrich_daemon.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Make ``scripts.*`` importable when launched detached (mirror enrich_daemon.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/daemons/pilot_search_daemon.py)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader            # noqa: E402
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder            # noqa: E402
from scripts.managers.factories.daemons import pilot_jobs                           # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import (                       # noqa: E402
    CONFIG_PATH, PILOT_IDLE_EXIT_S, PILOT_INTERACTIVE_WORKERS, PILOT_LOG_PATH,
    PILOT_PID_PATH, PILOT_POLL_INTERVAL_S, PILOT_PROCESSING_DIR, PILOT_QUEUE_DIR,
    PILOT_SEARCH_BATCH, PILOT_SEARCH_WORKERS, PILOT_STOP_SENTINEL,
)
from scripts.managers.services.sonarr.cache.pilot_interactive import (              # noqa: E402
    checkpoint_key,
    interactive_pilot_search,
)
from scripts.managers.services.sonarr.cache.jit_search import (                     # noqa: E402
    episodes_in_queue,
    jit_step_down_search,
    revert_inflight_qp,
)

# (connect, read) timeout. A Sonarr interactive search hits every indexer, so the read budget
# is generous; a slow/hung indexer can't stall the whole daemon past this.
_ARR_TIMEOUT = (10, 120)
_ARR_RETRIES = 3


# ── Logging ──────────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("pilot_search_daemon")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


log = _setup_logger()


class _LogAdapter:
    """Bridge the shared core's ``log_info/log_warning/log_debug`` calls onto the stdlib logger."""
    def log_info(self, msg):    log.info(msg)
    def log_warning(self, msg): log.warning(msg)
    def log_debug(self, msg):   log.debug(msg)
    def log_success(self, msg): log.info(msg)


_LOG = _LogAdapter()


# ── Stop sentinel + single-instance guard ─────────────────────────────────────────

def _stop_requested() -> bool:
    return PILOT_STOP_SENTINEL.exists()


def _pid_alive(pid: int) -> bool:
    """Window-free liveness probe (mirrors enrich_daemon): OpenProcess on Windows so the
    detached daemon never flashes a console; os.kill(0) on POSIX."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _interruptible_sleep(total_s: float) -> bool:
    """Sleep in small increments; return True if a stop was requested."""
    waited = 0.0
    while waited < total_s:
        if _stop_requested():
            return True
        time.sleep(min(PILOT_POLL_INTERVAL_S, max(0.05, total_s - waited)))
        waited += PILOT_POLL_INTERVAL_S
    return False


# ── Sonarr HTTP client (thin; mirrors BaseInstanceManager._make_request semantics) ──

class SonarrClient:
    """Per-instance Sonarr REST client built straight from the (secret-overlaid) config —
    the daemon does NOT reconstruct the manager hierarchy. ``_make_request`` matches the
    signature the shared search core expects (and BaseInstanceManager._make_request)."""

    def __init__(self, cfg: dict):
        self._instances = (cfg or {}).get("sonarr_instances", {}) or {}
        self._session = requests.Session()
        self._cache: dict[str, tuple[str, str]] = {}

    def _endpoint(self, instance: str) -> tuple[str, str]:
        if instance in self._cache:
            return self._cache[instance]
        inst_cfg = self._instances.get(instance) or {}
        raw = (inst_cfg.get("base_url") or inst_cfg.get("url") or "").strip()
        if raw and not raw.startswith(("http://", "https://")):
            proto = "https" if inst_cfg.get("ssl", True) else "http"
            raw = f"{proto}://{raw}"
            port = inst_cfg.get("port")
            if port and f":{port}" not in raw.split("://", 1)[-1]:
                raw = f"{raw}:{port}"
        base = raw.rstrip("/")
        api = (inst_cfg.get("api") or "").strip()
        self._cache[instance] = (base, api)
        return base, api

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        base, api = self._endpoint(instance)
        if not base or not api:
            log.warning(f"Sonarr '{instance}' not configured (no base_url/api) — returning fallback.")
            return fallback
        url = f"{base}/api/v3/{str(endpoint).lstrip('/')}"
        headers = {"X-Api-Key": api}
        method_upper = (method or "GET").upper()
        # GET/PUT are idempotent → safe to retry on any error. POST (EpisodeSearch / command) is
        # NOT — a retry after the request reached Sonarr would DOUBLE-queue the grab (re-creating
        # the command-queue flood batching exists to prevent), so POST is single-attempt EXCEPT on
        # a pre-send connect timeout (the request demonstrably never left the client).
        idempotent = method_upper in ("GET", "PUT")
        last_exc = None
        attempt = 0
        while True:
            attempt += 1
            if _stop_requested():
                return fallback
            try:
                if method_upper == "GET":
                    resp = self._session.get(url, headers=headers, timeout=_ARR_TIMEOUT)
                elif method_upper == "POST":
                    resp = self._session.post(url, headers=headers, json=payload, timeout=_ARR_TIMEOUT)
                elif method_upper == "PUT":
                    resp = self._session.put(url, headers=headers, json=payload, timeout=_ARR_TIMEOUT)
                else:
                    return fallback
                resp.raise_for_status()
                if not resp.content:
                    return fallback
                try:
                    return resp.json()
                except ValueError:
                    return fallback
            except requests.exceptions.ConnectTimeout as e:
                last_exc = e               # never sent → safe to retry for any method
                if attempt < _ARR_RETRIES:
                    time.sleep(0.5 * attempt)
                    continue
                break
            except Exception as e:
                last_exc = e
                # *arr SQLite "database is locked" fails fast and never commits, so a GET/PUT retry
                # is safe; a POST is not retried here (it may have reached Sonarr).
                if idempotent and attempt < _ARR_RETRIES:
                    time.sleep(0.5 * attempt)
                    continue
                break
        log.debug(f"{method_upper} /{endpoint} on '{instance}' failed after {attempt} attempt(s): {last_exc}")
        return fallback


# ── UNACQUIRABLE ledger (writes the EXACT key run_pilot_search reads) ──────────────

class LedgerCache:
    """Read/write the per-instance UNACQUIRABLE ledger JSON the run consumes. Uses the same
    CacheKeyBuilder path the GlobalCacheManager json handler does, so the daemon's writes are
    byte-compatible with what ``run_pilot_search`` loads. Atomic (temp + replace) so a reader
    never sees a partial file."""

    def __init__(self):
        self._kb = CacheKeyBuilder()

    def _path(self, key: str) -> Path:
        return self._kb.build_cache_path(*key.split("/"), suffix=".json")

    def get(self, key: str, default=None):
        path = self._path(key)
        try:
            if path.stat().st_size == 0:
                return default
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, OSError):
            return default
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ── Job processing ─────────────────────────────────────────────────────────────────

def process_job(cfg: dict, job: dict, ledger: LedgerCache, dry_run: bool) -> dict:
    """Run one queued pilot-search batch through the shared interactive-search core.
    Returns the core's outcome dict (or an empty one when the job is malformed/dry-run)."""
    instance = job.get("instance")
    mode     = job.get("mode", "interactive")
    if not instance:
        log.warning("Malformed job (no instance) — dropping.")
        return {"searched": [], "flagged": {}}
    if mode == "jit":
        return _process_jit_job(cfg, job, ledger, dry_run)
    items    = [(int(s), int(e)) for s, e in (job.get("items") or [])
                if s is not None and e is not None]
    ladder   = [(int(p), int(r)) for p, r in (job.get("ladder") or []) if p is not None]
    meta     = job.get("meta") or {}                # str-keyed; the core handles str/int keys
    indexers = list(job.get("current_indexers") or [])
    try:
        floor_res = int(job.get("floor_res") or 0)
    except (TypeError, ValueError):
        floor_res = 0

    if not items or not ladder:
        log.warning(f"Malformed job (instance={instance!r}, items={len(items)}, ladder={len(ladder)}) — dropping.")
        return {"searched": [], "flagged": {}}
    if mode != "interactive":
        # Only interactive + jit are offloaded; anything else shouldn't reach here.
        log.warning(f"Job mode {mode!r} not supported by the daemon — dropping ({len(items)} stub(s)).")
        return {"searched": [], "flagged": {}}

    # EpisodeSearch grab-triggers are fired in chunks of this many episode ids (config override →
    # constant default) so a huge batch posts a handful of commands to Sonarr's task queue, not one
    # per stub.
    try:
        batch = int(((cfg.get("daemons", {}) or {}).get("pilot_search", {}) or {})
                    .get("search_batch", PILOT_SEARCH_BATCH))
    except (TypeError, ValueError):
        batch = PILOT_SEARCH_BATCH
    batch = max(1, batch)
    try:
        workers = int(((cfg.get("pilot_interactive", {}) or {}).get("search_workers",
                                                                     PILOT_INTERACTIVE_WORKERS)))
    except (TypeError, ValueError):
        workers = PILOT_INTERACTIVE_WORKERS
    workers = max(1, workers)

    log.info(f"Processing pilot search: '{instance}' — {len(items)} stub(s), "
             f"ladder ≤{ladder[0][1]}p → ≤{ladder[-1][1]}p, {len(indexers)} indexer(s), "
             f"{workers} concurrent search(es), EpisodeSearch batch ≤{batch}.")
    if dry_run:
        log.info(f"[dry-run] would interactive-search {len(items)} pilot(s) for '{instance}' — no writes.")
        return {"searched": [], "flagged": {}}

    anime_ladder = [(int(p), int(r)) for p, r in (job.get("anime_ladder") or []) if p is not None]
    anime_sids   = [int(s) for s in (job.get("anime_sids") or [])]

    # Per-stub resume: if this exact job (matched by enqueued_at) left a checkpoint behind from an
    # interrupted run, skip the stubs it already finished instead of re-running the whole batch.
    job_id = job.get("enqueued_at")
    skip_sids = []
    try:
        ckpt = ledger.get(checkpoint_key(instance)) or {}
        if ckpt and str(ckpt.get("job_id")) == str(job_id):
            skip_sids = ckpt.get("done") or []
            if skip_sids:
                log.info(f"Resuming '{instance}' — skipping {len(skip_sids):,} stub(s) already done "
                         f"by an interrupted run.")
    except Exception:
        skip_sids = []

    client = SonarrClient(cfg)
    result = interactive_pilot_search(
        make_request=client._make_request,
        logger=_LOG,
        global_cache=ledger,
        instance=instance, items=items, ladder=ladder, meta=meta,
        current_indexers=indexers, floor_res=floor_res,
        max_workers=workers,
        search_batch_size=batch,
        search_no_resolution=bool(job.get("search_no_resolution", True)),
        skip_hard_rejects=bool(job.get("skip_hard_rejects", True)),
        soft_floor=bool(job.get("soft_floor", True)),
        anime_ladder=anime_ladder, anime_sids=anime_sids,
        job_id=job_id, skip_sids=skip_sids,
    )
    # Completed cleanly → drop the resume checkpoint (a crash mid-run leaves it for the resume above).
    if ledger is not None:
        try:
            ledger.set(checkpoint_key(instance), {})
        except Exception:
            pass
    log.info(f"Job done for '{instance}': {len(result.get('searched', []))} searched at the lowest "
             f"available tier, {len(result.get('flagged', {}))} flagged UNACQUIRABLE.")
    return result


def _process_jit_job(cfg: dict, job: dict, ledger, dry_run: bool) -> dict:
    """Run one queued JIT step-down batch through the shared ``jit_step_down_search`` core. The job's
    items are ``[[sid, [[tier_res, [[ep_id, season, episode], ...], [step_pid, ...]], ...]], ...]``."""
    instance = job.get("instance")
    items = []
    for entry in (job.get("items") or []):
        try:
            sid = int(entry[0])
            groups = []
            for g in entry[1]:
                tier_res  = int(g[0])
                eps       = [(int(e[0]), int(e[1]), int(e[2])) for e in g[1]]
                step_pids = [int(p) for p in g[2]]
                if eps and step_pids:
                    groups.append((tier_res, eps, step_pids))
            if groups:
                items.append((sid, groups))
        except (TypeError, ValueError, IndexError):
            continue
    if not items:
        log.warning(f"Malformed jit job for '{instance}' — dropping.")
        return {"failed": []}

    _grp = sum(len(g) for _s, g in items)
    log.info(f"Processing JIT step-down: '{instance}' — {len(items)} series, {_grp} tier-group(s).")
    if dry_run:
        log.info(f"[dry-run] would JIT step-down search {len(items)} series for '{instance}' — no writes.")
        return {"failed": []}

    client = SonarrClient(cfg)
    result = jit_step_down_search(
        make_request=client._make_request,
        in_queue=lambda inst, ids: episodes_in_queue(client._make_request, inst, ids),
        logger=_LOG,
        global_cache=ledger,
        instance=instance, items=items,
        max_workers=PILOT_SEARCH_WORKERS,
    )
    log.info(f"JIT job done for '{instance}': {len(result.get('failed', []))} ep(s) not grabbed "
             f"(re-enabled for retry next run).")
    return result


def _revert_stranded_jit_qp() -> None:
    """On daemon start, revert any series a crashed JIT job left at a bumped quality profile,
    restoring the pre-flip profile recorded in the inflight-QP store. The single-instance pid guard
    guarantees no other daemon is mid-flight, so every inflight entry is genuinely stranded. Runs
    BEFORE orphaned jit jobs are requeued (else a re-run would capture the bumped profile as the
    'original'). Best-effort; needs config to build a Sonarr client."""
    ddir = CacheKeyBuilder().base_dir / "sonarr" / "jit" / "inflight_qp"
    try:
        files = list(ddir.glob("*.json"))
    except OSError:
        files = []
    if not files:
        return
    try:
        cfg = ConfigLoader(CONFIG_PATH).load()
    except Exception as e:
        log.warning(f"[JIT] could not load config for resume-revert: {e}")
        return
    client = SonarrClient(cfg)
    ledger = LedgerCache()
    total = 0
    for f in files:
        instance = f.stem
        try:
            total += revert_inflight_qp(make_request=client._make_request, logger=_LOG,
                                        global_cache=ledger, instance=instance)
        except Exception as e:
            log.warning(f"[JIT] resume-revert failed for '{instance}': {e}")
    if total:
        log.info(f"[JIT] resume-reverted {total} stranded series profile(s) on start.")


# ── Status report (read-only) ──────────────────────────────────────────────────────

def _read_job_safe(path: Path) -> dict | None:
    try:
        if path.stat().st_size == 0:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fmt_age(secs: float) -> str:
    if secs < 0:
        return ""
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172_800:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.1f}d ago"


def _read_progress(instance: str) -> dict | None:
    """Read the live progress heartbeat (``{total, done, reasons, updated_at}``) for an in-flight
    interactive pass, written by the search core. None when absent/unreadable."""
    try:
        p = CacheKeyBuilder().build_cache_path("sonarr", "pilot", "progress", instance, suffix=".json")
        if p.stat().st_size == 0:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _report_dir(label: str, directory: Path, with_progress: bool = False) -> int:
    """Print one queue directory's jobs (instance, count, mode, age) and return total items.
    For in-flight interactive jobs, append live X/total progress so a long batch never looks hung."""
    try:
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        files = []
    print(f"\n  {label} ({len(files)} job(s)):")
    if not files:
        print("    (none)")
        return 0
    total = 0
    for f in files:
        job = _read_job_safe(f)
        items = (job or {}).get("items") or []
        total += len(items)
        inst = (job or {}).get("instance") or f.stem
        mode = (job or {}).get("mode", "?")
        try:
            ts = float((job or {}).get("enqueued_at") or 0)
            age = f"  enqueued {_fmt_age(time.time() - ts)}" if ts > 0 else ""
        except Exception:
            age = ""
        prog = ""
        if with_progress and mode == "interactive":
            pdata = _read_progress(inst)
            if pdata and pdata.get("total"):
                prog = (f"  [{pdata.get('done', 0)}/{pdata.get('total')} done"
                        + (f", {_fmt_age(time.time() - _parse_iso(pdata.get('updated_at')))} since last"
                           if _parse_iso(pdata.get("updated_at")) else "") + "]")
        flag = "" if job else "  ⚠️ unreadable/corrupt"
        unit = "series" if mode == "jit" else "stub(s)"
        print(f"    {inst:<18} {len(items):>7,} {unit:<7}  mode={mode}{age}{prog}{flag}")
    return total


def _parse_iso(s) -> float:
    """ISO-8601 → epoch seconds (0 if unparseable) — for the 'N since last update' progress hint."""
    try:
        from datetime import datetime as _dt
        d = _dt.fromisoformat(str(s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


def _diagnostics_report() -> None:
    """Print the latest per-instance ACQUISITION-REASON snapshot — why pilots did / didn't acquire
    (no_results / below_floor / rejected / available) plus the top Sonarr rejection reasons. Written
    by the search core each pass to sonarr/pilot/diagnostics/<instance>.json. Pure disk read."""
    ddir = CacheKeyBuilder().base_dir / "sonarr" / "pilot" / "diagnostics"
    try:
        files = sorted(ddir.glob("*.json"))
    except OSError:
        files = []
    print("\n  acquisition reasons (last pilot pass per instance):")
    if not files:
        print("    (none yet — run a pilot search pass; reasons appear after the daemon processes a batch)")
        return
    order = ["available", "rejected", "rejected_hard", "no_results", "no_resolution",
             "below_floor", "search_failed"]
    for f in files:
        data = _read_job_safe(f) or {}
        reasons = data.get("reasons") or {}
        inst = data.get("instance") or f.stem
        try:
            then = datetime.fromisoformat(data["at"])
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            when = _fmt_age((datetime.now(tz=timezone.utc) - then).total_seconds())
        except Exception:
            when = ""
        total = sum(reasons.values())
        print(f"    {inst}  ({total:,} stub(s){', ' + when if when else ''}):")
        for r in order + [k for k in reasons if k not in order]:
            if reasons.get(r):
                note = {"available": "found a usable release",
                        "rejected": "rejected now but a profile flip may clear it → still searched",
                        "rejected_hard": "all rejected for size/blocklist/incomplete a flip can't fix → skipped",
                        "no_results": "indexers returned nothing",
                        "no_resolution": "release(s) found but no resolution reported (likely SD-only) → searched at floor",
                        "below_floor": "only below the resolution floor",
                        "search_failed": "search call timed out / rate-limited → DEFERRED, not flagged (re-probes next run)"}.get(r, "")
                print(f"        {r:<12} {reasons[r]:>7,}  {note}")
        bo = data.get("batch_only") or 0
        if bo:
            print(f"        ↳ of the 'rejected', {bo} are SEASON-PACK ONLY "
                  f"(available only as anime batches — a SeasonSearch would grab them)")
        rej = data.get("top_rejections") or []
        if rej:
            print("        top rejections: " + ", ".join(f"{m} ×{c}" for m, c in rej[:6]))


def print_status() -> None:
    """One-shot, read-only queue + daemon report. Pure disk read — safe to run while the
    supervisor-spawned daemon is mid-search (it never claims a job or spawns a second daemon)."""
    print("=" * 64)
    print("  Sonarr Pilot-Search Daemon — status")
    print("=" * 64)

    try:
        pid = int(PILOT_PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        pid = None
    if pid and _pid_alive(pid):
        print(f"  daemon       RUNNING (pid {pid})")
    elif pid:
        print(f"  daemon       not running (stale pid {pid} — will re-spawn on the next enqueue)")
    else:
        print("  daemon       not running (spawned on demand when a run spills > threshold pilots)")
    if PILOT_STOP_SENTINEL.exists():
        print("  stop         STOP sentinel present (a shutdown was requested)")

    q_total = _report_dir("queued", PILOT_QUEUE_DIR)
    p_total = _report_dir("in-flight (processing)", PILOT_PROCESSING_DIR, with_progress=True)

    print(f"\n  TOTAL pending stubs: {q_total + p_total:,}")
    _diagnostics_report()
    print(f"\n  log: {PILOT_LOG_PATH}")
    print("=" * 64)


# ── Main loop ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sonarr pilot-search daemon")
    parser.add_argument("--once",    action="store_true",
                        help="Claim + process a single job (or exit if the queue is empty), then exit")
    parser.add_argument("--dry-run", action="store_true", help="Log claimed jobs without searching/writing")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--force",   action="store_true",
                        help="Start even if another instance appears to be running")
    parser.add_argument("--status",  action="store_true",
                        help="Print the queue + daemon status and exit; claims no jobs, spawns nothing")
    args = parser.parse_args()

    # Read-only report: print and exit BEFORE the pid guard / loop, so it works while the
    # supervisor-spawned daemon is running and never starts a second one.
    if args.status:
        print_status()
        return

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Single-instance guard: refuse to start if another live daemon owns the pid file, so two
    # spawns can't both hammer the Sonarr server / indexers. --force overrides.
    if PILOT_PID_PATH.exists() and not args.force:
        try:
            other = int(PILOT_PID_PATH.read_text().strip())
        except (ValueError, OSError):
            other = None
        try:
            alive = bool(other and other != os.getpid() and _pid_alive(other))
        except Exception:
            alive = False
        if alive:
            log.error(f"Another pilot_search_daemon is already running (pid {other}). "
                      f"Stop it first (or pass --force to override).")
            sys.exit(1)

    # PID + sentinel lifecycle.
    PILOT_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        PILOT_PID_PATH.write_text(str(os.getpid()))
    except OSError as e:
        log.warning(f"Could not write pid file: {e}")
    try:
        PILOT_STOP_SENTINEL.unlink(missing_ok=True)   # clear any stale stop from a prior run
    except OSError:
        pass

    log.info("=" * 60)
    log.info("  Sonarr Pilot-Search Daemon")
    log.info(f"  workers={PILOT_SEARCH_WORKERS}  poll={PILOT_POLL_INTERVAL_S}s  "
             f"idle_exit={PILOT_IDLE_EXIT_S}s  dry_run={args.dry_run}  pid={os.getpid()}")
    log.info("=" * 60)

    # JIT crash-safety: restore any series a crashed jit job left at a bumped quality profile,
    # BEFORE requeuing the orphaned job (else its re-run captures the bumped profile as "original").
    try:
        _revert_stranded_jit_qp()
    except Exception as e:
        log.warning(f"Could not run JIT resume-revert: {e}")

    # Resume any job a prior daemon crashed mid-search (move processing/ back to the queue).
    try:
        resumed = pilot_jobs.requeue_orphans()
        if resumed:
            log.info(f"Resumed {resumed} orphaned job(s) from a previous run.")
    except Exception as e:
        log.warning(f"Could not requeue orphaned jobs: {e}")

    ledger = LedgerCache()
    last_work = time.monotonic()
    try:
        while True:
            if _stop_requested():
                log.info("Stop requested — exiting.")
                break

            claimed = pilot_jobs.claim_next()
            if claimed is None:
                if args.once:
                    log.info("--once and the queue is empty — exiting.")
                    break
                # Idle: exit after a long quiet spell so a daemon never lingers forever; the
                # supervisor re-spawns it the next time a run enqueues a batch.
                if (PILOT_IDLE_EXIT_S and (time.monotonic() - last_work) >= PILOT_IDLE_EXIT_S
                        and pilot_jobs.pending_count() == 0):
                    log.info(f"Idle for ~{PILOT_IDLE_EXIT_S}s with no jobs — exiting "
                             f"(re-spawned on the next enqueue).")
                    break
                if _interruptible_sleep(PILOT_POLL_INTERVAL_S):
                    log.info("Stop requested during idle — exiting.")
                    break
                continue

            proc_path, job = claimed
            try:
                loader = ConfigLoader(CONFIG_PATH)       # overlays Sonarr secrets from keyring/env
                cfg    = loader.load()
                process_job(cfg, job, ledger, dry_run=args.dry_run)
            except KeyboardInterrupt:
                # Leave the claimed job in processing/ so the next start resumes it.
                log.info("Interrupted mid-job — exiting (job will resume on restart).")
                raise
            except Exception as e:
                log.error(f"Job {proc_path.name} failed: {e}", exc_info=True)
            finally:
                pilot_jobs.complete(proc_path)
            last_work = time.monotonic()

            if args.once:
                log.info("--once flag set — exiting after one job.")
                break
    except KeyboardInterrupt:
        log.info("Interrupted — exiting.")
    finally:
        log.info(f"Daemon shutting down cleanly (pid {os.getpid()}).")
        try:
            PILOT_PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()

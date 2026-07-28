"""
labels/first_run.py — reconstruct a first install's labels ONCE, automatically.
================================================================================
The prospective snapshot pipeline (``labels/snapshots.py``) starts logging the
day Glidearr is installed and produces its first *matured* label a horizon later
— so for the first two weeks a new household has exactly zero supervised
evidence, and everything downstream (threshold derivation, calibration, the
weight refit) has nothing to say. Meanwhile the same install already owns months
of Tautulli watch history: the labels exist, they simply have not been
reconstructed yet.

``scripts/support/tools/ml_backfill_snapshots.py`` does that reconstruction by
TRUNCATED REPLAY (rescoring every then-existing movie against the event prefix
strictly before each grid date). It is a standalone CLI, run by hand, which is
exactly the step a new user will not know to take. This module fires it ONCE,
automatically, on a fresh install:

    maybe_backfill_on_first_run(config, base_dir=…, logger=…)

WHEN IT FIRES (every condition, in cheapest-first order)
-------------------------------------------------------
1. ``ml.snapshots.enabled`` and ``ml.snapshots.backfill_on_first_run`` are both
   on (both DEFAULT TRUE).
2. No marker file at ``<cache>/ml/snapshots/first_run_backfill.json``. The
   marker is written on the first ATTEMPT, success or failure, so this can never
   become a per-run cost — deleting it is the documented way to retry.
3. The process has not already attempted it (a second manager graph in the same
   run cannot double-fire).
4. Tautulli history exists and is non-empty (nothing to replay otherwise) and
   the instance's ``movie_files.parquet`` exists (the entity set to rescore).
   Neither of those is durable on a first boot, so a miss here writes NO marker:
   the next run tries again.
5. The snapshot store carries NO usable evidence yet — no backfill rows, and no
   prospective row whose horizon has closed. NOT literally "no rows": this runs
   at the END of a run, from ``ledger/plan_summary``, which is the only moment
   when the caches the replay needs (Tautulli history, movie_files, the people
   buckets) are warm — and by then the run has already appended TODAY's
   prospective rows. Today's rows are not evidence; they mature in a horizon.
   An empty store trivially satisfies this.

BOUNDED BY CONSTRUCTION
-----------------------
Cost is (grid points × library size) rescores, so the grid is capped rather than
run over the whole history: ``ml.snapshots.backfill_grid_days`` (default 7) and
``ml.snapshots.backfill_max_grid_points`` (default 26) put a ~6-month, 26-point
ceiling on the first run, and the start is additionally clamped to the first real
event so pre-history grid points are never rescored. Both map onto the tool's own
existing ``--grid-days`` / ``--start`` arguments; no new replay code exists here.

NEVER FATAL, NEVER LOUD
-----------------------
Every path is wrapped: an exception, a missing cache, a non-zero exit — all
become a logged no-op that returns a status dict. The tool's stdout is captured
(it is a CLI and prints a full report) and collapsed into one log line. Nothing
here can affect a score, a decision, or an *arr write.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: One-shot marker; lives beside the snapshots it guards.
MARKER_NAME = "first_run_backfill.json"

DEFAULT_GRID_DAYS = 7          # -> --grid-days (the tool's own default)
DEFAULT_MAX_GRID_POINTS = 26   # ~6 months at weekly spacing
DEFAULT_HORIZON_DAYS = 14      # matches ml.thresholds.horizon_days' default
DEFAULT_INSTANCE = "standard"

_TOOL_REL = ("scripts", "support", "tools", "ml_backfill_snapshots.py")

#: Process-level guard: one attempt per process, whatever the caller does.
_ATTEMPTED = False


# ── config ────────────────────────────────────────────────────────────────────

def _snapshot_config(config) -> dict:
    """The ``ml.snapshots`` block. Always a dict — malformed reads as empty."""
    try:
        ml = (config or {}).get("ml", {}) or {}
    except Exception:
        return {}
    blk = ml.get("snapshots", {}) if isinstance(ml, dict) else {}
    return blk if isinstance(blk, dict) else {}


def _flag(blk: dict, key: str, default: bool) -> bool:
    v = blk.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on", "y"}
    try:
        return bool(v)
    except Exception:
        return default


def enabled(config) -> bool:
    """``ml.snapshots.backfill_on_first_run`` — DEFAULT TRUE, and additionally
    gated by ``ml.snapshots.enabled`` (backfilling into a store the household
    switched off would be rude)."""
    blk = _snapshot_config(config)
    return _flag(blk, "enabled", True) and _flag(blk, "backfill_on_first_run", True)


def grid_days(config) -> int:
    """``ml.snapshots.backfill_grid_days`` → the tool's ``--grid-days``."""
    try:
        v = int(_snapshot_config(config).get("backfill_grid_days", DEFAULT_GRID_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_GRID_DAYS
    return v if v >= 1 else DEFAULT_GRID_DAYS


def max_grid_points(config) -> int:
    """``ml.snapshots.backfill_max_grid_points`` — the wall-clock bound. Each
    point rescores the whole movie library, so this is the knob that decides
    whether the first run costs seconds or minutes. 0 = unbounded (the tool then
    replays from the first event, which is what the CLI does by hand)."""
    try:
        v = int(_snapshot_config(config).get("backfill_max_grid_points",
                                             DEFAULT_MAX_GRID_POINTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_GRID_POINTS
    return v if v >= 0 else DEFAULT_MAX_GRID_POINTS


def resolve_instance(config) -> str:
    """The radarr instance whose ``movie_files.parquet`` defines the entity set:
    the configured ``default_instance``, else the first configured one, else
    ``"standard"`` (the tool's default)."""
    try:
        insts = (config or {}).get("radarr_instances", {}) or {}
    except Exception:
        return DEFAULT_INSTANCE
    if not isinstance(insts, dict) or not insts:
        return DEFAULT_INSTANCE
    default = insts.get("default_instance")
    if isinstance(default, str) and default and default in insts:
        return default
    for name in insts:
        if name != "default_instance":
            return str(name)
    return DEFAULT_INSTANCE


# ── preconditions ─────────────────────────────────────────────────────────────

def marker_path(base_dir) -> Path:
    from scripts.managers.machine_learning.labels.snapshots import SNAPSHOT_SUBDIR
    return Path(base_dir).joinpath(*SNAPSHOT_SUBDIR, MARKER_NAME)


def _write_marker(base_dir, payload: dict) -> "Path | None":
    """Stamp the one-shot marker (atomic). A write failure is itself a no-op —
    worst case the trigger re-evaluates next run and finds the store populated."""
    try:
        path = marker_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def history_span(base_dir) -> "tuple[int, datetime | None]":
    """``(n_events, first_event_utc)`` from ``tautulli/history/all.json`` —
    read directly (not via the tool) so the precondition costs one file read and
    no pandas import."""
    try:
        raw = json.loads((Path(base_dir) / "tautulli" / "history" / "all.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, None
    if not isinstance(raw, list) or not raw:
        return 0, None
    first = None
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            ts = datetime.fromtimestamp(int(r.get("date")), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if first is None or ts < first:
            first = ts
    return len(raw), first


def store_has_evidence(base_dir, horizon_days: int = DEFAULT_HORIZON_DAYS,
                       now=None) -> bool:
    """Does the snapshot store already hold a label worth having?

    True when ANY backfill row exists (the reconstruction already ran, by hand or
    by us) or when ANY prospective row's horizon has closed (the household has
    grown its own evidence). Today's freshly-appended prospective rows are NOT
    evidence — that is precisely the fresh-install case this trigger serves."""
    try:
        from scripts.managers.machine_learning.labels.snapshots import (
            SOURCE_BACKFILL,
            load_snapshots,
        )
        df = load_snapshots(base_dir)
    except Exception:
        return False
    if df is None or getattr(df, "empty", True):
        return False
    try:
        if "source" in df.columns and bool((df["source"] == SOURCE_BACKFILL).any()):
            return True
        if "snapshot_ts" not in df.columns:
            return False
        import pandas as pd
        cutoff = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        cutoff = cutoff - pd.Timedelta(days=float(horizon_days))
        ts = pd.to_datetime(df["snapshot_ts"], utc=True, errors="coerce")
        return bool((ts.notna() & (ts <= cutoff)).any())
    except Exception:
        return False


def should_backfill(config, base_dir, *, horizon_days: int = DEFAULT_HORIZON_DAYS,
                    now=None) -> "tuple[bool, str, bool]":
    """``(should_run, reason, durable)``.

    *durable* says whether a "no" is permanent (the store already has evidence,
    or the marker/flag says never) and therefore worth recording so the check
    never runs again — as opposed to a transient miss (caches not built yet) that
    should simply be re-evaluated next run."""
    if not enabled(config):
        return False, "disabled (ml.snapshots.backfill_on_first_run=false)", False
    if _ATTEMPTED:
        return False, "already attempted in this process", False
    try:
        if marker_path(base_dir).exists():
            return False, f"already run once (marker: {marker_path(base_dir)})", False
    except Exception:
        pass

    n_events, first_event = history_span(base_dir)
    if not n_events or first_event is None:
        return False, "no Tautulli history cached yet — nothing to replay", False

    instance = resolve_instance(config)
    mf = Path(base_dir) / "radarr" / instance / "movie_files.parquet"
    if not mf.exists():
        return False, f"no {mf.name} for instance '{instance}' yet", False

    if store_has_evidence(base_dir, horizon_days, now=now):
        return False, "snapshot store already carries matured/backfilled labels", True
    return True, f"fresh install: {n_events} history event(s), no matured labels", False


# ── the run ───────────────────────────────────────────────────────────────────

def _load_tool():
    """Import ``ml_backfill_snapshots`` by path — ``scripts/support/tools`` is a
    plain directory (no ``__init__.py``), exactly as the tool's own test does."""
    import importlib.util
    repo_root = Path(__file__).resolve().parents[4]
    path = repo_root.joinpath(*_TOOL_REL)
    spec = importlib.util.spec_from_file_location("_glidearr_ml_backfill", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_argv(config, base_dir, *, horizon_days: int = DEFAULT_HORIZON_DAYS,
               first_event=None, now=None, config_path=None) -> list:
    """The bounded CLI invocation — only arguments the tool already has.

    ``--start`` is ``max(first_event, end_cap − (points−1)·grid_days)``: the cap
    bounds the work, the first-event clamp stops us rescoring the library over
    grid dates that predate the household's history (all-zero features, pure
    cost). ``--end`` is left to the tool, which clamps it to ``now − horizon``
    AND below the first prospective snapshot date (its never-mix guard)."""
    gd = grid_days(config)
    points = max_grid_points(config)
    now = now or datetime.now(tz=timezone.utc)
    argv = ["--cache-base", str(base_dir),
            "--instance", resolve_instance(config),
            "--grid-days", str(gd),
            "--horizon-days", str(int(horizon_days))]
    if points:
        start = (now - timedelta(days=float(horizon_days))
                 - timedelta(days=float(gd * (points - 1))))
        if first_event is not None and first_event > start:
            start = first_event
        argv += ["--start", f"{start:%Y-%m-%d}"]
    elif first_event is not None:
        argv += ["--start", f"{first_event:%Y-%m-%d}"]
    if config_path:
        argv += ["--config", str(config_path)]
    return argv


def maybe_backfill_on_first_run(config, base_dir=None, *, global_cache=None,
                                logger=None, horizon_days: int = DEFAULT_HORIZON_DAYS,
                                now=None, config_path=None, runner=None) -> dict:
    """Fire the truncated-replay backfill once on a fresh install. NEVER raises.

    Returns ``{"status": ...}`` where status is one of ``skipped`` (a
    precondition said no — ``reason`` explains), ``ok`` (the tool ran and
    returned 0), ``failed`` (ran non-zero or raised). ``runner`` is the tool's
    ``main(argv) -> int``; injected by the tests, loaded by path otherwise."""
    global _ATTEMPTED
    try:
        base = Path(base_dir) if base_dir is not None else _resolve_base_dir(global_cache)
        go, reason, durable = should_backfill(config, base,
                                              horizon_days=horizon_days, now=now)
        if not go:
            if durable:
                _write_marker(base, {"status": "not_needed", "reason": reason,
                                     "at": datetime.now(tz=timezone.utc).isoformat()})
            _log(logger, "debug", f"[MLBackfill] first-run backfill skipped: {reason}")
            return {"status": "skipped", "reason": reason}

        _ATTEMPTED = True
        _, first_event = history_span(base)
        argv = build_argv(config, base, horizon_days=horizon_days,
                          first_event=first_event, now=now, config_path=config_path)
        _log(logger, "info",
             "[MLBackfill] fresh install detected — reconstructing snapshots from "
             f"Tautulli history once ({' '.join(argv[4:])}). This run only.")

        started = datetime.now(tz=timezone.utc)
        out, rc = _invoke(runner, argv)
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        status = "ok" if rc == 0 else "failed"
        tail = _summarise(out)
        _write_marker(base, {"status": status, "reason": reason, "argv": argv,
                             "rc": rc, "elapsed_s": round(elapsed, 1),
                             "at": started.isoformat(), "summary": tail})
        _log(logger, "info" if rc == 0 else "warning",
             f"[MLBackfill] first-run backfill {status} in {elapsed:.1f}s"
             + (f" — {tail}" if tail else ""))
        return {"status": status, "rc": rc, "argv": argv,
                "elapsed_s": elapsed, "summary": tail}
    except Exception as e:                      # pragma: no cover - belt & braces
        _log(logger, "debug", f"[MLBackfill] first-run backfill skipped: {e}")
        return {"status": "failed", "error": str(e)}


# ── plumbing ──────────────────────────────────────────────────────────────────

def _invoke(runner, argv) -> "tuple[str, int]":
    """Run the tool with stdout captured (it is a CLI and prints a full report);
    a raise is reported as rc=1 rather than propagating."""
    import contextlib
    import io
    main = runner if runner is not None else _load_tool().main
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(main(list(argv)) or 0)
    except SystemExit as e:                     # argparse's exit path
        rc = int(getattr(e, "code", 1) or 0)
    except Exception:
        rc = 1
    return buf.getvalue(), rc


def _summarise(out: str) -> str:
    """The two lines a human wants from the tool's report: how many rows landed
    and how many positives they carry."""
    keep = [ln.strip() for ln in (out or "").splitlines()
            if ln.startswith("appended ") or ln.startswith("labels @ ")
            or ln.startswith("reconstructed rows:")]
    return "; ".join(keep[:3])[:300]


def _resolve_base_dir(global_cache):
    root = getattr(global_cache, "cache_root", None)
    if root:
        return Path(root)
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    return CacheKeyBuilder().base_dir


def _log(logger, level: str, msg: str) -> None:
    try:
        getattr(logger, f"log_{level}")(msg)
    except Exception:
        pass


def reset_process_guard() -> None:
    """Tests only: forget that this process already attempted a backfill."""
    global _ATTEMPTED
    _ATTEMPTED = False

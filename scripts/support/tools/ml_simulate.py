"""
ml_simulate.py — end-to-end simulation / parameter-recovery harness for the ML
pipeline. (offline, synthetic — NEVER touches the real cache)
================================================================================
Standalone CLI (never imported by main.py). Plants a KNOWN ground truth —
per-signal-group preference weights beta* (log-odds per raw score point), a
discrete-time rewatch hazard curve h*, and a synthetic movie library — then
simulates a household's watch behaviour from exactly those parameters, writes
the artifacts the real pipeline reads into a THROWAWAY cache dir:

  * Stage-1 snapshot Parquets, written through the REAL writer
    (labels/snapshots.build_movie_snapshot_rows + append_snapshot),
    one snapshot of every title per simulated day
  * tautulli/history/all.json          (event dicts: date=epoch secs,
                                        media_type, rating_key, percent_complete)
  * plex/movies/owned_inventory.json   (tmdb -> rating_key, the movie label join)
  * tautulli/group/household/tmdb_completions.json (relaxed-completion path)
  * plex/playlists/protected_movie_tmdbs/{movie,combined}.json (Up Next tmdbs —
    a random slice, so recommended_not_watched negatives exist)

and runs the REAL offline chain against that dir:

    labels/labeling.build_labels -> ml_forward_validation -> ml_weight_refit
                                                          -> ml_survival_report
    (+ ml_train_challenger, when lightgbm is importable — see below)

then asserts the chain RECOVERS what was planted:

  R1  n_pos comfortably past the ~87-positive power threshold (MATH_FOUNDATION §4)
  R2  refit ran as a true temporal holdout (non-degenerate split)
  R3  Spearman(planted beta*, fitted multipliers) >= 0.8 across signal groups
  R4  planted-zero groups fitted near zero (|multiplier| <= --zero-tol)
  R5  refit AUC-PR on the temporal TEST window materially beats a
      shuffled-label baseline (>= --ap-factor x)
  R6  survival's pooled household hazard within tolerance of h* on every
      bucket with enough at-risk mass (max abs bucket error reported)
  R7  forward-validation calibration table monotone-ish (rank corr of
      bin mean_pred vs watch_rate over populated bins)
  R8  recommended_not_watched negatives exist
  R9  planted watch events round-trip labeling (spot-check of positive rows)
  R10 the relaxed-completion labeling path fired (completed title, pct<90)

then, WHEN lightgbm IS IMPORTABLE, a CHALLENGER stage (after the refit) trains
the shadow GBT through ml_train_challenger's REAL path against the sim cache
(same temporal split date as the refit) and asserts:

  C1  challenger_trains: training rc=0, model + isotonic-calibration artifacts
      written under <sim>/ml/models/
  C2  challenger_ap: AP of the calibrated p-hat on the forward test window
      >= 1.5x the random base rate AND >= 0.75x the refit linear AP. This is
      a COMPETENCE floor, not a superiority test: the planted first-watch
      truth is linear-logistic in the sig_* groups, so on the refit's own
      features the GBT could at best MATCH it; any surplus AP it shows here
      comes from context features the linear refit does not use (chiefly
      watched_before ~ rewatch-pool membership under the planted hazard) —
      the same extra-information/interaction mechanism that, on real
      household data, must prove out as sustained forward delta-AP
      (MATH_FOUNDATION §8) before the challenger means anything
  C3  challenger_calibrated: ECE(calibrated p-hat) < ECE(min-max-scaled score)
      on the same window, calibration table monotone-ish
  C4  shadow_attach: attach_challenger_p stamps challenger_p on sample
      snapshot rows and logs its divergence line (Spearman rho +
      top-disagreements list)

Without lightgbm the stage prints one [SKIP] line, the four challenger
assertions are marked SKIP (not FAIL), and the original assertions plus the
exit code are unaffected.

and separately verifies the HONEST-NEGATIVE path: a 1-week sim (snapshots up to
"today", the day-one condition) must leave the refit REFUSING to bless the fit —
either zero positives (hard refusal) or the printed n_pos<100 high-variance
caveat — with n_pos below the 87-event power threshold.

GENERATIVE MODEL (all draws from one seeded numpy Generator — reproducible):
  * ~--titles synthetic movies; each signal group g fires with prob fire_g and
    contributes x_g ~ U(0, cap_g) points (penalty group: U(cap_g, 0)).
    watchability_score = clamp(sum_g x_g, 0, 100) — the production linear form
    with implicit unit weights; the breakdown dict {group: x_g} is what the
    snapshot writer flattens into sig_* columns.
  * FIRST watch of a title: daily Bernoulli with
        P(watch title i on any day) = sigmoid(b0 + sum_g beta*_g x_ig),
    b0 calibrated by bisection so the expected labeled positive-row rate hits
    --target-rate (~2-4%).
  * REWATCHES: once first-watched, the title's inter-watch gaps are drawn from
    the planted discrete-time hazard h* (bucket day uniform within the drawn
    bucket) until the window ends — the last gap is right-censored at "now",
    exactly the censoring the survival estimator expects.
  * Watches run right up to real "now"; snapshots stop --horizon-days + margin
    earlier so every label is mature in the main run.

HARD SAFETY RULE: this tool refuses to run against the real global cache
(scripts/support/cache) — the sim cache base must be a disjoint directory
(default: a fresh temp dir, deleted on success unless --keep-cache).

Usage
-----
    python scripts/support/tools/ml_simulate.py                      # full check
    python scripts/support/tools/ml_simulate.py --seed 7             # second seed
    python scripts/support/tools/ml_simulate.py --weeks 8 --titles 500
    python scripts/support/tools/ml_simulate.py --cache-base /tmp/simcache --keep-cache
    python scripts/support/tools/ml_simulate.py --power-gate-only    # just the gate

Exit code 0 only when EVERY assertion passes (challenger assertions SKIP —
not FAIL — when lightgbm is absent).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder            # noqa: E402
from scripts.managers.machine_learning.eval.np_metrics import (                     # noqa: E402
    average_precision,
    spearman_rho,
)
from scripts.managers.machine_learning.labels.labeling import build_labels          # noqa: E402
from scripts.managers.machine_learning.labels.snapshots import (                    # noqa: E402
    append_snapshot,
    build_movie_snapshot_rows,
    load_snapshots,
)

BUCKET_DAYS = 7
MAX_DAYS = 364
N_BUCKETS = MAX_DAYS // BUCKET_DAYS          # 52
POWER_THRESHOLD = 87                         # MATH_FOUNDATION §4 power sketch
INSTANCE = "standard"
CHALLENGER_AP_BASE_FACTOR = 1.5              # C2: AP_chal >= this x random base rate
CHALLENGER_AP_REFIT_FACTOR = 0.75            # C2: AP_chal >= this x refit linear AP

# (group, beta* log-odds per point, cap in points (sign = direction), fire rate)
DEFAULT_GROUPS = [
    ("A1_engagement",   0.40,  12.0, 0.85),
    ("B2_affinity",     0.30,  10.0, 0.80),
    ("C3_recency",      0.18,   8.0, 0.75),
    ("D4_franchise",    0.10,   8.0, 0.70),
    ("E5_quality",      0.00,  12.0, 0.90),   # planted zero
    ("F6_size_penalty", 0.00, -12.0, 0.60),   # planted zero (penalty-shaped)
]
DEFAULT_HAZARD_HEAD = [0.35, 0.30, 0.22, 0.16, 0.12, 0.10]
DEFAULT_HAZARD_TAIL = 0.10


# ── safety ────────────────────────────────────────────────────────────────────

def guard_cache_base(base: Path) -> None:
    """Refuse to run when the sim cache base touches the real global cache."""
    real = CacheKeyBuilder().base_dir.resolve()
    sim = base.resolve()
    if sim == real or real in sim.parents or sim in real.parents:
        raise SystemExit(
            f"REFUSING to run: sim cache base {sim} overlaps the real cache {real}. "
            "Pass a disjoint --cache-base (or omit it for a temp dir).")


# ── ground truth planting ─────────────────────────────────────────────────────

class World:
    """The planted ground truth + synthetic library for one simulation."""

    def __init__(self, rng: np.random.Generator, n_titles: int, groups: list):
        self.groups = groups                       # [(name, beta, cap, fire)]
        self.names = [g[0] for g in groups]
        self.beta = np.array([g[1] for g in groups], dtype=float)
        n_g = len(groups)
        x = np.zeros((n_titles, n_g))
        for j, (_, _, cap, fire) in enumerate(groups):
            fires = rng.random(n_titles) < fire
            mag = rng.uniform(0.0, abs(cap), size=n_titles)
            x[:, j] = np.where(fires, np.sign(cap) * mag, 0.0)
        self.x = x
        self.eta = x @ self.beta                   # planted linear predictor
        self.score = np.clip(np.round(x.sum(axis=1)), 0, 100)
        self.tmdb = np.arange(100001, 100001 + n_titles)
        self.rating_key = np.arange(500001, 500001 + n_titles)
        self.titles = [f"Sim Movie {i:04d}" for i in range(n_titles)]

    @property
    def planted_multipliers(self) -> np.ndarray:
        denom = float(np.max(np.abs(self.beta))) if self.beta.size else 0.0
        return self.beta / denom if denom > 0 else np.zeros_like(self.beta)


def hazard_full(head: list, tail: float) -> np.ndarray:
    hz = np.full(N_BUCKETS, float(tail))
    hz[:len(head)] = np.asarray(head, dtype=float)[:N_BUCKETS]
    return np.clip(hz, 0.0, 0.999)


def gap_probabilities(hz: np.ndarray) -> "tuple[np.ndarray, float]":
    """P(gap ends in bucket b) under discrete hazard hz + overflow mass."""
    surv = 1.0
    pmf = np.zeros(len(hz))
    for b, h in enumerate(hz):
        pmf[b] = surv * h
        surv *= (1.0 - h)
    return pmf, float(surv)


def window_watch_prob(hz: np.ndarray, horizon_days: float) -> float:
    """P(next event within horizon | fresh gap) under hz — calibration helper."""
    surv = 1.0
    pos = 0.0
    b = 0
    while pos < horizon_days and b < len(hz):
        seg = min(horizon_days - pos, BUCKET_DAYS)
        surv *= max(0.0, 1.0 - float(hz[b]) * seg / BUCKET_DAYS)
        pos += seg
        b += 1
    return 1.0 - surv


def calibrate_intercept(world: World, snap_days: int, watch_days: float,
                        horizon_days: int, hz: np.ndarray,
                        target_rate: float) -> float:
    """Bisect b0 so the EXPECTED labeled positive-row rate ~= target_rate.

    Approximation: a row (title i, snapshot day d) is positive when the first
    watch lands in (d, d+h] or the title entered the rewatch pool before d and
    a renewal event lands in the window (fresh-gap window probability q)."""
    q = window_watch_prob(hz, float(horizon_days))
    days = np.arange(snap_days, dtype=float)[None, :]          # (1, S)
    target_pos = target_rate * snap_days * len(world.eta)

    def expected_positives(b0: float) -> float:
        p = 1.0 / (1.0 + np.exp(-(b0 + world.eta)))            # daily first-watch
        logs = np.log1p(-np.clip(p, 0.0, 0.999999))[:, None]   # log(1-p)
        not_by_d = np.exp(logs * days)                         # (N, S)
        not_by_dh = np.exp(logs * np.minimum(days + horizon_days, watch_days))
        p_entry_in_win = not_by_d - not_by_dh
        p_in_pool = 1.0 - not_by_d
        row_p = np.clip(p_entry_in_win + p_in_pool * q, 0.0, 1.0)
        return float(row_p.sum())

    lo, hi = -20.0, 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if expected_positives(mid) < target_pos:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ── behaviour simulation ──────────────────────────────────────────────────────

def simulate_watches(rng: np.random.Generator, world: World, b0: float,
                     watch_days: float, hz: np.ndarray) -> "list[tuple[int, float]]":
    """[(title_idx, event_time_days)] — first watch via daily Bernoulli on
    sigmoid(b0 + beta*.x); rewatch gaps drawn from the planted hazard h*."""
    n = len(world.eta)
    p_daily = 1.0 / (1.0 + np.exp(-(b0 + world.eta)))
    n_days = int(math.ceil(watch_days))
    hits = rng.random((n, n_days)) < p_daily[:, None]
    entered = hits.any(axis=1)
    entry_day = hits.argmax(axis=1)

    pmf, overflow = gap_probabilities(hz)
    probs = np.append(pmf, overflow)
    probs = probs / probs.sum()
    n_choices = len(probs)

    events: list = []
    for i in np.nonzero(entered)[0]:
        t = float(entry_day[i]) + float(rng.random())
        if t >= watch_days:
            continue
        events.append((int(i), t))
        while True:
            b = int(rng.choice(n_choices, p=probs))
            if b >= N_BUCKETS:                      # no rewatch within max_days
                break
            t = t + b * BUCKET_DAYS + float(rng.random()) * BUCKET_DAYS
            if t >= watch_days:                     # right-censored at "now"
                break
            events.append((int(i), t))
    events.sort(key=lambda e: e[1])
    return events


# ── cache emission (into the SIM dir only) ────────────────────────────────────

def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def emit_cache(rng: np.random.Generator, base: Path, world: World,
               events: list, t0: datetime, watch_days: float,
               up_next_frac: float, completed_count: int) -> dict:
    """Write history/inventory/completions/up-next in EXACTLY the shapes
    labeling.py reads. Returns metadata used by the checks."""
    n = len(world.tmdb)
    watched_titles = sorted({i for i, _ in events})

    # completions: completed titles chosen among WATCHED ones so the relaxed
    # (<90 percent) labeling path is guaranteed to be exercised.
    completed = list(watched_titles[:completed_count])
    completed_set = set(completed)

    records = []
    relaxed_events: list = []
    per_title_events: dict = {i: [] for i in range(n)}
    for i, t in events:
        if i in completed_set and rng.random() < 0.5:
            pct = int(rng.uniform(60, 85))          # relaxed-completion watch
            relaxed_events.append((i, t))
        else:
            pct = int(rng.uniform(91, 100))         # ordinary completed watch
        epoch = int(t0.timestamp() + t * 86400.0)
        records.append({
            "date": epoch,
            "media_type": "movie",
            "rating_key": str(int(world.rating_key[i])),
            "title": world.titles[i],
            "percent_complete": pct,
            # A "relaxed-completion watch" lands at 60-85%, BELOW the global watched
            # bar's percentage fallback — the fixture's intent is that the household
            # DID finish it (a group with a relaxed completion_threshold), and only
            # Tautulli's own verdict can say so. Emitting watched_status explicitly
            # keeps the relaxed band counting as watched instead of silently becoming
            # a sample, and exercises the verdict-beats-percentage precedence.
            "watched_status": 1,
            "user": "sim_household",
        })
        per_title_events[i].append(t)
    # sub-threshold noise events — must be ignored by labels AND survival
    for i in rng.choice(n, size=max(3, n // 30), replace=False):
        t = float(rng.uniform(0, watch_days))
        records.append({
            "date": int(t0.timestamp() + t * 86400.0),
            "media_type": "movie",
            "rating_key": str(int(world.rating_key[int(i)])),
            "title": world.titles[int(i)],
            "percent_complete": int(rng.uniform(5, 45)),
            "watched_status": 0,      # sub-threshold noise — Tautulli agrees it is not a watch
            "user": "sim_household",
        })
    records.sort(key=lambda r: r["date"])
    _write_json(base / "tautulli" / "history" / "all.json", records)

    _write_json(base / "plex" / "movies" / "owned_inventory.json", {
        str(int(world.tmdb[i])): {
            "rating_key": str(int(world.rating_key[i])),
            "title": world.titles[i],
            "year": 2000 + (i % 25),
        } for i in range(n)})

    _write_json(base / "tautulli" / "group" / "household" / "tmdb_completions.json",
                {str(int(world.tmdb[i])): {"pct": 0.95, "threshold": 0.9}
                 for i in completed})

    up_next_idx = rng.choice(n, size=max(5, int(n * up_next_frac)), replace=False)
    up_next_tmdbs = sorted(int(world.tmdb[int(i)]) for i in up_next_idx)
    for name in ("movie", "combined"):
        _write_json(base / "plex" / "playlists" / "protected_movie_tmdbs" / f"{name}.json",
                    {"tmdbs": up_next_tmdbs})

    return {
        "up_next_tmdbs": set(up_next_tmdbs),
        "completed_idx": completed_set,
        "relaxed_events": relaxed_events,
        "per_title_events": per_title_events,
        "n_watched_titles": len(watched_titles),
    }


def emit_snapshots(base: Path, world: World, meta: dict, t0: datetime,
                   snap_days: int) -> int:
    """One snapshot of every title per simulated day, through the REAL
    build_movie_snapshot_rows + append_snapshot writer."""
    n = len(world.tmdb)
    breakdowns = [json.dumps({name: round(float(world.x[i, j]), 3)
                              for j, name in enumerate(world.names)})
                  for i in range(n)]
    sizes = (np.abs(world.score) * 40_000_000 + 800_000_000).astype(np.int64)
    sorted_events = {i: sorted(ts) for i, ts in meta["per_title_events"].items()}
    written = 0
    for d in range(snap_days):
        ts = (t0 + timedelta(days=d, hours=12)).replace(microsecond=0)
        day_offset = d + 0.5
        counts = [int(np.searchsorted(sorted_events[i], day_offset)) for i in range(n)]
        df = pd.DataFrame({
            "tmdb_id": world.tmdb,
            "title": world.titles,
            "watchability_score": world.score,
            "watchability_breakdown": breakdowns,
            "size_bytes": sizes,
            "resolution": 1080,
            "is_watched": [c > 0 for c in counts],
            "watch_count": counts,
            "planned_action": None,
        })
        rows = build_movie_snapshot_rows(df, INSTANCE, snapshot_ts=ts.isoformat(),
                                         up_next_tmdbs=meta["up_next_tmdbs"])
        written += append_snapshot(base, "radarr", INSTANCE, rows)
    return written


# ── running the real chain ────────────────────────────────────────────────────

def _load_tool(name: str):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_sim_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _latest_report(base: Path, prefix: str) -> "dict | None":
    reports = sorted((base / "ml" / "reports").glob(f"{prefix}_*.json"))
    if not reports:
        return None
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def run_chain(base: Path, split_date: "str | None", horizon_days: int,
              l2: float) -> dict:
    print(f"\n{'#' * 78}\n# running the REAL chain against {base}\n{'#' * 78}")
    fv = _load_tool("ml_forward_validation")
    wr = _load_tool("ml_weight_refit")
    sv = _load_tool("ml_survival_report")
    fv_args = ["--cache-base", str(base), "--service", "radarr",
               "--horizon-days", str(horizon_days)]
    wr_args = ["--cache-base", str(base), "--service", "radarr",
               "--horizon-days", str(horizon_days), "--l2", str(l2)]
    if split_date:
        fv_args += ["--split-date", split_date]
        wr_args += ["--split-date", split_date]
    rc_fv = fv.main(fv_args)
    rc_wr = wr.main(wr_args)
    rc_sv = sv.main(["--cache-base", str(base)])
    return {
        "rc": {"forward": rc_fv, "refit": rc_wr, "survival": rc_sv},
        "forward": _latest_report(base, "forward_validation"),
        "refit": _latest_report(base, "suggested_weights"),
        "survival": _latest_report(base, "survival"),
    }


# ── checks ────────────────────────────────────────────────────────────────────

class Checks:
    def __init__(self):
        self.rows: list = []                     # (name, "PASS"|"FAIL"|"SKIP", detail)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.rows.append((name, "PASS" if passed else "FAIL", detail))

    def skip(self, name: str, detail: str) -> None:
        """SKIPPED assertion (e.g. the challenger stage without lightgbm) —
        visible in the assertion block but never fails the harness."""
        self.rows.append((name, "SKIP", detail))

    @property
    def all_passed(self) -> bool:
        return all(status != "FAIL" for _, status, _ in self.rows)

    def print(self) -> None:
        print(f"\n{'=' * 78}\nASSERTIONS\n{'=' * 78}")
        for name, status, detail in self.rows:
            print(f"  [{status}] {name:<26} {detail}")


def spot_check_labels(labeled: pd.DataFrame, world: World, meta: dict,
                      t0: datetime, snap_days: int, horizon_days: int,
                      rng: np.random.Generator) -> "tuple[int, int, bool]":
    """Sample planted qualifying events; each must label some prior snapshot row
    positive. Returns (checked, verified, relaxed_path_verified)."""
    pos = {(str(r["entity_id"]), str(r["snapshot_date"])): bool(r["watched_within_h"])
           for r in labeled.to_dict("records")}

    def covering_day(t: float) -> "int | None":
        d = min(snap_days - 1, int(math.ceil(t - 0.5)) - 1)   # latest d with d+0.5 < t
        while d >= 0:
            if d + 0.5 < t <= d + 0.5 + horizon_days:
                return d
            d -= 1
        return None

    qualifying = [(i, t) for i, ts in meta["per_title_events"].items() for t in ts]
    rng.shuffle(qualifying)
    checked = verified = 0
    for i, t in qualifying:
        d = covering_day(t)
        if d is None:
            continue
        checked += 1
        key = (str(int(world.tmdb[i])),
               (t0 + timedelta(days=d)).date().isoformat())
        if pos.get(key):
            verified += 1
        if checked >= 25:
            break

    relaxed_ok = False
    for i, t in meta["relaxed_events"]:
        d = covering_day(t)
        if d is None:
            continue
        key = (str(int(world.tmdb[i])),
               (t0 + timedelta(days=d)).date().isoformat())
        if pos.get(key):
            relaxed_ok = True
            break
    return checked, verified, relaxed_ok


def main_simulation(args, rng: np.random.Generator, base: Path,
                    groups: list, hz: np.ndarray, checks: Checks) -> None:
    now = datetime.now(tz=timezone.utc)
    snap_days = args.weeks * 7
    # snapshots end horizon+margin before now => every label is mature;
    # watches keep running to real now => survival censoring is truthful.
    t0 = (now - timedelta(days=snap_days + args.horizon_days + 9)) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    watch_days = (now - t0).total_seconds() / 86400.0
    split_day = int(snap_days * args.split_frac)
    split_date = (t0 + timedelta(days=split_day)).date().isoformat()

    world = World(rng, args.titles, groups)
    b0 = calibrate_intercept(world, snap_days, watch_days, args.horizon_days,
                             hz, args.target_rate)
    events = simulate_watches(rng, world, b0, watch_days, hz)
    meta = emit_cache(rng, base, world, events, t0, watch_days,
                      args.up_next_frac, args.completed_count)
    appended = emit_snapshots(base, world, meta, t0, snap_days)
    n_entities = meta["n_watched_titles"]
    n_gaps = len(events) - n_entities
    print(f"\nplanted: {args.titles} titles x {snap_days} snapshot days "
          f"({appended} snapshot rows), intercept b0={b0:.3f}")
    print(f"simulated: {len(events)} qualifying watch events over "
          f"{watch_days:.1f}d, {n_entities} distinct titles watched, "
          f"{n_gaps} complete inter-watch gaps, split at {split_date}")

    out = run_chain(base, split_date, args.horizon_days, args.l2)
    refit = ((out["refit"] or {}).get("services") or {}).get("radarr")
    fwd = ((out["forward"] or {}).get("services") or {}).get("radarr")
    surv = out["survival"]
    if not refit or not fwd or not surv:
        checks.add("chain_ran", False,
                   f"missing report blocks (rc={out['rc']}) — cannot evaluate")
        return
    checks.add("chain_ran", True,
               f"rc={out['rc']}; reports under {base / 'ml' / 'reports'}")

    # R1 power: n_pos comfortably past the ~87 threshold
    n_pos = int(refit.get("n_pos_total") or 0)
    checks.add("n_pos_power", n_pos >= args.min_pos,
               f"n_pos_total={n_pos} (threshold ~{POWER_THRESHOLD}, "
               f"required >= {args.min_pos})")

    # R2 true temporal holdout
    checks.add("temporal_holdout", refit.get("evaluation_mode") == "forward_holdout",
               f"refit evaluation_mode={refit.get('evaluation_mode')!r}, "
               f"train={refit.get('n_train')}, test={refit.get('n_test')}")

    # R3/R4 planted beta* vs fitted multipliers
    fitted_by_sig = {w["signal"]: w for w in refit.get("weights", [])}
    planted_m = world.planted_multipliers
    fitted_m, pairs = [], []
    for j, name in enumerate(world.names):
        w = fitted_by_sig.get(f"sig_{name}")
        fm = w.get("fitted_multiplier") if w else None
        fitted_m.append(fm)
        if fm is not None:
            pairs.append((float(planted_m[j]), float(fm)))
    rho = spearman_rho([p for p, _ in pairs], [f for _, f in pairs]) \
        if len(pairs) >= 2 else float("nan")
    checks.add("beta_rank_recovery",
               np.isfinite(rho) and rho >= args.spearman_min and len(pairs) == len(world.names),
               f"Spearman(planted, fitted)={rho:.3f} over {len(pairs)}/"
               f"{len(world.names)} groups (required >= {args.spearman_min})")
    zero_details, zeros_ok = [], True
    for j, name in enumerate(world.names):
        if world.beta[j] == 0.0:
            fm = fitted_m[j]
            ok = fm is not None and abs(fm) <= args.zero_tol
            zeros_ok &= ok
            zero_details.append(f"{name}={fm if fm is not None else 'n/a'}")
    checks.add("planted_zeros_near_zero", zeros_ok,
               f"{', '.join(zero_details)} (|m| <= {args.zero_tol})")

    print(f"\n{'=' * 78}\nSUMMARY — planted vs fitted per signal group "
          f"(seed {args.seed})\n{'=' * 78}")
    print(f"  {'group':<18} {'beta*/pt':>9} {'planted_m':>10} {'fitted_m':>9} "
          f"{'fit_pp':>9} {'fires':>6}")
    for j, name in enumerate(world.names):
        w = fitted_by_sig.get(f"sig_{name}") or {}
        fm = w.get("fitted_multiplier")
        pp = w.get("log_odds_per_point")
        print(f"  {name:<18} {world.beta[j]:>9.3f} {planted_m[j]:>10.3f} "
              f"{(f'{fm:.3f}' if fm is not None else 'n/a'):>9} "
              f"{(f'{pp:+.4f}' if pp is not None else 'n/a'):>9} "
              f"{w.get('n_nonzero', 0):>6}")

    # R5 refit AP vs shuffled-label baseline on the SAME temporal test window
    snaps = load_snapshots(base, services=("radarr",))
    labeled = build_labels(snaps, base, horizon_days=args.horizon_days)
    labeled = labeled[labeled["label_mature"]].reset_index(drop=True)
    test = labeled[labeled["snapshot_date"] >= split_date]
    y_test = test["watched_within_h"].astype(float).to_numpy()
    s_test = pd.to_numeric(test["watchability_score"], errors="coerce").to_numpy(float)
    rng_shuffle = np.random.default_rng(args.seed + 1000)
    shuffled = [average_precision(rng_shuffle.permutation(y_test), s_test)
                for _ in range(20)]
    ap_shuffled = float(np.nanmean(shuffled))
    ap_refit = refit.get("auc_pr_refit_score")
    ap_cur = refit.get("auc_pr_current_score")
    ok = ap_refit is not None and np.isfinite(ap_shuffled) \
        and ap_refit >= args.ap_factor * ap_shuffled
    checks.add("refit_ap_beats_shuffled", ok,
               f"refit AP={ap_refit} vs shuffled-label AP={ap_shuffled:.4f} "
               f"(current score AP={ap_cur}; required >= {args.ap_factor}x)")

    # R6 hazard recovery
    rec = np.asarray(surv.get("household_hazard") or [], dtype=float)
    at_risk = np.asarray(surv.get("household_at_risk") or [], dtype=float)
    n_cmp = min(len(rec), len(hz))
    qual = np.nonzero(at_risk[:n_cmp] >= args.min_at_risk)[0]
    errs = np.abs(rec[:n_cmp] - hz[:n_cmp])
    max_err = float(errs[qual].max()) if len(qual) else float("nan")
    ok = len(qual) >= 3 and np.isfinite(max_err) and max_err <= args.hazard_tol
    checks.add("hazard_recovery", ok,
               f"max |h_hat - h*| = {max_err:.4f} over {len(qual)} buckets with "
               f"at_risk >= {args.min_at_risk} (tol {args.hazard_tol})")
    print(f"\n{'=' * 78}\nSUMMARY — planted vs recovered household hazard "
          f"(seed {args.seed})\n{'=' * 78}")
    print(f"  {'days':>9} {'planted':>8} {'recovered':>10} {'at_risk':>8} "
          f"{'abs_err':>8}  (* = counted toward max-abs-error)")
    show = sorted(set(range(min(8, n_cmp))) | set(int(q) for q in qual))
    for b in show:
        mark = "*" if b in qual else " "
        print(f"  {f'{b * 7}-{(b + 1) * 7}':>9} {hz[b]:>8.3f} {rec[b]:>10.3f} "
              f"{int(at_risk[b]):>8} {errs[b]:>8.3f} {mark}")

    # R7 calibration monotone-ish
    bins = [b for b in fwd["metrics"]["calibration"]
            if (b.get("n") or 0) >= args.calib_min_bin_n]
    rho_cal = spearman_rho([b["mean_pred"] for b in bins],
                           [b["watch_rate"] for b in bins]) \
        if len(bins) >= 3 else float("nan")
    checks.add("calibration_monotone",
               np.isfinite(rho_cal) and rho_cal >= args.calib_spearman_min
               and len(bins) >= 3,
               f"rank corr(bin mean_pred, watch_rate)={rho_cal:.3f} over "
               f"{len(bins)} bins with n >= {args.calib_min_bin_n} "
               f"(required >= {args.calib_spearman_min})")

    # R8 implicit negatives exist
    rnw = int(fwd["metrics"].get("recommended_not_watched") or 0)
    checks.add("recommended_not_watched", rnw > 0,
               f"{rnw} recommended_not_watched rows in the test window")

    # R9/R10 label plumbing spot checks
    checked, verified, relaxed_ok = spot_check_labels(
        labeled, world, meta, t0, snap_days, args.horizon_days, rng)
    checks.add("label_spot_check", checked >= 10 and verified == checked,
               f"{verified}/{checked} sampled planted events labeled positive")
    checks.add("relaxed_completion_path", relaxed_ok,
               f"{len(meta['relaxed_events'])} relaxed (<90 pct, completed-title) "
               "events emitted; at least one produced a positive label")

    print(f"\n  forward-validation test window: n={fwd['metrics']['n']}, "
          f"n_pos={fwd['metrics']['n_pos']}, base_rate={fwd['metrics']['base_rate']}, "
          f"AUC-PR={fwd['metrics']['auc_pr']}")

    # CHALLENGER stage (after the refit) — gated on lightgbm importability.
    challenger_stage(args, base, test, refit, snaps, split_date, checks)


class _CaptureLogger:
    """Print-through logger that records lines for the shadow_attach check."""

    def __init__(self):
        self.lines: list = []                    # (level, message)

    def _rec(self, level: str, msg) -> None:
        self.lines.append((level, str(msg)))
        print(f"  [challenger.{level}] {msg}")

    def log_info(self, m): self._rec("info", m)
    def log_warning(self, m): self._rec("warning", m)
    def log_debug(self, m): self._rec("debug", m)


def challenger_stage(args, base: Path, test: pd.DataFrame, refit: dict,
                     snaps: pd.DataFrame, split_date: str,
                     checks: Checks) -> None:
    """CHALLENGER stage (C1-C4) — after the refit, gated on lightgbm.

    Trains the shadow GBT through ml_train_challenger's REAL CLI path against
    the sim cache on the SAME temporal split date as the refit, evaluates the
    isotonic-calibrated p-hat on the forward test window, and exercises the
    runtime shadow hook (attach_challenger_p) on sample snapshot rows.

    HONESTY NOTES:
      * challenger_ap asserts COMPETENCE (floors vs random and vs the refit),
        never superiority. The planted first-watch truth is linear-logistic
        in the sig_* groups, so on the refit's own features the GBT could at
        best MATCH the linear refit; where it lands above, the surplus comes
        from context features the refit does not use (chiefly watched_before,
        which under the planted hazard marks the rewatch pool). That is the
        same mechanism — information/interactions outside the linear score —
        that on real household data must show up as sustained forward
        delta-AP (MATH_FOUNDATION §8) before the challenger means anything;
        this sim never judges that.
      * the forward test window doubles as the challenger's validation window
        (same split date as the refit), so early stopping and the isotonic fit
        have SEEN it — exactly like the real CLI's valid metrics. The
        assertions are lenient floors, not holdout claims.
    """
    from scripts.managers.machine_learning.challenger.gbt_shadow import (
        HAS_LIGHTGBM,
        attach_challenger_p,
        build_feature_matrix,
        load_challenger,
        model_paths,
        predict_probability,
    )

    names = ("challenger_trains", "challenger_ap",
             "challenger_calibrated", "shadow_attach")
    if not HAS_LIGHTGBM:
        print("\n[SKIP] challenger stage — lightgbm not installed")
        for name in names:
            checks.skip(name, "lightgbm not installed — challenger stage "
                              "skipped (pip install lightgbm to exercise it)")
        return

    from scripts.managers.machine_learning.eval.np_metrics import (
        calibration_table,
        expected_calibration_error,
        minmax_scale,
    )

    t_ch = time.monotonic()
    print(f"\n{'#' * 78}\n# CHALLENGER stage — ml_train_challenger against "
          f"{base}\n{'#' * 78}")
    tc = _load_tool("ml_train_challenger")
    rc_tc = tc.main(["--cache-base", str(base), "--service", "radarr",
                     "--horizon-days", str(args.horizon_days),
                     "--split-date", split_date,
                     "--rounds", str(args.challenger_rounds),
                     "--early-stopping", str(args.challenger_early_stopping)])
    model_path, calib_path = model_paths(base, "radarr")
    trained = rc_tc == 0 and model_path.is_file() and calib_path.is_file()
    checks.add("challenger_trains", trained,
               f"training rc={rc_tc}; model "
               f"{'written' if model_path.is_file() else 'MISSING'}, calib "
               f"{'written' if calib_path.is_file() else 'MISSING'} under "
               f"{model_path.parent}")
    if not trained:
        for name in names[1:]:
            checks.add(name, False, "not evaluated — challenger training failed")
        return

    # C2 — AP of the calibrated p-hat on the forward test window.
    bundle = load_challenger(base, "radarr")
    tst = test[test["service"] == "radarr"] if "service" in test.columns else test
    y_test = tst["watched_within_h"].astype(float).to_numpy()
    base_rate = float(y_test.mean())             # random-ranking AP floor
    X_test = build_feature_matrix(tst, bundle["features"])
    raw = np.asarray(bundle["booster"].predict(X_test), dtype=float)
    p_cal = predict_probability(bundle, tst)
    ap_chal = average_precision(y_test, p_cal)
    ap_raw = average_precision(y_test, raw)
    ap_refit = refit.get("auc_pr_refit_score")
    ap_cur = refit.get("auc_pr_current_score")
    # COMPETENCE floor, NOT a superiority test. The planted FIRST-WATCH truth
    # is linear-logistic in the sig_* groups, so on the refit's own features
    # the GBT could at best MATCH the linear refit. It usually lands ABOVE it
    # here anyway — legitimately: its feature set adds context the refit does
    # not use (chiefly watched_before, which under the planted hazard marks
    # the rewatch pool — an interaction outside the sig_*-linear form). That
    # is exactly the mechanism (information/interactions the linear score
    # lacks) that on REAL household data must prove out as sustained forward
    # delta-AP (MATH_FOUNDATION §8); this sim only asserts the floors below.
    ok_ap = (np.isfinite(ap_chal) and ap_refit is not None
             and ap_chal >= CHALLENGER_AP_BASE_FACTOR * base_rate
             and ap_chal >= CHALLENGER_AP_REFIT_FACTOR * float(ap_refit))
    checks.add("challenger_ap", ok_ap,
               f"AP_chal={ap_chal:.4f} (raw margin {ap_raw:.4f}) vs "
               f"refit={ap_refit} current={ap_cur} random~{base_rate:.4f} "
               f"(floors: >= {CHALLENGER_AP_BASE_FACTOR}x random AND >= "
               f"{CHALLENGER_AP_REFIT_FACTOR}x refit — competence, not "
               "superiority: the planted first-watch truth is linear-logistic "
               "in sig_*, so on the refit's features the GBT could at best "
               "match it; surplus here rides on context features the refit "
               "lacks (watched_before ~ rewatch pool), and real-data wins are "
               "judged only by sustained forward delta-AP)")

    # C3 — calibration: ECE of the calibrated p-hat vs the min-max-scaled
    # hand score read as a pseudo-probability (forward_validation's diagnostic).
    s_test = pd.to_numeric(tst["watchability_score"], errors="coerce").to_numpy(float)
    ece_score = expected_calibration_error(y_test, minmax_scale(s_test))
    ece_raw = expected_calibration_error(y_test, raw)
    ece_cal = expected_calibration_error(y_test, p_cal)
    cal_bins = [b for b in calibration_table(y_test, p_cal)
                if (b.get("n") or 0) >= 10]
    rho_cal = spearman_rho([b["mean_pred"] for b in cal_bins],
                           [b["watch_rate"] for b in cal_bins]) \
        if len(cal_bins) >= 2 else float("nan")
    ok_cal = (np.isfinite(ece_cal) and np.isfinite(ece_score)
              and ece_cal < ece_score
              and np.isfinite(rho_cal) and rho_cal >= args.calib_spearman_min)
    checks.add("challenger_calibrated", ok_cal,
               f"ECE calibrated p={ece_cal:.4f} < min-max-scaled score="
               f"{ece_score:.4f} (raw GBT p={ece_raw:.4f}); calibration-table "
               f"rank corr={rho_cal:.3f} over {len(cal_bins)} bins with "
               f"n >= 10 (required >= {args.calib_spearman_min})")

    # C4 — the runtime shadow hook on the latest snapshot day's rows.
    log = _CaptureLogger()
    srv = snaps[snaps["service"] == "radarr"] if "service" in snaps.columns else snaps
    last_day = str(srv["snapshot_date"].astype(str).max())
    rows = srv[srv["snapshot_date"].astype(str) == last_day].to_dict("records")
    rows = attach_challenger_p({"scoring": {"ml_challenger": {"enabled": True}}},
                               log, base, "radarr", rows)
    ps = np.array([float(r["challenger_p"]) if r.get("challenger_p") is not None
                   else np.nan for r in rows], dtype=float)
    n_ok = int(np.sum(np.isfinite(ps) & (ps >= 0.0) & (ps <= 1.0)))
    rho_sp = spearman_rho([float(r.get("watchability_score") or 0.0)
                           for r in rows], ps)
    div_lines = [m for lvl, m in log.lines
                 if lvl == "info" and "Spearman rho" in m]
    top_lines = [m for _, m in log.lines if "rank disagreement" in m]
    ok_attach = (len(rows) > 0 and n_ok == len(rows) and len(div_lines) >= 1
                 and np.isfinite(rho_sp))
    checks.add("shadow_attach", ok_attach,
               f"challenger_p stamped on {n_ok}/{len(rows)} snapshot rows "
               f"({last_day}); divergence line logged ({len(div_lines)}), "
               f"top-disagreements listed ({len(top_lines)}, non-crashing); "
               f"Spearman rho(score, p)={rho_sp:.3f}")

    print(f"\n  challenger stage summary (seed {args.seed}): "
          f"AP chal={ap_chal:.4f} refit={ap_refit} current={ap_cur} "
          f"random~{base_rate:.4f}; ECE minmax-score={ece_score:.4f} "
          f"raw={ece_raw:.4f} calibrated={ece_cal:.4f}; attach rho={rho_sp:.3f} "
          f"[{time.monotonic() - t_ch:.1f}s]")


def power_gate_simulation(args, rng: np.random.Generator, base: Path,
                          groups: list, hz: np.ndarray, checks: Checks) -> None:
    """1-week, day-one-condition sim: snapshots end at 'now', so labels are
    immature and positives are far below the power threshold — the refit must
    refuse to bless the fit (zero-positive refusal OR the n_pos<100 caveat)."""
    now = datetime.now(tz=timezone.utc)
    snap_days = 7
    t0 = (now - timedelta(days=snap_days + 1)) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    watch_days = (now - t0).total_seconds() / 86400.0

    world = World(rng, args.titles, groups)
    # SAME household intensity as the reference full-length run: the short
    # window (not a cranked intercept) is what starves the fit of positives.
    ref_snap_days = max(args.weeks, 8) * 7
    b0 = calibrate_intercept(world, ref_snap_days,
                             ref_snap_days + args.horizon_days + 9,
                             args.horizon_days, hz, args.target_rate)
    events = simulate_watches(rng, world, b0, watch_days, hz)
    meta = emit_cache(rng, base, world, events, t0, watch_days,
                      args.up_next_frac, args.completed_count)
    emit_snapshots(base, world, meta, t0, snap_days)
    print(f"\npower-gate sim: {snap_days} snapshot days ending today, "
          f"{len(events)} watch events, b0={b0:.3f} (reference-calibrated)")

    out = run_chain(base, None, args.horizon_days, args.l2)
    refit_report = out["refit"]
    block = ((refit_report or {}).get("services") or {}).get("radarr")
    if block is None:
        checks.add("power_gate_refusal", out["rc"]["refit"] != 0,
                   f"refit refused outright (rc={out['rc']['refit']}, "
                   "zero positives — nothing to fit)")
        return
    n_pos = int(block.get("n_pos_total") or 0)
    caveat = bool(block.get("caveat"))
    checks.add("power_gate_refusal", caveat and n_pos < POWER_THRESHOLD,
               f"n_pos_total={n_pos} < {POWER_THRESHOLD} and high-variance "
               f"caveat {'PRESENT' if caveat else 'MISSING'} — suggestions "
               "explicitly not trustable")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--weeks", type=int, default=8,
                    help="weeks of daily snapshots in the main sim (default 8)")
    ap.add_argument("--titles", type=int, default=500)
    ap.add_argument("--target-rate", type=float, default=0.04,
                    help="target labeled positive-row rate (default 0.04)")
    ap.add_argument("--horizon-days", type=int, default=7)
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--split-frac", type=float, default=0.75,
                    help="fraction of snapshot days used for training (temporal split)")
    ap.add_argument("--beta", default=None,
                    help='JSON overriding planted per-point betas by group, e.g. '
                         '\'{"A1_engagement": 0.5, "E5_quality": 0.0}\'')
    ap.add_argument("--hazard", default=None,
                    help="comma-separated head of the planted hazard curve "
                         f"(7d buckets; default {DEFAULT_HAZARD_HEAD})")
    ap.add_argument("--hazard-tail", type=float, default=DEFAULT_HAZARD_TAIL)
    ap.add_argument("--up-next-frac", type=float, default=0.10)
    ap.add_argument("--completed-count", type=int, default=15)
    ap.add_argument("--min-pos", type=int, default=150,
                    help="required n_pos (comfortably past the ~87 power threshold)")
    ap.add_argument("--spearman-min", type=float, default=0.8)
    ap.add_argument("--zero-tol", type=float, default=0.25)
    ap.add_argument("--ap-factor", type=float, default=3.0)
    ap.add_argument("--hazard-tol", type=float, default=0.12)
    ap.add_argument("--min-at-risk", type=int, default=40)
    ap.add_argument("--calib-spearman-min", type=float, default=0.5)
    ap.add_argument("--calib-min-bin-n", type=int, default=40)
    ap.add_argument("--challenger-rounds", type=int, default=150,
                    help="max boosting rounds for the sim challenger stage "
                         "(kept modest for runtime; early stopping applies)")
    ap.add_argument("--challenger-early-stopping", type=int, default=20)
    ap.add_argument("--cache-base", default=None,
                    help="throwaway dir for the sim caches (default: fresh temp dir). "
                         "MUST be disjoint from the real cache; sim_main/ and "
                         "sim_powergate/ under it are wiped per run.")
    ap.add_argument("--keep-cache", action="store_true")
    ap.add_argument("--skip-power-gate", action="store_true")
    ap.add_argument("--power-gate-only", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t_start = time.monotonic()

    groups = [list(g) for g in DEFAULT_GROUPS]
    if args.beta:
        overrides = json.loads(args.beta)
        by_name = {g[0]: g for g in groups}
        for name, b in overrides.items():
            if name not in by_name:
                raise SystemExit(f"--beta: unknown group {name!r} "
                                 f"(known: {sorted(by_name)})")
            by_name[name][1] = float(b)
    head = [float(v) for v in args.hazard.split(",")] if args.hazard \
        else list(DEFAULT_HAZARD_HEAD)
    hz = hazard_full(head, args.hazard_tail)

    made_temp = args.cache_base is None
    base = Path(args.cache_base) if args.cache_base \
        else Path(tempfile.mkdtemp(prefix="glidearr_ml_sim_"))
    guard_cache_base(base)
    base.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"ML SIMULATION HARNESS — seed={args.seed}, weeks={args.weeks}, "
          f"titles={args.titles}, horizon={args.horizon_days}d")
    print(f"sim cache base: {base}  (real cache is NEVER touched)")
    print("=" * 78)
    print("planted hazard head:", " ".join(f"{v:.2f}" for v in hz[:8]),
          f"... tail {args.hazard_tail:.2f}")

    checks = Checks()
    rng = np.random.default_rng(args.seed)
    try:
        if not args.power_gate_only:
            main_dir = base / "sim_main"
            if main_dir.exists():
                shutil.rmtree(main_dir)
            main_simulation(args, rng, main_dir, groups, hz, checks)
        if not args.skip_power_gate:
            pg_dir = base / "sim_powergate"
            if pg_dir.exists():
                shutil.rmtree(pg_dir)
            power_gate_simulation(args, np.random.default_rng(args.seed + 500),
                                  pg_dir, groups, hz, checks)
    except Exception as e:                          # noqa: BLE001 — report + fail
        import traceback
        traceback.print_exc()
        checks.add("no_unhandled_exception", False, f"{type(e).__name__}: {e}")

    checks.print()
    ok = checks.all_passed and bool(checks.rows)
    elapsed = time.monotonic() - t_start
    print(f"\n{'=' * 78}")
    print(f"SIMULATION HARNESS: {'ALL ASSERTIONS PASSED' if ok else 'FAILURES'} "
          f"(seed {args.seed}, {elapsed:.1f}s)")
    print("NOTE: this validates that the pipeline can recover known parameters "
          "from synthetic data. It says nothing about the real household — "
          "production weights must come from organic watch history.")
    print("=" * 78)

    if made_temp and ok and not args.keep_cache:
        shutil.rmtree(base, ignore_errors=True)
    elif made_temp:
        print(f"sim cache kept for inspection: {base}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

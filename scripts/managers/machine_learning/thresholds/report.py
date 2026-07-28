"""
thresholds/report.py — the once-per-run derivation, table and audit JSON.
================================================================================
The runtime side of the shadow rollout. Called exactly once, at the very end of
the run, from ``ledger/plan_summary`` — the only place that already has both
service parquets open and runs after every phase has stamped its plans.

Per run it:
  1. loads the snapshot store and joins Tautulli ground truth ONCE
     (``labels/build_labels``) at ``ml.thresholds.horizon_days`` (default 14),
     INCLUDING backfilled rows by default (``ml.thresholds.include_backfill``,
     see ``registry.DEFAULT_INCLUDE_BACKFILL`` for why this one differs);
  2. fits one isotonic calibrator per service on the MATURED rows (§5);
  3. inverts each target probability into a raw score-equivalent (§9) and
     SHRINKS it toward the hand-set constant — ``effective = w·derived +
     (1−w)·constant``, ``w = n_pos/(n_pos+k)`` (``derive.blend_threshold``, the
     survival model's own empirical-Bayes pool). No calibrator at all → no
     derived term, effective = constant, reason recorded;
  4. counts, per threshold, how many entities would flip sides at the EFFECTIVE
     cutoff (``shadow.compare``);
  5. logs one compact table — current · derived · w · effective · n_pos split by
     provenance · confidence · would-flip — and writes
     ``<cache>/ml/reports/thresholds_{date}.json`` so drift is auditable
     run-over-run;
  6. primes ``registry`` so that a process running in ``mode="derived"`` and
     a subsequent run both read the same committed EFFECTIVE numbers.

READ-ONLY and best-effort throughout: every entry point is wrapped, a failure
logs at debug and the run continues. ``mode="off"`` (or
``GLIDEARR_THRESHOLDS_OFF=1``) skips all of it, including the snapshot load.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scripts.managers.machine_learning.thresholds import registry, shadow
from scripts.managers.machine_learning.thresholds.derive import (
    MIN_POS_FOR_FIT,
    blend_threshold,
    confidence_label,
    data_gate,
    derive_threshold,
    fit_calibrator,
    mature_candidates,
    reports_dir,
    shrinkage_weight,
)

_SERVICES = ("radarr", "sonarr")


# ── build ─────────────────────────────────────────────────────────────────────

def _resolve_base_dir(base_dir=None, global_cache=None):
    if base_dir is not None:
        return Path(base_dir)
    root = getattr(global_cache, "cache_root", None)
    if root:
        return Path(root)
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    return CacheKeyBuilder().base_dir


def build_report(config, *, base_dir, scores_by_service=None, now=None,
                 snapshots=None, warnings=None) -> "dict | None":
    """The full shadow comparison as a JSON-safe dict, or None when there is no
    snapshot store to work from."""
    # Local imports: mode="off" must not pay for pandas/parquet at all.
    from scripts.managers.machine_learning.labels.labeling import build_labels
    from scripts.managers.machine_learning.labels.snapshots import load_snapshots

    warns: list = list(warnings or [])
    mode = registry.threshold_mode(config)
    horizon = registry.horizon_days(config)
    targets = registry.target_probabilities(config)
    include_backfill = registry.include_backfill(config)
    k = registry.shrinkage_k(config)

    snaps = snapshots if snapshots is not None else load_snapshots(base_dir)
    if snaps is None or getattr(snaps, "empty", True):
        return None

    if "source" in snaps.columns:
        src = snaps["source"].fillna("prospective").replace("", "prospective")
        rows_by_source = {str(k_): int(v) for k_, v in src.value_counts(dropna=False).items()}
        if not include_backfill:
            n_bf = int(rows_by_source.get("backfill", 0))
            snaps = snaps[src != "backfill"]
            if n_bf:
                warns.append(
                    f"{n_bf} backfill snapshot row(s) EXCLUDED from the calibrator "
                    "(ml.thresholds.include_backfill=false). On a fresh install "
                    "those are usually the ONLY labels that exist — the default is "
                    "true precisely so a new household gets a derived value at all.")
    else:
        rows_by_source = {"prospective": int(len(snaps))}
    if snaps.empty:
        snaps = None

    # Bound the per-run cost: the snapshot store is append-only, the label join
    # walks rows in Python, and a year of matured labels is already far past what
    # the §10 gate asks for. 0 = unbounded.
    fit_days = registry.max_fit_days(config)
    if snaps is not None and fit_days and "snapshot_date" in snaps.columns:
        from datetime import timedelta
        cutoff = ((now if now is not None else datetime.now(tz=timezone.utc))
                  - timedelta(days=float(fit_days)))
        older = snaps["snapshot_date"].astype(str) < f"{cutoff:%Y-%m-%d}"
        n_old = int(older.sum())
        if n_old:
            snaps = snaps[~older]
            warns.append(f"{n_old} snapshot row(s) older than "
                         f"ml.thresholds.max_fit_days={fit_days} excluded from the "
                         "calibrator fit (cost bound; set 0 for the full history).")
        if snaps.empty:
            snaps = None

    labeled = None
    if snaps is not None:
        # A frame that already carries labels (a caller's pre-joined frame, or a
        # test fixture) is used as-is; otherwise join Tautulli ground truth once,
        # on the pre-filtered candidates only.
        if {"watched_within_h", "label_mature"} <= set(snaps.columns):
            labeled = snaps
        else:
            candidates = mature_candidates(snaps, horizon, now=now)
            if candidates is not None and not candidates.empty:
                labeled = build_labels(candidates, base_dir,
                                       horizon_days=horizon, now=now)
        if labeled is not None and len(labeled):
            mature_mask = (labeled["label_mature"].astype("boolean")
                           .fillna(False).to_numpy(dtype=bool))
            labeled = labeled[mature_mask]

    # ── one calibrator per service ────────────────────────────────────────────
    calibrators: dict = {}
    gates: dict = {}
    for svc in _SERVICES:
        n = n_pos = n_pos_pro = n_pos_bf = 0
        if labeled is not None and not labeled.empty and "service" in labeled.columns:
            sub = labeled[labeled["service"] == svc]
            n = int(len(sub))
            if n:
                pos = (sub["watched_within_h"].astype("boolean")
                       .fillna(False).to_numpy(dtype=bool))
                n_pos = int(pos.sum())
                if "source" in sub.columns:
                    is_bf = (sub["source"].fillna("prospective")
                             .replace("", "prospective").astype(str)
                             .to_numpy() == "backfill")
                    n_pos_bf = int((pos & is_bf).sum())
                    n_pos_pro = n_pos - n_pos_bf
                else:
                    n_pos_pro = n_pos
        ok, reason = data_gate(n_pos)
        cal = None
        if n:
            # The §10 milestone is an annotation now (require_gate=False): the
            # value that DRIVES anything is the shrunk one, and the weight below
            # already scales it by exactly how much evidence stands behind it.
            cal = fit_calibrator(labeled, horizon, service=svc,
                                 include_backfill=include_backfill,
                                 require_gate=False)
        gates[svc] = {"n": n, "n_pos": n_pos, "n_pos_prospective": n_pos_pro,
                      "n_pos_backfill": n_pos_bf, "gate_ok": bool(ok),
                      "reason": reason, "fitted": cal is not None,
                      "confidence": confidence_label(n_pos, cal is not None),
                      "w": (round(shrinkage_weight(n_pos, k), 4)
                            if cal is not None else 0.0)}
        calibrators[svc] = cal
        if cal is not None and not cal.gate_ok:
            gates[svc]["diagnostic"] = cal.to_dict(with_knots=False)
        if cal is None and n_pos and n_pos < MIN_POS_FOR_FIT:
            warns.append(
                f"{svc}: {n_pos} matured positive(s) is below the "
                f"MIN_POS_FOR_FIT={MIN_POS_FOR_FIT} floor — no calibrator, so "
                "every cutoff stays exactly at its configured constant. "
                "Shrinkage is for 'some evidence', not 'no evidence'.")

    # ── constants, raw derived equivalents, shrunk effective values ───────────
    current: dict = {}
    derived: dict = {}
    effective: dict = {}
    weights: dict = {}
    p_at_current: dict = {}
    target_by_name: dict = {}
    n_pos_by_name: dict = {}
    n_pos_split_by_name: dict = {}
    confidence_by_name: dict = {}
    gate_reason_by_name: dict = {}
    score_key: dict = {}

    for spec in registry.THRESHOLD_SPECS:
        cur = registry.resolve_constant(spec, config)
        current[spec.name] = cur
        target_by_name[spec.name] = targets.get(spec.bucket)
        score_key[spec.name] = spec.service
        g = gates.get(spec.service, {})
        n_pos = int(g.get("n_pos", 0))
        n_pos_by_name[spec.name] = n_pos
        n_pos_split_by_name[spec.name] = {
            "prospective": int(g.get("n_pos_prospective", 0)),
            "backfill": int(g.get("n_pos_backfill", 0))}
        gate_reason_by_name[spec.name] = g.get("reason", "")
        cal = calibrators.get(spec.service)
        d = None
        if cal is not None:
            try:
                p_at_current[spec.name] = cal.p_at(cur)
            except Exception:
                p_at_current[spec.name] = None
            # Unrouted thresholds are derived too — the report is where their
            # drift becomes visible; only `registry.get_threshold` decides who
            # may read it.
            try:
                d = derive_threshold(cal, float(target_by_name[spec.name]))
            except (ValueError, TypeError):
                d = None
        else:
            p_at_current[spec.name] = None
        derived[spec.name] = d
        # THE BLEND. d is None -> effective is the constant, bit-identically.
        eff, w = blend_threshold(d, cur, n_pos, k=k)
        effective[spec.name] = eff
        weights[spec.name] = w
        confidence_by_name[spec.name] = confidence_label(n_pos, d is not None)

    comparison = shadow.compare(current, derived, scores_by_service or {},
                                effective=effective, weights=weights,
                                calibrated_p_at_current=p_at_current,
                                target_p=target_by_name, score_key=score_key)

    rows: list = []
    for spec in registry.THRESHOLD_SPECS:
        row = dict(comparison["thresholds"].get(spec.name, {}))
        split = n_pos_split_by_name.get(spec.name, {})
        row.update({
            "name": spec.name,
            "bucket": spec.bucket,
            "service": spec.service,
            "consumer": spec.consumer,
            "rule": spec.rule,
            "config_key": spec.config_key,
            "routed": bool(spec.routed),
            "note": spec.note,
            "n_pos": n_pos_by_name.get(spec.name, 0),
            "n_pos_prospective": int(split.get("prospective", 0)),
            "n_pos_backfill": int(split.get("backfill", 0)),
            "confidence": confidence_by_name.get(spec.name, "none"),
            "shrinkage_k": k,
            # §10 milestone — an ANNOTATION now, not a veto: the value below is
            # shrunk, not withheld, when this is False.
            "gate_ok": bool(gates.get(spec.service, {}).get("gate_ok", False))
                       and row.get("derived") is not None,
            "gate_reason": gate_reason_by_name.get(spec.name, ""),
        })
        rows.append(row)

    # ── honesty warnings ──────────────────────────────────────────────────────
    if not any(r.get("derived") is not None for r in rows):
        warns.append(
            "No calibrator could be fit for any service (best n_pos="
            f"{max((g.get('n_pos', 0) for g in gates.values()), default=0)}, floor "
            f"{MIN_POS_FOR_FIT}) — every 'effective' value below IS the configured "
            "constant, bit-identically, and mode='derived' would change nothing.")
    tot_pos = sum(g.get("n_pos", 0) for g in gates.values())
    tot_bf = sum(g.get("n_pos_backfill", 0) for g in gates.values())
    if tot_pos and tot_bf * 2 >= tot_pos:
        warns.append(
            f"CAVEAT: {tot_bf} of {tot_pos} matured positive(s) are BACKFILL "
            "reconstructions (leakage: credits_today, metadata_today, "
            "deletions_unknown) — the derived side of every blend below rests "
            "mostly on replayed history, not on observed forward outcomes.")

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "mode": mode,
        "horizon_days": horizon,
        "max_fit_days": fit_days,
        "shrinkage_k": k,
        "targets": dict(targets),
        "target_defaults": dict(registry.DEFAULT_TARGET_P),
        "include_backfill": include_backfill,
        "rows_by_source": rows_by_source,
        "n_pos_by_source": {"prospective": sum(g.get("n_pos_prospective", 0)
                                               for g in gates.values()),
                            "backfill": tot_bf},
        "gates": gates,
        "calibrators": {svc: (v.to_dict() if v is not None else None)
                        for svc, v in calibrators.items()},
        "thresholds": rows,
        "totals": comparison["totals"],
        "warnings": warns,
    }


# ── persist ───────────────────────────────────────────────────────────────────

def write_report(report: dict, base_dir) -> "Path | None":
    """``<cache>/ml/reports/thresholds_{YYYY-MM-DD}.json`` (atomic; one file per
    day, overwritten by later runs the same day)."""
    if not report:
        return None
    out_dir = reports_dir(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(report.get("generated_at") or "")[:10] or \
        f"{datetime.now(tz=timezone.utc):%Y-%m-%d}"
    path = out_dir / f"thresholds_{stamp}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ── render ────────────────────────────────────────────────────────────────────

def log_report(report: dict, logger) -> None:
    """One compact table + the warnings. Never raises."""
    if not report or logger is None:
        return
    entries = report.get("thresholds", [])
    comparison = {"thresholds": {r["name"]: r for r in entries}}
    rows = shadow.render_rows(
        comparison,
        specs_by_name=registry.SPEC_BY_NAME,
        n_pos_by_name={r["name"]: r.get("n_pos", 0) for r in entries},
        n_pos_split_by_name={r["name"]: {"prospective": r.get("n_pos_prospective", 0),
                                         "backfill": r.get("n_pos_backfill", 0)}
                             for r in entries},
        confidence_by_name={r["name"]: r.get("confidence", "none") for r in entries},
    )
    if not rows:
        return
    mode = report.get("mode", registry.DEFAULT_MODE)
    totals = report.get("totals", {})
    k = report.get("shrinkage_k", registry.DEFAULT_SHRINKAGE_K)
    split = report.get("n_pos_by_source", {}) or {}
    horizon = report.get("horizon_days")
    n_pro = int(split.get("prospective", 0))
    n_bf = int(split.get("backfill", 0))
    n_tot = n_pro + n_bf
    w_now = (n_tot / (n_tot + k)) if (n_tot + k) else 0.0
    n_der = totals.get("n_with_derived", 0)
    n_thr = totals.get("n_thresholds", 0)
    n_flip = totals.get("n_flip_total", 0)

    title = (f"Threshold derivation [{mode}] — the score cutoffs set by hand, "
             f"vs what this household's own watch history says they should be")

    # The caption is the ONLY documentation most readers of a log will ever see, so it
    # carries a full per-column glossary in plain language rather than the formula alone.
    # Newlines are honoured by _box_table (each segment wraps on its own).
    if mode == registry.MODE_DERIVED:
        status = ("STATUS: mode='derived' — the 'effective' column IS what every consumer "
                  "read this run. Set ml.thresholds.mode='shadow' to go back to reporting "
                  "only, or 'off' to skip the derivation entirely.")
    else:
        status = (f"STATUS: mode='{mode}' — REPORT ONLY. Nothing below changed a single "
                  f"decision this run; every consumer still used its own 'current' value. "
                  f"Set ml.thresholds.mode='derived' to let 'effective' actually drive.")

    caption = (
        f'WHAT THIS IS: every "delete below X" / "acquire above X" score cutoff in the '
        f'system, re-derived from whether titles at that score were ACTUALLY watched '
        f'within {horizon} days, then blended with the number already in use. Read any row '
        f'as: "today I use <current>; the watch data alone says <derived>; trusting that '
        f'data <w> of the way gives <effective>."\n'
        f"\n"
        f"  threshold    which decision the cutoff gates. A trailing * means the row is "
        f"shown for visibility but is never actually read by anything.\n"
        f"  current      the number in force RIGHT NOW — the config.json value if one is "
        f"set, otherwise the shipped default.\n"
        f"  derived      what the watch history ALONE would pick. Blank means too few "
        f"watches to fit a curve — {n_der} of {n_thr} row(s) have one at all.\n"
        f"  w            how far to trust 'derived': 0.00 = ignore it, 1.00 = trust it "
        f"completely. It is watches/(watches+{k:g}) and climbs on its own as watch history "
        f"accumulates — currently {w_now:.2f}, so the hand-set number still dominates.\n"
        f"  effective    the blend that would actually be used = w × derived + (1−w) × "
        f"current. At a low w it stays close to 'current' by design, so a new install is "
        f"never yanked around by a handful of early watches.\n"
        f"  n_pos (p+b)  the EVIDENCE behind the row — titles watched inside the {horizon}d "
        f"window. p = {n_pro} observed live since install; b = {n_bf} reconstructed from "
        f"older Tautulli history (usable, but weaker than a live observation).\n"
        f"  confidence   how seriously to read the gap between 'current' and 'effective': "
        f"directional (<100 watches) → usable (100+) → stable (300+) → magnitude-grade "
        f"(1000+).\n"
        f"  would flip   how many titles would get a DIFFERENT answer if 'effective' "
        f"replaced 'current' — {n_flip} in total across every row below.\n"
        f"\n"
        + status)
    try:
        logger.log_table(shadow.RENDER_HEADERS, rows, title=title, caption=caption)
    except Exception:
        try:
            logger.log_info(f"[Thresholds] {title} — {caption}")
        except Exception:
            return
    for w in report.get("warnings", []):
        try:
            logger.log_debug(f"[Thresholds] {w}")
        except Exception:
            break


# ── the single entry point plan_summary calls ─────────────────────────────────

def run(config, *, base_dir=None, global_cache=None, scores_by_service=None,
        logger=None, now=None, write: bool = True) -> "dict | None":
    """Derive → compare → log → persist → prime. Returns the report dict (or
    None when disabled / no data). Best-effort: never raises."""
    try:
        if not registry.report_enabled(config):
            return None
        base = _resolve_base_dir(base_dir, global_cache)
        report = build_report(config, base_dir=base,
                              scores_by_service=scores_by_service, now=now)
        if report is None:
            if logger is not None:
                try:
                    logger.log_debug("[Thresholds] no snapshot store yet — "
                                     "threshold derivation skipped.")
                except Exception:
                    pass
            return None
        log_report(report, logger)
        if write:
            try:
                write_report(report, base)
            except Exception as e:
                if logger is not None:
                    try:
                        logger.log_debug(f"[Thresholds] report write skipped: {e}")
                    except Exception:
                        pass
        # Consumers read the EFFECTIVE (shrunk) value. A row with no derived
        # term primes None, so `get_threshold` hands back the caller's OWN
        # literal object — identical in value to the effective one, but without
        # converting an int cutoff into a float copy of itself.
        registry.prime(
            {r["name"]: (r.get("effective") if r.get("derived") is not None else None)
             for r in report.get("thresholds", [])},
            source="this run",
            meta={"generated_at": report.get("generated_at"),
                  "horizon_days": report.get("horizon_days"),
                  "shrinkage_k": report.get("shrinkage_k")})
        return report
    except Exception as e:
        if logger is not None:
            try:
                logger.log_debug(f"[Thresholds] derivation skipped: {e}")
            except Exception:
                pass
        return None

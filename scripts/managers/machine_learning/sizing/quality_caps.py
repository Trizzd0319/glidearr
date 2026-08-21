"""
sizing/quality_caps.py — a grab-time size ceiling derived from the library itself.
================================================================================
THE CASE THAT BUILT THIS. Radarr grades a release by its FILENAME, never its
bytes: ``The.Godfather.3.1990.720p.BluRay.DTS.x264-CyTSuNee`` parses as
``Bluray-720p`` whether the payload is an 8 GiB encode or a 50.9 GiB disc image.
Size is not part of the grab decision, so nothing in the chain refuses it.

Glidearr already catches this -- in ``sizing/anomaly.py`` -- but only AFTER the
file has downloaded, imported and hardlinked. The existence of a post-hoc
detector is the argument for a pre-hoc ceiling.

Glidearr does not pick releases (it adds titles and triggers *arr's own search),
so a ceiling has to live where the picking happens: Radarr/Sonarr **quality
definitions**, whose ``maxSize`` is expressed in MB per minute -- the same shape
as this package's MiB/min model. This module turns one into the other.

ONE THRESHOLD, TWO ENFORCEMENT POINTS. The multiplier is ``over_ratio``, the
same number ``anomaly.py`` uses to flag a landed file. The rule reads: *if we
would call it an anomaly after the grab, refuse it before.* Measured against the
real search results for that film (runtime 162 min, ``Bluray-720p`` = 52.4
MiB/min measured over n=1170):

    50.9 GiB  321.7 MiB/min  6.14x  REJECT   (mislabelled disc image)
    49.2 GiB  311.0 MiB/min  5.93x  REJECT   (BD50, explicitly)
    28.0 GiB  177.0 MiB/min  3.38x  REJECT   (split archive volume)
    19.9 GiB  125.8 MiB/min  2.40x  allow    (heavy but real encode)
     8.6 GiB   54.4 MiB/min  1.04x  allow
     5.9 GiB   37.3 MiB/min  0.71x  allow

CAPS MAY ONLY TIGHTEN -- READ BEFORE EDITING
--------------------------------------------
A proposal that would RAISE an existing ceiling is never emitted. The rate
feeding these numbers comes from the library, and a library polluted by bloat
produces a higher rate, which would produce a *looser* cap, which admits more
bloat. That is the same self-loosening ratchet ``measured_stats``' outlier
rejection exists to break (``GLD-SIZ-12``), and this is the second guard on it:
even if a poisoned rate reaches this module, it cannot widen anything.

Thin tiers get NO cap at all. ``Bluray-1080p`` carries n=18 in this library; a
single mislabelled file moves its mean by ~21%. Deriving a ceiling from that is
deriving a ceiling from noise, and the failure is asymmetric -- too low a cap
silently starves a whole quality tier of releases, with no error anywhere, just
an *arr that mysteriously never grabs. ``min_samples`` is the guard, and skipped
tiers are REPORTED rather than dropped silently.

Pure module: no I/O, no manager access, stdlib only. The caller owns the fetch,
the diff, the dry-run gate and the PUT.
"""
from __future__ import annotations

# Radarr/Sonarr express quality-definition sizes in decimal MB per minute; this
# package measures binary MiB per minute. Skipping the conversion understates
# every cap by 4.9%, which is a silent over-tightening.
MIB_TO_MB = 1.048576

# Above this a file is not a real encode, it is a units bug or a disc image --
# the same ceiling size_model uses to reject corrupt rows. No proposal is ever
# emitted above it, whatever the measured rate says.
ABSOLUTE_MAX_MB_PER_MIN = 900.0 * MIB_TO_MB

DEFAULTS = {
    # OFF at the module level: this writes to *arr CONFIGURATION, which is a
    # different class of change from anything else this package does. A tester
    # who copies the code changes nothing until they opt in.
    "enabled": False,
    # Mirrors size_anomaly.over_ratio on purpose (see the module docstring). The
    # caller passes the operator's actual value so the two cannot drift.
    "over_ratio": 3.0,
    # Minimum measured files in a tier before a ceiling may be derived from it.
    # 30 is deliberately conservative: the cost of skipping a tier is that it
    # keeps whatever ceiling it already had, while the cost of capping from
    # noise is a tier that silently stops grabbing.
    "min_samples": 30,
    # A proposed cap must sit at least this far above the tier's own measured
    # mean. Guards the degenerate case where over_ratio is configured very low
    # and the ceiling lands under the tier's own typical file.
    "min_headroom_ratio": 1.5,
    # Never propose a ceiling below this, whatever the arithmetic produces.
    "floor_mb_per_min": 5.0,
}


def config_for(cfg) -> dict:
    """Merge a ``quality_caps`` block over the defaults; ``None`` values and
    unknown keys are ignored so a partial block keeps every other default."""
    raw = {}
    try:
        raw = (cfg.get("quality_caps", {}) or {}) if hasattr(cfg, "get") else {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if k in out and v is not None:
            out[k] = v
    return out


def _as_pos_float(v):
    """float(v) when it parses to a positive number, else None. Unknown is
    UNKNOWN -- never 0, which here would read as 'a cap of zero'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def current_max(defn: dict):
    """The effective ceiling on an *arr quality definition, in MB/min.

    ``None`` means UNLIMITED, and that is a real state: Radarr stores an absent
    or null ``maxSize`` for tiers it will never refuse on size. Unlimited must
    not be read as 0 -- 0 would compare as 'tighter than any proposal' and
    suppress every cap this module exists to set (P-C)."""
    if not isinstance(defn, dict):
        return None
    return _as_pos_float(defn.get("maxSize"))


def quality_name(defn: dict) -> str:
    """The tier name an *arr definition carries, for joining to measured rates.
    Radarr nests it under ``quality``; some payloads carry a flat ``title``."""
    if not isinstance(defn, dict):
        return ""
    q = defn.get("quality")
    if isinstance(q, dict) and q.get("name"):
        return str(q["name"])
    return str(defn.get("title") or "")


def propose_max_mb_per_min(rate_mib_per_min, cfg) -> "float | None":
    """The ceiling for one tier, in MB/min, or None when the rate is unusable.

    ``rate x over_ratio`` in MiB/min, converted to MB/min, floored, and clamped
    to :data:`ABSOLUTE_MAX_MB_PER_MIN`."""
    rate = _as_pos_float(rate_mib_per_min)
    if rate is None:
        return None
    ratio = max(float(cfg["over_ratio"]), float(cfg["min_headroom_ratio"]))
    proposed = rate * ratio * MIB_TO_MB
    proposed = max(proposed, float(cfg["floor_mb_per_min"]))
    return min(proposed, ABSOLUTE_MAX_MB_PER_MIN)


def plan_caps(definitions, measured, cfg) -> tuple:
    """``(proposals, skipped)`` for one *arr instance.

    ``definitions`` is the *arr ``qualitydefinition`` payload as fetched.
    ``measured`` is ``{quality_name: {"mean": mib_per_min, "n": count}}`` from
    ``size_model.measured_stats`` -- the LIVE library, not the cold-start table,
    because a ceiling should reflect what this library actually holds.

    Every definition lands in exactly one of the two lists, each carrying the
    reason, so the caller's diff table can account for all of them. A tier is
    never dropped on the floor: 'skipped' is a reported outcome, not silence.
    """
    proposals, skipped = [], []
    min_n = int(cfg["min_samples"])
    for defn in (definitions or []):
        name = quality_name(defn)
        if not name:
            skipped.append({"quality": "?", "reason": "unnamed definition"})
            continue
        stat = (measured or {}).get(name)
        if not isinstance(stat, dict):
            skipped.append({"quality": name, "reason": "no measured files in this library"})
            continue
        n = int(stat.get("n") or 0)
        rate = _as_pos_float(stat.get("mean"))
        if rate is None:
            skipped.append({"quality": name, "n": n, "reason": "unusable measured rate"})
            continue
        if n < min_n:
            # Asymmetric failure: too low a cap starves the tier silently.
            skipped.append({"quality": name, "n": n,
                            "reason": f"thin sample (n={n} < {min_n}) - no cap from noise"})
            continue
        proposed = propose_max_mb_per_min(rate, cfg)
        if proposed is None:
            skipped.append({"quality": name, "n": n, "reason": "unusable measured rate"})
            continue
        cur = current_max(defn)
        # CAPS MAY ONLY TIGHTEN. `cur is None` is UNLIMITED, so any finite
        # proposal tightens it; a finite current is only replaced by something
        # smaller. See the module docstring for why loosening is the dangerous
        # direction.
        if cur is not None and proposed >= cur:
            skipped.append({"quality": name, "n": n, "current_max": cur,
                            "proposed": round(proposed, 1),
                            "reason": "existing cap already tighter - never loosened"})
            continue
        proposals.append({
            "quality": name,
            "n": n,
            "rate_mib_per_min": round(rate, 1),
            "current_max": cur,                       # None = unlimited
            "proposed_max": round(proposed, 1),
            "id": defn.get("id"),
        })
    return proposals, skipped


def apply_to_definition(defn: dict, proposed_max: float) -> dict:
    """A COPY of ``defn`` with only ``maxSize`` changed.

    ``minSize`` and ``preferredSize`` are deliberately untouched: this module
    reasons about a ceiling and has no evidence about either of the others, and
    an *arr PUT round-trips the whole object -- so anything not preserved here
    would be silently rewritten from a value this module never computed."""
    out = dict(defn or {})
    out["maxSize"] = round(float(proposed_max), 1)
    return out


def gib_at_runtime(mb_per_min: float, runtime_minutes: float) -> float:
    """The MB/min ceiling expressed as GiB for a given runtime -- the number an
    operator can actually sanity-check against a search result page."""
    mib_per_min = float(mb_per_min) / MIB_TO_MB
    return (mib_per_min * float(runtime_minutes)) / 1024.0


def measured_from_calibration(payload) -> dict:
    """``{quality: {"mean": mib_per_min, "n": count}}`` from the persisted
    ``size_model/calibration`` payload, which stores the rate and the sample count
    in two SEPARATE maps (``table`` and ``counts``).

    This is the whole reason a cap adapter needs no parquet access: the calibrator
    already measured the live library this run and persisted both halves.

    A tier present in ``table`` but ABSENT from ``counts`` gets ``n=0``, which the
    thin-sample guard then refuses to cap from. That is the safe direction and it
    is deliberate: an unknown sample size must never be treated as a large one,
    because the whole point of the guard is that a ceiling derived from noise
    starves a tier silently (P-C).
    """
    if not isinstance(payload, dict):
        return {}
    table = payload.get("table")
    counts = payload.get("counts")
    if not isinstance(table, dict):
        return {}
    counts = counts if isinstance(counts, dict) else {}
    out = {}
    for q, rate in table.items():
        r = _as_pos_float(rate)
        if r is None:
            continue
        try:
            n = int(counts.get(q, 0) or 0)
        except (TypeError, ValueError):
            n = 0
        out[str(q)] = {"mean": r, "n": max(0, n)}
    return out


def render_rows(proposals, skipped, runtime_minutes: float) -> tuple:
    """``(headers, rows, descriptions)`` for the operator-facing diff table.

    ONE renderer for BOTH services (P-E). Radarr and Sonarr already keep two
    copies of everything around quality definitions -- separate cache keys
    (``radarr.{i}.quality.definitions`` vs ``sonarr/{i}/quality_definitions.json``),
    separate fetch calls, separate summary lines -- and each pair has drifted in
    wording. A second diff table would drift the same way; the only thing that
    legitimately differs is ``runtime_minutes``, which is a caller argument.

    ``runtime_minutes`` converts an abstract MB/min ceiling into the number the
    operator can check against a search page: a typical feature for Radarr, a
    typical episode for Sonarr. It is presentational ONLY -- the cap that gets
    written is always the rate, never this product.
    """
    headers = ["Quality", "n", "Rate MiB/min", "Current", "New cap", f"~GiB @{int(runtime_minutes)}m", "Outcome"]
    rows, descs = [], []
    for p in (proposals or []):
        cur = "unlimited" if p["current_max"] is None else f"{p['current_max']:.0f}"
        rows.append([
            str(p["quality"])[:22], p["n"], p["rate_mib_per_min"], cur,
            f"{p['proposed_max']:.1f}",
            round(gib_at_runtime(p["proposed_max"], runtime_minutes), 1),
            "CAP",
        ])
        descs.append("ceiling tightened from the library's own measured rate")
    for s in (skipped or []):
        rows.append([
            str(s.get("quality", "?"))[:22], s.get("n", "-"), "-",
            ("unlimited" if s.get("current_max") is None else f"{s['current_max']:.0f}")
            if "current_max" in s else "-",
            "-", "-", "skip",
        ])
        descs.append(str(s.get("reason", ""))[:70])
    return headers, rows, descs


def push_caps(*, definitions, measured, cfg, runtime_minutes, put, logger,
              dry_run: bool, label: str) -> dict:
    """Plan, report, and (when armed) write the ceilings for ONE instance.

    Deliberately service-agnostic: ``put(defn) -> truthy`` and ``logger`` are
    injected, so Radarr and Sonarr share this entire path and cannot drift. The
    caller owns the fetch, the instance loop, and the endpoint.

    ``dry_run`` is the caller's ``effective_dry_run`` -- this writes *arr
    CONFIGURATION, which outlives the run and affects every future grab,
    including ones Glidearr never initiates. It is therefore gated harder than a
    grab: a withheld write is reported as ``would``, never silently skipped.

    A failed PUT counts as FAILED and the definition is left alone. ``put`` is
    expected to follow this codebase's ``_make_request`` contract -- it LOGS and
    returns a falsy fallback rather than raising -- so a falsy return is checked
    explicitly. That is the `GLD-SON-20` lesson: an unchecked write result turns
    four rejections into four reported successes.
    """
    proposals, skipped = plan_caps(definitions, measured, cfg)
    stats = {"proposed": len(proposals), "skipped": len(skipped),
             "applied": 0, "would": 0, "failed": 0}
    headers, rows, descs = render_rows(proposals, skipped, runtime_minutes)
    if rows:
        logger.log_table(
            headers, rows,
            title=f"[QualityCaps] {label}" + (" [dry_run]" if dry_run else ""),
            caption=("Grab-time size ceiling per quality tier, derived from this "
                     "library's own measured MiB/min at the same over_ratio the "
                     "size-anomaly detector uses. Caps only ever tighten."),
            descriptions=descs,
        )
    for p in proposals:
        if dry_run:
            stats["would"] += 1
            continue
        src = next((d for d in definitions if quality_name(d) == p["quality"]), None)
        if src is None:                       # cannot happen from plan_caps, but a
            stats["failed"] += 1              # silent skip here would be invisible
            continue
        if put(apply_to_definition(src, p["proposed_max"])):
            stats["applied"] += 1
        else:
            stats["failed"] += 1
            logger.log_warning(
                f"[QualityCaps] {label}: '{p['quality']}' cap REJECTED by the API "
                f"- ceiling unchanged, retried next run.")
    return stats

"""
thresholds/drift.py — the axis-drift detector (pure).
================================================================================
WHY THIS EXISTS
---------------
Every absolute cutoff in ``registry.THRESHOLD_SPECS`` is a point on a score axis,
and it was chosen against a DISTRIBUTION. Move the distribution and the cutoff
means something different — without anyone editing the cutoff.

That has already happened once, and nothing caught it:

    Group D v2 (SCORER_REVISION 4) replaced a near-constant +12 bonus with a
    0-to-negative transcode-risk penalty. The file-owning median moved 21 -> 8.
    The delete family had to be re-anchored 20 -> 17, and ``likelihood
    .untouched_base`` 12 -> 25 — the latter only AFTER untouched titles reaching
    1080p had collapsed 456 -> 8 (-98.2%).

The collapse was found by someone measuring it by hand. There was no check, and
until this module there still wasn't one. See registry.py's delete block for the
full account and for what can translate the axis again.

WHAT IT MEASURES — AND WHY NOT THE MEDIAN
-----------------------------------------
The re-anchor that fixed Group D v2 did NOT match medians. It matched
SELECTIVITY: "17 is the value that PRESERVES THE ORIGINAL SELECTIVITY", computed
by reconstructing the pre-Group-D distribution and matching the share of the
population below the cutoff (84.3% pre-D -> 17.0 on the v2 axis).

So selectivity is what this module tracks. It is the quantity a threshold
actually controls, and it is the one that stayed constant across a legitimate
re-anchor. A median can move without changing what any cutoff admits; a cutoff's
share can move without the median budging. Only the second one is a behaviour
change.

SCALE-AGNOSTIC ON PURPOSE
-------------------------
``untouched_base`` sits on the LIKELIHOOD scale and has no ThresholdSpec, which
is exactly why a re-anchor pass walking only ``THRESHOLD_SPECS`` missed it.
Everything here takes a bare sequence of numbers and a bare mapping of cutoffs,
so the watchability axis, the likelihood axis and any future one are all just
inputs. Track a downstream population the same way: "untouched titles reaching
1080p" is a selectivity on the likelihood scale.

PURE
----
stdlib only — no pandas, no numpy, no I/O, no logging, no config, no clock.
Given the same inputs it returns the same dict, so it is trivially testable and
safe to call from anywhere. The CALLER owns fetching scores, loading the anchor
and reporting the verdict.

NOT WIRED IN YET
----------------
This module computes; nothing calls it. The intended seam is
``ledger/plan_summary``, which already holds both parquets open and already fits
the threshold calibrator at end of run — see ``build_anchor``/``assess`` below
and the wiring note in §"INTEGRATION" at the bottom.
"""
from __future__ import annotations

import math

# ── tolerances ────────────────────────────────────────────────────────────────
#
# These are REVIEWED DEFAULTS, not derived constants — say so rather than
# implying a calibration nobody ran. Two measured points anchor them:
#
#   * BENIGN CHURN. ``thresholds`` percentile mode, which is immune to
#     translation by construction, still moved 0.0% / +4.9% across the same
#     window where absolute cutoffs moved -98.2%. So ~5 points of selectivity
#     movement is ordinary library turnover, not drift.
#   * THE INCIDENT. Group D v1 left the delete ceiling admitting 39.0% of movies
#     where the pre-D axis admitted 84.3% — a 45-point swing that ran unnoticed
#     for a whole scorer revision.
#
# WARN at 10 (about twice the observed benign ceiling — high enough not to cry
# wolf on turnover, low enough to fire long before 45) and ALARM at 20.
WARN_DELTA_PP = 10.0
ALARM_DELTA_PP = 20.0

#: Below this many samples a share is noise and no verdict is issued. A cutoff
#: whose population is this small was not calibrated against anything either.
MIN_SAMPLES = 50

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_ALARM = "alarm"
SEVERITY_UNKNOWN = "unknown"


def _clean(scores) -> list:
    """Finite floats only, sorted ascending. Non-numeric and NaN/inf entries are
    DROPPED rather than coerced: a NaN score is an absent measurement, and
    counting it as 0.0 would fake exactly the downward drift this module looks
    for."""
    out = []
    for s in scores or ():
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    out.sort()
    return out


def _quantile(sorted_vals: list, q: float):
    """Linear-interpolated quantile of an ALREADY-SORTED list. ``None`` on an
    empty list — not 0.0, which would read as a real score at the axis floor."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * max(0.0, min(1.0, q))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def selectivity(scores, cutoff) -> "float | None":
    """Share of *scores* STRICTLY BELOW *cutoff*, as a percentage (0-100).

    Strict, because every delete-family consumer is strict (``delete iff score <
    17``, ``restore iff score > 17``) — a title exactly at the cutoff is neither,
    and the registry's ``series_restore`` note depends on that being true.

    ``None`` when there is no usable sample: an empty population has no
    selectivity, and 0.0 would claim the cutoff admits everything.
    """
    vals = _clean(scores)
    if not vals:
        return None
    try:
        c = float(cutoff)
    except (TypeError, ValueError):
        return None
    below = sum(1 for v in vals if v < c)
    return 100.0 * below / len(vals)


def fingerprint(scores, cutoffs: "dict | None" = None) -> dict:
    """A distribution's shape plus its selectivity at each named cutoff.

    ``cutoffs`` is ``{name: value}`` — e.g.
    ``{"movie_delete_ceiling": 17, "movie_demote": 20}``. Names are opaque, so a
    likelihood-scale population can pass ``{"untouched_base": 25}`` and the
    machinery is identical.

    Every summary field is ``None`` on an empty sample rather than 0.0.
    """
    vals = _clean(scores)
    n = len(vals)
    mean = (sum(vals) / n) if n else None
    if n > 1 and mean is not None:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = None

    shares = {}
    for name, cut in (cutoffs or {}).items():
        shares[str(name)] = {"cutoff": cut, "share_below": selectivity(vals, cut)}

    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "median": _quantile(vals, 0.50),
        "p95": _quantile(vals, 0.95),
        "p99": _quantile(vals, 0.99),
        "min": vals[0] if n else None,
        "max": vals[-1] if n else None,
        "shares": shares,
    }


def build_anchor(scores, cutoffs: dict, *, label: str, revision=None) -> dict:
    """The artifact to persist WHEN A RE-ANCHOR IS PERFORMED — the distribution
    the current cutoffs were chosen against.

    Store this beside the threshold report and never regenerate it automatically:
    an anchor that refreshes itself each run measures nothing, because the
    baseline drifts with the thing it is supposed to catch. It is a deliberate
    record of a human decision, replaced only when the cutoffs are re-derived.

    ``label`` names the population (``"radarr.owned_movies"``); ``revision``
    records what the anchor was taken against (e.g. ``SCORER_REVISION``), so a
    later reader can tell whether the anchor predates a known translation.
    """
    return {
        "label": str(label),
        "revision": revision,
        "fingerprint": fingerprint(scores, cutoffs),
    }


def compare(anchor: dict, current_scores, *, cutoffs: "dict | None" = None) -> dict:
    """Per-cutoff selectivity movement between an anchor and the current scores.

    Cutoffs default to the anchor's own, which is the point: drift is measured
    against the values that were anchored, not against whatever is configured
    today. Pass *cutoffs* explicitly only to ask a what-if.

    Each row carries ``delta_pp`` — the change in percentage POINTS, positive
    meaning the cutoff now admits MORE of the population (the axis fell) and
    negative meaning it admits fewer (the axis rose).
    """
    base_fp = (anchor or {}).get("fingerprint") or {}
    base_shares = base_fp.get("shares") or {}
    use = cutoffs or {n: r.get("cutoff") for n, r in base_shares.items()}

    cur_fp = fingerprint(current_scores, use)
    rows = {}
    for name, cur in (cur_fp.get("shares") or {}).items():
        base = base_shares.get(name) or {}
        b, c = base.get("share_below"), cur.get("share_below")
        delta = (c - b) if (b is not None and c is not None) else None
        rows[name] = {
            "cutoff": cur.get("cutoff"),
            "anchor_cutoff": base.get("cutoff"),
            "anchor_share": b,
            "current_share": c,
            "delta_pp": delta,
            # A cutoff that CHANGED since the anchor is not drift — someone
            # re-anchored, or someone edited a config. Flag it so the delta is
            # not read as an axis movement it is not.
            "cutoff_changed": (base.get("cutoff") is not None
                               and cur.get("cutoff") is not None
                               and float(base["cutoff"]) != float(cur["cutoff"])),
        }

    return {
        "label": (anchor or {}).get("label"),
        "anchor_revision": (anchor or {}).get("revision"),
        "anchor_n": base_fp.get("n"),
        "current_n": cur_fp.get("n"),
        "anchor_median": base_fp.get("median"),
        "current_median": cur_fp.get("median"),
        "rows": rows,
    }


def assess(comparison: dict, *, warn_pp: float = WARN_DELTA_PP,
           alarm_pp: float = ALARM_DELTA_PP, min_samples: int = MIN_SAMPLES) -> dict:
    """Turn a comparison into a verdict.

    Severity is the WORST row's. A row is ``unknown`` — never ``ok`` — when
    either side lacks a share or the current sample is too small: "we could not
    tell" and "nothing moved" are different answers, and collapsing them is how
    a detector reports health it never established.

    A row whose CUTOFF changed is reported but does not drive severity: the
    delta then measures a deliberate edit, not axis movement.
    """
    rows = (comparison or {}).get("rows") or {}
    cur_n = (comparison or {}).get("current_n") or 0

    findings = []
    worst = SEVERITY_OK if rows else SEVERITY_UNKNOWN

    def _rank(s):
        return {SEVERITY_OK: 0, SEVERITY_UNKNOWN: 1,
                SEVERITY_WARN: 2, SEVERITY_ALARM: 3}[s]

    for name, row in sorted(rows.items()):
        delta = row.get("delta_pp")
        if row.get("cutoff_changed"):
            sev, why = SEVERITY_OK, "cutoff changed since the anchor — not axis drift"
        elif delta is None or cur_n < min_samples:
            sev, why = SEVERITY_UNKNOWN, (
                f"insufficient sample (n={cur_n} < {min_samples})" if delta is not None
                else "no comparable share on one side")
        elif abs(delta) >= alarm_pp:
            sev, why = SEVERITY_ALARM, (
                f"selectivity moved {delta:+.1f} pp "
                f"({row.get('anchor_share'):.1f}% -> {row.get('current_share'):.1f}%)")
        elif abs(delta) >= warn_pp:
            sev, why = SEVERITY_WARN, (
                f"selectivity moved {delta:+.1f} pp "
                f"({row.get('anchor_share'):.1f}% -> {row.get('current_share'):.1f}%)")
        else:
            sev, why = SEVERITY_OK, f"selectivity moved {delta:+.1f} pp"

        findings.append({"name": name, "severity": sev, "reason": why, **row})
        if _rank(sev) > _rank(worst):
            worst = sev

    return {
        "label": (comparison or {}).get("label"),
        "severity": worst,
        "drifted": worst in (SEVERITY_WARN, SEVERITY_ALARM),
        "findings": findings,
        "anchor_n": (comparison or {}).get("anchor_n"),
        "current_n": cur_n,
        "anchor_median": (comparison or {}).get("anchor_median"),
        "current_median": (comparison or {}).get("current_median"),
    }


def format_lines(verdict: dict) -> list:
    """One line per finding plus a header, for a caller that wants to log it.
    Returns strings; this module never logs."""
    sev = (verdict or {}).get("severity", SEVERITY_UNKNOWN)
    mark = {SEVERITY_OK: "OK", SEVERITY_WARN: "WARN",
            SEVERITY_ALARM: "ALARM", SEVERITY_UNKNOWN: "??"}.get(sev, "??")
    label = (verdict or {}).get("label") or "?"
    a_med, c_med = verdict.get("anchor_median"), verdict.get("current_median")
    med = (f"median {a_med:.1f} -> {c_med:.1f}"
           if a_med is not None and c_med is not None else "median n/a")
    out = [f"[AxisDrift] {mark} {label}: {med} "
           f"(anchor n={verdict.get('anchor_n')}, now n={verdict.get('current_n')})"]
    for f in verdict.get("findings") or ():
        out.append(f"    {f['severity']:<7} {f['name']}: {f['reason']}")
    return out


# ── INTEGRATION (not wired) ───────────────────────────────────────────────────
#
# Intended seam: ``ledger/plan_summary``, which already holds both parquets open
# and already fits the threshold calibrator at end of run.
#
#     from scripts.managers.machine_learning.thresholds import drift
#     from scripts.managers.machine_learning.thresholds.registry import (
#         THRESHOLD_SPECS, resolve_constant)
#
#     cuts = {s.name: resolve_constant(s, config)
#             for s in THRESHOLD_SPECS if s.service == "radarr"}
#     verdict = drift.assess(drift.compare(anchor, movie_scores, cutoffs=cuts))
#     for line in drift.format_lines(verdict):
#         logger.log_warning(line) if verdict["drifted"] else logger.log_info(line)
#
# TWO THINGS THE CALLER MUST GET RIGHT, or the detector reports health it never
# established:
#
#   1. THE ANCHOR IS NOT AUTO-REFRESHED. Write it with ``build_anchor`` only when
#      the cutoffs are deliberately re-derived. An anchor regenerated every run
#      measures nothing — the baseline drifts with the thing it is watching.
#   2. FEED IT THE RIGHT POPULATION. Anchor and current must be the same
#      population, or the delta is composition, not drift. The registry's own
#      measurements are per-service and file-owning-vs-stub; mixing stubs into an
#      anchor taken on file-owning rows would show a translation that is really a
#      change of denominator.
#
# WHAT THIS CANNOT CATCH: a translation that moves the distribution and the
# cutoffs TOGETHER (someone re-anchoring wrongly but consistently), and drift on
# an axis nobody anchored. It only ever answers "does this cutoff still admit the
# share it was chosen to admit?"

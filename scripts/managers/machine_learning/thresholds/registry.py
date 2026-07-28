"""
thresholds/registry.py — the ONE accessor every decision cutoff reads through.
================================================================================
Consumers used to embed their cutoff as a literal (``score >= 35``) or a bare
config read. Each of those is a private opinion about what "35" means, so a
calibrated replacement could never be swapped in without touching every call
site individually. This module is that single seam:

    from scripts.managers.machine_learning.thresholds.registry import get_threshold
    ...
    threshold = get_threshold("movie_monitor", self.config, threshold)

``default`` is the consumer's OWN fully-resolved value — its literal, or its
config override. In the default mode (``"shadow"``) ``get_threshold`` returns
that object unchanged, identity-preserved, having read exactly one config key.
Shadow and off are therefore byte-identical to the pre-registry code; the tests
assert it.

MODES  (``ml.thresholds.mode``)
-------------------------------
``"shadow"``  DEFAULT. Consumers get their literal. The end-of-run ledger still
              fits the calibrator, derives every cutoff, counts the entities
              that WOULD flip, and writes the audit JSON. Report only.
``"derived"`` Consumers get the EFFECTIVE cutoff — the calibrated value shrunk
              toward their own literal by ``w = n_pos/(n_pos+k)``
              (``derive.blend_threshold``), so a household with 38 positives
              moves a fifth of the way and one with none does not move at all.
              A threshold with no derived value (no calibrator: no labels, too
              few positives, degenerate map) falls back to the literal and logs
              ONE warning naming the threshold and the reason. Never a silent
              switch, and never an unshrunk one.
``"off"``     No derivation, no report, no cost. Consumers get their literal.

WHERE THE DERIVED VALUES COME FROM
----------------------------------
Consumers run all through the pass; the calibrator is fit once at the END of the
run (``ledger/plan_summary`` — the only place with both parquets already open).
Rather than refit per consumer, or hazard a mid-run ordering dependency, mode
``"derived"`` reads the newest ``<cache>/ml/reports/thresholds_*.json`` — the
artifact the previous run committed. A derived cutoff is therefore always a
value someone can open, diff and blame, and it can only change between runs, not
during one. ``prime()`` lets a caller (or a test) inject values directly.

THE INVENTORY (``THRESHOLD_SPECS``)
-----------------------------------
Every hand-set watchability cutoff found in the codebase, its owning file, its
literal, and the target-probability bucket it belongs to. Entries with
``routed=False`` are reported but still read their literal — the reason is on
the spec.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# ── modes ─────────────────────────────────────────────────────────────────────
MODE_SHADOW = "shadow"
MODE_DERIVED = "derived"
MODE_OFF = "off"
DEFAULT_MODE = MODE_SHADOW
MODES = (MODE_SHADOW, MODE_DERIVED, MODE_OFF)

# ── target-probability buckets ────────────────────────────────────────────────
# The four decision families §9 names. Every routed threshold belongs to exactly
# one; its target probability is the bucket's.
BUCKETS = ("acquire", "monitor", "delete", "uhd")

#: Defaults obtained by INVERTING today's constants through today's calibrator —
#: see the derivation in ``derive.py``'s module docstring (real snapshot store,
#: H=14d, radarr, n=11443 / n_pos=38):
#:     score 35 -> 0.037815   score 30 -> 0.029851
#:     score 20 -> 0.002322   score 70 -> 0.037815 (saturated: flat above 31)
#: Chosen to REPRODUCE current behaviour as closely as the calibrator can
#: express it, not to change it. Each is TRUNCATED (never rounded up) to four
#: significant figures: a target one ulp above a plateau's top is a target no
#: score reaches, and the inverse would clamp to 100 — "nothing qualifies".
DEFAULT_TARGET_P = {
    "acquire": 0.0378,    # score 35 — series monitor / acquisition band
    "monitor": 0.0298,    # score 30 — owned-movie re-monitor
    "delete": 0.0023,     # score 20 — delete / demote / dormant floor
    "uhd": 0.0378,        # score 70 — 4K routing (== acquire until data separates them)
}

DEFAULT_HORIZON_DAYS = 14
DEFAULT_MAX_FIT_DAYS = 365     # bound the per-run label join; 0 = unbounded

#: ``ml.thresholds.shrinkage_k`` — prior strength of the HAND-SET constant in
#: ``effective = w·derived + (1−w)·constant``, ``w = n_pos/(n_pos+k)``. 150 is
#: the midpoint of §10's "usable" (100) and "stable" (300) rungs: the derived
#: value is trusted half-way exactly where the milestones say it has stopped
#: being a rumour and has not yet settled. See ``derive.blend_threshold``.
DEFAULT_SHRINKAGE_K = 150.0

#: ``ml.thresholds.include_backfill`` — DEFAULT **TRUE**, and the only ML entry
#: point where that is so. A fresh install has no matured prospective labels at
#: all (the pipeline starts logging on installation day and matures a horizon
#: later), so excluding truncated-replay rows means excluding every label there
#: is. Threshold derivation tolerates the known leakage (``credits_today,
#: metadata_today, deletions_unknown``) far better than an accuracy claim does —
#: those channels move individual titles along the score axis, while a monotone
#: score→P map only needs the ordering to be roughly right. ``ml_forward
#: _validation`` — which IS an accuracy claim — keeps excluding them, and the
#: report always prints n_pos split by source.
DEFAULT_INCLUDE_BACKFILL = True

_REPORT_GLOB = "thresholds_*.json"


# ── the inventory ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThresholdSpec:
    """One hand-set decision cutoff on the 0-100 watchability scale."""

    name: str            # registry key
    bucket: str          # which target probability applies
    constant: float      # today's literal / config default
    service: str         # "radarr" | "sonarr" — whose calibrator inverts it
    consumer: str        # file:line, for the report
    rule: str            # the decision in one phrase
    config_key: "str | None" = None   # deployment override, when one exists
    routed: bool = True  # False → reported only, still reads its literal
    note: str = ""
    falsy_means_default: bool = False  # consumer writes ``cfg or DEFAULT`` — 0/"" = unset


THRESHOLD_SPECS: "tuple[ThresholdSpec, ...]" = (
    # ── acquire (35) ─────────────────────────────────────────────────────────
    ThresholdSpec(
        name="series_monitor", bucket="acquire", constant=35, service="sonarr",
        consumer="services/sonarr/series/quality.py:463",
        rule="keep/raise a series monitored at score >= 35",
        config_key="series_monitor_score_threshold",
        note="NOT re-anchored alongside the delete family — it gates the STUB "
             "population, which Group D v2 moved by only −2, and exactly ZERO stubs "
             "cross 35 either way. See the block comment on pilot_min_watchability."),
    # ── monitor (30) ─────────────────────────────────────────────────────────
    ThresholdSpec(
        name="movie_monitor", bucket="monitor", constant=30, service="radarr",
        consumer="services/radarr/repair/anomaly.py:541",
        rule="re-monitor an owned but unmonitored movie at score >= 30",
        config_key="owned_monitor_score_threshold",
        note="AXIS LEGACY (see the delete block) — never re-anchored, and must not be: "
             "anomaly.py's _score_owned axis did not move under Group D v2. "
             "schema default 30; the in-code fallback if config is absent is 35"),
    # ══════════════════════════════════════════════════════════════════════════
    # THE DELETE FAMILY SPANS **TWO DIFFERENT SCORE AXES**. READ THIS BEFORE
    # "FIXING" ANY NUMBER BELOW TO MATCH ANY OTHER NUMBER BELOW.
    # ══════════════════════════════════════════════════════════════════════════
    # Every constant in this file is a point on "the 0-100 watchability axis" — except
    # there are two of them, produced by two different call paths, and they do NOT sit on
    # top of each other:
    #
    #   AXIS V2 ("the persisted axis") — the ``watchability_score`` column that
    #       ``refresh_scores`` writes. Computed by ``_build_score_map`` -> ``_score_row``,
    #       which passes the FULL input set: platform usage, transcode stats, the household
    #       ``transcode_profile`` (so Group D v2's negative D4 term is live), per-user
    #       affinity, kids/adult split, the related graph (C3), the person matrix (C4),
    #       real watch_count / percent_complete / last_watched_at.
    #       Read by: space_pressure's delete ceiling + downgrade band, the coordinator pool,
    #       Sonarr's series demote + TV ceiling, episode_files' restore leg.
    #
    #   AXIS LEGACY ("the anomaly axis") — recomputed on the fly by
    #       ``repair/anomaly.py::_score_owned``, which calls ``score_movie`` on the RAW
    #       Radarr dict with almost none of that: no ``transcode_profile`` (so Group D takes
    #       the v1 path), no ``platform_usage``/``transcode_stats``/``target_resolution``/
    #       ``video_codec`` (so D1 = 0.0, D3 = 0.0 and D2 falls into its "unknown codec"
    #       branch at a flat +2.0 — NOT the +12 the persisted path used to get), no C3, no
    #       C4, no per-user affinity, no audience split, and ``completion_pct`` hard-coded
    #       to 0.0.
    #       Read by: repair/anomaly.py's demote, restore, unmonitor and monitor legs.
    #
    # MEASURED, on this household's real cache (2,151 owned movies joined on tmdb_id):
    #
    #     axis     mean   sd    median  p95  p99  max   share below 17   share below 20
    #     -------  -----  ----  ------  ---  ---  ----  ---------------  --------------
    #     LEGACY    7.43  5.18     7     16   21   36        95.6%            97.8%
    #     V2        9.30  8.16     8     24   34   58        82.7%            89.2%
    #
    #     Pearson r(legacy, v2) = 0.659. At the values adopted below (anomaly floor 20 on
    #     LEGACY, delete ceiling 17 on V2) the two axes DISAGREE about delete-eligibility
    #     for 354 of those 2,151 movies (16.5%) — 349 that LEGACY calls below-floor and V2
    #     does not, 5 the other way. That gap is structural (missing C3/C4/engagement), NOT
    #     a threshold-value problem, and no single number closes it.
    #
    # SO: Group D v2 translated axis V2 down ~13 points and DID NOT MOVE axis LEGACY (whose
    # Group D term went from a flat +2.0 to a flat +2.0 — the v1 branch is unreachable in
    # the anomaly path's input set either way). A pass that re-anchored the whole family
    # 20 -> 17 therefore tightened four thresholds that had moved and four that had not.
    # The two groups below are split accordingly.
    #
    # ── delete family, AXIS V2 (17 — RE-ANCHORED from 20) ─────────────────────
    # WHY 17 AND NOT 20 on this axis. Group D v2 (SCORER_REVISION 4) replaced a
    # near-constant +12 bonus with a 0-to-negative transcode-risk penalty, so the median
    # owned movie moved 21 -> 8. A literal that stayed at 20 would silently have become a
    # far more aggressive delete ceiling than the one that was reviewed — the same class of
    # failure as leaving the quality ladder anchored on a pool of file-less stubs.
    #
    # 17 is the value that PRESERVES THE ORIGINAL SELECTIVITY, computed by reconstructing
    # the pre-Group-D distribution (each title's persisted ``_total_raw`` minus its v1
    # D1+D2+D3, re-clamped) and matching percentiles:
    #
    #   population                  share below 20      equivalent on the v2 axis
    #   --------------------------  ------------------  --------------------------
    #   movies, `standard` (1,997)  84.3%  (pre-D)      17.0   <- the value adopted
    #   movies, all instances       82.3%  (pre-D)      17.0
    #   series owning a file        88.3%  (pre-D)      18.0
    #   all series (incl. stubs)    94.9%  (pre-D)      18.0
    #
    # The v2 sub-family takes ONE value, so it takes the movie one: the brief's anchor is
    # stated in movie terms, and 17 is the SAFER of the two candidates (a lower delete
    # ceiling makes fewer titles eligible). For reference, the ceiling's selectivity had
    # already drifted hard in the other direction — with Group D v1 live, 20 admitted only
    # 39.0% of movies where the pre-D axis admitted 84.3%.
    #
    # NOT AN INVITATION TO ROUND IT BACK UP: re-derive it (same reconstruction) whenever
    # the score axis is translated again. The long-term replacement is ``ml.thresholds``.
    ThresholdSpec(
        name="movie_delete_ceiling", bucket="delete", constant=17, service="radarr",
        consumer="services/radarr/quality/space_pressure.py:247,1094,1279",
        rule="space-pressure may delete an unwatched movie below 17",
        config_key="space_pressure_score_ceiling",
        note="AXIS V2 — reads the persisted watchability_score column"),
    ThresholdSpec(
        name="tv_delete_ceiling", bucket="delete", constant=17, service="sonarr",
        consumer="services/sonarr/series/space_pressure.py:159",
        rule="space-pressure may delete TV below 17",
        config_key="tv_space_pressure_score_ceiling",
        note="AXIS V2 — reads the persisted watchability_score column"),
    ThresholdSpec(
        name="series_demote", bucket="delete", constant=17, service="sonarr",
        consumer="services/sonarr/series/quality.py:472",
        rule="dormant a series that stays below 17 for the dwell",
        config_key="series_demote_score_threshold",
        note="AXIS V2 — reads the persisted watchability_score column"),
    # ``series_restore`` — the Sonarr episode-restore floor. THE REASON THIS KEY EXISTS.
    # Until this spec was added, ``restore_recovered_episode_deletions`` read Radarr's
    # ``owned_restore_score_threshold``: ONE config key serving a LEGACY-axis consumer
    # (anomaly.py, wants 20) and a V2-axis consumer (this one, wants 17). No single value
    # is correct for both, so the key was SPLIT rather than compromised. Three things make
    # the split the right call rather than config bloat:
    #
    #   1. It restores the family's OWN naming convention. Every other member is already
    #      service-split — space_pressure_score_ceiling / tv_space_pressure_score_ceiling,
    #      owned_monitor_score_threshold / series_monitor_score_threshold,
    #      owned_demote_score_threshold / series_demote_score_threshold. The restore floor
    #      was the ONLY one shared across the two services, and it was shared by accident:
    #      the Sonarr twin was written later and reused the Radarr key's name.
    #   2. Each restore floor is welded to a DIFFERENT hysteresis partner, and the partners
    #      are already separate keys on separate axes. Radarr: deleted when
    #      score < owned_demote_score_threshold (LEGACY, 20), restored when
    #      score > owned_restore_score_threshold — set them apart and a movie scoring 18 is
    #      deleted every run and restored every run (owned_restore_min_age_days defaults to
    #      0, so nothing damps it). Sonarr: episodes are deleted by the COORDINATOR under
    #      tv_space_pressure_score_ceiling (V2, 17), so 17 is the partner value here, and
    #      floor == ceiling is thrash-free because both comparisons are strict (delete iff
    #      score < 17, restore iff score > 17 — a series at exactly 17 is neither).
    #   3. The invariant "a title's delete-eligibility must not depend on which code path
    #      scored it" is NOT weakened by the split: the two consumers act on DISJOINT entity
    #      types (Radarr movies vs Sonarr episodes), so no title is ever subject to both.
    #
    # MIGRATION: this key does NOT inherit ``owned_restore_score_threshold``. An existing
    # install's value for that key was chosen for the Radarr movie axis, and carrying it
    # onto the TV axis is precisely the bug being fixed. The blast radius of getting it
    # wrong is bounded — a restore floor only gates RE-ACQUISITION; it can never delete
    # anything — and the effective value is printed in the pass's own outcome table.
    ThresholdSpec(
        name="series_restore", bucket="delete", constant=17, service="sonarr",
        consumer="services/sonarr/cache/episode_files.py:2209",
        rule="re-acquire coordinator-deleted episodes whose series recovers above 17",
        config_key="tv_restore_score_threshold",
        note="AXIS V2 — reads the persisted watchability_score column. Split out of "
             "owned_restore_score_threshold, which serves the LEGACY-axis Radarr twin"),
    # ── delete family, AXIS LEGACY (20 — DELIBERATELY NOT RE-ANCHORED) ────────
    # These four read ``_score_owned``, which never sees a ``transcode_profile``, so Group
    # D v2 CANNOT reach them: their axis did not move, and re-anchoring them to 17 would
    # have tightened a floor against a distribution that never shifted. Reverted to 20/30.
    #
    # THE CORRECT END STATE IS NOT "PASS A transcode_profile HERE". Threading a profile and
    # the file facts into ``_score_owned`` would unify Group D and NOTHING ELSE — the path
    # would still be missing C3, C4, per-user affinity, the audience split and real
    # engagement, so the result would be a THIRD axis rather than axis V2. Real unification
    # means making the demote/restore legs READ the persisted ``watchability_score`` column
    # the way their Sonarr twins already do, which drops the live ``has_credits`` deferral
    # (today's "never delete on un-enriched affinity" guard) and makes the prune depend on
    # refresh_scores having run in the same process. That is a genuine behaviour change to
    # demote/restore/monitor and needs its own before/after count — it is tracked, not
    # smuggled in behind a threshold edit.
    #
    # MITIGATION THAT ALREADY EXISTS, and why this is not urgent: with
    # ``space_coordinator_enabled`` on, ``coordinator_owns_deletion`` makes
    # ``demote_stale_monitored``'s DELETE stage inert (``delete_active = pressure_active and
    # not coordinator_owns_deletion(...)``). On such an install the LEGACY axis can only
    # UNMONITOR — reversible, no data loss — while every actual file deletion goes through
    # the coordinator on axis V2.
    ThresholdSpec(
        name="movie_demote", bucket="delete", constant=20, service="radarr",
        consumer="services/radarr/repair/anomaly.py:793",
        rule="unmonitor, then eventually delete, an owned movie below 20",
        config_key="owned_demote_score_threshold",
        note="AXIS LEGACY — anomaly.py re-scores raw Radarr dicts with no "
             "transcode_profile, so Group D v2 never reaches it and 20 still means "
             "what it meant before SCORER_REVISION 4"),
    ThresholdSpec(
        name="movie_restore", bucket="delete", constant=20, service="radarr",
        consumer="services/radarr/repair/anomaly.py:1152",
        rule="re-acquire a deleted movie whose score recovers above 20",
        config_key="owned_restore_score_threshold",
        note="AXIS LEGACY. Hysteresis partner of movie_demote — it must NOT drop below "
             "that floor or a movie in the gap is deleted and restored every run"),
    ThresholdSpec(
        name="movie_unmonitor", bucket="delete", constant=20, service="radarr",
        consumer="services/radarr/repair/anomaly.py:1301",
        rule="unmonitor a monitored-but-missing movie below 20",
        note="AXIS LEGACY — same _score_owned path as movie_demote"),
    # ── DELIBERATELY LEFT AT 20 / 35 — DO NOT "FIX" THESE ─────────────────────
    # ``pilot_min_watchability`` (20) and ``series_monitor`` (35, in the acquire block
    # above) look like they belong to the family that just moved. THEY DO NOT, and the
    # reason is measurable: they act on the STUB population — the 7,600 of this
    # library's 11,973 series that own NO episode file — and the stub distribution was
    # never translated by Group D the way the file-owning one was.
    #
    #   * Under v1 a stub collected a flat +2.0 from D2's "unknown codec" branch (it has
    #     no codec), while a title that owns a file collected +12. So activating Group D
    #     moved the file-owning median 12 -> 21 and left the all-series median at 12.
    #   * Under v2 a stub scores exactly 0.0 on Group D (no file → every risk axis is
    #     unmeasurable → neutral). That is a −2 translation of the stub axis, against
    #     −13 for the file-owning one.
    #
    # MEASURED EFFECT OF THE −2 ON THE TWO STUB THRESHOLDS:
    #   pilot_min_watchability (20): stubs clearing it 134 -> 96  (−38, 0.50% of stubs)
    #   series_monitor (35):         stubs clearing it   0 ->  0  (no change at all)
    # Both are small enough that re-anchoring would be fitting to noise, and moving
    # them by the file-owning family's −3 would be plainly wrong: it would loosen a
    # gate whose population barely moved. Left at their reviewed values.
    ThresholdSpec(
        name="pilot_min_watchability", bucket="delete", constant=20, service="sonarr",
        consumer="services/sonarr/cache/episode_files.py:4797",
        rule="interactive-search a stub pilot only at score >= 20",
        config_key="pilot_interactive.min_watchability",
        note="acts on the STUB population, which Group D v2 moved by only −2 (134 -> 96 "
             "stubs clear it); deliberately NOT re-anchored with the delete family"),
    # ── uhd (75 live / 70 documented) ────────────────────────────────────────
    ThresholdSpec(
        name="uhd_dual", bucket="uhd", constant=75, service="radarr",
        consumer="services/radarr/repair/anomaly.py:1350",
        rule="a 4K-instance title warrants its 4K copy at score >= 75",
        config_key="routing.movies.4k_dual_min_score", falsy_means_default=True,
        note="the only UHD gate that compares the WATCHABILITY score; the "
             "shipped config value 0 means 'unset' (`cfg or DEFAULT_UHD_SCORE`), "
             "so the effective constant is 75, NOT 70"),

    # ── reported, NOT routed ─────────────────────────────────────────────────
    ThresholdSpec(
        name="movie_search", bucket="acquire", constant=60, service="radarr",
        consumer="services/radarr/repair/anomaly.py:1277", routed=False,
        rule="actively search a monitored-but-missing movie at score >= 60",
        note="no target bucket owns 60 — it is the mid-rung of a three-way "
             "routing decision (search / adjust-and-search / unmonitor), not a "
             "binary act-or-not cutoff; deriving it needs the per-tier "
             "storage-cost break-even of §7, not one probability"),
    # REMOVED: "uhd_universe" (SCORE_4K_THRESHOLD = 70). The constant was dead —
    # no reader anywhere — and has been deleted from universe.py. The live 4K gate
    # for the universe upgrade path is space.universe_quality.upgrade_target, which
    # tests WATCH LIKELIHOOD against watch_likelihood.uhd_cutoff (default 75).
    # Deriving it is future work: it needs a calibrator fit on the likelihood
    # scale, not on watchability_score, so it is deliberately NOT listed here as a
    # watchability-scale threshold (listing it produced a shadow row that compared
    # two different scales).
    ThresholdSpec(
        name="uhd_acquire", bucket="uhd", constant=75, service="radarr",
        consumer="services/acquisition/resolver.py:452", routed=False,
        rule="emit a companion 4K copy for a NEW acquisition at score >= 75",
        config_key="routing.movies.4k_dual_min_score", falsy_means_default=True,
        note="compares the ACQUISITION score of an unowned candidate — a "
             "different quantity from watchability_score, and unowned titles "
             "never enter the snapshot store the calibrator is fit on"),
    ThresholdSpec(
        name="uhd_reconcile", bucket="uhd", constant=75, service="radarr",
        consumer="services/routing/uhd_reconcile.py:432,496,737", routed=False,
        rule="proactively acquire a 4K copy at watch-likelihood >= 75",
        config_key="routing.movies.4k_dual_min_score", falsy_means_default=True,
        note="compares watch_likelihood, not watchability_score — routing a "
             "watchability-calibrated cutoff onto a different scale would be "
             "silently wrong"),
    ThresholdSpec(
        name="quality_ladder", bucket="acquire", constant=35, service="radarr",
        consumer="machine_learning/scoring/_shared.py:QUALITY_PROFILE_THRESHOLDS",
        routed=False, config_key="scoring.quality_ladder",
        rule="score -> quality profile ladder (62/46/41/36/35/0)",
        note="a six-rung ladder, not one cutoff; §9 maps it to calibrated "
             "P-bands WITH per-tier storage-cost break-evens (§7), so one "
             "probability per rung is the wrong shape — tracked separately. "
             "The constant here is the ladder's ENTRY rung (>=1080p). The rungs "
             "are now PERCENTILE-CALIBRATED to the real title-level distribution "
             "(p99.9/p99.5/p99/p98/p97) rather than absolute guesses — the old "
             "80/70 rungs were above the observed maximum, so 4K was unreachable. "
             "That calibration is household-specific and is exactly what this "
             "registry exists to replace with calibrated probabilities."),
    ThresholdSpec(
        name="downgrade_protect", bucket="delete", constant=6, service="radarr",
        consumer="services/radarr/quality/space_pressure.py:86", routed=False,
        rule="protect a movie from step-down at score >= 6",
        note="already overridden at runtime by space_pressure_score_ceiling "
             "when the widen-band flag is on; routing both would double-derive "
             "the same decision"),
    ThresholdSpec(
        name="mal_min_watchability", bucket="delete", constant=20, service="sonarr",
        consumer="services/calendar/__init__.py:164", routed=False,
        rule="surface an unowned MAL calendar entry at score >= 20",
        config_key="calendar.mal_min_watchability",
        note="scores UNOWNED titles, which never enter the snapshot store — the "
             "calibrator is fit on owned-library rows and does not cover them. Also "
             "NOT re-anchored with the delete family: an unowned title has no file, so "
             "Group D v2 scores it a hard 0.0 exactly as it does a pilot STUB (a −2 "
             "shift, not the file-owning population's −13)"),
)

SPEC_BY_NAME = {s.name: s for s in THRESHOLD_SPECS}
ROUTED_SPECS = tuple(s for s in THRESHOLD_SPECS if s.routed)


# ── config ────────────────────────────────────────────────────────────────────

def _cfg_get(config, key, default=None):
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    try:
        v = getter(key, default)
    except Exception:
        return default
    return default if v is None else v


def threshold_config(config) -> dict:
    """The ``ml.thresholds`` block (``thresholds`` at top level is accepted as an
    alias). Always a dict — a malformed block reads as empty, i.e. defaults."""
    ml = _cfg_get(config, "ml", {}) or {}
    blk = ml.get("thresholds", {}) if isinstance(ml, dict) else {}
    if not isinstance(blk, dict) or not blk:
        alias = _cfg_get(config, "thresholds", {}) or {}
        if isinstance(alias, dict) and ("mode" in alias or "targets" in alias
                                        or "horizon_days" in alias):
            blk = alias
    return blk if isinstance(blk, dict) else {}


def threshold_mode(config) -> str:
    """``ml.thresholds.mode`` — DEFAULT ``"shadow"``. An unrecognised value reads
    as the default (a typo must never arm ``derived``)."""
    mode = str(threshold_config(config).get("mode", DEFAULT_MODE) or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def horizon_days(config) -> int:
    try:
        v = int(threshold_config(config).get("horizon_days", DEFAULT_HORIZON_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS
    return v if v > 0 else DEFAULT_HORIZON_DAYS


def max_fit_days(config) -> int:
    """``ml.thresholds.max_fit_days`` — how far back the calibrator fit reaches.

    The label join walks rows in Python, so an append-only snapshot store makes
    the end-of-run cost grow without bound (one row per entity per day, forever).
    A year of matured labels is far more than the §10 gate asks for and bounds
    the work; ``0`` means unbounded for anyone who wants the whole history."""
    try:
        v = int(threshold_config(config).get("max_fit_days", DEFAULT_MAX_FIT_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FIT_DAYS
    return v if v >= 0 else DEFAULT_MAX_FIT_DAYS


def shrinkage_k(config) -> float:
    """``ml.thresholds.shrinkage_k`` — see :data:`DEFAULT_SHRINKAGE_K`.

    A non-positive or non-numeric value reads as the default: ``k <= 0`` would
    mean "believe the calibrator completely at one positive", which is exactly
    the failure mode the shrinkage exists to prevent. (``derive.shrinkage_weight``
    still honours k<=0 if a caller passes it explicitly — this accessor simply
    refuses to configure it.)"""
    try:
        v = float(threshold_config(config).get("shrinkage_k", DEFAULT_SHRINKAGE_K))
    except (TypeError, ValueError):
        return DEFAULT_SHRINKAGE_K
    return v if v > 0 else DEFAULT_SHRINKAGE_K


def include_backfill(config) -> bool:
    """``ml.thresholds.include_backfill`` — DEFAULT TRUE
    (:data:`DEFAULT_INCLUDE_BACKFILL` documents why this one differs from every
    other ML tool)."""
    v = threshold_config(config).get("include_backfill", DEFAULT_INCLUDE_BACKFILL)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on", "y"}
    try:
        return bool(v)
    except Exception:
        return DEFAULT_INCLUDE_BACKFILL


def resolve_constant(spec: ThresholdSpec, config) -> float:
    """The value the consumer of *spec* actually uses today: its deployment
    override (``spec.config_key``, dotted keys walked) or its literal.

    This is the report's "current" column, so it has to agree with the consumer
    exactly — including that a consumer reading a config key gets the config
    value, not the spec's documentation of the default."""
    key = spec.config_key
    if key:
        node = config
        for part in str(key).split("."):
            if node is None:
                break
            node = _cfg_get(node, part, None) if not isinstance(node, dict) \
                else node.get(part)
        if node is not None and not isinstance(node, (dict, list, bool)):
            try:
                v = float(node)
            except (TypeError, ValueError):
                v = None
            # A consumer written as ``cfg.get(k) or DEFAULT`` treats 0/"" as
            # "unset" — the report's "current" column must agree with it.
            if v is not None and not (spec.falsy_means_default and v == 0.0):
                return v
    return float(spec.constant)


def target_probabilities(config) -> dict:
    """``ml.thresholds.targets.{acquire,monitor,delete,uhd}`` over
    :data:`DEFAULT_TARGET_P`. Out-of-range or non-numeric entries fall back to
    the default for that bucket (a probability outside (0, 1] is not a target)."""
    out = dict(DEFAULT_TARGET_P)
    raw = threshold_config(config).get("targets", {})
    if isinstance(raw, dict):
        for bucket in BUCKETS:
            if bucket not in raw:
                continue
            try:
                v = float(raw[bucket])
            except (TypeError, ValueError):
                continue
            if 0.0 < v <= 1.0:
                out[bucket] = v
    return out


# ── the derived-value store ───────────────────────────────────────────────────

_DERIVED: dict = {}        # name -> float
_DERIVED_META: dict = {}   # {"source": ..., "generated_at": ..., "gates": {...}}
_PRIMED = False
_WARNED: set = set()


def prime(values: dict, *, source: str = "in-process", meta: "dict | None" = None) -> None:
    """Install derived cutoffs for this process (the report layer and tests use
    this). ``None`` values mean "gated out" and fall back to the literal."""
    global _PRIMED
    _DERIVED.clear()
    _DERIVED.update({str(k): v for k, v in (values or {}).items()})
    _DERIVED_META.clear()
    _DERIVED_META.update({"source": source, **(meta or {})})
    _PRIMED = True


def clear() -> None:
    """Forget everything primed/loaded (tests; also resets the warn-once set)."""
    global _PRIMED
    _DERIVED.clear()
    _DERIVED_META.clear()
    _WARNED.clear()
    _PRIMED = False


def derived_snapshot() -> dict:
    """``{"values": {...}, "meta": {...}, "primed": bool}`` — for the report."""
    return {"values": dict(_DERIVED), "meta": dict(_DERIVED_META), "primed": _PRIMED}


def _default_base_dir():
    from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
    return CacheKeyBuilder().base_dir


def latest_report_path(base_dir=None) -> "Path | None":
    """Newest ``<cache>/ml/reports/thresholds_*.json``, or None."""
    try:
        d = Path(base_dir if base_dir is not None else _default_base_dir()) / "ml" / "reports"
        paths = sorted(d.glob(_REPORT_GLOB))
        return paths[-1] if paths else None
    except Exception:
        return None


def _load_from_report(base_dir=None) -> None:
    """Lazy prime from the last committed audit JSON (mode="derived" only).

    Reads the **effective** (shrunk) column, which is the whole point: a report
    written when the household had 38 positives hands consumers a cutoff a fifth
    of the way from their constant, not the raw inverted one. Reports written
    before shrinkage existed carry only ``derived``/``gate_ok``, so those are
    read as the effective value when ``effective`` is absent."""
    values: dict = {}
    meta: dict = {"source": "none"}
    path = latest_report_path(base_dir)
    if path is not None:
        try:
            blob = json.loads(Path(path).read_text(encoding="utf-8"))
            for row in (blob.get("thresholds") or []):
                name = row.get("name")
                if not name:
                    continue
                if "effective" in row:
                    # No derived term → the effective value IS the constant, and
                    # the consumer's own literal is the authoritative copy of it.
                    values[str(name)] = (row.get("effective")
                                         if row.get("derived") is not None else None)
                else:
                    values[str(name)] = row.get("derived") if row.get("gate_ok") else None
            meta = {"source": str(path), "generated_at": blob.get("generated_at"),
                    "horizon_days": blob.get("horizon_days")}
        except Exception:
            values, meta = {}, {"source": f"{path} (unreadable)"}
    prime(values, source=meta.pop("source", "none"), meta=meta)


def _warn_once(logger, key: str, msg: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        (getattr(logger, "log_warning", None) or (lambda _m: None))(msg)
    except Exception:
        pass


# ── the accessor ──────────────────────────────────────────────────────────────

def get_threshold(name: str, config, default, *, logger=None, base_dir=None):
    """The cutoff *name* should use, given *config* — ``default`` being the
    caller's own fully-resolved literal/config value.

    * mode ``"shadow"`` / ``"off"`` (and any unknown mode): returns ``default``
      **unchanged and unconverted** — the same object, so a routed consumer is
      indistinguishable from the pre-registry code.
    * mode ``"derived"``: returns the EFFECTIVE cutoff — the calibrated value
      already shrunk toward this same constant by ``w = n_pos/(n_pos+k)`` — when
      one exists. With no derived term at all it returns ``default`` and logs
      ONE warning per threshold per process saying so. An ``int`` default yields
      an ``int`` (rounded) so downstream formatting/typing is unchanged.

      At ``n_pos = 0`` the two branches agree by construction: the effective
      value would be the constant exactly, and the ``None`` fallback returns the
      caller's own object rather than a float copy of it, so a cold install is
      byte-identical to ``"shadow"``.
    """
    mode = threshold_mode(config)
    if mode != MODE_DERIVED:
        return default

    if not _PRIMED:
        _load_from_report(base_dir)

    if name not in _DERIVED:
        _warn_once(logger, f"{name}:missing",
                   f"[Thresholds] mode=derived but '{name}' has no derived value "
                   f"({_DERIVED_META.get('source', 'no report')}) — using the "
                   f"configured value {default}. Run once in mode=shadow to "
                   "produce <cache>/ml/reports/thresholds_*.json.")
        return default

    value = _DERIVED.get(name)
    if value is None:
        spec = SPEC_BY_NAME.get(name)
        _warn_once(logger, f"{name}:gated",
                   f"[Thresholds] mode=derived but '{name}'"
                   f"{f' ({spec.rule})' if spec else ''} has no derived value "
                   "(the calibrator could not be fit — no matured labels, too "
                   f"few positives, or a degenerate map) — using the configured "
                   f"value {default}, which is exactly what a shrinkage weight "
                   "of 0 would have produced. See the run's threshold table.")
        return default

    try:
        v = float(value)
    except (TypeError, ValueError):
        _warn_once(logger, f"{name}:bad",
                   f"[Thresholds] derived value for '{name}' is not numeric "
                   f"({value!r}) — using the configured value {default}.")
        return default
    if isinstance(default, bool):
        return default
    if isinstance(default, int):
        return int(round(v))
    return v


# ── env escape hatch for the report layer ─────────────────────────────────────

def report_enabled(config) -> bool:
    """The end-of-run derivation + audit JSON runs in every mode except
    ``"off"``. ``GLIDEARR_THRESHOLDS_OFF=1`` force-disables it (ops kill switch,
    same spelling style as the other consent/kill env vars)."""
    if str(os.environ.get("GLIDEARR_THRESHOLDS_OFF", "")).strip().lower() in {
            "1", "true", "yes", "on", "y"}:
        return False
    return threshold_mode(config) != MODE_OFF

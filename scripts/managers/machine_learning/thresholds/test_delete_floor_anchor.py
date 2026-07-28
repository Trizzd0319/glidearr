"""thresholds/test_delete_floor_anchor.py — the DELETE family sits on TWO score axes.
================================================================================
Group D v2 (SCORER_REVISION 4) replaced a near-constant +12 Group-D bonus with a
0-to-negative transcode-risk penalty and TRANSLATED the persisted 0-100 watchability
axis: the median owned movie moved 21 → 8. Every absolute threshold on THAT axis had to
move with it or silently change meaning — which is exactly how the delete ceiling had
already drifted from admitting 84.3% of movies (pre-Group-D) to 39.0% (Group D v1)
without one line of code changing.

But not every delete threshold reads that axis, and the ones that don't must NOT move.

  AXIS V2 — the persisted ``watchability_score`` column (``refresh_scores`` →
      ``_build_score_map`` → ``_score_row``, which passes the household
      ``transcode_profile``, the related graph, the person matrix, per-user affinity and
      real engagement). Translated by Group D v2. Floor re-anchored 20 → 17.

  AXIS LEGACY — recomputed per-run by ``repair/anomaly.py::_score_owned``, which calls
      ``score_movie`` on the raw Radarr dict with NO ``transcode_profile`` (so Group D
      takes its v1 branch) and no ``platform_usage`` / ``target_resolution`` /
      ``video_codec`` (so D1 = D3 = 0.0 and D2 sits on its flat +2.0 unknown-codec
      branch) — and no C3, no C4, no per-user affinity, no audience split, with
      ``completion_pct`` hard-coded to 0.0. Group D v2 cannot reach it. NOT re-anchored.

Measured on the real cache (2,151 owned movies joined on tmdb_id): LEGACY mean 7.43 /
max 36 against V2 mean 9.30 / max 58, Pearson r = 0.659, and at the landed values the
two disagree about delete-eligibility for 354 of them (16.5%). One number cannot serve
both, which is why ``owned_restore_score_threshold`` was SPLIT rather than compromised.

Four things are pinned here. The last two are the ones a future reader is most likely
to break, because on the surface they look like inconsistencies to tidy up.
"""
from __future__ import annotations

import inspect
import re

from scripts.managers.machine_learning.thresholds.registry import (
    SPEC_BY_NAME,
    THRESHOLD_SPECS,
)


def spec_for(name):
    return SPEC_BY_NAME[name]


#: The value the V2-axis thresholds share, derived by reconstructing the pre-Group-D
#: distribution (each title's persisted ``_total_raw`` minus its v1 D1+D2+D3, re-clamped)
#: and matching percentiles on the v2 axis. See the derivation table in registry.py.
DELETE_FLOOR_V2 = 17

#: The value the LEGACY-axis thresholds keep. Not a stale leftover — their axis never
#: moved, so 20 still means on it exactly what it meant before SCORER_REVISION 4.
DELETE_FLOOR_LEGACY = 20

#: Every threshold that reads the PERSISTED watchability_score column.
V2_AXIS = ("movie_delete_ceiling", "tv_delete_ceiling", "series_demote", "series_restore")

#: Every threshold that reads anomaly.py's ``_score_owned`` recomputation.
LEGACY_AXIS = ("movie_demote", "movie_restore", "movie_unmonitor")

#: The stub-population gates. They moved by −2, not −13, because a file-less series has
#: no file to be transcode-risky about.
STUB_ANCHORED = {"pilot_min_watchability": 20, "series_monitor": 35}

#: The other two delete-BUCKET thresholds, both ``routed=False`` (reported, never acted
#: on through the registry), and both deliberately unmoved:
#:   * ``downgrade_protect`` (6) — a step-down guard, not a delete gate, and already
#:     overridden at runtime by ``space_pressure_score_ceiling`` whenever the widen-band
#:     flag is on. Moving it would double-derive the same decision.
#:   * ``mal_min_watchability`` (20) — scores UNOWNED MAL calendar entries. An unowned
#:     title has no file, so Group D v2 gives it a hard 0.0 exactly as it gives a pilot
#:     stub one: the same −2 shift, the same "do not re-anchor" conclusion.
UNMOVED_DELETE_BUCKET = {"downgrade_protect": 6, "mal_min_watchability": 20}


# ── 1. each axis is internally consistent ─────────────────────────────────────

def test_the_v2_axis_family_moved_together():
    for name in V2_AXIS:
        assert spec_for(name).constant == DELETE_FLOOR_V2, name


def test_the_legacy_axis_family_did_not_move():
    """DO NOT "re-anchor" these to 17 to match the block above. Group D v2 never reaches
    the path that produces their scores, so 17 would tighten a floor against a
    distribution that never shifted."""
    for name in LEGACY_AXIS:
        assert spec_for(name).constant == DELETE_FLOOR_LEGACY, name
    assert spec_for("movie_monitor").constant == 30       # the legacy axis' promote rung


def test_every_delete_spec_names_its_axis():
    """The axis is the single most load-bearing fact about each of these constants, so it
    is recorded ON the spec — a reader meets it before they meet the inconsistency."""
    for name in V2_AXIS:
        assert "AXIS V2" in (spec_for(name).note or ""), name
    for name in LEGACY_AXIS + ("movie_monitor",):
        assert "AXIS LEGACY" in (spec_for(name).note or ""), name


# ── 2. hysteresis partners ────────────────────────────────────────────────────

def test_hysteresis_partners_share_one_floor_on_each_axis():
    """Deletion fires at ``score < floor`` and restore at ``score > floor``. Set the two
    apart in the wrong direction and every title in the gap is deleted one run and
    re-acquired the next, forever (owned_restore_min_age_days defaults to 0)."""
    # Radarr movies, AXIS LEGACY: demote floor vs restore floor.
    assert spec_for("movie_demote").constant == spec_for("movie_restore").constant
    # Sonarr episodes, AXIS V2: the coordinator's TV delete ceiling vs the restore floor.
    # Equal is thrash-free here because BOTH comparisons are strict — a series at exactly
    # 17 is neither deleted nor restored.
    assert spec_for("tv_delete_ceiling").constant == spec_for("series_restore").constant


def test_movies_and_tv_share_one_delete_ceiling():
    """The space coordinator ranks movies and TV into ONE pool; two different ceilings
    would make the pool's ordering depend on which service a title came from."""
    assert spec_for("movie_delete_ceiling").constant == spec_for("tv_delete_ceiling").constant


# ── 3. THE SPLIT ITSELF ───────────────────────────────────────────────────────

def test_the_two_restore_floors_are_separate_config_keys():
    """``owned_restore_score_threshold`` used to be read by BOTH anomaly.py (AXIS LEGACY,
    wants 20) and episode_files.py (AXIS V2, wants 17) — one key, two axes, no correct
    value. Re-merging them re-opens that. The split also restores the family's own
    convention: every other member is already service-split (space_pressure_score_ceiling
    / tv_space_pressure_score_ceiling, owned_* / series_*)."""
    movie, series = spec_for("movie_restore"), spec_for("series_restore")
    assert movie.config_key == "owned_restore_score_threshold"
    assert series.config_key == "tv_restore_score_threshold"
    assert movie.config_key != series.config_key
    assert movie.service == "radarr" and series.service == "sonarr"


def test_the_split_is_visible_to_the_shadow_report():
    """Both legs must be routed specs, or the derivation report covers one axis and
    silently ignores the other."""
    assert spec_for("series_restore").routed is True
    assert spec_for("movie_restore").routed is True
    assert spec_for("series_restore").bucket == spec_for("movie_restore").bucket == "delete"


def test_the_two_restore_consumers_act_on_disjoint_entity_types():
    """The invariant 'a title's delete-eligibility must not depend on which code path
    scored it' survives the split because no title is subject to both legs: one acts on
    Radarr movies, the other on Sonarr episodes."""
    assert "radarr" in spec_for("movie_restore").consumer
    assert "sonarr" in spec_for("series_restore").consumer


def test_the_new_key_ships_with_a_schema_default():
    from scripts.managers.factories.onboarding.schema import empty_config
    skeleton = empty_config()
    assert skeleton["tv_restore_score_threshold"] == DELETE_FLOOR_V2
    # ...and the legacy key is still there, still on its own axis' value.
    assert skeleton["owned_restore_score_threshold"] == DELETE_FLOOR_LEGACY
    assert skeleton["owned_demote_score_threshold"] == DELETE_FLOOR_LEGACY
    assert skeleton["space_pressure_score_ceiling"] == DELETE_FLOOR_V2
    assert skeleton["tv_space_pressure_score_ceiling"] == DELETE_FLOOR_V2


# ── 4. the axis split is grounded in the CODE, not just in these constants ────

def test_anomaly_still_scores_without_a_transcode_profile():
    """THE TRIPWIRE. The whole two-axis split rests on one code fact: ``_score_owned``
    passes no ``transcode_profile``, so Group D v2 cannot reach it. If someone threads one
    in, the LEGACY thresholds above are instantly measuring a different distribution and
    every constant in this file has to be re-derived. Fail loudly at that moment rather
    than silently deleting against a floor that no longer means what it says.

    (And note what threading a profile would NOT achieve: ``_score_owned`` would still be
    missing C3, C4, per-user affinity, the audience split and real engagement, so it would
    become a THIRD axis, not axis V2. Real unification means reading the persisted column
    — a behaviour change that needs its own before/after count.)"""
    from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager

    src = inspect.getsource(RadarrRepairAnomalyManager._score_owned)
    assert "score_movie(" in src
    assert "transcode_profile" not in src, (
        "_score_owned now passes a transcode_profile — the LEGACY delete thresholds "
        "(movie_demote/movie_restore/movie_unmonitor/movie_monitor) are no longer on an "
        "untranslated axis and MUST be re-derived. See registry.py's delete block."
    )
    for missing in ("platform_usage", "transcode_stats", "target_resolution", "video_codec"):
        assert missing not in src, f"_score_owned now passes {missing} — Group D v1 is live for it"


def test_the_v2_consumers_read_the_persisted_column():
    """The other half of the same fact: the V2-axis consumers compare against the stored
    ``watchability_score``, which is what Group D v2 translated."""
    from scripts.managers.services.sonarr.cache.episode_files import (
        SonarrCacheEpisodeFilesManager,
    )

    src = inspect.getsource(SonarrCacheEpisodeFilesManager.restore_recovered_episode_deletions)
    assert "watchability_score" in src
    assert 'get("tv_restore_score_threshold"' in src
    # The prose may (and does) NAME the Radarr key to explain the split; what must never
    # come back is a READ of it here.
    assert 'get("owned_restore_score_threshold"' not in src, (
        "the Sonarr restore leg is reading Radarr's key again — that is the exact "
        "one-key-two-axes collision this split exists to close"
    )


def test_only_the_radarr_leg_reads_the_radarr_restore_key():
    """Belt-and-braces on the split: ``owned_restore_score_threshold`` has exactly ONE
    consumer that reads it out of config, and it is the Radarr one. (Declaring it —
    schema/env_map/registry — is fine; reading it anywhere else is the collision.)"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]      # scripts/managers
    readers = []
    for path in root.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r'get\(\s*["\']owned_restore_score_threshold["\']', text):
            readers.append(path.relative_to(root).as_posix())
    assert readers == ["services/radarr/repair/anomaly.py"], readers


# ── 5. exhaustiveness + the stub gates ────────────────────────────────────────

def test_the_stub_thresholds_were_deliberately_left_alone():
    """DO NOT "fix" these to match the delete family. They act on the file-less pilot
    stubs, whose Group-D contribution moved by −2 (a flat +2.0 "unknown codec" credit
    under v1 → a hard 0.0 under v2), not by the −13 the file-owning population saw.
    Measured: 134 → 96 stubs clear 20; 0 → 0 clear 35."""
    for name, expected in STUB_ANCHORED.items():
        assert spec_for(name).constant == expected, name
    assert spec_for("pilot_min_watchability").constant != DELETE_FLOOR_V2
    for name in STUB_ANCHORED:
        note = (spec_for(name).note or "").lower()
        assert "stub" in note, f"{name} must carry the stub-population rationale"


def test_no_delete_bucket_threshold_was_missed():
    """Every delete-bucket threshold is accounted for on exactly one axis, or as
    deliberately unmoved. Asserted EXHAUSTIVELY rather than by enumerating the ones we
    happened to remember: a new one added later and left on the wrong axis would be a
    silent behaviour change."""
    delete_bucket = {s.name for s in THRESHOLD_SPECS if s.bucket == "delete"}
    accounted = (set(V2_AXIS) | set(LEGACY_AXIS) | {"pilot_min_watchability"}
                 | set(UNMOVED_DELETE_BUCKET))
    assert delete_bucket == accounted


def test_the_unmoved_delete_bucket_thresholds_are_pinned_with_reasons():
    for name, expected in UNMOVED_DELETE_BUCKET.items():
        spec = spec_for(name)
        assert spec.constant == expected, name
        assert spec.routed is False, f"{name} is unrouted — moving it needs a rethink"
        assert spec.note, f"{name} must say why it stayed put"


def test_the_floor_is_below_the_ladders_lowest_hd_rung():
    """Sanity: a title the delete pass may remove must not simultaneously be one the
    quality ladder wants to hold at 1080p."""
    from scripts.managers.machine_learning.scoring._shared import QUALITY_PROFILE_THRESHOLDS
    lowest_hd = min(t for t, label in QUALITY_PROFILE_THRESHOLDS if t > 0)
    assert DELETE_FLOOR_V2 < lowest_hd
    assert DELETE_FLOOR_LEGACY < lowest_hd

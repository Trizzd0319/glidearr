"""thresholds/test_installed_config_axis.py — the INSTALLED config must be axis-correct.
================================================================================
Every consumer reads its cutoff as ``config.get(key, LITERAL)``, so **the config wins and
the literal is only a fallback**. That is how a reviewed re-anchor can land in the code
and change nothing at all: the delete family was moved 20 -> 17 in the source while the
installed ``config.json`` still pinned 20, so the live run kept deleting on the LOOSER
value with no warning anywhere.

These checks close that gap for a real install. They are SKIPPED when there is no
``scripts/support/config/config.json`` (a fresh clone, CI), because the file is
deliberately gitignored — it holds credentials. Where one exists, it is checked against
the registry it is supposed to be implementing.

They assert values, not merely consistency, on purpose: an operator is free to tune these
knobs, but a value that silently differs from the reviewed constant is exactly the failure
this file exists to make loud. If you are deliberately re-tuning, move the spec constant
too — that is the artefact reviewers read.
"""
from __future__ import annotations

import json

import pytest

from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH
from scripts.managers.machine_learning.thresholds.registry import SPEC_BY_NAME

#: name -> config key, for every threshold whose value drives a DELETE (or the restore
#: that partners one). Split by axis exactly as registry.py splits it.
V2_AXIS_KEYS = {
    "movie_delete_ceiling": "space_pressure_score_ceiling",
    "tv_delete_ceiling":    "tv_space_pressure_score_ceiling",
    "series_demote":        "series_demote_score_threshold",
    "series_restore":       "tv_restore_score_threshold",
}
LEGACY_AXIS_KEYS = {
    "movie_demote":  "owned_demote_score_threshold",
    "movie_restore": "owned_restore_score_threshold",
    "movie_monitor": "owned_monitor_score_threshold",
}


def _installed():
    if not CONFIG_PATH.exists():
        pytest.skip(f"no installed config at {CONFIG_PATH} (fresh clone / CI)")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"installed config unreadable: {exc}")


@pytest.mark.parametrize("name,key", sorted(V2_AXIS_KEYS.items()))
def test_installed_v2_axis_keys_match_the_reanchored_constant(name, key):
    """These read the persisted ``watchability_score`` column — the axis Group D v2
    translated down ~13 points. A pinned 20 here is a delete ceiling ~3 points LOOSER
    than the one that was reviewed."""
    cfg = _installed()
    if key not in cfg:
        pytest.skip(f"{key} not pinned in the installed config (schema default applies)")
    assert cfg[key] == SPEC_BY_NAME[name].constant, (
        f"{key}={cfg[key]} in the installed config but the reviewed constant for "
        f"{name} is {SPEC_BY_NAME[name].constant}. Config beats the literal, so the "
        f"live run is using {cfg[key]}."
    )


@pytest.mark.parametrize("name,key", sorted(LEGACY_AXIS_KEYS.items()))
def test_installed_legacy_axis_keys_stayed_put(name, key):
    """These read anomaly.py's ``_score_owned`` recomputation, which Group D v2 never
    reaches. Re-anchoring them to 17 would tighten a floor against a distribution that
    never moved."""
    cfg = _installed()
    if key not in cfg:
        pytest.skip(f"{key} not pinned in the installed config (schema default applies)")
    assert cfg[key] == SPEC_BY_NAME[name].constant, (
        f"{key}={cfg[key]} in the installed config but the reviewed constant for "
        f"{name} is {SPEC_BY_NAME[name].constant}."
    )


def test_installed_hysteresis_pairs_do_not_invert():
    """A restore floor BELOW its delete floor deletes and re-acquires the same title
    every run. Checked on the resolved (config-or-default) values, because that is what
    the consumers actually compare against."""
    cfg = _installed()

    def val(name, key):
        return cfg.get(key, SPEC_BY_NAME[name].constant)

    # Radarr movies, AXIS LEGACY.
    assert val("movie_restore", "owned_restore_score_threshold") >= \
        val("movie_demote", "owned_demote_score_threshold")
    # Sonarr episodes, AXIS V2 (deleted by the coordinator under the TV ceiling).
    assert val("series_restore", "tv_restore_score_threshold") >= \
        val("tv_delete_ceiling", "tv_space_pressure_score_ceiling")


def test_installed_monitor_sits_above_its_demote_floor():
    """The sticky band: promote at the monitor threshold, act only below the demote
    floor. Inverted, a movie would be re-monitored and demoted on the same pass."""
    cfg = _installed()
    monitor = cfg.get("owned_monitor_score_threshold", SPEC_BY_NAME["movie_monitor"].constant)
    demote = cfg.get("owned_demote_score_threshold", SPEC_BY_NAME["movie_demote"].constant)
    assert monitor > demote

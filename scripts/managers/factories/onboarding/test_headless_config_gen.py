"""Docker/headless config-generation smoke test.

The whole point of the 6-phase reorg is that the headless container path still produces a
COMPLETE, valid config. With NO env vars, running all phases headlessly must not crash and
must leave the full feature skeleton intact (every leaf writes only its own keys from env;
the skeleton from deep_merge(empty_config(), {}) supplies the rest)."""
from __future__ import annotations

import os

from scripts.managers.factories.config.secret_store import SecretStore
from scripts.managers.factories.onboarding import schema
from scripts.managers.factories.onboarding.prompts import make_prompter
from scripts.managers.factories.onboarding.steps import build_steps


def test_headless_zero_env_runs_all_phases_and_keeps_skeleton_complete(monkeypatch):
    for k in list(os.environ):
        if k.startswith("RECOMMENDARR_"):
            monkeypatch.delenv(k, raising=False)

    p = make_prompter("headless", logger=None, secret_store=SecretStore())
    assert p.is_interactive is False

    cfg = schema.deep_merge(schema.empty_config(), {})
    ctx: dict = {"root_folders": []}
    phases = build_steps(logger=None)
    assert len(phases) == 6
    for phase in phases:
        phase.run(p, cfg, ctx)                 # per-child isolation means this never raises

    # Every feature block the operator might configure survives a zero-env headless run.
    for key in ("size_anomaly", "backup_before_destructive", "english_dub", "routing",
                "free_space_limit", "watch_likelihood", "daemons"):
        assert key in cfg, f"missing skeleton key: {key}"
    assert cfg["rootFolders"]["reality"] == ""
    assert set(cfg["english_dub"]) == {
        "cf_scoring", "theatrical_seek", "english_ladder", "lock_owned_dubs", "auto_enroll"}
    assert "playlists" in cfg["plex"]
    twih = cfg["plex"]["playlists"]["this_week_in_history"]
    assert twih["enabled"] is False and twih["cap"] == 7 and twih["opt_in_users"] == []
    assert cfg["size_anomaly"]["enabled"] is True
    assert cfg["backup_before_destructive"] is True

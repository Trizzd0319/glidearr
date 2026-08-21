"""ConfigManager's default config path must be ABSOLUTE (module-relative), not a bare
"support/config/config.json" that only resolves when the cwd happens to be scripts/. A run from
the repo root used to fall back to an empty config and log "Config file not found" once per manager
that loads config without an inherited one."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.managers.factories.config.__Init__ import ConfigManager, _DEFAULT_CONFIG


def test_default_config_path_is_absolute_under_scripts_support_config():
    assert _DEFAULT_CONFIG.is_absolute()
    assert _DEFAULT_CONFIG.parts[-3:] == ("support", "config", "config.json")
    # the package above support/ is the scripts package, NOT the repo root
    assert _DEFAULT_CONFIG.parents[2].name == "scripts"
    # The installed config does NOT ship in the repo - it is gitignored
    # (**/config/config.json), so it is absent on a fresh clone and in CI. The contract
    # this test exists for - absolute and module-relative - is asserted above and runs
    # everywhere; only the "and it is really installed" half needs the file. Same skip
    # thresholds/test_installed_config_axis already uses.
    if not _DEFAULT_CONFIG.exists():
        pytest.skip(f"no installed config at {_DEFAULT_CONFIG} (fresh clone / CI)")


def test_default_resolves_independently_of_cwd(monkeypatch, tmp_path):
    # the whole point: from any cwd (here a temp dir, the way the app runs from the repo root),
    # ConfigManager() still finds the real config instead of an empty {}.
    monkeypatch.chdir(tmp_path)
    assert not (Path("support") / "config" / "config.json").exists()   # bare-relative would miss
    cm = ConfigManager()
    assert cm.path == _DEFAULT_CONFIG
    # cwd-independence is proven by cm.path above and holds with or without an install;
    # only "loaded a non-empty config" needs the gitignored file.
    if not _DEFAULT_CONFIG.exists():
        pytest.skip(f"no installed config at {_DEFAULT_CONFIG} (fresh clone / CI)")
    assert cm.config                                        # loaded the real, non-empty config


def test_explicit_config_path_is_respected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"dry_run": True, "marker": 7}), encoding="utf-8")
    cm = ConfigManager(config_path=str(p))
    assert cm.path == Path(str(p))
    assert cm.config.get("marker") == 7

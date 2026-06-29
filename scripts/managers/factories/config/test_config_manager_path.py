"""ConfigManager's default config path must be ABSOLUTE (module-relative), not a bare
"support/config/config.json" that only resolves when the cwd happens to be scripts/. A run from
the repo root used to fall back to an empty config and log "Config file not found" once per manager
that loads config without an inherited one."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.managers.factories.config.__Init__ import ConfigManager, _DEFAULT_CONFIG


def test_default_config_path_is_absolute_under_scripts_support_config():
    assert _DEFAULT_CONFIG.is_absolute()
    assert _DEFAULT_CONFIG.parts[-3:] == ("support", "config", "config.json")
    # the package above support/ is the scripts package, NOT the repo root
    assert _DEFAULT_CONFIG.parents[2].name == "scripts"
    assert _DEFAULT_CONFIG.exists()                         # the real config ships in the repo


def test_default_resolves_independently_of_cwd(monkeypatch, tmp_path):
    # the whole point: from any cwd (here a temp dir, the way the app runs from the repo root),
    # ConfigManager() still finds the real config instead of an empty {}.
    monkeypatch.chdir(tmp_path)
    assert not (Path("support") / "config" / "config.json").exists()   # bare-relative would miss
    cm = ConfigManager()
    assert cm.path == _DEFAULT_CONFIG
    assert cm.config                                        # loaded the real, non-empty config


def test_explicit_config_path_is_respected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"dry_run": True, "marker": 7}), encoding="utf-8")
    cm = ConfigManager(config_path=str(p))
    assert cm.path == Path(str(p))
    assert cm.config.get("marker") == 7

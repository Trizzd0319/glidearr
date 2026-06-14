"""SonarrInstanceManager.run() idempotency — validation may be hoisted to startup
(Main._validate_service_apis) AND remains in the Phase-2 component loop, so a second
call (apis already populated) must no-op instead of re-bootstrapping."""
from __future__ import annotations

from scripts.managers.services.sonarr.instance import SonarrInstanceManager


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _bare():
    m = SonarrInstanceManager.__new__(SonarrInstanceManager)
    m.logger = _Log()
    m.all_components_loaded = None
    return m


def test_run_noops_when_already_validated():
    m = _bare()
    m.sonarr_apis = {"sonarr": object()}          # already validated
    hit = {"bootstrap": False}
    m._credential_bootstrap = lambda: hit.__setitem__("bootstrap", True) or True
    m.run()
    assert hit["bootstrap"] is False              # short-circuited before any work


def test_run_proceeds_when_not_yet_validated():
    m = _bare()
    m.sonarr_apis = {}                            # not validated → must run
    hit = {"bootstrap": False}

    def _boot():
        hit["bootstrap"] = True
        return False                             # abort right after, we only assert entry

    m._credential_bootstrap = _boot
    m.run()
    assert hit["bootstrap"] is True

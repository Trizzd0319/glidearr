"""
MALInstanceManager — validate/refresh the MAL OAuth token at runtime.
================================================================================
Mirrors ``TraktInstanceManager`` but for MAL: confirm credentials, refresh an
expired token (initial authorization is handled by onboarding, not here), and
resolve the username. Refresh + username reuse the shared ``onboarding.oauth``
MAL helpers. Never raises — returns False so MALManager can skip gracefully.
"""
from __future__ import annotations

import re
import time

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.onboarding import oauth
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class MALInstanceManager(BaseManager, ComponentManagerMixin):
    parent_name = "MALManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "MALManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

    @staticmethod
    def _expired(auth: dict) -> bool:
        try:
            created = int(auth.get("created_at", 0))
            life = int(auth.get("expires_in", 0))
        except (TypeError, ValueError):
            return True
        if not created or not life:
            return True
        return (int(time.time()) - created) > life

    @LoggerManager().log_function_entry
    @timeit("register_and_validate")
    def register_and_validate(self) -> bool:
        mal = (self.config.get("mal", {}) if self.config else {}) or {}
        client_id = mal.get("client_id", "")
        if not client_id:
            self.logger.log_debug("[MALInstance] no client_id configured — MAL disabled.")
            return False

        auth = mal.get("authorization", {}) or {}
        if not auth.get("access_token") or self._expired(auth):
            self.logger.log_debug("[MALInstance] token missing/expired — refreshing.")
            new = oauth.mal_refresh_token(client_id, mal.get("client_secret", ""),
                                          auth.get("refresh_token", ""), logger=self.logger)
            if not new:
                self.logger.log_warning(
                    "[MALInstance] no valid MAL token (run onboarding to authorize) — skipping MAL.")
                return False
            mal["authorization"] = new
            auth = new
            self.config.set("mal", mal)

        self._ensure_username(mal)
        self.logger.log_debug(f"[MALInstanceManager] OK: {mal.get('username') or '?'}")
        return True

    def _ensure_username(self, mal: dict) -> str:
        existing = mal.get("username")
        if existing and _USERNAME_RE.match(str(existing)):
            return existing
        token = (mal.get("authorization") or {}).get("access_token", "")
        name = oauth.mal_fetch_username(token, logger=self.logger)
        if name and _USERNAME_RE.match(str(name)):
            mal["username"] = name
            self.config.set("mal", mal)
            return name
        return ""

    def prepare(self) -> None:
        pass

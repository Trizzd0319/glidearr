"""
TraktInstanceValidatorManager
==============================
Validates Trakt credentials and token health.

Provides:
- validate_keys()       — checks required config keys are present and token is live
- is_token_expired()    — pure timestamp check (no HTTP call)
- validate_or_refresh() — tries validate; falls back to a token refresh
"""
import time
import traceback

import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_BASE_URL = "https://api.trakt.tv"


class TraktInstanceValidatorManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        trakt_cfg = (self.config.get("trakt", {}) if self.config else {})
        token     = (trakt_cfg.get("authorization") or {}).get("access_token")
        self.logger.log_debug(f"[TraktValidator] access_token: {'set' if token else 'not set'}")

    # ── Public ────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("validate_keys")
    def validate_keys(self) -> bool:
        trakt = (self.config.get("trakt", {}) if self.config else {})
        token = trakt.get("authorization", {})

        required = {
            "client_id":     trakt.get("client_id"),
            "client_secret": trakt.get("client_secret"),
            "access_token":  token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
        }

        missing = [k for k, v in required.items() if not v]
        if missing:
            self.logger.log_warning(f"[TraktValidator] Missing credentials: {', '.join(missing)}")
            return False

        try:
            created  = int(token.get("created_at", 0))
            expires  = int(token.get("expires_in", 0))
            now      = int(time.time())
            age      = now - created
            self.logger.log_debug(
                f"[TraktValidator] Token age: {age}s of {expires}s "
                f"(created_at={created}, now={now})"
            )
        except Exception as e:
            self.logger.log_warning(f"[TraktValidator] Failed to compute token age: {e}")
            return False

        if self.is_token_expired(token):
            self.logger.log_warning("[TraktValidator] Token is expired.")
            return False

        return True

    @LoggerManager().log_function_entry
    @timeit("validate_or_refresh")
    def validate_or_refresh(self) -> bool:
        return self.validate_keys() or self._refresh_token()

    @LoggerManager().log_function_entry
    @timeit("validate")
    def validate(self) -> bool:
        return self.validate_or_refresh()

    # ── Token helpers ─────────────────────────────────────────────────────

    def is_token_expired(self, token: dict) -> bool:
        try:
            created  = int(token.get("created_at", 0))
            lifespan = int(token.get("expires_in", 7_776_000))
        except (TypeError, ValueError):
            return True

        now = int(time.time())
        if created > now:
            self.logger.log_warning("[TraktValidator] 'created_at' is in the future — treating as not expired.")
            return False

        return (now - created) > lifespan

    @LoggerManager().log_function_entry
    @timeit("_refresh_token")
    def _refresh_token(self) -> bool:
        try:
            trakt_cfg     = (self.config.get("trakt", {}) if self.config else {})
            client_id     = trakt_cfg.get("client_id", "")
            client_secret = trakt_cfg.get("client_secret", "")
            token_info    = trakt_cfg.get("authorization", {})
            refresh_token = token_info.get("refresh_token", "")

            if not all([client_id, client_secret, refresh_token]):
                self.logger.log_warning("[TraktValidator] Missing fields for token refresh.")
                return False

            self.logger.log_info("[TraktValidator] Attempting token refresh...")
            resp = requests.post(
                f"{_BASE_URL}/oauth/token",
                json={
                    "refresh_token": refresh_token,
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type":    "refresh_token",
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            new_auth = resp.json()

            if "access_token" not in new_auth:
                self.logger.log_error("[TraktValidator] Refresh response missing access_token.")
                return False

            updated_auth                = token_info.copy()
            updated_auth["access_token"]  = new_auth["access_token"]
            updated_auth["refresh_token"] = new_auth.get("refresh_token", refresh_token)
            updated_auth["created_at"]    = int(time.time())
            updated_auth["expires_in"]    = new_auth.get("expires_in", 7_776_000)

            trakt_cfg["authorization"] = updated_auth
            if self.config:
                self.config.set("trakt", trakt_cfg)

            self.logger.log_info("[TraktValidator] Token refreshed and saved.")
            return True

        except Exception as e:
            self.logger.log_error(f"[TraktValidator] Refresh error: {e}\n{traceback.format_exc()}")
            return False

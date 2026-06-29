"""
TraktInstanceManager
=====================
Validates and manages the Trakt OAuth token.

Inherits from BaseManager (not BaseInstanceManager, which is arrapi-specific).
Token refresh, device-flow auth, and username resolution are all consolidated
here so TraktManager.prepare() stays a simple one-liner.

Normalised to the same logging contract as Radarr/Sonarr instance managers:
  - All per-step noise goes to log_debug.
  - One summary line emitted via _finalize() matching BaseInstanceManager format.
  - auth_validator.py calls _check_trakt() independently before any manager
    is constructed, so this class never needs to repeat that work at INFO level.
"""
from __future__ import annotations

import re
import time

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.onboarding import oauth
from scripts.managers.services.trakt.instances.registrar import TraktInstanceRegistrarManager
from scripts.managers.services.trakt.instances.summary import TraktInstanceSummaryFormatterManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

# The Trakt username is externally controlled and later flows into cache file
# paths (trakt/<username>/...). Enforce a strict allow-list at the point of
# entry so it can never introduce path separators or traversal segments.
_USERNAME_RE   = re.compile(r"^[A-Za-z0-9_-]+$")


class TraktInstanceManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.load_summary: dict = {}

        _child_kw = dict(logger=self.logger, config=self.config, global_cache=self.global_cache,
                         validator=self.validator, registry=self.registry, manager=self,
                         dry_run=getattr(self, "dry_run", False))
        self.registrar         = TraktInstanceRegistrarManager(**_child_kw)
        self.summary_formatter = TraktInstanceSummaryFormatterManager(**_child_kw)

    # ── Public entry point ────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("register_and_validate")
    def register_and_validate(self) -> bool:
        """
        Validate config, refresh/obtain token if needed, resolve username.
        Emits one summary line matching the BaseInstanceManager format.
        """
        if not self.registrar.check_config():
            self.logger.log_error("[TraktInstance] Config registration failed.")
            self.load_summary["trakt"] = "❌"
            self._finalize(ok=False)
            return False

        trakt_cfg  = self.config.get("trakt", {})
        token_data = trakt_cfg.get("authorization", {})

        if not token_data or self._is_token_expired(token_data):
            self.logger.log_debug("[TraktInstance] token missing/expired — refreshing")
            if not self._refresh_token(trakt_cfg):
                self.logger.log_debug("[TraktInstance] refresh failed — starting device flow")
                if not self._run_device_flow(trakt_cfg):
                    self.load_summary["trakt"] = "❌"
                    self._finalize(ok=False)
                    return False
        else:
            self.logger.log_debug("[TraktInstance] token valid")

        self._ensure_username(trakt_cfg)
        self.load_summary["trakt"] = "✅"
        self._finalize(ok=True)
        return True

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _is_token_expired(self, token: dict) -> bool:
        try:
            created  = int(token.get("created_at", 0))
            lifespan = int(token.get("expires_in", 0))
        except (TypeError, ValueError):
            return True
        now = int(time.time())
        if created > now:
            self.logger.log_warning(
                "[TraktInstance] 'created_at' is in the future — treating as not expired."
            )
            return False
        return (now - created) > lifespan

    def _ensure_username(self, trakt_cfg: dict | None = None) -> str:
        """
        Fetch and persist the Trakt username if not already in config.
        Returns the username (or empty string on failure).
        """
        if trakt_cfg is None:
            trakt_cfg = self.config.get("trakt", {})
        existing = trakt_cfg.get("username")
        if existing:
            if _USERNAME_RE.match(str(existing)):
                return existing
            self.logger.log_warning(
                "[TraktInstance] Stored username failed validation — ignoring."
            )

        access_token = (trakt_cfg.get("authorization") or {}).get("access_token", "")
        client_id    = trakt_cfg.get("client_id", "")
        if not access_token or not client_id:
            return ""

        username = self._fetch_username(access_token, client_id)
        if username and not _USERNAME_RE.match(str(username)):
            self.logger.log_warning(
                "[TraktInstance] Fetched username failed validation — discarding."
            )
            return ""
        if username:
            trakt_cfg["username"] = username
            self.config.set("trakt", trakt_cfg)
            self.logger.log_debug(f"[TraktInstance] authenticated as: {username}")
        return username

    def _fetch_username(self, access_token: str, client_id: str) -> str:
        """Single /users/me call — returns username or empty string."""
        return oauth.fetch_username(access_token, client_id, logger=self.logger)

    # ── Token refresh ─────────────────────────────────────────────────────────

    def _refresh_token(self, trakt_cfg: dict) -> bool:
        client_id     = trakt_cfg.get("client_id", "")
        client_secret = trakt_cfg.get("client_secret", "")
        refresh_token = (trakt_cfg.get("authorization") or {}).get("refresh_token", "")

        if not all([client_id, client_secret, refresh_token]):
            self.logger.log_warning("[TraktInstance] Cannot refresh — missing credentials.")
            return False

        new_auth = oauth.refresh_token(client_id, client_secret, refresh_token, logger=self.logger)
        if not new_auth:
            return False
        trakt_cfg["authorization"] = new_auth
        self.config.set("trakt", trakt_cfg)
        self.logger.log_debug("[TraktInstance] token refreshed")
        # Resolve username with the new token
        self._ensure_username(trakt_cfg)
        return True

    # ── Device flow ───────────────────────────────────────────────────────────

    def _run_device_flow(self, trakt_cfg: dict) -> bool:
        client_id     = trakt_cfg.get("client_id", "")
        client_secret = trakt_cfg.get("client_secret", "")

        token = oauth.device_flow(
            client_id, client_secret,
            logger=self.logger,
            notice=lambda m: print(f"\n📺 {m}"),
        )
        if not token:
            return False
        trakt_cfg["authorization"] = token
        self.config.set("trakt", trakt_cfg)
        self.logger.log_debug("[TraktInstance] device flow authorized — token stored")
        self._ensure_username(trakt_cfg)
        return True

    # ── Finalization ──────────────────────────────────────────────────────────

    def _finalize(self, ok: bool) -> None:
        """
        Emit one summary line matching the BaseInstanceManager format:
            [TraktInstanceManager] ✅ 1/1: trakt✅
        """
        username = (self.config.get("trakt") or {}).get("username", "?")
        label    = username if ok else "?"
        status   = "✅" if ok else "❌"
        self.logger.log_debug(
            f"[{self.__class__.__name__}] {status} 1/1: {label}{status}"
        )

    def prepare(self) -> None:
        """No sub-components to prepare — validation runs in register_and_validate()."""
        pass

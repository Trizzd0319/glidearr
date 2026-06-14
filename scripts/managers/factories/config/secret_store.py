"""
SecretStore — keeps API keys / tokens out of plaintext ``config.json``.
================================================================================
Resolution order for a secret (first hit wins):

  1. Environment variable  ``RECOMMENDARR_<PATH>``
        12-factor / production: inject via Docker secrets, k8s, CI, or a cloud
        secret manager. e.g. trakt.client_secret -> RECOMMENDARR_TRAKT_CLIENT_SECRET
  2. OS keyring  (service="recommendarr", username=<dotted path>)
        Desktop: Windows Credential Manager / macOS Keychain / Linux Secret
        Service — encrypted at rest, per-user, never a readable file.
  3. (nothing) -> ``get`` returns None; the caller keeps whatever was already in
        config.json (supports gradual migration; an un-migrated secret still works).

This lets the in-memory config carry real secret values while ``config.json`` on
disk stores only structure with BLANK secret fields — so secrets are never
committed to git and never world-readable. Downstream code is unchanged: it still
reads ``config["trakt"]["client_secret"]`` / ``instance_config["api"]``; only the
*source* of the value changed.

The keyring dependency is OPTIONAL: if it (or a usable backend) is absent, the
store degrades to env-vars-only and never raises.
"""
from __future__ import annotations

import os
import re

# ── Optional keyring backend (degrade gracefully if missing) ──────────────────
try:
    import keyring as _keyring
    try:
        _kr = _keyring.get_keyring()
        # A "fail"/null backend means keyring is installed but unusable here.
        _KEYRING_OK = _kr is not None and "fail" not in _kr.__class__.__name__.lower()
    except Exception:
        _KEYRING_OK = False
except Exception:
    _keyring = None
    _KEYRING_OK = False

SERVICE = "recommendarr"
ENV_PREFIX = "RECOMMENDARR_"

# ── Which config keys hold secrets ────────────────────────────────────────────
# Substrings that unambiguously mark a credential-bearing key.
_SECRET_KEY_SUBSTR = (
    "apikey", "api_key", "client_secret", "client_id", "access_token",
    "refresh_token", "plex_token", "password", "secret", "webhook",
)
# Exact key names (avoids over-matching e.g. "mapping" -> "pin").
_SECRET_KEY_EXACT = {"token", "pin", "auth", "key", "api"}


def is_secret_key(key) -> bool:
    """True if a config key's value should be treated as a credential."""
    if not isinstance(key, str):
        return False
    kl = key.lower()
    return kl in _SECRET_KEY_EXACT or any(h in kl for h in _SECRET_KEY_SUBSTR)


def env_name(path: str) -> str:
    """Map a dotted config path to its env-var name (RECOMMENDARR_TRAKT_CLIENT_SECRET)."""
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", path).upper().strip("_")


def iter_secret_paths(obj, prefix: str = ""):
    """Yield (dotted_path, value) for every secret leaf in a config structure.

    Handles nesting and dict/list-of-instances:
      trakt.client_secret, trakt.authorization.access_token,
      radarr-standard.api, sonarr_instances.720.api, sonarr_instances.0.api
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                yield from iter_secret_paths(v, p)
            elif is_secret_key(k):
                yield p, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_secret_paths(v, f"{prefix}.{i}")


class SecretStore:
    """Read/write secrets from env vars (first) or the OS keyring (encrypted)."""

    def __init__(self, logger=None, service: str = SERVICE):
        self.logger = logger
        self.service = service
        self.keyring_ok = _KEYRING_OK

    def _log(self, level: str, msg: str):
        fn = getattr(self.logger, level, None) if self.logger is not None else None
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    # ── read ──────────────────────────────────────────────────────────────
    def get(self, path: str):
        """Resolve a secret by dotted path: env var, then keyring, then None."""
        env = os.environ.get(env_name(path))
        if env:
            return env
        if self.keyring_ok and _keyring is not None:
            try:
                v = _keyring.get_password(self.service, path)
                if v:
                    return v
            except Exception as e:
                self._log("log_debug", f"[SecretStore] keyring read failed for '{path}': {e}")
        return None

    # ── write ─────────────────────────────────────────────────────────────
    def set(self, path: str, value: str) -> bool:
        """Persist a secret to the keyring. Returns False if no backend."""
        if not value:
            return False
        if self.keyring_ok and _keyring is not None:
            try:
                _keyring.set_password(self.service, path, value)
                return True
            except Exception as e:
                self._log("log_warning", f"[SecretStore] keyring write failed for '{path}': {e}")
                return False
        self._log("log_warning",
                  f"[SecretStore] no keyring backend available — cannot persist '{path}'. "
                  f"Provide it via the env var {env_name(path)} instead.")
        return False

    def delete(self, path: str) -> bool:
        if self.keyring_ok and _keyring is not None:
            try:
                _keyring.delete_password(self.service, path)
                return True
            except Exception:
                return False
        return False

    def available(self) -> bool:
        """True if a usable keyring backend is present (env vars always work)."""
        return self.keyring_ok

    def backend_name(self) -> str:
        if self.keyring_ok and _keyring is not None:
            try:
                return _keyring.get_keyring().__class__.__name__
            except Exception:
                pass
        return "env-only (no keyring backend)"

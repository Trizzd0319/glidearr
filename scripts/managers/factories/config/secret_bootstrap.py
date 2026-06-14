"""
SecretBootstrap — first-run / reinstall detection + setup for the SecretStore.
================================================================================
On startup it AUDITS every secret the config expects and reports where each one
resolves from:

    env      → RECOMMENDARR_* environment variable (production / containers)
    keyring  → OS keyring (Windows Credential Manager / Keychain / Secret Service)
    inline   → still sitting in PLAINTEXT config.json (should be migrated)
    missing  → empty everywhere (needs to be provided)

Because the OS keyring persists across reinstalls, a *fresh* config.json on a
machine whose keyring still holds the secrets is detected as already-provisioned —
no re-prompting. If secrets are genuinely missing AND we're on an interactive
terminal AND this looks like a first run, it launches a getpass setup wizard and
stores the answers in the keyring. On a non-interactive host (CI / container) it
never prompts — it logs exactly which RECOMMENDARR_* env vars to set.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from scripts.managers.factories.config.secret_store import (
    iter_secret_paths, env_name,
)

# Keyring marker recording that interactive setup has already run (so we don't
# nag about optional secrets the user deliberately skipped on a later launch).
SENTINEL_PATH = "_bootstrap.completed"


class SecretBootstrap:
    def __init__(self, loader, logger=None):
        self.loader = loader
        self.store = loader._secret_store
        self.logger = logger or getattr(loader, "logger", None)

    def _log(self, level: str, msg: str):
        fn = getattr(self.logger, level, None) if self.logger is not None else None
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    # ── Detection ─────────────────────────────────────────────────────────────
    def audit(self, cfg) -> dict:
        """Map every secret path to its source: env | keyring | inline | missing."""
        result = {}
        for path, val in iter_secret_paths(cfg):
            if os.environ.get(env_name(path)):
                result[path] = "env"
            elif self.store.get(path):           # env already excluded above → keyring
                result[path] = "keyring"
            elif isinstance(val, str) and val:
                result[path] = "inline"
            else:
                result[path] = "missing"
        return result

    def provisioned(self) -> bool:
        """True if interactive setup has run before (sentinel in the keyring)."""
        try:
            return bool(self.store.get(SENTINEL_PATH))
        except Exception:
            return False

    def report(self, audit: dict) -> Counter:
        c = Counter(audit.values())
        self._log(
            "log_info",
            f"🔐 SecretStore [{self.store.backend_name()}]: "
            f"{c.get('keyring', 0)} keyring · {c.get('env', 0)} env · "
            f"{c.get('inline', 0)} plaintext · {c.get('missing', 0)} missing "
            f"(of {len(audit)})",
        )
        if c.get("inline"):
            self._log("log_warning",
                      f"⚠️ {c['inline']} secret(s) in PLAINTEXT config.json — run "
                      f"`python scripts/support/setup/migrate_secrets.py` to move them into the keyring.")
        return c

    # ── Setup ─────────────────────────────────────────────────────────────────
    def interactive_setup(self, cfg, paths) -> int:
        """Prompt (hidden) for each path and store it in the keyring. Returns count."""
        import getpass
        self._log("log_info",
                  f"🔧 Secret setup — {len(paths)} value(s) needed. Press Enter to skip an optional one.")
        stored = 0
        for p in paths:
            try:
                val = getpass.getpass(f"   {p}: ").strip()
            except (EOFError, KeyboardInterrupt):
                self._log("log_warning", "Secret setup interrupted — remaining values left unset.")
                break
            if val and self.store.set(p, val):
                stored += 1
        # Pull whatever we just stored into the live config + register for log scrubbing.
        try:
            self.loader._overlay_secrets(cfg)
        except Exception:
            pass
        self._log("log_info", f"✅ Stored {stored} secret(s) in the keyring.")
        return stored

    def ensure(self, cfg, interactive=None) -> dict:
        """Audit + report; run the wizard on a first-run interactive TTY; otherwise
        guide the operator. Returns the (possibly updated) audit."""
        audit = self.audit(cfg)
        self.report(audit)
        missing = [p for p, s in audit.items() if s == "missing"]
        if not missing:
            return audit

        if interactive is None:
            interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

        if interactive and not self.provisioned():
            self.interactive_setup(cfg, missing)
            try:
                self.store.set(SENTINEL_PATH, "1")   # mark setup complete
            except Exception:
                pass
            audit = self.audit(cfg)
            self.report(audit)
        elif not interactive:
            self._log("log_warning",
                      f"⚠️ {len(missing)} secret(s) missing and no interactive terminal — "
                      f"provide them as env vars: "
                      + ", ".join(env_name(p) for p in missing[:6])
                      + ("…" if len(missing) > 6 else ""))
        else:  # interactive but already provisioned → don't nag
            self._log("log_info",
                      f"ℹ️ {len(missing)} optional secret(s) unset — run "
                      f"`python scripts/support/setup/setup_secrets.py` to add them.")
        return audit

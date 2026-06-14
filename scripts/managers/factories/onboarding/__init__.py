"""
OnboardingManager — first-run / full-setup wizard for Recommendarr.
================================================================================
Establishes an entire setup in one flow: root folders, the number of
Sonarr/Radarr sessions + their API keys, Trakt OAuth (incl. token generation),
Tautulli, Plex, TVDB, MAL, genres, free-space limit, dry-run and Discord
notifications. Secrets are persisted to the OS keyring via the existing
ConfigLoader machinery; nothing secret is written to disk.

Runs two ways:
  * Standalone — ``python scripts/support/setup/onboarding.py`` (interactive or ``--headless``).
  * Auto first-run — ``OnboardingManager.run_if_needed()`` called from main.py
    before any service is constructed.

This module is kept import-light (steps are imported lazily inside ``run``) so the
runtime Trakt manager can ``import onboarding.oauth`` without pulling the wizard.
"""
from __future__ import annotations

import os
from pathlib import Path

from scripts.managers.factories.config.config_loader import ConfigLoader
from scripts.managers.factories.config.secret_bootstrap import SENTINEL_PATH
from scripts.managers.factories.onboarding import schema
from scripts.managers.factories.onboarding.prompts import bold, make_prompter
from scripts.support.utilities.logger.logger import LoggerManager

# This file: .../scripts/managers/factories/onboarding/__init__.py → scripts/ is parents[3].
_SCRIPTS_DIR = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _SCRIPTS_DIR / "support" / "config" / "config.json"


class OnboardingManager:
    def __init__(self, logger=None, config_path=None, loader=None, existing=None,
                 prompter=None, mode=None, reconfigure=False, only_service=None):
        self.logger = logger or LoggerManager()
        self.path = Path(config_path) if config_path else _DEFAULT_CONFIG
        self.loader = loader or ConfigLoader(self.path, logger=self.logger)
        self.store = self.loader._secret_store
        self._existing = existing if existing is not None else self.loader.load()
        self.prompter = prompter or make_prompter(mode, logger=self.logger, secret_store=self.store)
        self.reconfigure = reconfigure
        self.only_service = only_service

    # ── First-run detection ───────────────────────────────────────────────────
    @staticmethod
    def _config_dict(config) -> dict:
        if config is None:
            return {}
        if isinstance(config, dict):
            return config
        if hasattr(config, "raw_data"):
            return config.raw_data
        if hasattr(config, "config"):
            return config.config
        return {}

    @classmethod
    def needs_onboarding(cls, config) -> bool:
        """True only when setup hasn't completed AND the config looks empty.

        The flag alone is not enough: a fully-populated config may still carry
        ``firstRunCompleted: false`` (the shipped state), so we additionally require
        that no Sonarr/Radarr instance and no Trakt credentials are configured.
        This guarantees the auto first-run hook never fires on a real setup.
        """
        cfg = cls._config_dict(config)
        if not isinstance(cfg, dict) or not cfg:
            return True
        if cfg.get("firstRunCompleted"):
            return False
        return cls._looks_fresh(cfg)

    @staticmethod
    def _looks_fresh(cfg: dict) -> bool:
        def has_arr(key: str) -> bool:
            for name, inst in (cfg.get(key) or {}).items():
                if name == "default_instance" or not isinstance(inst, dict):
                    continue
                if inst.get("url") or inst.get("base_url"):
                    return True
            return False

        has_trakt = bool((cfg.get("trakt") or {}).get("client_id"))
        return not (has_arr("sonarr_instances") or has_arr("radarr_instances") or has_trakt)

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self) -> bool:
        from scripts.managers.factories.onboarding import steps as _steps
        from scripts.managers.factories.onboarding.steps.base import StepResult

        p = self.prompter
        p.notice(bold("Recommendarr onboarding"))
        p.notice(f"Mode: {'interactive' if p.is_interactive else 'headless (env-driven)'}  |  "
                 f"Config: {self.path}  |  SecretStore: {self.store.backend_name()}")

        cfg = schema.deep_merge(schema.empty_config(), self._existing or {})
        ctx: dict = {"root_folders": []}
        results: list = []

        try:
            for step in _steps.build_steps(logger=self.logger, only_service=self.only_service):
                try:
                    results.extend(step.run(p, cfg, ctx) or [])
                except (KeyboardInterrupt, EOFError):
                    raise
                except Exception as e:
                    self.logger.log_warning(f"[Onboarding] step '{step.name}' error: {e}")
                    results.append(StepResult(step.name, ok=False, detail=f"error: {e}"[:60]))
        except (KeyboardInterrupt, EOFError):
            # EOFError = forced-interactive with no usable stdin. Abort without
            # writing so we never persist a half/empty config or stamp completion.
            p.warn("\nOnboarding cancelled — no changes saved.")
            return False

        # Only claim setup is complete when the config is actually usable. A
        # headless run with no env values yields an empty skeleton; stamping
        # firstRunCompleted there would trap the user (auto-onboarding wouldn't
        # re-run). A single-service reconfigure never changes the flag.
        usable = not self._looks_fresh(cfg)
        if not self.only_service:
            cfg["firstRunCompleted"] = bool(usable)

        self._save(cfg)
        self._summary(results)

        missing = sorted(set(getattr(p, "missing_required", []) or []))
        if not p.is_interactive and missing:
            p.warn(f"[Onboarding] {len(missing)} required value(s) missing — set: " + ", ".join(missing))
        if not self.only_service and not usable:
            p.warn("[Onboarding] No usable configuration collected (no Sonarr/Radarr instances, no Trakt creds).")
            if not p.is_interactive:
                p.warn("[Onboarding] No interactive terminal detected — run "
                       "`python scripts/support/setup/onboarding.py --interactive` to set up, or provide RECOMMENDARR_* "
                       "env vars (see `python scripts/support/setup/onboarding.py --print-env-template`).")
            p.warn("[Onboarding] firstRunCompleted left false — setup will run again next launch.")
        return bool(usable)

    def _save(self, cfg: dict) -> None:
        try:
            self.loader.save(cfg)               # strips secrets → keyring/env, blanks on disk
            self.store.set(SENTINEL_PATH, "1")  # mark provisioned so SecretBootstrap won't re-prompt
            self.prompter.success(f"Configuration written to {self.path}")
        except Exception as e:
            self.logger.log_error(f"[Onboarding] failed to save config: {e}")

    def _summary(self, results: list) -> None:
        rows = [[r.icon, r.service, (r.detail or "")[:60]] for r in results]
        try:
            self.logger.log_table(["", "service", "detail"], rows, title="Onboarding summary")
        except Exception:
            for r in results:
                self.prompter.notice(f"  {r.icon} {r.service}: {r.detail}")

    # ── Auto first-run entry ──────────────────────────────────────────────────
    @classmethod
    def run_if_needed(cls, logger=None, config_path=None) -> str:
        """Run onboarding iff this looks like a fresh install. Honoured by main.py.

        Returns a status: ``"skipped"`` (already configured / bypassed), ``"ok"``
        (ran and produced a usable config), or ``"incomplete"`` (ran but collected
        nothing usable — e.g. headless with no env vars and no TTY).
        Set ``RECOMMENDARR_SKIP_ONBOARDING`` to bypass entirely.
        """
        if os.environ.get("RECOMMENDARR_SKIP_ONBOARDING"):
            return "skipped"
        logger = logger or LoggerManager()
        path = Path(config_path) if config_path else _DEFAULT_CONFIG
        loader = ConfigLoader(path, logger=logger)
        existing = loader.load()
        if not cls.needs_onboarding(existing):
            return "skipped"
        logger.log_info("[Onboarding] First run detected — launching setup.")
        usable = cls(logger=logger, config_path=str(path), loader=loader, existing=existing).run()
        return "ok" if usable else "incomplete"

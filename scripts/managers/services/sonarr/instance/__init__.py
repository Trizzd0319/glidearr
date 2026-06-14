from arrapi import SonarrAPI as ArrapiSonarrAPI

from scripts.managers.factories.base_instance_manager import BaseInstanceManager
from scripts.managers.services.sonarr.instance.updater import SonarrInstanceUpdaterManager
from scripts.managers.services.sonarr.repair.instance import (
    SonarrRepairInstanceManager,
    SonarrRepairInstanceCredentialsManager,
)
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrInstanceManager(BaseInstanceManager):

    # ── BaseInstanceManager interface ─────────────────────────────────────────
    def _api_class(self):           return ArrapiSonarrAPI
    def _config_key(self) -> str:   return "sonarr_instances"
    def _apis_attr(self) -> str:    return "sonarr_apis"
    def _service_name(self) -> str: return "Sonarr"

    # ── Init ──────────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager           = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        self.sonarr_apis  = {}
        self.load_summary = {}

        # dry_run arrives as a kwarg from SonarrManager, but BaseManager doesn't
        # capture it — store it here so the repair/updater children below inherit
        # it (otherwise they default to dry_run=False and could write live).
        self.dry_run = kwargs.get("dry_run", False)

        shared = dict(
            logger=self.logger, config=self.config,
            global_cache=self.global_cache, validator=self.validator,
            registry=self.registry, sonarr_cache=self.sonarr_cache, manager=self,
            dry_run=self.dry_run,
        )
        self.repair  = SonarrRepairInstanceManager(**shared)
        self.updater = SonarrInstanceUpdaterManager(**shared)

        # Expose as canonical API ref
        self.sonarr_api = self

    # ── run (deferred validation — called during SonarrManager.prepare) ───────

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        # Idempotent: validation is hoisted to startup (Main._validate_service_apis)
        # so a validated client exists for EVERY phase, but this is also still in
        # SonarrManager's Phase-2 component loop. Once the API set is populated we
        # skip — one validation serves all phases. A failed run leaves it empty and
        # retries.
        if self.sonarr_apis:
            return
        if not self._credential_bootstrap():
            self.logger.log_error("[SonarrInstance] Aborting — credential bootstrap failed.")
            self.all_components_loaded = False
            return

        self.repair.run()

        raw_instances = self.config.get("sonarr_instances", {})
        corrected     = self.updater.apply_corrections({n: "success" for n in raw_instances})

        for name, cfg in corrected.items():
            if name == "default_instance":
                continue
            result = self._process_instance(name, cfg)
            if result == "recovered":
                self._confirm_and_clear_failed_flag(name, cfg)

        self._finalize(
            service_name="Sonarr",
            flag_key="sonarr.instance_manager_initialized",
        )

    # ── Credential bootstrap (Sonarr-specific) ────────────────────────────────

    def _credential_bootstrap(self) -> bool:
        try:
            audit  = SonarrRepairInstanceCredentialsManager(
                logger=self.logger, config=self.config,
                global_cache=self.global_cache, validator=self.validator,
                registry=self.registry, manager=self,
            )
            result = audit.run(mode="bootstrap")
            return result.get("success", False)
        except Exception as e:
            self.logger.log_error(f"[SonarrInstance] Credential bootstrap error: {e}")
            return False

    # ── Instance resolution ───────────────────────────────────────────────────

    def get_all_sonarr_apis(self):              return self.sonarr_apis
    def get_sonarr_api(self, name):             return self.sonarr_apis.get(name)
    def get_all_instance_names(self):           return list(self.sonarr_apis)

    def get_default_api(self):
        if len(self.sonarr_apis) == 1:
            return next(iter(self.sonarr_apis.values()))
        default_name = (self.get_default_instance() or {}).get("name")
        return self.sonarr_apis.get(default_name)

    def get_default_instance(self) -> dict:
        instances    = self.config.get("sonarr_instances", {})
        default_name = instances.get("default_instance")
        if isinstance(default_name, str) and default_name:
            return {"name": default_name}
        if self.sonarr_apis:
            return {"name": next(iter(self.sonarr_apis))}
        for name, cfg in instances.items():
            if name != "default_instance" and isinstance(cfg, dict):
                return {"name": name}
        return {}

    def resolve_instance(self, name=None) -> str:
        if isinstance(name, str) and name:
            return name
        return (self.get_default_instance() or {}).get("name")

    def get_client(self, instance_name):
        return self.get_sonarr_api(self.resolve_instance(instance_name))

    def set_sonarr_cache(self, cache_manager):
        self.sonarr_cache = cache_manager
        for sub in (getattr(self, "repair", None), getattr(self, "updater", None)):
            if sub and hasattr(sub, "set_sonarr_cache"):
                sub.set_sonarr_cache(cache_manager)

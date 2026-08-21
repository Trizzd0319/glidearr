from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.sizing import quality_caps
from scripts.support.utilities.backup_gate import effective_dry_run


class SonarrCacheQualityManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr quality and profile caches.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrCache"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        manager = kwargs.get("manager") or {}

        # ✅ Dual-cache setup
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, self.global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def refresh_quality_profiles(self, instance):
        profiles = self.sonarr_api.get_quality_profiles(instance)
        if profiles:
            self.global_cache.set(f"sonarr/{instance}/quality_profiles.json", profiles)
            self.logger.log_info(f"✅ Refreshed quality profiles cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality profiles retrieved for {instance}")

    def get_quality_profiles(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/quality_profiles.json", default=[])

    def refresh_custom_formats(self, instance):
        formats = self.sonarr_api.get_custom_formats(instance)
        if formats:
            self.global_cache.set(f"sonarr/{instance}/custom_formats.json", formats)
            self.logger.log_info(f"✅ Refreshed custom formats cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No custom formats retrieved for {instance}")

    def get_custom_formats(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/custom_formats.json", default=[])

    def refresh_quality_definitions(self, instance):
        definitions = self.sonarr_api.get_quality_definitions(instance)
        if definitions:
            self.global_cache.set(f"sonarr/{instance}/quality_definitions.json", definitions)
            self.logger.log_info(f"✅ Refreshed quality definitions cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality definitions retrieved for {instance}")

    def get_quality_definitions(self, instance):
        return self.global_cache.get(f"sonarr/{instance}/quality_definitions.json", default=[])

    def log_quality_summary(self, instance):
        profiles = self.get_quality_profiles(instance)
        formats = self.get_custom_formats(instance)
        definitions = self.get_quality_definitions(instance)

        self.logger.log_info(f"📊 Quality Summary for {instance}:")
        self.logger.log_info(f" - Profiles: {len(profiles)} entries")
        self.logger.log_info(f" - Custom Formats: {len(formats)} entries")
        self.logger.log_info(f" - Quality Definitions: {len(definitions)} entries")

    # ── grab-time size ceiling (GLD-SON-21) ─────────────────────────────
    _EPISODE_RUNTIME_HINT = 42.0   # display only: a typical drama episode, so the
                                   # "~GiB" column is checkable against a search page

    def apply_size_caps(self, instance):
        """Sonarr half of the grab-time size ceiling (``GLD-SON-21``).

        Byte-for-byte the same logic as Radarr's adapter: MiB/min is a BITRATE, so a
        42-minute episode and a 162-minute feature are priced by identical arithmetic.
        Only the runtime LABEL on the diff table differs, and it is presentational --
        the value written is always the rate, never the product.

        The planning, rendering and write-accounting all live in ``sizing/quality_caps``
        precisely so this cannot drift from Radarr's copy. The two services already
        keep two of everything around quality definitions (different cache-key formats,
        different fetch calls, different summary wording) and those pairs HAVE drifted;
        a second diff table and write loop would have drifted the same way.

        Closes the same P-A as Radarr: definitions were fetched and cached every run
        and consumed only by the summary line's ``len()``.
        """
        _none = {"proposed": 0, "skipped": 0, "applied": 0, "would": 0, "failed": 0}
        cfg = quality_caps.config_for((self.config or {}).get("acquisition", {}) or {})
        if not cfg.get("enabled"):
            return _none
        definitions = self.get_quality_definitions(instance) or []
        if not definitions:
            self.logger.log_debug(
                f"[QualityCaps] sonarr/{instance}: no cached quality definitions - skipped.")
            return _none
        try:
            payload = self.global_cache.get("size_model/calibration") if self.global_cache else None
        except Exception as e:
            self.logger.log_warning(
                f"[QualityCaps] sonarr/{instance}: calibration unreadable ({e}) - "
                f"no caps derived this run.")
            return _none
        measured = quality_caps.measured_from_calibration(payload)
        if not measured:
            self.logger.log_debug(
                f"[QualityCaps] sonarr/{instance}: no measured rates yet - skipped.")
            return _none

        def _put(defn):
            if not self.sonarr_api:
                return None
            return self.sonarr_api._make_request(
                instance, f"qualitydefinition/{int(defn.get('id'))}",
                method="PUT", payload=defn, fallback=None)

        stats = quality_caps.push_caps(
            definitions=definitions, measured=measured, cfg=cfg,
            runtime_minutes=self._EPISODE_RUNTIME_HINT, put=_put, logger=self.logger,
            dry_run=effective_dry_run(self.dry_run, self.global_cache),
            label=f"sonarr/{instance}")
        if stats["applied"]:
            self.refresh_quality_definitions(instance)
        return stats

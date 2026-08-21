from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.sizing import quality_caps
from scripts.support.utilities.backup_gate import effective_dry_run


class RadarrQualityCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr quality profiles, custom formats, and definitions.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrCacheManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # SLASH-delimited, per-instance keys that READER and WRITER agree on. The old
    # pair never reconciled: refresh wrote "radarr.quality_profiles.<inst>" /
    # "radarr.custom_formats.<inst>" while the getters read
    # "radarr.<inst>.quality.profiles" / "...custom_formats" — different files, so
    # a read never saw what a refresh had written. The custom-format key matches
    # the live managers (radarr/custom_formats/<inst>) so they share one cache.
    # compressed= is dropped: GlobalCacheManager.set doesn't accept it (it would
    # TypeError and the write would never happen) and compression is a no-op anyway.
    def refresh_quality_profiles(self, instance):
        profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) if self.radarr_api else []
        if profiles:
            self.global_cache.set(f"radarr/quality_profiles/{instance}", profiles)
            self.logger.log_info(f"✅ Cached quality profiles for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No quality profiles for {instance}")

    def refresh_custom_formats(self, instance):
        formats = self.radarr_api._make_request(instance, "customformat", fallback=[]) if self.radarr_api else []
        if formats:
            self.global_cache.set(f"radarr/custom_formats/{instance}", formats)
            self.logger.log_info(f"✅ Cached custom formats for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No custom formats for {instance}")

    def get_quality_profiles(self, instance):
        return self.global_cache.get(f"radarr/quality_profiles/{instance}", default=[])

    def get_custom_formats(self, instance):
        return self.global_cache.get(f"radarr/custom_formats/{instance}", default=[])

    def refresh_quality_definitions(self, instance):
        try:
            definitions = self.radarr_api._make_request(instance, "qualitydefinition", fallback=[]) if self.radarr_api else []
            if definitions:
                self.global_cache.set(f"radarr.{instance}.quality.definitions", definitions)
                self.logger.log_info(f"✅ Cached {len(definitions)} quality definitions for {instance}")
            else:
                self.logger.log_warning(f"⚠️ No quality definitions retrieved for {instance}")
        except Exception as e:
            self.logger.log_error(f"❌ Failed to refresh quality definitions for {instance}: {e}")

    def get_quality_definitions(self, instance):
        return self.global_cache.get(f"radarr.{instance}.quality.definitions", default=[])

    def log_quality_summary(self, instance):
        profiles = self.get_quality_profiles(instance)
        formats = self.get_custom_formats(instance)
        definitions = self.get_quality_definitions(instance)

        self.logger.log_info(f"📊 Radarr Quality Summary for {instance}:")
        self.logger.log_info(f" • Profiles: {len(profiles)}")
        self.logger.log_info(f" • Custom Formats: {len(formats)}")
        self.logger.log_info(f" • Definitions: {len(definitions)}")

    # ── grab-time size ceiling (GLD-RAD-34) ───────────────────────────────
    _MOVIE_RUNTIME_HINT = 162.0    # display only: a long feature, so the "~GiB"
                                   # column is checkable against a search page

    def apply_size_caps(self, instance):
        """Derive a maxSize ceiling per quality tier from this library's OWN measured
        MiB/min and write it to Radarr's quality definitions (``GLD-RAD-34``).

        Closes a P-A that sat in this file for its whole life: ``refresh_quality_definitions``
        fetched and cached the definitions every run and the ONLY consumer was the
        summary line's ``len()``. The exact data a grab-time cap needs was already
        being pulled -- and used for a count.

        All planning, rendering and write-accounting live in ``sizing/quality_caps``
        so Sonarr runs the identical path; this method is the adapter, nothing more.
        Measured rates come from the persisted ``size_model/calibration`` payload the
        calibrator already produces each run, so there is no parquet read here.

        Gated on ``effective_dry_run``: this writes *arr CONFIGURATION, which outlives
        the run and governs every future grab, including ones Glidearr never initiates.
        """
        _none = {"proposed": 0, "skipped": 0, "applied": 0, "would": 0, "failed": 0}
        cfg = quality_caps.config_for((self.config or {}).get("acquisition", {}) or {})
        if not cfg.get("enabled"):
            return _none
        definitions = self.get_quality_definitions(instance) or []
        if not definitions:
            self.logger.log_debug(
                f"[QualityCaps] radarr/{instance}: no cached quality definitions - skipped.")
            return _none
        try:
            payload = self.global_cache.get("size_model/calibration") if self.global_cache else None
        except Exception as e:
            self.logger.log_warning(
                f"[QualityCaps] radarr/{instance}: calibration unreadable ({e}) - "
                f"no caps derived this run.")
            return _none
        measured = quality_caps.measured_from_calibration(payload)
        if not measured:
            # No measured library => no evidence => no ceiling. Deliberately NOT falling
            # back to the static cold-start table: those numbers describe SOME library,
            # not THIS one, and a cap derived from them could starve a tier that
            # legitimately runs hotter than the seed.
            self.logger.log_debug(
                f"[QualityCaps] radarr/{instance}: no measured rates yet - skipped.")
            return _none

        def _put(defn):
            if not self.radarr_api:
                return None
            return self.radarr_api._make_request(
                instance, f"qualitydefinition/{int(defn.get('id'))}",
                method="PUT", payload=defn, fallback=None)

        stats = quality_caps.push_caps(
            definitions=definitions, measured=measured, cfg=cfg,
            runtime_minutes=self._MOVIE_RUNTIME_HINT, put=_put, logger=self.logger,
            dry_run=effective_dry_run(self.dry_run, self.global_cache),
            label=f"radarr/{instance}")
        if stats["applied"]:
            # The cache now disagrees with Radarr; refetch so the next reader sees truth.
            self.refresh_quality_definitions(instance)
        return stats

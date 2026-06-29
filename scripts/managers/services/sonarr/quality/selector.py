from datetime import timezone, datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrQualitySelectorManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrQuality"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        # every public method calls self.instance_manager.resolve_instance(...) and several use
        # self.key_builder — resolve both here (kwargs → manager → parent) or they AttributeError.
        self.instance_manager = (kwargs.get("instance_manager")
                                 or getattr(self.manager, "instance_manager", None)
                                 or getattr(parent, "instance_manager", None))
        self.key_builder = (kwargs.get("key_builder")
                            or getattr(self.manager, "key_builder", None)
                            or getattr(parent, "key_builder", None))
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {self.__class__.__name__} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")


    @LoggerManager().log_function_entry
    @timeit("request_quality_change")
    def request_quality_change(self, series_title, season, episode, resolution, instance, decision):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        self.logger.log_info(f"🔁 Requesting {decision} for {series_title} S{season}E{episode} in {resolved_instance}")
        episode_id = self.sonarr_api.get_episode_id(series_title, season, episode, resolved_instance)

        if not episode_id:
            self.logger.log_warning(f"⚠️ Episode not found for {series_title} S{season}E{episode}")
            return False

        new_profile_id = self.compare_profiles_for_series(resolved_instance, series_title, season, episode)
        if not new_profile_id:
            self.logger.log_warning("⚠️ No suitable profile found.")
            return False

        result = self.sonarr_api._make_request(
            resolved_instance,
            f"episode/{episode_id}",
            method="PUT",
            payload={"qualityProfileId": new_profile_id}
        )

        if result:
            self.logger.log_info(f"✅ Quality profile updated for {series_title} S{season}E{episode}.")
            return True
        else:
            self.logger.log_warning("❌ Failed to update quality profile.")
            return False

    @LoggerManager().log_function_entry
    @timeit("compare_profiles_for_series")
    def compare_profiles_for_series(self, instance, series_title, season, episode):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profiles = self.manager.get_quality_profiles(resolved_instance)
        cf_scores = self.manager.get_custom_format_scores(resolved_instance)
        penalties = self.manager.ml_manager.get_transcode_history(series_title)

        profile_data = []
        for profile in profiles:
            profile_id = profile["id"]
            profile_name = profile["name"]

            if not self._is_valid_profile(profile_name, resolved_instance):
                continue

            cf_score = cf_scores.get(profile_id, 0)
            penalty = penalties.get(series_title, 0)
            final_score = cf_score - penalty
            cf_count = len(cf_scores.get(profile_id, {})) if isinstance(cf_scores.get(profile_id), dict) else 0

            profile_data.append((profile_id, profile_name, cf_score, penalty, cf_count, final_score))

        profile_data.sort(key=lambda x: (x[5], x[4]), reverse=True)

        self.logger.log_table(
            ["ID", "Name", "CF Score", "Penalty", "CF Count", "Final Score"],
            profile_data,
            title=f"📊 Profile Comparison: {series_title} S{season}E{episode}"
        )

        return profile_data[0][0] if profile_data else self.manager.get_default_quality_profile(resolved_instance)

    @LoggerManager().log_function_entry
    @timeit("get_best_quality_profile_ai")
    def get_best_quality_profile_ai(self, instance, series_title):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profiles = self.manager.get_quality_profiles(resolved_instance)
        cf_scores = self.manager.get_custom_format_scores(resolved_instance)
        ai_predictions = self.manager.ml_manager.predict_best_quality_profile(series_title)

        best_profile, best_score = None, float("-inf")
        for profile in profiles:
            name = profile["name"]
            profile_id = profile["id"]

            cf_score = cf_scores.get(name, 0)
            ai_score = ai_predictions.get(name, 0)
            final_score = cf_score + ai_score

            if final_score > best_score:
                best_score = final_score
                best_profile = profile_id

        return best_profile

    @LoggerManager().log_function_entry
    @timeit("get_quality_profiles")
    def get_quality_profiles(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        profiles = self.sonarr_api._make_request(resolved_instance, "qualityprofile") or []
        return profiles

    @LoggerManager().log_function_entry
    @timeit("_is_valid_profile")
    def _is_valid_profile(self, profile_name: str, instance: str) -> bool:
        resolved_instance = self.instance_manager.resolve_instance(instance)
        if not profile_name or not resolved_instance:
            return False

        if self.config.get("ignore_resolution_check", False):
            self.logger.log_debug("⚠️ Skipping resolution validation due to config override.")
            return True

        fallback_names = {"default", "unknown"}
        if profile_name.strip().lower() in fallback_names:
            self.logger.log_debug(f"🚫 Skipping fallback profile '{profile_name}' in {resolved_instance}.")
            return False

        # Single (un-tiered) instance: the instance name carries no resolution marker
        # (e.g. the collapsed 'sonarr'), so there is no per-instance resolution gate —
        # per-episode JIT governs quality. Accept any real profile, including 'Any'.
        if not any(m in resolved_instance.lower() for m in ("4k", "2160", "1080", "720")):
            return True

        if profile_name.strip().lower() == "any":
            if "4k" in resolved_instance.lower() or "2160" in resolved_instance:
                self.logger.log_debug(f"✔️ Allowing 'Any' profile under 4K instance: {resolved_instance}")
                return True
            else:
                self.logger.log_debug(f"🚫 'Any' profile disallowed for non-4K instance: {resolved_instance}")
                return False

        if not hasattr(self, "_cached_profiles"):
            self._cached_profiles = {}
        if resolved_instance not in self._cached_profiles:
            self._cached_profiles[resolved_instance] = self.manager.get_quality_profiles(resolved_instance)

        profiles = self._cached_profiles[resolved_instance]

        target_res = "2160" if "4k" in resolved_instance else "1080" if "1080" in resolved_instance else "720"
        resolution_patterns = self.config.get("resolution_patterns", {
            "720": ["720p"],
            "1080": ["1080p"],
            "2160": ["2160p", "4k"]
        })

        valid_patterns = resolution_patterns.get(target_res, [])
        profile = next((p for p in profiles if p["name"].lower() == profile_name.lower()), None)

        if not profile:
            self.logger.log_debug(f"🚫 Profile '{profile_name}' not found in {resolved_instance}.")
            return False

        allowed_qualities = profile.get("items") or profile.get("qualities") or []
        for q in allowed_qualities:
            quality_name = (q.get("quality") or {}).get("name", "").lower()
            allowed = q.get("allowed", False)
            if allowed and any(pat in quality_name for pat in [r.lower() for r in valid_patterns]):
                return True

        self.logger.log_debug(
            f"🚫 Profile '{profile_name}' in {resolved_instance} has no allowed qualities matching resolution '{target_res}'"
        )
        return False

    @LoggerManager().log_function_entry
    @timeit("run_quality_data_pull")
    def run_quality_data_pull(self, instance):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, arrapi_client in all_instances:
            cache_key = self.key_builder.format_cache_key("sonarr", instance_name, "quality_profiles")
            quality_profiles = arrapi_client.quality_profile()

            serialized = [
                {
                    "id": q.id,
                    "name": q.name,
                    "items": [vars(item) for item in q.items] if hasattr(q, 'items') else []
                }
                for q in quality_profiles
            ]

            updated_cache = {
                "qualityProfiles": serialized,
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "instance": instance_name,
                    "count": len(serialized)
                }
            }

            self.global_cache.set_with_pretty_output(cache_key, updated_cache)
            self.logger.log_info(f"✅ Quality profiles cached for {instance_name} ({len(serialized)} profiles)")

    def get_next_quality(self, config, series_id, blacklisted):
        qualities = config.get("quality_order", ["SD", "720p", "1080p", "4K"])
        current = [q.lower() for q in blacklisted if isinstance(q, str)]

        for q in qualities:
            if q.lower() not in current:
                return q
        return None

    @LoggerManager().log_function_entry
    @timeit("sync_quality_profiles_across_instances")
    def sync_quality_profiles_across_instances(self):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())
        quality_profiles_map = {}

        for instance_name, client in all_instances:
            profiles = client.quality_profile() or []
            for profile in profiles:
                name = profile.name
                if name not in quality_profiles_map:
                    quality_profiles_map[name] = profile

        for instance_name, client in all_instances:
            existing_profiles = {p.name: p for p in client.quality_profile() or []}
            for name, profile in quality_profiles_map.items():
                if name not in existing_profiles:
                    self.logger.log_info(f"➕ Syncing profile '{name}' to instance '{instance_name}'")
                    try:
                        client._make_request("qualityprofile", method="POST", data=vars(profile))
                    except Exception as e:
                        self.logger.log_warning(f"⚠️ Failed to sync profile '{name}' to '{instance_name}': {e}")

    @LoggerManager().log_function_entry
    @timeit("log_missing_profiles")
    def log_missing_profiles(self):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())
        profile_sets = {name: set(p.name for p in client.quality_profile() or []) for name, client in all_instances}

        common_profiles = set.intersection(*profile_sets.values()) if profile_sets else set()

        for instance, profiles in profile_sets.items():
            missing = common_profiles - profiles
            if missing:
                self.logger.log_warning(f"⚠️ Instance '{instance}' is missing profiles: {missing}")

    @LoggerManager().log_function_entry
    @timeit("normalize_profile_names")
    def normalize_profile_names(self):
        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, client in all_instances:
            profiles = client.quality_profile() or []
            for profile in profiles:
                original = profile.name
                normalized = original.strip().title()
                if original != normalized:
                    self.logger.log_info(f"✏️ Renaming '{original}' → '{normalized}' in {instance_name}")
                    try:
                        client._make_request(f"qualityprofile/{profile.id}", method="PUT", data={"name": normalized})
                    except Exception as e:
                        self.logger.log_warning(f"❌ Rename failed for profile {profile.id} in {instance_name}: {e}")

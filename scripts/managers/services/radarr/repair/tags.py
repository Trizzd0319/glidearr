"""
RadarrRepairTagsManager
========================
Enforces tag consistency across the Radarr library:
- Ensures keep tags are consistent across instances for the same movie
- Applies configurable tag rules (e.g. auto-tag by genre or resolution)
- Reports movies with conflicting or missing tags
"""

from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

# Well-known tag labels used by the lifecycle system
KEEP_TAG_LABELS = {"keep", "keep_forever", "keep_movie"}


class RadarrRepairTagsManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrRepairManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        **kwargs,
    ):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Tag catalogue helpers ────────────────────────────────────────────────────

    def _get_tag_label_map(self, instance: str) -> dict[int, str]:
        """Return {tag_id: label} for all tags in this instance."""
        if self.radarr_api is None:
            return {}
        raw = self.radarr_api._make_request(instance, "tag", fallback=[]) or []
        return {t["id"]: t["label"] for t in raw if t.get("id") is not None}

    def _get_or_create_tag(self, instance: str, label: str, existing_map: dict[int, str]) -> int | None:
        """
        Return the tag ID for ``label``, creating it if it doesn't exist.
        Updates ``existing_map`` in place.
        Returns None on failure.
        """
        # Check existing
        for tid, lbl in existing_map.items():
            if lbl.lower() == label.lower():
                return tid

        if self.radarr_api is None:
            return None

        try:
            result = self.radarr_api._make_request(
                instance,
                "tag",
                method="POST",
                payload={"label": label},
            )
            new_id = result.get("id") if result else None
            if new_id is not None:
                existing_map[new_id] = label
                self.logger.log_info(f"[Tags] Created new tag '{label}' (id={new_id}) in '{instance}'")
            return new_id
        except Exception as e:
            self.logger.log_warning(f"[Tags] Failed to create tag '{label}': {e}")
            return None

    # ── Inconsistent keep tags ───────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_inconsistent_keep_tags")
    def find_inconsistent_keep_tags(self, instance: str) -> list[dict]:
        """
        Find movies that have conflicting keep tags (e.g. both 'keep_forever'
        and 'keep_movie' applied simultaneously).

        Returns list of {movie_id, title, year, tag_ids, conflicting_labels}
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        tag_map = self._get_tag_label_map(instance)
        movies  = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        results = []

        for m in movies:
            tag_ids  = m.get("tags") or []
            labels   = {tag_map.get(tid, "").lower() for tid in tag_ids}
            keep_hit = labels & KEEP_TAG_LABELS
            if len(keep_hit) > 1:
                results.append({
                    "movie_id":          m.get("id"),
                    "title":             m.get("title"),
                    "year":              m.get("year"),
                    "tag_ids":           tag_ids,
                    "conflicting_labels": sorted(keep_hit),
                })

        self.logger.log_info(
            f"[Tags] Inconsistent keep tags in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Movies without any tag ───────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("find_untagged_movies")
    def find_untagged_movies(self, instance: str) -> list[dict]:
        """
        Find movies that have no tags at all.
        These may be missing important metadata signals.
        """
        instance = self._resolve_instance(instance)

        if self.radarr_api is None:
            return []

        movies  = self.radarr_api._make_request(instance, "movie", fallback=[]) or []
        results = [
            {"movie_id": m.get("id"), "title": m.get("title"), "year": m.get("year")}
            for m in movies
            if not m.get("tags")
        ]

        self.logger.log_info(
            f"[Tags] Untagged movies in '{instance}': {len(results)} movie(s)"
        )
        return results

    # ── Apply tag rule ───────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("apply_tag_rule")
    def apply_tag_rule(
        self,
        instance: str,
        genre: str | None = None,
        resolution: int | None = None,
        tag_label: str = "",
    ) -> dict:
        """
        Apply a tag to all movies matching the given genre and/or resolution.

        Parameters
        ----------
        genre
            If provided, only movies with this genre are tagged.
        resolution
            If provided, only movies with this resolution (exact match) are tagged.
        tag_label
            The tag label to apply.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "tagged": 0, "already_tagged": 0, "failed": 0}

        if not tag_label or self.radarr_api is None:
            return stats

        tag_map = self._get_tag_label_map(instance)
        tag_id  = self._get_or_create_tag(instance, tag_label, tag_map)
        if tag_id is None:
            self.logger.log_warning(f"[Tags] Could not resolve tag id for '{tag_label}'")
            return stats

        movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []

        _rows = []
        for m in movies:
            stats["checked"] += 1

            # Genre filter
            if genre:
                movie_genres = [g.lower() for g in (m.get("genres") or [])]
                if genre.lower() not in movie_genres:
                    continue

            # Resolution filter
            if resolution is not None and m.get("hasFile"):
                mf  = m.get("movieFile") or {}
                qq  = ((mf.get("quality") or {}).get("quality") or {})
                res = qq.get("resolution")
                try:
                    if int(res) != resolution:
                        continue
                except (TypeError, ValueError):
                    continue

            current_tags = list(m.get("tags") or [])
            if tag_id in current_tags:
                stats["already_tagged"] += 1
                continue

            new_tags = current_tags + [tag_id]
            mid = m.get("id")

            if self.dry_run:
                _rows.append([str(m.get("title") or "")[:28], str(mid)])
                stats["tagged"] += 1
                continue

            try:
                self.radarr_api._make_request(
                    instance,
                    f"movie/{mid}",
                    method="PUT",
                    payload={**m, "tags": new_tags},
                )
                stats["tagged"] += 1
                self.logger.log_debug(
                    f"[Tags] Added '{tag_label}' to '{m.get('title')}' (id={mid})"
                )
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(
                    f"[Tags] Failed to tag '{m.get('title')}' (id={mid}): {e}"
                )

        self.logger.log_grid(
            ["Title", "Id"],
            _rows,
            title=f"[dry_run] Would tag '{tag_label}'",
            cap=28,
        )
        self.logger.log_info(
            f"[Tags] Apply rule '{tag_label}': {stats['tagged']} tagged, "
            f"{stats['already_tagged']} already tagged, {stats['failed']} failed."
        )
        return stats

    # ── Fix inconsistent keep tags ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("fix_inconsistent_keep_tags")
    def fix_inconsistent_keep_tags(self, instance: str) -> dict:
        """
        For movies with conflicting keep tags, keep only the strongest policy:
        keep_forever > keep_movie > keep.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {"checked": 0, "fixed": 0, "failed": 0}

        if self.radarr_api is None:
            return stats

        tag_map     = self._get_tag_label_map(instance)
        id_by_label = {v.lower(): k for k, v in tag_map.items()}

        conflicts   = self.find_inconsistent_keep_tags(instance)
        policy_order = ["keep_forever", "keep_movie", "keep"]

        _rows = []
        for conflict in conflicts:
            stats["checked"] += 1
            mid      = conflict["movie_id"]
            tag_ids  = list(conflict["tag_ids"])
            c_labels = {tag_map.get(tid, "").lower() for tid in tag_ids}

            # Find strongest keep policy present
            strongest = next((p for p in policy_order if p in c_labels), None)
            if not strongest:
                continue

            # Remove all keep tags except the strongest
            keep_tags_to_remove = {
                id_by_label[lbl] for lbl in KEEP_TAG_LABELS
                if lbl in id_by_label and lbl != strongest
            }
            new_tags = [tid for tid in tag_ids if tid not in keep_tags_to_remove]

            if self.dry_run:
                _rows.append([str(conflict["title"] or "")[:28], str(mid), str(strongest)])
                stats["fixed"] += 1
                continue

            try:
                # Fetch full movie record for PUT
                movie_rec = self.radarr_api._make_request(instance, f"movie/{mid}", fallback={}) or {}
                if movie_rec:
                    self.radarr_api._make_request(
                        instance,
                        f"movie/{mid}",
                        method="PUT",
                        payload={**movie_rec, "tags": new_tags},
                    )
                    stats["fixed"] += 1
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(
                    f"[Tags] Fix failed for '{conflict['title']}' (id={mid}): {e}"
                )

        self.logger.log_grid(
            ["Title", "Id", "Keep"],
            _rows,
            title="[dry_run] Would fix keep tags",
            cap=28,
        )
        return stats

    # ── Full tags scan ───────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_tags_scan")
    def run(self, instance: str) -> dict:
        instance = self._resolve_instance(instance)
        return {
            "inconsistent_keep_tags": self.find_inconsistent_keep_tags(instance),
            "untagged_movies":        self.find_untagged_movies(instance),
        }

"""
QueueCancelMixin
================
Shared helper for cancelling in-flight Sonarr / Radarr queue items before
triggering a fresh search at the best determined quality.

Both ``SonarrCacheEpisodeFilesManager._do_acquire_next_episodes`` and
``RadarrAnomalyManager.triage_monitored_missing`` inherit or compose with
this mixin to avoid duplicating the queue-management logic.

Sonarr queue endpoint:
    GET  /api/v3/queue?seriesId=<id>&pageSize=100
    DELETE /api/v3/queue/<id>?removeFromClient=true&blocklist=false

Radarr queue endpoint:
    GET  /api/v3/queue?movieId=<id>&pageSize=100
    DELETE /api/v3/queue/<id>?removeFromClient=true&blocklist=false

``blocklist=false`` means "cancel but don't blacklist the release" so
the same release can be grabbed again if it's still the best choice.
``removeFromClient=true`` tells the download client to also stop/remove
the torrent/NZB rather than just deleting the Radarr/Sonarr queue record.
"""
from __future__ import annotations


class QueueCancelMixin:
    """
    Mixin that provides ``_cancel_queue_items`` for Sonarr and Radarr managers.

    The host class must expose:
        self.sonarr_api / self.radarr_api — whichever applies
        self.logger
        self.dry_run
    """

    # ── Sonarr ───────────────────────────────────────────────────────────────

    def _cancel_sonarr_queue_for_episodes(
        self,
        instance: str,
        episode_ids: list[int],
        *,
        series_title: str = "",
    ) -> int:
        """
        Cancel all Sonarr queue items for the given *episode_ids*.

        Returns the number of queue items cancelled.
        """
        if not episode_ids:
            return 0

        cancelled = 0
        try:
            # Sonarr paginates; grab a generous page size
            resp = self.sonarr_api._make_request(
                instance,
                f"queue?pageSize=500&includeEpisode=true",
                fallback={},
            ) or {}
            records = resp.get("records") or (resp if isinstance(resp, list) else [])
        except Exception as e:
            self.logger.log_warning(
                f"[Queue] Could not fetch Sonarr queue for '{series_title}': {e}"
            )
            return 0

        ep_id_set = set(episode_ids)
        to_delete = [
            r["id"] for r in records
            if isinstance(r, dict) and r.get("episodeId") in ep_id_set
        ]

        for qid in to_delete:
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would cancel queue item {qid} for '{series_title}'"
                )
                cancelled += 1
                continue
            try:
                self.sonarr_api._make_request(
                    instance,
                    f"queue/{qid}?removeFromClient=true&blocklist=false",
                    method="DELETE",
                )
                self.logger.log_info(
                    f"  🗑️ Cancelled queue item {qid} for '{series_title}'"
                )
                cancelled += 1
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Failed to cancel queue item {qid}: {e}"
                )

        return cancelled

    def _cancel_sonarr_queue_for_series(
        self,
        instance: str,
        series_id: int,
        *,
        series_title: str = "",
    ) -> int:
        """
        Cancel ALL Sonarr queue items for the given *series_id*.
        Returns the number cancelled.
        """
        cancelled = 0
        try:
            resp = self.sonarr_api._make_request(
                instance,
                f"queue?pageSize=500&seriesId={series_id}",
                fallback={},
            ) or {}
            records = resp.get("records") or (resp if isinstance(resp, list) else [])
        except Exception as e:
            self.logger.log_warning(
                f"[Queue] Could not fetch Sonarr queue for series {series_id}: {e}"
            )
            return 0

        for r in records:
            if not isinstance(r, dict):
                continue
            qid = r.get("id")
            if not qid:
                continue
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would cancel queue item {qid} for '{series_title}'"
                )
                cancelled += 1
                continue
            try:
                self.sonarr_api._make_request(
                    instance,
                    f"queue/{qid}?removeFromClient=true&blocklist=false",
                    method="DELETE",
                )
                self.logger.log_info(
                    f"  🗑️ Cancelled queue item {qid} for '{series_title}'"
                )
                cancelled += 1
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Failed to cancel queue item {qid}: {e}"
                )

        return cancelled

    # ── Radarr ───────────────────────────────────────────────────────────────

    def _cancel_radarr_queue_for_movie(
        self,
        instance: str,
        movie_id: int,
        *,
        movie_title: str = "",
    ) -> int:
        """
        Cancel all Radarr queue items for the given *movie_id*.
        Returns the number cancelled.
        """
        cancelled = 0
        try:
            resp = self.radarr_api._make_request(
                instance,
                f"queue?pageSize=500&movieId={movie_id}",
                fallback={},
            ) or {}
            records = resp.get("records") or (resp if isinstance(resp, list) else [])
        except Exception as e:
            self.logger.log_warning(
                f"[Queue] Could not fetch Radarr queue for '{movie_title}': {e}"
            )
            return 0

        for r in records:
            if not isinstance(r, dict):
                continue
            qid = r.get("id")
            if not qid:
                continue
            if self.dry_run:
                self.logger.log_info(
                    f"  [dry_run] Would cancel Radarr queue item {qid} for '{movie_title}'"
                )
                cancelled += 1
                continue
            try:
                self.radarr_api._make_request(
                    instance,
                    f"queue/{qid}?removeFromClient=true&blocklist=false",
                    method="DELETE",
                )
                self.logger.log_info(
                    f"  🗑️ Cancelled Radarr queue item {qid} for '{movie_title}'"
                )
                cancelled += 1
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Failed to cancel Radarr queue item {qid}: {e}"
                )

        return cancelled

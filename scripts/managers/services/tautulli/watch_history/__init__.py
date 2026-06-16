from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.affinity.group_completion import (
    group_movie_completions,
)

_HISTORY_TTL = 86_400  # 24 hours

# Data-minimization (PII): the raw Tautulli history record carries household
# PII that is NEVER consumed downstream from this "tautulli/history/all" cache
# and therefore must not be persisted to disk. We project each record down to
# only the fields the codebase actually reads (verified by grepping consumers in
# scripts/managers/services/tautulli, radarr/orchestration and sonarr/series/sync):
#
#   user, user_id            — affinity grouping (users service) + stable id
#   rating_key               — metadata index + affinity lookup
#   grandparent_rating_key   — kept (non-PII id; mirrors the ML watch-history cache)
#   title, grandparent_title — title fallbacks (radarr/sonarr) + series stats
#   media_type               — type filtering across every consumer
#   percent_complete         — completion stats + group movie completions
#   platform                 — device/platform usage stats
#   transcode_decision,
#   stream_video_codec,
#   stream_audio_codec       — transcode format stats
#   subtitle_decision        — subtitle handling (none/copy/burn); a burned-in sub
#                              forces a transcode. NOT PII — a playback setting.
#   stream_video_full_resolution — streamed resolution + HDR tier; a media property,
#                              NOT PII.
#   location                 — coarse lan/wan bit only (home vs. remote); NOT an IP
#                              and NOT geolocation, so far less identifying than the
#                              dropped ip_address. Feeds the WAN-bandwidth transcode read.
#       ↳ the three above feed the per-device transcode-capability fingerprint
#         (quality_analytics.transcode_fingerprint) read by the Stage-C remote-play gate;
#         without them the matrix self-degrades to a codec-only read.
#
# DROPPED on purpose (and why):
#   friendly_name  — household members' real display names (PII). Not read from
#                    this cache; "user" is the identifier every consumer uses.
#   ip_address     — WAN IP of the viewer (PII / location-linkable). Never read.
#                    (location's lan/wan bit is admitted; the raw IP stays dropped.)
#   machine_id     — device fingerprint that can re-identify a viewer (PII).
#                    Never read. (device granularity stays per-platform, not per-box.)
# user_id is retained as a non-PII stable identifier; friendly_name is dropped
# entirely since no consumer needs a human-readable display name from this cache.
_CACHED_HISTORY_FIELDS = (
    "user",
    "user_id",
    "rating_key",
    "grandparent_rating_key",
    "title",
    "grandparent_title",
    "media_type",
    "percent_complete",
    "platform",
    "transcode_decision",
    "stream_video_codec",
    "stream_audio_codec",
    # Transcode-capability fingerprint axes (Stage-C remote-play gate). See the PII
    # rationale block above — these are media/playback properties, and location is a
    # coarse lan/wan bit, not an IP.
    "subtitle_decision",
    "stream_video_full_resolution",
    "location",
    "date",   # unix watch timestamp — drives temporal affinity decay (not PII)
    # season / episode indices (non-PII) — let the playlist watched-filter match an
    # owned episode by (series, season, episode), which survives Plex ratingKey churn
    # (a re-scan reassigns episode ratingKeys, so the historical key goes stale).
    "parent_media_index",   # season number
    "media_index",          # episode number
)


class TautulliWatchHistoryManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.tautulli_api = kwargs.get("tautulli_api")

    @staticmethod
    def _project_record(entry: dict) -> dict:
        """Project a raw Tautulli history record down to the non-PII fields that
        are actually consumed downstream (see _CACHED_HISTORY_FIELDS). Drops
        friendly_name / ip_address / machine_id so household PII is never cached."""
        if not isinstance(entry, dict):
            return {}
        return {k: entry[k] for k in _CACHED_HISTORY_FIELDS if k in entry}

    def get_all_history(self, user_id=None, page_size: int = 1000) -> list:
        """Fetch all history records with pagination. Returns flat list of entries
        projected to only the non-PII fields consumed downstream."""
        if not self.tautulli_api:
            self.logger.log_warning("[TautulliHistory] No API available.")
            return []

        entries = []
        start = 0
        while True:
            resp = self.tautulli_api.get_history(
                length=page_size, start=start, user_id=user_id
            )
            if not resp:
                break
            data = (resp.get("response") or {}).get("data", {})
            page = data.get("data", []) if isinstance(data, dict) else []
            if not page:
                break
            entries.extend(self._project_record(e) for e in page)
            total = int(data.get("recordsFiltered", 0)) if isinstance(data, dict) else 0
            start += page_size
            if start >= total:
                break

        self.logger.log_info(f"[TautulliHistory] Fetched {len(entries)} total history entries.")
        return entries

    def get_all_history_cached(self, user_id=None) -> list:
        """Return all history entries, cached for 24 hours."""
        if not self.global_cache:
            return self.get_all_history(user_id=user_id)
        key = "tautulli/history/all" if user_id is None else f"tautulli/history/user/{user_id}"
        return self.global_cache.get_or_generate_cache(
            key=key,
            generator_function=lambda: self.get_all_history(user_id=user_id),
            expiration_time=_HISTORY_TTL,
            # Upstream source of truth for the household watched-set — refresh on
            # TTL so new Plex watches flow through (Tautulli is local; no rate
            # limit to fear). Frozen history was silently staling everything.
            regenerate_on_expiry=True,
        )

    def get_group_movie_completions(
        self,
        history_entries: list,
        rating_groups_cfg: dict,
    ) -> dict:
        """Per-group, per-movie max completion across group members. The COMPUTATION
        lives in the brain (affinity.group_completion.group_movie_completions); the
        manager keeps the raw history FETCH and the orchestration keeps the
        rating_key -> tmdb_id resolution + cache write. Returns
        ``{group_name: {rating_key: {"pct": float(0-1), "threshold": float}}}``."""
        return group_movie_completions(history_entries, rating_groups_cfg)

    def _extract_entries(self, data) -> list:
        """Extract entry list from a raw Tautulli history response dict."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = (data.get("response") or {}).get("data", {})
            if isinstance(inner, dict):
                return inner.get("data", [])
            if isinstance(inner, list):
                return inner
        return []

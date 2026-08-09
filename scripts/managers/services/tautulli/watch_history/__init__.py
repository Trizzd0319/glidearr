from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.affinity.group_completion import (
    group_movie_completions,
)

# Refreshed hourly (down from 24 h): with things watched frequently, the resume/recency/JIT
# signals the playlists ride on need to reflect what was JUST watched. A full re-fetch is cheap
# (one paginated call of ~hundreds of rows) and — unlike a date-delta — also picks up in-progress
# %-complete updates to already-seen watches. Kept positive (not per-run) because ~6 consumers
# call get_all_history_cached per run; the TTL must exceed a run's duration so they share one fetch.
_HISTORY_TTL = 3_600  # 1 hour

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
    # Tautulli internal history-row id (NOT PII — an internal DB row id, not a person/device).
    # Required to fetch a play's per-stream detail via get_stream_data for the transcode-cause
    # breakdown (the per-stream decisions get_history itself omits).
    "row_id",
    "reference_id",
    "title",
    "grandparent_title",
    "media_type",
    "percent_complete",
    # Tautulli's OWN watched verdict for the play (1 / 0.5 / 0). NOT PII — a
    # playback-completion flag. It already reflects whatever completion threshold
    # the operator configured in Tautulli (85% out of the box, shared with Plex),
    # so consumers that need "did they actually watch this, or sample it?" can
    # honour the server's answer instead of inventing a second, disagreeing
    # definition. Read by the per-viewer episode retention rule
    # (machine_learning/lifecycle/viewer_retention.watched_by_tautulli).
    # ABSENT on rows cached before this key was admitted, and on a Tautulli old
    # enough not to emit it — watched_by_tautulli falls back to percent_complete
    # (which those rows DO carry), so the transition is silent: no historical row
    # reads as unwatched merely because the cache has not cycled yet.
    "watched_status",
    "platform",
    "transcode_decision",
    "stream_video_codec",
    "stream_audio_codec",
    # Per-stream transcode decisions (Plex ground truth: directplay/copy/transcode) — let the
    # transcode-cause breakdown attribute a transcode to the VIDEO vs AUDIO stream rather than
    # guessing. Media/playback properties, not PII. Absent on older cached rows (the projection
    # drops a missing key) → the breakdown falls back to a source-vs-streamed-codec heuristic.
    "video_decision",
    "audio_decision",
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


class HistoryFetchError(RuntimeError):
    """Raised when the Tautulli history fetch fails or is incomplete.

    EXISTS SO A FAILURE IS NOT INDISTINGUISHABLE FROM "NOTHING WAS WATCHED".
    ``get_all_history`` used to return ``[]`` on a dead API and to ``break`` out of
    the page loop on a failed page, so a transient blip produced an empty (or
    silently truncated) history that ``get_or_generate_cache`` then stored for a
    full hour. Roughly six consumers per run read that key, and one of them is the
    hard guard that stops a WATCHED movie being deleted
    (``radarr/repair/anomaly.demote_stale_monitored``:
    ``if keep_policy or tmdb_id in watched_tmdb_ids: guarded``).

    The reference for this shape is ``plex/metadata``:
        ``if not resp: return None  # transient - allow retry on a later run``
    """


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
        projected to only the non-PII fields consumed downstream.

        RAISES ``HistoryFetchError`` rather than returning a short list when the
        API is missing or a page fails. A partial history is not a smaller truth --
        every consumer reads absence as "not watched", so a truncated fetch is
        strictly more dangerous than no fetch at all. See the class docstring.
        """
        if not self.tautulli_api:
            raise HistoryFetchError(
                "[TautulliHistory] No API available - cannot distinguish this from "
                "an empty watch history, so refusing to return one."
            )

        entries = []
        start = 0
        while True:
            resp = self.tautulli_api.get_history(
                length=page_size, start=start, user_id=user_id
            )
            if not resp:
                # A failed page, NOT the end of the data. Breaking here would return
                # everything fetched so far as though it were complete.
                raise HistoryFetchError(
                    f"[TautulliHistory] history fetch failed at offset {start} "
                    f"(after {len(entries)} entries) - refusing to cache a partial history."
                )
            data = (resp.get("response") or {}).get("data", {})
            page = data.get("data", []) if isinstance(data, dict) else []
            if not page:
                break                      # genuine end of data
            entries.extend(self._project_record(e) for e in page)

            # SHORT PAGE = end of data. This is the primary terminator and it works
            # without recordsFiltered; the grand-total check below is only a
            # fast-path. Previously an ABSENT recordsFiltered defaulted total to 0,
            # so `start >= 0` fired immediately and the whole history truncated to
            # ONE page -- exactly the failure plex/ documents avoiding by falling
            # through to its empty-page terminator.
            if len(page) < page_size:
                break

            start += page_size
            try:
                total = int(data["recordsFiltered"]) if isinstance(data, dict) else None
            except (KeyError, TypeError, ValueError):
                total = None               # absent/garbage -> rely on the page terminators
            if total is not None and start >= total:
                break

        self.logger.log_info(f"[TautulliHistory] Fetched {len(entries)} total history entries.")
        return entries

    def get_all_history_cached(self, user_id=None) -> list:
        """Return all history entries, cached for _HISTORY_TTL (1 hour).

        ON FETCH FAILURE, PREFERS STALE OVER EMPTY. A stale watched-set is wrong by
        an hour; an empty one is wrong about every title in the library, and the
        consumers cannot tell the difference. Returns `[]` only when there is
        genuinely nothing to fall back on, and says so loudly when it does.
        """
        if not self.global_cache:
            try:
                return self.get_all_history(user_id=user_id)
            except HistoryFetchError as e:
                self.logger.log_warning(f"{e} No cache available - returning empty.")
                return []

        key = "tautulli/history/all" if user_id is None else f"tautulli/history/user/{user_id}"

        def _stale_or_empty(reason: str) -> list:
            try:
                stale = self.global_cache.get(key)
            except Exception:
                stale = None
            if isinstance(stale, list) and stale:
                self.logger.log_warning(
                    f"{reason} Serving {len(stale)} STALE entries from '{key}' rather than "
                    f"an empty history (an empty watched-set un-guards every watched title)."
                )
                return stale
            self.logger.log_warning(
                f"{reason} No usable cached history at '{key}' - returning EMPTY. "
                f"Downstream this reads as 'nothing was watched': affinity, completion, "
                f"the delete grace window and the watched-set delete guard are all "
                f"degraded for this run."
            )
            return []

        try:
            result = self.global_cache.get_or_generate_cache(
                key=key,
                generator_function=lambda: self.get_all_history(user_id=user_id),
                expiration_time=_HISTORY_TTL,
                # Upstream source of truth for the household watched-set — refresh on
                # TTL so new Plex watches flow through (Tautulli is local; no rate
                # limit to fear). Frozen history was silently staling everything.
                regenerate_on_expiry=True,
            )
        except HistoryFetchError as e:
            return _stale_or_empty(str(e))

        # Belt-and-braces: if the cache layer swallows generator exceptions and hands
        # back a falsy value instead, that is the same failure wearing a different
        # coat -- do not let it through as "nothing was watched" either.
        if not result:
            return _stale_or_empty("[TautulliHistory] history cache returned nothing.")
        return result

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

"""
CalendarManager — Trakt calendars → cache + ensure library is monitored ahead.
================================================================================
Populates the long-declared-but-dormant ``trakt/<user>/calendar/*`` cache keys
from the user's Trakt calendar (upcoming episodes, premieres, movie releases), then
— for titles already in the Sonarr/Radarr library — ensures they are monitored so
the *arr grabs them on air. Optional search behind a flag (default off, matching
the acquisition policy). dry_run-gated; no-op unless ``calendar.enabled``.

MAL upcoming (``calendar.mal``, default on when MAL is configured): the current
MAL seasonal chart is filtered to entries whose WATCHABILITY — ``score_show`` on
the signals an unowned upcoming anime actually has (its genres × the household's
genre affinity, plus the MAL community mean as the rating) — clears
``calendar.mal_min_watchability`` (0–100, default 20; unowned entries realistically
score 0–25 since the household-intent groups are all 0). Passing entries are cached
under ``mal/{user}/calendar/upcoming`` and, when ``ensure_monitored`` is on, any
that are already in the Sonarr/Radarr library (exact normalized-title match) are
ensured monitored exactly like the Trakt upcoming titles.
"""
from __future__ import annotations

from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.scoring.show_scorer import score_show
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_MAL_SEASONS = {1: "winter", 2: "winter", 3: "winter", 4: "spring", 5: "spring", 6: "spring",
                7: "summer", 8: "summer", 9: "summer", 10: "fall", 11: "fall", 12: "fall"}


def mal_upcoming_above_threshold(seasonal, *, genre_affinity=None, threshold=20) -> list:
    """Filter a MAL seasonal list to the entries whose watchability clears
    ``threshold`` (0–100), highest first. PURE — the score is ``score_show`` on the
    signals an unowned upcoming anime has: its genres × the household genre affinity
    (Group B) and the MAL community ``mean`` (0–10) as the rating (Group F). The
    household-intent groups are all 0 for unowned titles, so realistic scores run
    0–25 — hence the low default threshold. Malformed / titleless entries drop."""
    out = []
    for item in (seasonal or []):
        node = item.get("node", item) if isinstance(item, dict) else {}
        if not isinstance(node, dict):
            continue
        title = node.get("title")
        if not title:
            continue
        genres = [g.get("name") for g in (node.get("genres") or [])
                  if isinstance(g, dict) and g.get("name")]
        try:
            mean = float(node.get("mean")) if node.get("mean") is not None else None
        except (TypeError, ValueError):
            mean = None
        score = score_show(
            {"genres": genres or ["anime"]},
            genre_affinity=genre_affinity or {},
            sonarr_rating=mean,
        )
        if score >= threshold:
            start = node.get("start_season") or {}
            alt = node.get("alternative_titles") or {}
            out.append({
                "title": title,
                "title_en": alt.get("en") or None,
                "mal_id": node.get("id"),
                "media_type": node.get("media_type"),
                "year": start.get("year"),
                "mean": mean,
                "genres": genres,
                "watchability": int(score),
            })
    out.sort(key=lambda e: -e["watchability"])
    return out


class CalendarManager(BaseManager, ComponentManagerMixin):
    parent_name = "CalendarManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "CalendarManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        parent = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt = kwargs.get("trakt")
        self.sonarr = kwargs.get("sonarr")
        self.radarr = kwargs.get("radarr")
        self.mal = kwargs.get("mal")

    def prepare(self) -> None:
        pass

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> None:
        cal = (self.config.get("calendar", {}) if self.config else {}) or {}
        if not cal.get("enabled"):
            self.logger.log_debug("[Calendar] disabled — skipping.")
            return
        api = getattr(self.trakt, "trakt_api", None)
        if not api:
            self.logger.log_warning("[Calendar] Trakt API unavailable — skipping.")
            return

        days = int(cal.get("days", 33))
        start = datetime.now().strftime("%Y-%m-%d")
        shows = api._make_request(f"calendars/my/shows/{start}/{days}") or []
        premieres = api._make_request(f"calendars/my/shows/premieres/{start}/{days}") or []
        movies = api._make_request(f"calendars/my/movies/{start}/{days}") or []

        user = (self.config.get("trakt", {}) or {}).get("username", "default")
        if self.global_cache:
            self.global_cache.set(f"trakt/{user}/calendar/shows", shows)
            self.global_cache.set(f"trakt/{user}/calendar/shows/premieres", premieres)
            self.global_cache.set(f"trakt/{user}/calendar/movies", movies)
        self.logger.log_info(
            f"[Calendar] upcoming: {len(shows)} episodes, {len(premieres)} premieres, {len(movies)} movies.")

        # ── MAL upcoming (seasonal chart above the watchability threshold) ──────
        # Before the ensure_monitored early-return so the MAL view is always built
        # while calendar.mal is on; its own monitoring half obeys ensure_monitored.
        if cal.get("mal", True):
            try:
                self._mal_upcoming(cal)
            except Exception as e:
                self.logger.log_warning(f"[Calendar] MAL upcoming failed: {e}")

        if not cal.get("ensure_monitored", True):
            return

        search = bool(cal.get("search", False))
        up_tvdbs = {self._gid(i, "show", "tvdb") for i in (shows + premieres)} - {None}
        up_tmdbs = {self._gid(i, "movie", "tmdb") for i in movies} - {None}
        self._ensure("sonarr", "tvdbId", up_tvdbs, search)
        self._ensure("radarr", "tmdbId", up_tmdbs, search)

    # ── MAL upcoming ─────────────────────────────────────────────────────────────
    def _mal_upcoming(self, cal: dict) -> None:
        """Build the watchability-gated MAL upcoming view and (when ensure_monitored)
        ensure already-owned matches are monitored. FETCH = the Phase-2 seasonal cache
        (live fallback); the DECISION is the pure mal_upcoming_above_threshold above;
        APPLY reuses the same _ensure path as the Trakt titles."""
        mal = self.mal or (self.registry.get("manager", "MALManager") if self.registry else None)
        if mal is None or not getattr(mal, "enabled", False):
            self.logger.log_debug("[Calendar] MAL disabled/unavailable — skipping MAL upcoming.")
            return

        now = datetime.now()
        season = _MAL_SEASONS[now.month]
        seasonal = (self.global_cache.get(f"mal/seasonal/{now.year}/{season}")
                    if self.global_cache else None)
        if not seasonal:
            seasonal = getattr(mal, "mal_api", None) and mal.mal_api.get_seasonal(now.year, season) or []
        if not seasonal:
            self.logger.log_debug("[Calendar] no MAL seasonal data — skipping MAL upcoming.")
            return

        try:
            threshold = int(cal.get("mal_min_watchability", 20))
        except (TypeError, ValueError):
            threshold = 20
        genre_affinity = (self.global_cache.get("tautulli/affinity") if self.global_cache else None) or {}

        passing = mal_upcoming_above_threshold(
            seasonal, genre_affinity=genre_affinity, threshold=threshold)

        mal_user = (self.config.get("mal", {}) or {}).get("username", "default") or "default"
        if self.global_cache:
            self.global_cache.set(f"mal/{mal_user}/calendar/upcoming", passing)
        top = ", ".join(f"'{e['title']}' ({e['watchability']})" for e in passing[:3])
        self.logger.log_info(
            f"[Calendar] MAL upcoming: {len(passing)}/{len(seasonal)} seasonal entries ≥ "
            f"watchability {threshold}" + (f" — top: {top}" if top else "."))

        if not passing or not cal.get("ensure_monitored", True):
            return
        search = bool(cal.get("search", False))
        # Exact normalized-title match against the library only — monitoring flips,
        # never adds (same semantics as the Trakt flow above).
        tv_titles = {t for e in passing if (e.get("media_type") or "tv") != "movie"
                     for t in (e["title"], e.get("title_en")) if t}
        mv_titles = {t for e in passing if e.get("media_type") == "movie"
                     for t in (e["title"], e.get("title_en")) if t}
        self._ensure("sonarr", "tvdbId",
                     self._library_ids_by_title("sonarr", "tvdbId", tv_titles), search)
        self._ensure("radarr", "tmdbId",
                     self._library_ids_by_title("radarr", "tmdbId", mv_titles), search)

    def _library_ids_by_title(self, service, id_field, titles) -> set:
        """{id_field as str} for library items whose title exactly matches (casefold)
        one of ``titles``. Conservative by design: fuzzy matching could monitor the
        wrong series, so a non-match simply means no monitoring flip."""
        wanted = {str(t).casefold().strip() for t in (titles or set()) if t}
        out: set = set()
        if not wanted:
            return out
        gw = ArrGateway(service, getattr(getattr(self, service), "instance_manager", None),
                        self.config, self.logger)
        if not gw.available:
            return out
        for inst in (self._instance_names(service) or [gw.default_instance()]):
            for item in gw.library_items(inst):
                if not isinstance(item, dict):
                    continue
                if (item.get("title") or "").casefold().strip() in wanted \
                        and item.get(id_field) is not None:
                    out.add(str(item.get(id_field)))
        return out

    @staticmethod
    def _gid(item, kind, idk):
        ids = ((item.get(kind) or {}).get("ids") or {}) if isinstance(item, dict) else {}
        v = ids.get(idk)
        return str(v) if v is not None else None

    def _instance_names(self, service):
        insts = self.config.get(f"{service}_instances", {}) or {}
        return [k for k, v in insts.items() if k != "default_instance" and isinstance(v, dict)]

    def _ensure(self, service, id_field, upcoming_ids, search):
        if not upcoming_ids:
            return
        gw = ArrGateway(service, getattr(getattr(self, service), "instance_manager", None), self.config, self.logger)
        if not gw.available:
            return
        endpoint_base = "series" if service == "sonarr" else "movie"
        cmd_name = "SeriesSearch" if service == "sonarr" else "MoviesSearch"
        monitored = searched = 0
        for inst in (self._instance_names(service) or [gw.default_instance()]):
            for item in gw.library_items(inst):
                if not isinstance(item, dict) or str(item.get(id_field)) not in upcoming_ids:
                    continue
                item_id = item.get("id")
                if not item.get("monitored"):
                    if self.dry_run:
                        self.logger.log_info(f"[Calendar] dry_run — would monitor {service} '{item.get('title')}'")
                    else:
                        upd = dict(item); upd["monitored"] = True
                        gw.put(inst, f"{endpoint_base}/{item_id}", upd)
                    monitored += 1
                if search and item_id:
                    if self.dry_run:
                        self.logger.log_debug(f"[Calendar] dry_run — would search {service} '{item.get('title')}'")
                    else:
                        key = "seriesId" if service == "sonarr" else "movieIds"
                        payload = {"name": cmd_name, key: (item_id if service == "sonarr" else [item_id])}
                        gw.command(inst, payload)
                    searched += 1
        self.logger.log_info(
            f"[Calendar] {service}: {monitored} {'would-monitor' if self.dry_run else 'monitored'}"
            + (f", {searched} searched" if search else ""))

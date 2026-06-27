"""
resolver.py — decide WHERE, WHAT QUALITY, and EXPECTED SIZE for a candidate.
================================================================================
For each candidate: look it up against the relevant *arr (to get genres/runtime/
canonical ids), dedup against the target instance's library, then resolve the
target instance, quality profile, root folder, and an estimated size. The
resolved object doubles as the add-payload base in the adder.

Library routing: every candidate is classified into one library bucket via the shared
``library_classifier``, with Common Sense Media age (``recommended_age``, read from the
MDBList age cache by tmdbId) as the PRIMARY kids signal for BOTH shows and movies, and
routed to the matching ``rootFolders`` entry. Anime shows keep ``seriesType=anime``
but stay on the single Sonarr instance; anime MOVIES route to Radarr's optional
dedicated anime instance. MAL candidates are anime by construction. See
``support/utilities/library_classifier.py`` for the classification contract.
"""
from __future__ import annotations

from scripts.managers.machine_learning.classification import library_router
from scripts.managers.machine_learning.space import dual_version
from scripts.managers.services.mdblist import age_cache
from scripts.support.utilities.library_classifier import classify_movie, classify_show, is_anime_media
from scripts.support.utilities.size_model import estimate_gb, profile_max_quality, target_resolution_for_score


class Resolver:
    def __init__(self, gateways: dict, config, logger):
        self.gw = gateways                # {"sonarr": ArrGateway, "radarr": ArrGateway}
        self.config = config
        self.logger = logger
        self._anime_genres = {
            str(g).lower() for g in (config.get("animeGenres", []) or []) if g
        }
        self._kids_genres = [str(g) for g in (config.get("kidsGenres", []) or []) if g]
        self._kids_certs = [str(c) for c in (config.get("kidsCertifications", []) or []) if c]
        self._kids_networks = [str(n) for n in (config.get("kidsNetworks", []) or []) if n]
        self._reality_genres = [str(g) for g in (config.get("realityGenres", []) or []) if g]
        self._doc_genres = [str(g) for g in (config.get("documentaryGenres", []) or []) if g]
        self._preschool_genres = [str(g) for g in (config.get("preschoolGenres", []) or []) if g]
        self._non_kids_genres = [str(g) for g in (config.get("nonKidsGenres", []) or []) if g]
        self._root_folders = config.get("rootFolders", {}) or {}
        self._movie_root_folders = config.get("movieRootFolders", {}) or {}
        self._acq = config.get("acquisition", {}) or {}
        # Pilot/discovery floor: SHOW adds are capped to this resolution so the first (pilot) grab lands
        # <= floor and the watch-based upgrade path raises it later — mirrors pilot_interactive.floor_res
        # so add-time and the pilot search agree. 0 (default) disables the cap → byte-identical. Movies
        # are unaffected. This keeps EVERY show-add path (universe walk, search-on-add) off a
        # 1080p-allowing profile, which Sonarr (quality-first) would otherwise grab at 1080p.
        try:
            self._pilot_floor_res = max(0, int((config.get("pilot_interactive", {}) or {}).get("floor_res", 0) or 0))
        except (TypeError, ValueError):
            self._pilot_floor_res = 0
        # Operator routing preferences (the `routing` onboarding step). Honoured ONLY once
        # the step has run (the ``configured`` stamp), so a never-onboarded install routes
        # exactly as before — see _route_category.
        self._routing = config.get("routing", {}) or {}
        self._routing_on = bool(self._routing.get("configured"))
        # Common Sense Media age caches — the PRIMARY kids signal for both movies and shows
        # (see library_classifier). Loaded lazily once per Resolver and reused per candidate
        # (pure file reads, no network). The movie cache is keyed by Radarr tmdbId, the TV
        # cache by Sonarr series tmdbId (separate files — movie/show tmdbIds share a space).
        self._movie_age_cache = None
        self._show_age_cache = None
        # Plain-language reason for the LAST profile pick (pinned / score-tier / fallback). Set by
        # _pick_profile (which can't return it without changing a tested signature) and read by the
        # caller right after — safe because the resolver runs the candidate loop single-threaded.
        self._last_profile_reason = ""

    def prepare(self, cand: dict) -> dict:
        is_show = cand.get("type") == "show"
        gw = self.gw.get("sonarr" if is_show else "radarr")
        out = dict(cand)
        out["skip_reason"] = None
        if not gw or not gw.available:
            out["skip_reason"] = "no instance available"
            return out

        default_inst = gw.default_instance()
        obj = self._lookup(gw, default_inst, cand, is_show)
        if not obj:
            out["skip_reason"] = "no lookup match"
            return out

        genres = obj.get("genres") or cand.get("genres") or []
        runtime = int(obj.get("runtime") or cand.get("runtime") or 0)
        year = obj.get("year") or cand.get("year")
        certification = obj.get("certification") or cand.get("certification")
        _ol = obj.get("originalLanguage")
        original_language = _ol.get("name") if isinstance(_ol, dict) else (_ol or cand.get("language"))
        # Kids/family STUDIO is the movie-only fallback when CSM has no age (Radarr movies
        # carry a single ``studio`` string; shows have none). See classify_movie_explained.
        studio = None if is_show else (obj.get("studio") or cand.get("studio"))

        # Classify shows into a library bucket (folder). ``is_anime`` is tracked
        # separately for Sonarr seriesType — a children-genre anime routes to the
        # Kids library but must keep anime episode parsing. Movies keep their
        # existing anime-or-not behaviour (``rootFolders`` is TV-only).
        if is_show:
            category = classify_show(
                genres=genres,
                certification=certification,
                series_type=obj.get("seriesType"),
                original_language=original_language,
                network=obj.get("network"),
                is_anime_hint=bool(cand.get("is_anime")),
                recommended_age=self._csm_show_age(obj.get("tmdbId")),
                anime_genres=self._anime_genres,
                kids_genres=self._kids_genres,
                kids_certs=self._kids_certs,
                kids_networks=self._kids_networks,
                reality_genres=self._reality_genres,
                documentary_genres=self._doc_genres,
                preschool_genres=self._preschool_genres,
                non_kids_genres=self._non_kids_genres,
            )
            is_anime = is_anime_media(
                genres=genres,
                series_type=obj.get("seriesType"),
                original_language=original_language,
                is_anime_hint=bool(cand.get("is_anime")),
                anime_genres=self._anime_genres,
                studio=obj.get("network"),
            )
        else:
            # Movie library bucket: kids → anime → 4k → standard (content wins).
            # At ADD time the file doesn't exist yet, so the resolution is unknown
            # (is_uhd=False) — a new add routes by content only (kids/anime), else
            # standard. The 4k-vs-standard split is resolution-based and applied by
            # router_movie.py once the file has landed.
            is_anime = bool(cand.get("is_anime")) or self._is_anime(genres)
            category = classify_movie(
                genres=genres,
                certification=certification,
                original_language=original_language,
                studio=studio,
                recommended_age=self._csm_movie_age(obj.get("tmdbId")),
                is_anime_hint=is_anime,
                is_uhd=False,
                anime_genres=self._anime_genres,
                kids_genres=self._kids_genres,
                kids_certs=self._kids_certs,
                preschool_genres=self._preschool_genres,
                non_kids_genres=self._non_kids_genres,
            )

        id_field = "tvdbId" if is_show else "tmdbId"
        ext_id = obj.get(id_field) or (cand.get("ids") or {}).get("tvdb" if is_show else "tmdb")

        # Apply the operator's routing preferences to the classified category, giving the
        # EFFECTIVE bucket for FOLDER (and anime-movie INSTANCE) routing. A no-op until the
        # routing step has run; the classified ``category`` is still reported downstream.
        route_category = self._route_category(category, is_show)

        # Anime MOVIES route to Radarr's optional dedicated "anime" instance — and fall back to the
        # DEFAULT instance when no anime session is configured (gateway.categorized_instance returns
        # default for an unmapped label) OR when the operator chose anime_policy=standard_only (then
        # route_category collapsed anime → standard). Anime SHOWS stay on the single Sonarr instance;
        # seriesType=anime is set on the add payload regardless of which FOLDER they land in.
        if route_category == "anime" and not is_show:
            inst = gw.categorized_instance("anime")
        else:
            inst = default_inst

        if gw.in_library(inst, id_field, ext_id):
            out["skip_reason"] = "already in library"
            return out

        profile = self._pick_profile(gw, inst, is_show=is_show, is_anime=is_anime)
        profile_reason = self._last_profile_reason
        root = self._pick_root_folder(gw, inst, is_show, route_category)
        size, unit = self._expected_size(profile, runtime, is_movie=not is_show)

        out.update({
            "instance": inst,
            "quality_profile": profile,
            "profile_reason": profile_reason,
            "root_folder": root,
            "expected_size_gb": size,
            "size_unit": unit,
            "genres": genres,
            "runtime": runtime,
            "year": year,
            "certification": certification,
            "category": category,
            "route_category": route_category,
            "is_anime": is_anime,
            "ext_id": ext_id,
            "id_field": id_field,
            "lookup": obj,
        })
        return out

    # ── helpers ─────────────────────────────────────────────────────────────
    def _lookup(self, gw, inst, cand, is_show):
        ids = cand.get("ids", {}) or {}
        if is_show and ids.get("tvdb"):
            term = f"tvdb:{ids['tvdb']}"
        elif (not is_show) and ids.get("tmdb"):
            term = f"tmdb:{ids['tmdb']}"
        elif ids.get("imdb"):
            term = f"imdb:{ids['imdb']}"
        else:
            term = cand.get("title") or ""
        if not term:
            return None
        matches = gw.lookup(inst, term) or []
        if not matches:
            return None
        id_field = "tvdbId" if is_show else "tmdbId"
        want = ids.get("tvdb" if is_show else "tmdb")
        if want is not None:
            for m in matches:
                if isinstance(m, dict) and str(m.get(id_field)) == str(want):
                    return m
        return matches[0] if isinstance(matches[0], dict) else None

    def _is_anime(self, genres) -> bool:
        if not self._anime_genres:
            return False
        return any(str(g).lower() in self._anime_genres for g in (genres or []))

    def _csm_movie_age(self, tmdb_id):
        """Common Sense recommended age for a movie tmdbId (None if uncached/no rating)."""
        if self._movie_age_cache is None:
            self._movie_age_cache = age_cache.load(age_cache.AGE_CACHE_PATH)
        return age_cache.age_for(tmdb_id, cache=self._movie_age_cache)

    def _csm_show_age(self, tmdb_id):
        """Common Sense recommended age for a show tmdbId (None if uncached/no rating). The
        TV cache is keyed by the Sonarr series tmdbId — present on Sonarr lookup objects
        even though the resolver routes/dedupes shows by tvdbId."""
        if self._show_age_cache is None:
            self._show_age_cache = age_cache.load(age_cache.TV_AGE_CACHE_PATH)
        return age_cache.age_for(tmdb_id, cache=self._show_age_cache)

    @staticmethod
    def _res_label(res: int) -> str:
        """A resolution tier int (from ``target_resolution_for_score``) → a log label:
        2160 -> '2160p', 0/480 -> 'SD'."""
        try:
            res = int(res)
        except (TypeError, ValueError):
            return "?"
        return f"{res}p" if res > 480 else "SD"

    def _show_floor_profile(self, profiles, is_anime):
        """The floor profile a SHOW add lands on so its pilot grabs <= the pilot floor (the watch-based
        upgrade path raises it later). Within the show's FAMILY (anime vs live-action — never put a
        live-action stub on an [Anime] profile or vice-versa), pick the HIGHEST profile whose max allowed
        resolution is <= the floor; fall back to the family's LOWEST profile when none is <= floor (e.g.
        anime has no <=720 profile yet). None when there are no profiles."""
        floor = self._pilot_floor_res

        def _anime_p(p):
            return str(p.get("name") or "").strip().lower().startswith("[anime]")

        pool = [p for p in profiles if _anime_p(p) == bool(is_anime)] or list(profiles)
        ranked = sorted(((profile_max_quality(p)[0] or 0, p) for p in pool), key=lambda t: t[0])
        if not ranked:
            return None
        eligible = [p for (res, p) in ranked if res <= floor]
        return eligible[-1] if eligible else ranked[0][1]

    def _pick_profile(self, gw, inst, score=None, *, is_show=False, is_anime=False) -> dict:
        profiles = gw.quality_profiles(inst) or []
        want = (self._acq.get("quality_profile") or "").strip().lower()
        chosen, reason = None, ""
        if want:
            # Explicit config override by profile name — always wins.
            chosen = next((p for p in profiles if str(p.get("name", "")).lower() == want), None)
            if chosen is not None:
                reason = f"pinned to '{chosen.get('name')}' by acquisition.quality_profile"
        # SHOW adds are capped to the pilot floor (default-off via pilot_interactive.floor_res=0) so the
        # first (pilot) grab lands <= floor REGARDLESS of score — the watch-based upgrade path raises it
        # later. This keeps every show-add path (universe walk, search-on-add) off a 1080p-allowing
        # profile, which Sonarr (quality-first ranking) would otherwise grab at 1080p before the pilot
        # flip. Movies are unaffected.
        if chosen is None and not want and is_show and self._pilot_floor_res > 0 and profiles:
            chosen = self._show_floor_profile(profiles, is_anime)
            if chosen is not None:
                reason = (f"show add capped to the <={self._pilot_floor_res}p pilot floor "
                          f"('{chosen.get('name')}'); quality climbs via the watch-based upgrade path")
        if chosen is None and not want and score is not None and profiles:
            # Matrix-driven: map the watchability/acquisition score to a target
            # resolution tier and pick the highest-quality profile at or under it,
            # so high-want adds get higher quality and low-want adds get 720p/SD —
            # instead of always defaulting to the first ("Any") profile.
            chosen = self._profile_for_score(profiles, int(score))
            if chosen is not None:
                reason = (f"score {int(score)} picks up to the "
                          f"{self._res_label(self._target_resolution_for_score(int(score)))} tier")
        if chosen is None and profiles:
            chosen = profiles[0]
            reason = ("pinned name not found; using first available profile" if want
                      else "first available profile (no score/pin)")
        self._last_profile_reason = reason
        return self._profile_view(chosen)

    @staticmethod
    def _profile_view(chosen) -> dict:
        """Normalise a raw *arr quality profile into the resolver's add-payload view
        ``{id, name, cutoff, max_quality, max_res}``. Sizing keys off the highest quality
        the profile is ALLOWED to grab (``profile_max_quality``), not its cutoff (the
        "good enough, stop upgrading" floor). ``None`` → the neutral default profile."""
        if chosen is None:
            return {"id": 1, "name": "(default)", "cutoff": None, "max_quality": None, "max_res": -1}
        max_res, max_q = profile_max_quality(chosen)
        return {
            "id": chosen.get("id"),
            "name": chosen.get("name"),
            "cutoff": chosen.get("cutoff"),
            "max_quality": max_q,
            "max_res": max_res,
        }

    @staticmethod
    def _target_resolution_for_score(score: int) -> int:
        """Watchability/acquisition score → target resolution tier. Delegates to the shared
        ``size_model.target_resolution_for_score`` so add-time and the dual-version HD baseline
        agree on what resolution a score justifies."""
        return target_resolution_for_score(score)

    def _profile_for_score(self, profiles: list, score: int):
        """Pick the highest-resolution profile whose max allowed resolution does not
        exceed the score's target tier. Falls back to the lowest available profile when
        every profile exceeds the tier."""
        target = self._target_resolution_for_score(score)
        ranked = sorted(
            ((profile_max_quality(p)[0] or 0, p) for p in profiles),
            key=lambda t: t[0],
        )
        eligible = [p for (res, p) in ranked if res <= target]
        if eligible:
            return eligible[-1]
        return ranked[0][1] if ranked else None

    def resolve_quality(self, enriched: dict, score) -> dict:
        """Re-pick the quality profile from the score (the matrix) AFTER scoring, then
        refresh the size estimate. No-op when the candidate was skipped/instance-less
        or when a fixed ``acquisition.quality_profile`` name is configured (explicit
        override already applied in :meth:`prepare`)."""
        if enriched.get("skip_reason") or not enriched.get("instance"):
            return enriched
        if (self._acq.get("quality_profile") or "").strip():
            return enriched
        is_show = enriched.get("type") == "show"
        gw = self.gw.get("sonarr" if is_show else "radarr")
        if not gw:
            return enriched
        profile = self._pick_profile(gw, enriched["instance"], score=score,
                                     is_show=is_show, is_anime=bool(enriched.get("is_anime")))
        size, unit = self._expected_size(profile, int(enriched.get("runtime") or 0), is_movie=not is_show)
        enriched["quality_profile"]  = profile
        enriched["profile_reason"]   = self._last_profile_reason
        enriched["expected_size_gb"] = size
        enriched["size_unit"]        = unit
        return enriched

    # ── dual-version (1080p baseline + 4K bonus) ──────────────────────────────
    # Active only when the operator chose routing.movies.4k_policy == "both" AND a DISTINCT
    # 4K Radarr instance exists. Then a movie is kept as a <=1080 baseline on the standard
    # instance (the durable, remote-play floor) PLUS — when warranted — a 2160p copy on the
    # 4K instance. With the default "highest_only" every method below short-circuits before
    # touching anything, so existing installs add exactly one copy as before.
    _UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")

    def _uhd_instance(self, gw):
        """The DISTINCT 4K/UHD Radarr instance name, or None when there is no separate one.
        ``categorized_instance`` returns the default for an unmapped/whitespace label, so a
        label that resolves back to the default means "no dedicated 4K session" → degrade to a
        single baseline. Alias-aware (``4K``/``4k``/``uhd``/``2160p``) because the role map is
        written as ``4K`` while the folder bucket is ``4k`` (see RadarrStep.categorize_labels)."""
        default_inst = gw.default_instance()
        for label in self._UHD_LABELS:
            inst = gw.categorized_instance(label)
            if inst and inst != default_inst:
                return inst
        return None

    def dual_active(self, enriched) -> bool:
        """True when this candidate should be kept as a dual version — a <=1080 baseline on
        the standard instance plus a 2160p copy on a distinct 4K instance. Requires
        routing.configured, movies.4k_policy=='both', a MOVIE (not a show), a NON-anime route
        (anime movies ride the dedicated anime instance — a single copy), and an actual
        separate 4K Radarr instance."""
        if enriched.get("type") == "show":
            return False
        mv = self._routing.get("movies", {}) or {}
        if not self._routing_on or mv.get("4k_policy") != "both":
            return False
        if self._route_category(enriched.get("category", "standard"), False) == "anime":
            return False
        gw = self.gw.get("radarr")
        if not gw or not gw.available:
            return False
        return self._uhd_instance(gw) is not None

    def apply_hd_baseline(self, enriched: dict) -> dict:
        """When :meth:`dual_active`, RE-CAP the primary copy to the score-adaptive <=1080
        baseline on the standard instance — the durable, remote-play-friendly floor any client
        can direct-play. The default ``_pick_profile`` caps at the SCORE tier (2160 for a high
        score), which would put a 4K file on the standard instance; this clamps it to <=1080 via
        the shared ``dual_version.pick_hd_profile`` so the 2160p copy lives ONLY on the 4K
        instance. No-op (returns unchanged) when dual is inactive. Mutates + returns ``enriched``."""
        if not self.dual_active(enriched):
            return enriched
        gw = self.gw.get("radarr")
        raw = dual_version.pick_hd_profile(gw.quality_profiles(enriched["instance"]) or [],
                                           enriched.get("score"))
        if raw is not None:
            view = self._profile_view(raw)
            size, unit = self._expected_size(view, int(enriched.get("runtime") or 0), is_movie=True)
            enriched["quality_profile"]  = view
            enriched["profile_reason"]   = (f"dual-version HD baseline (clamped to '{view.get('name')}' "
                                            f"as the <=1080p durable floor; 4K copy added separately)")
            enriched["expected_size_gb"] = size
            enriched["size_unit"]        = unit
        enriched["dual_baseline"] = True
        return enriched

    def _uhd_profile(self, gw, inst) -> dict:
        """The 4K copy's profile: the HIGHEST-resolution profile the 4K instance offers (a 2160p
        library by construction). Not score-capped — the 4K instance is the premium tier, so the
        copy that lands there is always its top quality. ``profiles[0]`` would be fragile to
        ordering, so we explicitly take the max by allowed resolution."""
        profiles = gw.quality_profiles(inst) or []
        if not profiles:
            return self._profile_view(None)
        top = max(profiles, key=lambda p: (profile_max_quality(p)[0] or 0))
        return self._profile_view(top)

    def plan_uhd_companion(self, enriched: dict, *, space_ok, keep_tagged: bool = False,
                           can_remote_play: bool = True) -> "dict | None":
        """Build the 2160p companion add (a SECOND enriched dict on the 4K instance) to sit ON
        TOP of the <=1080 baseline, or ``None``. Emitted only when :meth:`dual_active`, the 4K
        copy is WARRANTED (``dual_version.wants_uhd`` — keep-tagged OR score>=threshold, AND the
        4K instance has space, AND a viewer can use it), and the title is not already in the 4K
        library. ``space_ok(inst) -> bool`` reports whether an instance is above its pressure
        band (the bonus is never added at the baseline's expense). Does NOT mutate the primary.

        ``keep_tagged`` is False at add time (a fresh candidate carries no *arr tags yet).
        ``can_remote_play`` is supplied by the caller (the Stage-C transcode gate computes it once
        per run via ``routing_targets.uhd_remote_play_ok``); it defaults True so an unwired/flag-off
        caller behaves exactly as before. The threshold is ``routing.movies.4k_dual_min_score``
        (0/unset → the shared default 70)."""
        if not self.dual_active(enriched):
            return None
        gw = self.gw.get("radarr")
        inst_4k = self._uhd_instance(gw)
        mv = self._routing.get("movies", {}) or {}
        try:
            threshold = int(mv.get("4k_dual_min_score") or dual_version.DEFAULT_UHD_SCORE)
        except (TypeError, ValueError):
            threshold = dual_version.DEFAULT_UHD_SCORE
        if not dual_version.wants_uhd(keep_tagged=keep_tagged, score=enriched.get("score"),
                                      space_allows=bool(space_ok(inst_4k)), uhd_threshold=threshold,
                                      can_remote_play=can_remote_play):
            return None
        ext_id, id_field = enriched.get("ext_id"), enriched.get("id_field")
        if gw.in_library(inst_4k, id_field, ext_id):
            return None                                # already a 4K copy — nothing to add
        profile = self._uhd_profile(gw, inst_4k)
        if profile.get("id") is None:
            return None                                # 4K instance has no usable profile
        root = self._movie_root_folders.get("4k") or self._pick_root_folder(gw, inst_4k, False, "standard")
        if not root:
            return None
        size, unit = self._expected_size(profile, int(enriched.get("runtime") or 0), is_movie=True)
        companion = dict(enriched)
        companion.update({
            "instance": inst_4k,
            "quality_profile": profile,
            "profile_reason": f"4K instance top quality ('{profile.get('name')}', premium tier)",
            "root_folder": root,
            "expected_size_gb": size,
            "size_unit": unit,
            "is_uhd_companion": True,
        })
        return companion

    def _route_category(self, category, is_show):
        """Apply the operator's ``routing`` preferences to the classified category, returning
        the EFFECTIVE library bucket for folder (and anime-movie instance) routing. A no-op
        until the routing step has run (``routing.configured``), so a never-onboarded install
        routes exactly as before. Redirects only when a bucket is turned OFF:
          • movie kids  → standard  when routing.movies.kids_bucket_enabled is off
          • movie anime → standard  when routing.movies.anime_policy == "standard_only"
          • show  anime → series    when routing.tv.anime_policy == "series_type"
          • show  kids  → series    when routing.tv.kids_bucket_enabled is off
        ``seriesType``=anime is tracked separately, so a ``series_type`` anime still parses as
        anime — it just lands in the series folder instead of a dedicated anime one. Delegates to
        the shared ``library_router.route_category`` so the re-organizer makes the same decision."""
        if not self._routing_on:
            return category
        return library_router.route_category(category, is_show, self._routing)

    def _pick_root_folder(self, gw, inst, is_show, category) -> str:
        # Show → the configured folder for its library bucket (series fallback).
        if is_show:
            target = self._root_folders.get(category) or self._root_folders.get("series")
            if target:
                return target
        else:
            # Movie → the configured folder for its bucket (standard fallback).
            target = self._movie_root_folders.get(category) or self._movie_root_folders.get("standard")
            if target:
                return target
        folders = gw.root_folders(inst) or []
        for f in folders:
            if isinstance(f, dict) and f.get("path"):
                return f["path"]
        return ""

    def _expected_size(self, profile, runtime_min, is_movie):
        """
        Estimated grab size at the profile's TOP allowed quality. Movies → total
        size; shows → per-episode. Returns ``(gb | None, unit)``.

        Sizing is delegated to the shared ``size_model`` so every estimator in
        the app agrees. ``measured`` is left at None here (the calibrated table
        is already derived from this library); it could be threaded in
        per-instance later if the acquisition pipeline gains parquet access.
        """
        unit = "movie" if is_movie else "per-episode"
        if not runtime_min:
            return None, unit
        gb = estimate_gb(
            profile.get("max_quality"),
            runtime_min,
            n_items=1,
            resolution=(profile.get("max_res") if profile.get("max_res", -1) > 0 else None),
        )
        return (round(gb, 2) if gb > 0 else None), unit

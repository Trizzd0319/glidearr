"""
resolver.py — decide WHERE, WHAT QUALITY, and EXPECTED SIZE for a candidate.
================================================================================
For each candidate: look it up against the relevant *arr (to get genres/runtime/
canonical ids), dedup against the target instance's library, then resolve the
target instance, quality profile, root folder, and an estimated size. The
resolved object doubles as the add-payload base in the adder.

Library routing: every show is classified into one library bucket via the shared
``library_classifier`` (precedence anime → kids → reality → documentary → series)
and routed to the matching ``rootFolders`` entry. Anime shows keep ``seriesType=anime``
but stay on the single Sonarr instance; anime MOVIES route to Radarr's optional
dedicated anime instance. MAL candidates are anime by construction. See
``support/utilities/library_classifier.py`` for the classification contract.
"""
from __future__ import annotations

from scripts.support.utilities.library_classifier import classify_movie, classify_show, is_anime_media
from scripts.support.utilities.size_model import estimate_gb, profile_max_quality


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
        self._reality_genres = [str(g) for g in (config.get("realityGenres", []) or []) if g]
        self._doc_genres = [str(g) for g in (config.get("documentaryGenres", []) or []) if g]
        self._preschool_genres = [str(g) for g in (config.get("preschoolGenres", []) or []) if g]
        self._non_kids_genres = [str(g) for g in (config.get("nonKidsGenres", []) or []) if g]
        self._root_folders = config.get("rootFolders", {}) or {}
        self._movie_root_folders = config.get("movieRootFolders", {}) or {}
        self._acq = config.get("acquisition", {}) or {}

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
                is_anime_hint=bool(cand.get("is_anime")),
                anime_genres=self._anime_genres,
                kids_genres=self._kids_genres,
                kids_certs=self._kids_certs,
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

        # Anime MOVIES route to Radarr's optional dedicated "anime" instance — and fall back to the
        # DEFAULT instance when no anime session is configured (gateway.categorized_instance returns
        # default for an unmapped label). Anime SHOWS stay on the single Sonarr instance (Sonarr is
        # single-instance now — no tier/anime session to split out); seriesType=anime is set on the
        # add payload regardless. A kids-routed anime is category=="kids" (kids wins).
        if category == "anime" and not is_show:
            inst = gw.categorized_instance("anime")
        else:
            inst = default_inst

        if gw.in_library(inst, id_field, ext_id):
            out["skip_reason"] = "already in library"
            return out

        profile = self._pick_profile(gw, inst)
        root = self._pick_root_folder(gw, inst, is_show, category)
        size, unit = self._expected_size(profile, runtime, is_movie=not is_show)

        out.update({
            "instance": inst,
            "quality_profile": profile,
            "root_folder": root,
            "expected_size_gb": size,
            "size_unit": unit,
            "genres": genres,
            "runtime": runtime,
            "year": year,
            "certification": certification,
            "category": category,
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

    def _pick_profile(self, gw, inst, score=None) -> dict:
        profiles = gw.quality_profiles(inst) or []
        want = (self._acq.get("quality_profile") or "").strip().lower()
        chosen = None
        if want:
            # Explicit config override by profile name — always wins.
            chosen = next((p for p in profiles if str(p.get("name", "")).lower() == want), None)
        if chosen is None and not want and score is not None and profiles:
            # Matrix-driven: map the watchability/acquisition score to a target
            # resolution tier and pick the highest-quality profile at or under it,
            # so high-want adds get higher quality and low-want adds get 720p/SD —
            # instead of always defaulting to the first ("Any") profile.
            chosen = self._profile_for_score(profiles, int(score))
        if chosen is None and profiles:
            chosen = profiles[0]
        if chosen is None:
            return {"id": 1, "name": "(default)", "cutoff": None, "max_quality": None, "max_res": -1}
        # Size off the highest quality the profile is ALLOWED to grab, not its
        # cutoff (the "good enough, stop upgrading" floor).
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
        """Watchability/acquisition score → target resolution tier (naming-agnostic,
        mirrors the scorer's QUALITY_PROFILE_THRESHOLDS bands)."""
        if score >= 70:
            return 2160
        if score >= 35:
            return 1080
        if score >= 20:
            return 720
        return 480

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
        profile = self._pick_profile(gw, enriched["instance"], score=score)
        size, unit = self._expected_size(profile, int(enriched.get("runtime") or 0), is_movie=not is_show)
        enriched["quality_profile"]  = profile
        enriched["expected_size_gb"] = size
        enriched["size_unit"]        = unit
        return enriched

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

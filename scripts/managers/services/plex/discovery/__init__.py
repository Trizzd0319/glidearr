"""
plex/discovery — "This Week in History" anniversary-shelf BUILDER (READ-ONLY, Phase 2).
================================================================================
For each opted-in Home profile, build two per-user shelf plans — ``Anniversary Picks`` (movies) +
``On This Week`` (shows) — from titles released/aired during the current Sun–Sat week in ANY past
year. The pure pipeline lives in ``machine_learning/discovery`` (window → candidates → score); this
manager only GATHERS the inputs (the full Radarr catalog, the owned-anywhere sets, the owned Plex
inventories, each user's age tier + library allowlist), runs the per-user assembly, caches the two
plans (which the existing playlist writeback renders), and caches a household PREVIEW of the net-new
finds. It performs NO acquisition and NO deletion — net-new picks are preview-only until Phase 3.

Default-OFF behind ``plex.playlists.this_week_in_history.enabled`` → byte-identical when unset.
"""
from __future__ import annotations

from datetime import datetime

from scripts.managers.machine_learning.discovery.candidates import (
    episode_candidates,
    movie_candidates,
)
from scripts.managers.machine_learning.discovery.scoring import score_and_floor
from scripts.managers.machine_learning.discovery.shelf import (
    gated_plan,
    movie_resolver,
    personalize,
    show_resolver,
)
from scripts.managers.machine_learning.playlists.cert_gate import ADULT, tier_level
from scripts.managers.services.acquisition.scorer import AcquisitionScorer
from scripts.managers.services.plex.playlists.builder import PlexPlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_resolver import _norm as _norm_movie
from scripts.managers.services.plex.playlists.movie_resolver import watched_movie_keys
from scripts.managers.services.plex.playlists.tv_resolver import _norm as _norm_tv
from scripts.managers.services.plex.playlists.writeback import (
    _TWIH_MOVIE_PLAN_KEY,
    _TWIH_SHOW_PLAN_KEY,
)

_MOVIE_INV_KEY = "plex/movies/owned_inventory"
_EP_INV_KEY = "plex/episodes/owned_inventory"
_SECTIONS_KEY = "plex/sections"
_PREVIEW_KEY = "discovery/this_week/preview"           # household would-shelf preview (read by --status)
_DEFAULT_POPULARITY_WEIGHT = 0.30   # anniversary-shelf popularity weight (vs the add pipeline's 0.10)


class DiscoveryShelfBuilderManager(PlexPlaylistBuilderManager):
    parent_name = "PlexManager"

    # ── run (I/O gather → pure assembly → cache) ─────────────────────────────────
    def run(self) -> dict:
        cfg = self._twih_cfg()
        if not cfg.get("enabled"):
            self.logger.log_debug("[Anniversary] this_week_in_history disabled — skipped.")
            return {"enabled": False}

        tracked = [u for u in self._tracked_users() if self._opted_in(u, cfg)]
        if not tracked:
            self.logger.log_info("[Anniversary] enabled but no opted-in users — nothing to build.")
            return {"enabled": True, "users": 0, "built": 0}

        now = self._household_now(cfg)
        tzname = cfg.get("timezone") or None
        cap = self._cap(cfg)
        floor = self._floor(cfg)

        # Candidate pools (run-scoped, shared across users — household watchability is user-independent).
        movie_rows, owned_tmdbs = self._normalized_movie_catalog()
        owned_eps = self._load_owned_episodes()
        movie_cands = movie_candidates(movie_rows, now, tz=tzname, owned_tmdbs=owned_tmdbs,
                                       min_votes=self._min_votes(cfg))
        show_cands = episode_candidates(owned_eps, now, tz=tzname)
        self._attach_movie_certs(movie_cands)
        self._attach_show_meta(show_cands, owned_eps)

        # Score ONCE, then per-user gate/cap (no N cold passes). This shelf's scorer weights all-time
        # popularity heavier than the add pipeline (a notable past title should beat a recent obscure
        # one on a HISTORY shelf) — same metric, just re-weighted; the add pipeline is untouched.
        scorer = AcquisitionScorer(self.global_cache, self.logger, self.config,
                                   weight_overrides=self._scorer_weight_overrides(cfg))
        scored_movies = score_and_floor(movie_cands, scorer, floor=floor)
        scored_shows = score_and_floor(show_cands, scorer, floor=floor)

        movie_inv = self._cache_get(_MOVIE_INV_KEY, {}) or {}
        ep_inv = self._cache_get(_EP_INV_KEY, {}) or {}
        mr, sr = movie_resolver(movie_inv), show_resolver(ep_inv)   # unscoped — the household preview
        movie_keys, show_keys = self._section_type_keys()
        users_mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        profile_ages = self._profile_ages()

        # Per-user re-rank: the household score is the watchability BASE; each viewer's own genre
        # affinity tilts the order (affinity > household), matching how the other playlists personalize.
        weights, gm_opts = self._priority_weights(), self._genre_match_opts()
        hh_max = max((c.get("score") for c in (scored_movies + scored_shows)
                      if isinstance(c.get("score"), (int, float))), default=0.0) or 1.0

        built = 0
        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                continue
            level = tier_level(u.get("restriction_profile"),
                               profile_ages.get(u.get("title")) or profile_ages.get(safe))
            allowed = users_mgr.allowed_sections(u) if users_mgr else set()
            user_aff = self._user_affinity(u.get("tautulli_username"))
            # PER-SECTION library gate (fail-CLOSED): each owned candidate resolves only from a section
            # this user was actually shared (``section`` on the owned inventories). A viewer granted a
            # SUBSET of a medium's libraries now gets a properly-scoped shelf instead of nothing; an
            # un-granted/section-less entry never resolves. Net-new candidates carry no section → still
            # preview-only. We only personalize a medium the user can reach at all (skips dead work).
            mr_u = movie_resolver(movie_inv, allowed=allowed)
            sr_u = show_resolver(ep_inv, allowed=allowed)
            # EXPLORATORY: the shelf surfaces only titles this viewer HASN'T SEEN, so owned picks are
            # filtered to per-user UNWATCHED (the same Tautulli finished-set the playlist builders use).
            # Movies are title-level (finished THIS movie); shows are SERIES-level (watched ANY owned
            # episode → the whole series is excluded, not just the pilot). No Tautulli match → empty →
            # fail-OPEN (show owned, don't hide an empty shelf).
            uid = u.get("tautulli_user_id")
            movie_watched = self._watched_movies_for(uid)
            watched_tvdbs = self._watched_series_tvdbs(uid, ep_inv)
            # Match ALL the identities the watched set carries (ratingKey + the (title,year) tuple that
            # survives a Plex re-scan) — a bare-ratingKey check goes inert after a re-scan and re-surfaces
            # finished titles. Series exposure is precomputed in watched_tvdbs (same robustness for TV).
            m_seen = lambda c, rk, _w=movie_watched: (
                str(rk) in _w or (_norm_movie(c.get("title")), self._coerce_int(c.get("year"))) in _w)
            s_seen = lambda c, rk, _t=watched_tvdbs: self._coerce_int(c.get("tvdb_id")) in _t
            m_scored = (personalize(scored_movies, user_aff, hh_max=hh_max, weights=weights,
                                    gm_opts=gm_opts) if (movie_keys & allowed) else [])
            s_scored = (personalize(scored_shows, user_aff, hh_max=hh_max, weights=weights,
                                    gm_opts=gm_opts) if (show_keys & allowed) else [])
            m_items, _ = gated_plan(m_scored, level=level, cap=cap, resolve=mr_u, seen=m_seen)
            s_items, _ = gated_plan(s_scored, level=level, cap=cap, resolve=sr_u, seen=s_seen)
            self.global_cache.set(f"{_TWIH_MOVIE_PLAN_KEY}/{safe}", self._plan_dict("twih_movie", m_items))
            self.global_cache.set(f"{_TWIH_SHOW_PLAN_KEY}/{safe}", self._plan_dict("twih_show", s_items))
            self.logger.log_info(
                f"[Anniversary] {self._anon(u)} -> {len(m_items)} movie + {len(s_items)} show "
                f"owned pick(s) (tier {level}).")
            built += 1

        self._detect_saves(tracked, scored_movies, scored_shows)
        self._publish_preview(scored_movies, scored_shows, cap, mr, sr, built)
        self.logger.log_info(
            f"[Anniversary] built {built} user shelf plan(s) from "
            f"{len(scored_movies)} movie + {len(scored_shows)} show candidate(s) "
            f"(read-only — no grabs, no writes).")
        return {"enabled": True, "users": len(tracked), "built": built,
                "movie_candidates": len(scored_movies), "show_candidates": len(scored_shows)}

    # ── household preview (the net-new finds; what acquisition WOULD grab in Phase 3) ──
    def _publish_preview(self, scored_movies, scored_shows, cap, mr, sr, users_built) -> None:
        try:
            m_items, m_new = gated_plan(scored_movies, level=ADULT, cap=cap, resolve=mr)
            s_items, s_new = gated_plan(scored_shows, level=ADULT, cap=cap, resolve=sr)
            preview = {
                "users": users_built,
                "owned_movies": len(m_items), "owned_shows": len(s_items),
                "net_new_movies": len(m_new), "net_new_shows": len(s_new),
                "movies": m_new, "shows": s_new,
            }
            if self.global_cache:
                self.global_cache.set(_PREVIEW_KEY, preview)
            self._log_net_new(m_new + s_new)
        except Exception as e:
            self.logger.log_debug(f"[Anniversary] preview build skipped: {e}")

    def _log_net_new(self, rows) -> None:
        if not rows:
            return
        try:
            data = [[(r.get("title") or "")[:48], r.get("media") or "",
                     str(r.get("years_ago") if r.get("years_ago") is not None else ""),
                     "" if r.get("score") is None else str(r.get("score"))]
                    for r in rows[:25]]
            self.logger.log_table(["Title", "Kind", "Yrs", "Score"], data,
                                  title="[dry-run] Anniversary net-new finds",
                                  caption="What acquisition would grab this week (Phase 3 — not grabbed now).")
        except Exception:
            pass

    # ── SAVE detection (read-only): an anniversary title on a user's watchlist = a KEEP signal ──
    def _detect_saves(self, tracked, scored_movies, scored_shows) -> None:
        """An anniversary candidate that a user has put on their Plex watchlist is an implicit SAVE —
        recorded (sticky, merge-only) under ``discovery/saved/{safe}``. ISOLATED: this signal is read
        only by the discovery layer and NEVER feeds ``tautulli/affinity`` / the deletion model. Matches
        by ownership id (tmdb/tvdb), never title."""
        union = self._cache_get("plex/watchlist/union", []) or []
        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                continue
            per_user = self._cache_get(f"plex/users/{safe}/watchlist", None)
            want_tmdb, want_tvdb = self._watchlist_ids(per_user if per_user is not None else union)
            if not (want_tmdb or want_tvdb):
                continue
            saved = dict(self._cache_get(f"discovery/saved/{safe}", {}) or {})
            before = len(saved)
            for c in scored_movies:
                t = self._coerce_int(c.get("tmdb_id"))
                if t is not None and t in want_tmdb:
                    saved[f"tmdb:{t}"] = {"media": "movie", "title": c.get("title"), "source": "watchlist"}
            for c in scored_shows:
                t = self._coerce_int(c.get("tvdb_id"))
                if t is not None and t in want_tvdb:
                    saved[f"tvdb:{t}"] = {"media": "show", "title": c.get("series_title"),
                                         "source": "watchlist"}
            if self.global_cache and len(saved) != before:
                self.global_cache.set(f"discovery/saved/{safe}", saved)

    def _watchlist_ids(self, entries):
        """``(tmdb_ints, tvdb_ints)`` from a watchlist union/list of ``{ids: {tmdb, tvdb}}`` entries."""
        tmdb, tvdb = set(), set()
        for e in entries or []:
            ids = (e.get("ids") if isinstance(e, dict) else None) or {}
            t = self._coerce_int(ids.get("tmdb"))
            v = self._coerce_int(ids.get("tvdb"))
            if t is not None:
                tmdb.add(t)
            if v is not None:
                tvdb.add(v)
        return tmdb, tvdb

    # ── gather: movies ───────────────────────────────────────────────────────────
    def _normalized_movie_catalog(self):
        """``(rows, owned_tmdbs)``: the FULL Radarr catalog (owned + unowned, every instance) projected
        to the snake_case shape ``movie_candidates`` reads, plus the global owned-anywhere tmdb set."""
        rows: list = []
        owned: set = set()
        seen: set = set()
        for inst in self._radarr_instances():
            raw = self._cache_get(f"radarr.movies.{inst}.full", []) or []
            if isinstance(raw, dict):
                raw = list(raw.values())
            for m in raw:
                if not isinstance(m, dict):
                    continue
                tmdb = self._coerce_int(m.get("tmdbId", m.get("tmdb_id")))
                if tmdb is None:
                    continue
                if bool(m.get("hasFile")):
                    owned.add(tmdb)
                if tmdb in seen:
                    continue
                seen.add(tmdb)
                rows.append(self._norm_movie(m, tmdb))
        return rows, owned

    @staticmethod
    def _norm_movie(m, tmdb) -> dict:
        ratings = m.get("ratings") if isinstance(m.get("ratings"), dict) else {}
        tmdb_r = ratings.get("tmdb") if isinstance(ratings.get("tmdb"), dict) else {}
        return {
            "tmdb_id": tmdb,
            "title": m.get("title"),
            # theatrical first (the iconic "released this week" date), then home dates as a fallback.
            "release_date": m.get("inCinemas") or m.get("digitalRelease") or m.get("physicalRelease"),
            "year": m.get("year"),
            "genres": list(m.get("genres") or []),
            "vote_count": tmdb_r.get("votes"),
            "rating": tmdb_r.get("value"),
            "certification": m.get("certification"),
        }

    def _radarr_instances(self) -> list:
        insts = [k for k, v in ((self.config.get("radarr_instances", {}) if self.config else {}) or {}).items()
                 if k != "default_instance" and isinstance(v, dict)]
        return insts or ["radarr"]

    # ── gather: age-gate metadata (cert + Common Sense age fallback) ──────────────
    def _attach_movie_certs(self, movie_cands) -> None:
        """Movie candidates already carry ``certification`` (Radarr); attach the CSM-age fallback
        (tmdb → Common Sense age) for titles with no certification."""
        csm = self._movie_csm_ages()
        if not csm:
            return
        for c in movie_cands:
            c["csm_age"] = csm.get(self._coerce_int(c.get("tmdb_id")))

    def _attach_show_meta(self, show_cands, owned_eps) -> None:
        """Attach ``certification`` + ``csm_age`` (the age gate) and ``genres`` (the per-user affinity
        re-rank) to each show candidate by joining tvdb → series_id (owned-episode rows) → the series
        cert / Common Sense age / genre maps."""
        tvdb_to_series: dict = {}
        for r in owned_eps or []:
            tvdb = self._coerce_int(r.get("series_tvdb_id"))
            sid = r.get("series_id")
            if tvdb is not None and sid is not None and tvdb not in tvdb_to_series:
                tvdb_to_series[tvdb] = sid
        certs, csm = self._series_certs(), self._series_csm_ages()
        _, series_genres = self._series_scores_and_genres()
        for c in show_cands:
            sid = tvdb_to_series.get(self._coerce_int(c.get("tvdb_id")))
            if sid is None:
                continue
            c["certification"] = certs.get(sid)
            c["csm_age"] = csm.get(sid)
            if not c.get("genres"):
                c["genres"] = list(series_genres.get(sid) or [])

    def _watched_movies_for(self, user_id) -> set:
        """Per-user finished-MOVIE identities (Plex ratingKeys + (title,year)) from Tautulli — the
        exploratory filter for owned movies. ``set()`` when the profile has no Tautulli match (→ the
        shelf fails OPEN and shows owned). Mirrors MoviePlaylistBuilderManager._watched_movies_for; the
        TV twin (``_watched_for``) is inherited from the base builder."""
        if user_id is None or not self.registry:
            return set()
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return set()
        try:
            return watched_movie_keys(hm.get_all_history_cached(user_id))
        except Exception:
            return set()

    def _watched_series_tvdbs(self, user_id, ep_inv) -> set:
        """tvdb ids of series this viewer has watched ANY OWNED episode of — the SERIES-level exposure
        signal (watching one episode means they've been exposed to the show, so it's not a discovery,
        even if the watched episode isn't the pilot the shelf would surface). Built by intersecting the
        user's finished-episode ratingKeys (Tautulli) with the owned-episode inventory, whose keys are
        ``{tvdb}:{season}:{episode}``. ``set()`` on no Tautulli match → fail-OPEN."""
        watched_eps = self._watched_for(user_id)
        if not watched_eps:
            return set()
        out: set = set()
        for join_key, entry in (ep_inv or {}).items():
            if not isinstance(entry, dict):
                continue
            parts = str(join_key).split(":")
            tvdb = self._coerce_int(parts[0])
            if tvdb is None or tvdb in out:
                continue
            rk = entry.get("rating_key")
            st = _norm_tv(entry.get("series_title"))
            season = self._coerce_int(parts[1]) if len(parts) > 1 else None
            episode = self._coerce_int(parts[2]) if len(parts) > 2 else None
            ep_title = _norm_tv(entry.get("title"))
            # Match ANY identity watched_episode_keys carries — the (series,season,episode) tuple is the
            # one that survives a Plex re-scan (a bare ratingKey often doesn't), the same recovery the
            # shipped builders rely on.
            if ((rk is not None and str(rk) in watched_eps)
                    or (st and season is not None and episode is not None
                        and (st, season, episode) in watched_eps)
                    or (st and ep_title and (st, ep_title) in watched_eps)):
                out.add(tvdb)
        return out

    def _movie_csm_ages(self) -> dict:
        """{tmdb_id(int): Common Sense age(int)} from the MDBList movie age cache — the cert-gate
        fallback for owned movies that carry no certification. {} on any failure."""
        try:
            from scripts.managers.services.mdblist import age_cache
            out: dict = {}
            for k, v in (age_cache.load() or {}).items():
                if isinstance(v, int):
                    try:
                        out[int(k)] = v
                    except (TypeError, ValueError):
                        continue
            return out
        except Exception:
            return {}

    # ── per-user library media access (movie/show section-type level) ────────────
    def _section_type_keys(self):
        """``(movie_section_keys, show_section_keys)`` from the global section index — the cheap
        medium-reachability check (a user whose allowlist intersects no movie-type section can't have a
        movie shelf, so we skip personalizing it). Actual scoping is per-section in the resolvers."""
        sections = self._cache_get(_SECTIONS_KEY, {}) or {}
        movie, show = set(), set()
        if isinstance(sections, dict):
            for k, v in sections.items():
                t = (v or {}).get("type") if isinstance(v, dict) else None
                if t == "movie":
                    movie.add(str(k))
                elif t == "show":
                    show.add(str(k))
        return movie, show

    # ── config knobs (plex.playlists.this_week_in_history.*) ─────────────────────
    def _twih_cfg(self) -> dict:
        return (self._pl_cfg().get("this_week_in_history", {}) or {})

    def _opted_in(self, user, cfg) -> bool:
        """A user is in scope when listed in ``opt_in_users`` (by title or safe_user); an EMPTY list
        means the feature is on household-wide → every tracked user."""
        want = cfg.get("opt_in_users") or []
        if not want:
            return True
        want = {str(w).strip().lower() for w in want if str(w).strip()}
        return (str(user.get("title", "")).strip().lower() in want
                or str(user.get("safe_user", "")).strip().lower() in want)

    def _cap(self, cfg) -> int:
        try:
            return max(1, int(cfg.get("cap", 7)))
        except (TypeError, ValueError):
            return 7

    def _floor(self, cfg) -> int:
        try:
            return int(cfg.get("floor", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _min_votes(self, cfg) -> int:
        try:
            return int(cfg.get("min_votes", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _popularity_weight(self, cfg) -> float:
        """The all-time-popularity signal weight for the anniversary shelf — higher than the add
        pipeline's 0.10 so a NOTABLE past title outranks a recent obscure one on a HISTORY shelf
        (0 = popularity ignored). Reuses the scorer's existing log-scaled-votes metric; only its
        weight changes. Invalid/negative → the default."""
        try:
            w = float(cfg.get("popularity_weight", _DEFAULT_POPULARITY_WEIGHT))
            return w if w >= 0 else _DEFAULT_POPULARITY_WEIGHT
        except (TypeError, ValueError):
            return _DEFAULT_POPULARITY_WEIGHT

    def _scorer_weight_overrides(self, cfg) -> dict:
        """Per-shelf scorer weight overrides (see ``AcquisitionScorer(weight_overrides=…)``)."""
        return {"popularity": self._popularity_weight(cfg)}

    def _household_now(self, cfg):
        tzname = cfg.get("timezone")
        if tzname:
            try:
                from zoneinfo import ZoneInfo
                return datetime.now(ZoneInfo(str(tzname)))
            except Exception:
                pass
        return datetime.now()

    @staticmethod
    def _coerce_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _plan_dict(family, items) -> dict:
        """The cached-plan shape writeback reads (``_load_plan`` needs non-empty ``items``;
        ``_desired_items`` re-resolves each item's ``rating_key``)."""
        return {"family": family, "items": items}

    @staticmethod
    def _anon(user) -> str:
        return (user.get("safe_user") or user.get("title") or "?")

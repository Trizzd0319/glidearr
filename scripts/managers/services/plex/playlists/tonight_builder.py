"""plex/playlists/tonight_builder.py — the per-profile list for ONE upcoming day.

"Raina watches Rick and Morty on Tuesdays." This builds tomorrow's list for each
tracked profile from that claim: five to ten different groups, ONE item each.

    Tautulli history -> habits.weekday_profile   (which groups own which weekday)
                     -> habits.shows_for_day     (the ones that own TOMORROW)
                     -> candidates.*             (what to offer for each)
                     -> candidates.pick_one_per_group
                     -> plex/playlists/tonight_plan/{safe_user}

BUILD + CACHE ONLY. Like every other builder here it writes a PLAN and performs
no Plex calls; ``PlaylistWritebackManager`` turns the plan into a playlist and is
separately armed. Nothing in this file can touch Plex.

WHY PER-USER AND NOT A SHELF. A weekday habit is a property of one profile. An
account-wide smart shelf cannot hold a different five shows for each member, so
Tonight is a per-user PLAYLIST alongside Up Next - which also means it cannot be
pinned to a Home row, because Plex will not promote a playlist. That trade was
made deliberately: personal and correct beats account-wide and generic.

TOMORROW, NOT TODAY. The list should be waiting when somebody sits down, not
appear after. ``day_offset`` is 1 by default; the plan records the date it was
built FOR, which is also what ``outcomes.py`` scores the play against.

ANTI-BINGE. One item per group, never two. If they want the next episode Plex
autoplays it - a list offering six episodes of one series would be a binge queue
wearing a different name.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from scripts.managers.machine_learning.playlists import candidates as C
from scripts.managers.machine_learning.playlists import habits as H
from scripts.managers.machine_learning.playlists.models import PLACEHOLDER_AFFINITY
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager

_PLAN_KEY = "plex/playlists/tonight_plan"           # + /{safe_user}
_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"

#: ``{rating_key: [genre, ...]}``, published because this manager already builds
#: it and nothing else does. ``poster_render`` needs genres to fill the copy for
#: Touch & Go / Hidden Gems / Tonight, and the alternative was a THIRD place that
#: reads the Radarr parquet and re-derives them - which is how the last two
#: silent failures started. Creator publishes; consumers read.
_GENRES_KEY = "plex/playlists/genres_by_rating_key"

#: ``{rating_key: runtime_minutes}``, built from the parquets we ALREADY hold and
#: published for reuse. Measured coverage on this library:
#:
#:   radarr/*/movie_files.parquet   runtime_minutes   938/938   (100%)
#:   sonarr/*/episode_files.parquet runtime_seconds   3939/3949 (99.7% of OWNED)
#:
#: The episode figure looks like 52% against the raw file (16,033 rows) because
#: that parquet also holds episodes we do not own; joined to owned_episodes it is
#: all but complete. So the runtime cap costs ZERO Plex calls in the normal path -
#: an earlier design would have spent ~292 batched /library/metadata calls to fill
#: the same field the download clients already gave us.
_RUNTIME_KEY = "plex/playlists/runtime_minutes_by_rating_key"

#: How many groups the list may carry. Five to ten different shows, one episode
#: each - small enough to scan at the end of a long day.
_DEFAULT_LIMIT = 8

#: Built for TOMORROW by default. 0 would build for today, which is too late to
#: be a recommendation and would score against a day already half spent.
_DEFAULT_DAY_OFFSET = 1


def _affinity(value) -> str | None:
    """A real grouping label, or None for a placeholder / NaN / blank.

    THIS EXISTS BECAUSE OF A LIVE BUG. ``universe_name or collection_name`` looks
    safe and is not: the movie_files parquet stores a missing universe as float
    NaN, and **NaN is truthy in Python**. So the ``or`` short-circuits on NaN,
    ``str(nan)`` is the string ``"nan"``, and every movie without a universe
    collapsed into one group called ``franchise:nan`` - which then outweighed
    every real franchise and became two profiles' entire Tonight list.

    ``models.PLACEHOLDER_AFFINITY`` already guarded this exact class of mistake
    for the grouping path (its comment records a bare "universe" label once
    fusing ~220 unrelated movies into one mega-group). Reusing it here rather
    than writing a second guard keeps one list of junk tokens, not two.
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:      # NaN
        return None
    text = str(value).strip()
    return None if text.casefold() in PLACEHOLDER_AFFINITY else text


def _labels(value) -> list:
    """A clean list of label strings from whatever the parquet round-trip produced.

    THREE SHAPES ARRIVE HERE and only one of them is a plain list. The Radarr
    ``genres`` column comes back as a real list, as a numpy array, or as a JSON
    STRING like ``'["drama"]'`` depending on how the frame was written. Treating
    the third as a comma-separated string yields the single label ``["drama"]``
    - brackets and quotes included - which is exactly what shipped onto a live
    poster reading ``6 under an hour - ["drama"] . Batman Beyond``.

    So: parse JSON first, fall back to a comma split, and drop placeholders.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                import json
                value = json.loads(text)
            except ValueError:
                value = [p for p in text.strip("[]").split(",")]
        else:
            value = text.split(",")
    try:
        items = list(value)
    except TypeError:
        items = [value]
    out = []
    for x in items:
        lab = _affinity(str(x).strip().strip("\"'"))
        if lab:
            out.append(lab)
    return out


class TonightPlaylistBuilderManager(MoviePlaylistBuilderManager):
    """Caches one weekday-habit plan per profile. No Plex writes."""

    parent_name = "PlexManager"

    # ── config ───────────────────────────────────────────────────────────────
    def _tonight_cfg(self) -> dict:
        return (self._pl_cfg().get("tonight", {}) or {})

    def _enabled(self) -> bool:
        """``plex.playlists.tonight.enabled``. Default OFF, so an install that
        has not opted in caches nothing and write-back finds no plan - which is
        byte-identical to Tonight not existing."""
        return bool(self._tonight_cfg().get("enabled", False))

    def _num(self, key, default):
        try:
            return type(default)(self._tonight_cfg().get(key, default))
        except (TypeError, ValueError):
            return default

    def _tz_offset_hours(self) -> float:
        """Local offset for weekday bucketing. "Tuesday night" in a US household
        is Wednesday morning in UTC, so without this every late-evening habit
        lands on the wrong day. Defaults to the HOST's current offset, which is
        right for a desktop install and overridable for a container in UTC."""
        cfg = self._tonight_cfg().get("tz_offset_hours")
        if cfg is not None:
            try:
                return float(cfg)
            except (TypeError, ValueError):
                pass
        off = datetime.now().astimezone().utcoffset()
        return (off.total_seconds() / 3600.0) if off else 0.0

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        if not self._enabled():
            return {"enabled": False, "built": 0}
        tracked = self._tracked_users()
        tv_inv = self._cache_get(_TV_INVENTORY_KEY, {}) or {}
        movie_inv = self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {}
        return self._build_for_users(tracked, tv_inv, movie_inv)

    def _build_for_users(self, tracked, tv_inv, movie_inv) -> dict:
        """The tested core: pure given its inputs plus the history fetch."""
        limit = self._num("limit", _DEFAULT_LIMIT)
        offset = self._num("day_offset", _DEFAULT_DAY_OFFSET)
        threshold = self._num("day_threshold", H.DEFAULT_DAY_THRESHOLD)
        tz = self._tz_offset_hours()

        target = date.today() + timedelta(days=int(offset))
        weekday = target.weekday()
        now_ts = datetime.now(timezone.utc).timestamp()

        # Movie side, keyed by PLEX RATING KEY throughout. The plan must carry
        # ratingKeys (write-back re-resolves against them), and keying the maps
        # the same way means no id translation at the end - which is where a
        # tmdb/ratingKey mix-up would otherwise creep in.
        movie_by_rk, fran_by_rk, gen_by_rk, score_by_rk = self._movie_maps(movie_inv)
        # ratingKey -> tmdb, built HERE from the tmdb-keyed inventory so the plan can
        # carry a stable id. Inverting this later, after a re-scan, is exactly the
        # lookup that breaks.
        tmdb_by_rk = {str(r["rating_key"]): str(t)
                      for t, r in (movie_inv or {}).items()
                      if isinstance(r, dict) and r.get("rating_key") is not None}
        # Publish the genre map for the poster copy. Written even when Tonight
        # itself plans nothing: the map describes the LIBRARY, not this run's
        # picks, and a profile with no habit still has posters to fill.
        if gen_by_rk and self.global_cache:
            try:
                self.global_cache.set(_GENRES_KEY, gen_by_rk)
            except Exception as e:
                self.logger.log_warning(f"[Tonight] could not cache genres: {e}")

        # Runtime, straight from the parquets the download clients already filled.
        runtime = self._runtime_minutes(movie_inv, tv_inv)
        if runtime and self.global_cache:
            try:
                self.global_cache.set(_RUNTIME_KEY, runtime)
            except Exception as e:
                self.logger.log_warning(f"[Tonight] could not cache runtimes: {e}")

        stats = {"enabled": True, "users": len(tracked), "built": 0,
                 "empty": 0, "target_day": weekday,
                 "target_date": target.isoformat()}

        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                continue
            rows = self._history_rows(u.get("tautulli_user_id"))
            if not rows:
                stats["empty"] += 1
                self.logger.log_debug(
                    f"[Tonight] '{self._who(u)}' has no watch history - no habit to read.")
                continue

            def _key(row, _f=fran_by_rk, _g=gen_by_rk):
                return H.derive_key(row, franchise_by_rk=_f,
                                    genre_by_rk={k: (v[0] if v else None)
                                                 for k, v in _g.items()})

            profile = H.weekday_profile(rows, now_ts=now_ts, key=_key,
                                        tz_offset_hours=tz)
            groups = H.shows_for_day(profile, weekday, limit=limit,
                                     threshold=threshold)
            if not groups:
                stats["empty"] += 1
                self.logger.log_debug(
                    f"[Tonight] '{self._who(u)}' has no group above {threshold:.2f} "
                    f"for {target:%A} - nothing is that day's habit yet.")
                continue

            watched_eps = self._watched_for(u.get("tautulli_user_id"))

            # THE CAP IS LEARNED, NOT ASSUMED. "Long films at the weekend" is a
            # claim about somebody else's week - on this household it is wrong for
            # two profiles in three. session_profile starts at the convention and
            # moves as that profile's own history accumulates, so day one is
            # sensible and month three is right.
            sess = H.session_profile(
                rows, runtime_of=lambda r, _rt=runtime: _rt.get(str(r.get("rating_key"))),
                now_ts=now_ts, tz_offset_hours=tz)
            cap = H.runtime_cap_for(sess, weekday,
                                    short_cap_minutes=self._num("short_cap_minutes", 60.0))

            def _accept(item, _cap=cap, _rt=runtime, _tv=tv_inv):
                """Runtime gate for one candidate, applied DURING selection so a
                rejected pick falls through to its group's next option.

                UNKNOWN RUNTIME IS ADMITTED, not excluded - the opposite of the
                habit model's rule, and deliberately so. There the unknown was
                evidence being counted; here it is a candidate being judged, and
                refusing everything unmeasured would empty the list over a gap in
                metadata rather than over anything the household did.
                """
                if _cap is None:
                    return True
                rk = item
                if ":" in str(item):                 # a tvdb:s:e inventory key
                    rk = str((_tv.get(item) or {}).get("rating_key") or item)
                mins = _rt.get(str(rk))
                return mins is None or float(mins) <= float(_cap)

            pools = C.merge_pools(
                C.episode_candidates(
                    tv_inv, is_watched=C.watched_by_rating_key(watched_eps),
                    groups=groups),
                C.movie_candidates(
                    movie_by_rk, is_watched=C.watched_by_rating_key(watched_eps),
                    franchise_by_id=fran_by_rk, genres_by_id=gen_by_rk,
                    score_by_id=score_by_rk, groups=groups),
            )
            picked = C.pick_one_per_group(groups, pools, limit=limit, accept=_accept)
            if not picked:
                stats["empty"] += 1
                continue
            stats["capped"] = stats.get("capped", 0) + (1 if cap is not None else 0)

            plan = self._plan(picked, tv_inv, profile, weekday, target,
                              tmdb_by_rk=tmdb_by_rk)
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{safe}", plan)
            stats["built"] += 1
            self._preview(u, plan, groups, target)

            # NOT RECORDED HERE - see writeback._record_surfaced. This builder caches
            # a plan; write-back is what publishes it, and only it knows whether the
            # playlist actually reached Plex.

        self.logger.log_info(
            f"[Tonight] {stats['built']}/{stats['users']} profile(s) planned for "
            f"{target:%a %d %b} · {stats['empty']} with no habit yet"
            + (f" · {stats['recorded']} pick(s) recorded to the ledger"
               if stats.get("recorded") else "")
            + (f" · {stats['unrecordable']} with no stable id"
               if stats.get("unrecordable") else "")
            + " — detail in support/logs/playlists.log.")
        return stats

    # ── plan assembly ────────────────────────────────────────────────────────
    def _plan(self, picked, tv_inv, profile, weekday, target, tmdb_by_rk=None) -> dict:
        """The cached plan, in the shape ``writeback._desired_items`` reads.

        ``target_date`` and ``target_day`` ride along because attribution needs
        them: ``outcomes.py`` credits Tonight only when the play lands on the day
        it was built FOR, and that day has to survive into the plan or the
        write-back cannot pass it on.

        STABLE IDENTITY RIDES ALONG TOO (``tvdb_join_key`` / ``tmdb_id``). A
        ratingKey is a Plex-local handle a re-scan RETIRES - 11/117 on one series
        when it was measured - so a recommendation ledger keyed on it would either
        lose the row or match a different title that inherited the number. Both
        ids are already in hand here: the episode's join key IS the pool item, and
        the movie map was built from the tmdb-keyed inventory.
        """
        items = []
        for ordinal, (group, item) in enumerate(picked):
            rk = item
            reason = group
            join_key = tmdb = None
            if group.startswith("series:"):
                row = tv_inv.get(item) or {}
                rk = str(row.get("rating_key") or item)
                reason = f"{row.get('series_title') or 'series'} · {item.split(':', 1)[-1]}"
                join_key = str(item)          # 'tvdb:s:e' - the episode's stable identity
            else:
                tmdb = (tmdb_by_rk or {}).get(str(rk))
            share = (profile.get(group, {}).get("share") or [0.0] * 7)[weekday]
            entry = {"rating_key": str(rk), "ordinal": ordinal,
                     "group_key": group, "group_kind": group.split(":", 1)[0],
                     "score": round(float(share), 4), "reason": reason}
            if join_key:
                entry["tvdb_join_key"] = join_key
                entry["media"] = "episode"
            if tmdb is not None:
                entry["tmdb_id"] = tmdb
                entry["media"] = "movie"
            items.append(entry)
        return {"family": "tonight", "considered": len(picked),
                "dropped_watched": 0, "truncated": 0, "coverage": {},
                "target_day": int(weekday), "target_date": target.isoformat(),
                "items": items}

    # ── inputs ───────────────────────────────────────────────────────────────
    def _history_rows(self, user_id) -> list:
        """RAW Tautulli rows for one profile - the habit model needs the unix
        ``date`` on each play, not the derived watched SET. ``[]`` on any miss,
        which reads downstream as "no habit yet" rather than an error."""
        if user_id is None or not self.registry:
            return []
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return []
        try:
            return list(hm.get_all_history_cached(user_id) or [])
        except Exception:
            return []

    def _runtime_minutes(self, movie_inv, tv_inv) -> dict:
        """``{rating_key: minutes}`` from the Radarr and Sonarr parquets.

        MOVIES: ``runtime_minutes`` straight off the movie_files row.
        EPISODES: ``runtime_seconds`` from episode_files, joined on
        ``(series_id, season_number, episode_number)`` through owned_episodes to
        reach a ``tvdb_join_key``, then to the Plex ratingKey via the inventory.

        Absent is absent - a key simply missing means "unknown", and the caller
        must not read that as short (P-C). Nothing here fabricates a nominal
        series runtime to fill a gap: a series' advertised length is not this
        episode's, and a 90-minute finale would be waved through by it.
        """
        out: dict = {}
        # ---- movies -------------------------------------------------------
        rk_by_tmdb = {str(t): str(r["rating_key"])
                      for t, r in (movie_inv or {}).items()
                      if isinstance(r, dict) and r.get("rating_key") is not None}
        for m in (self._load_owned_movies() or []):
            if not isinstance(m, dict):
                continue
            rk = rk_by_tmdb.get(str(m.get("tmdb_id")))
            mins = m.get("runtime_minutes")
            if rk is None or mins is None:
                continue
            try:
                mins = float(mins)
            except (TypeError, ValueError):
                continue
            if mins == mins and mins > 0:            # NaN-safe
                out[rk] = mins
        # ---- episodes -----------------------------------------------------
        rk_by_join = {str(k): str(r["rating_key"])
                      for k, r in (tv_inv or {}).items()
                      if isinstance(r, dict) and r.get("rating_key") is not None}
        for join_key, secs in self._episode_runtimes().items():
            rk = rk_by_join.get(join_key)
            if rk is not None and secs:
                out[rk] = float(secs) / 60.0
        return out

    def _episode_runtimes(self) -> dict:
        """``{tvdb_join_key: runtime_seconds}`` across every Sonarr instance.

        Reads BOTH parquets because neither alone is enough: episode_files has
        the measured runtime but keys on Sonarr's internal ``series_id``, while
        owned_episodes carries the ``tvdb_join_key`` the rest of the pipeline
        speaks. The join is on ``(series_id, season_number, episode_number)``.

        Any read failure yields ``{}`` and the cap simply has less to work with -
        a missing parquet must not cost the household its list.
        """
        out: dict = {}
        if not (self.global_cache and getattr(self.global_cache, "key_builder", None)):
            return out
        try:
            import pandas as pd
        except Exception:
            return out
        base = self.global_cache.key_builder.base_dir
        for inst_dir in sorted((base / "sonarr").glob("*/")):
            ef, oe = inst_dir / "episode_files.parquet", inst_dir / "owned_episodes.parquet"
            if not (ef.is_file() and oe.is_file()):
                continue
            try:
                fdf = pd.read_parquet(
                    ef, columns=["series_id", "season_number", "episode_number",
                                 "runtime_seconds"])
                odf = pd.read_parquet(
                    oe, columns=["series_id", "season_number", "episode_number",
                                 "tvdb_join_key"])
            except Exception as e:
                self.logger.log_debug(f"[Tonight] runtime parquet read failed in {inst_dir}: {e}")
                continue
            fdf = fdf.dropna(subset=["series_id", "season_number", "episode_number",
                                     "runtime_seconds"])
            odf = odf.dropna(subset=["series_id", "season_number", "episode_number",
                                     "tvdb_join_key"])
            try:
                merged = fdf.merge(odf, on=["series_id", "season_number", "episode_number"],
                                   how="inner")
            except Exception:
                continue
            for key, secs in zip(merged["tvdb_join_key"], merged["runtime_seconds"]):
                out[str(key)] = float(secs)
        return out

    def _movie_maps(self, movie_inv):
        """``(by_rk, franchise_by_rk, genres_by_rk, score_by_rk)``.

        SOURCE IS ``_load_owned_movies()``, not a cache key. Every field needed is
        already on one Radarr row - ``tmdb_id``, ``genres``, ``watchability_score``,
        ``universe_name``, ``collection_name`` (see
        ``MoviePlaylistBuilderManager._OWNED_MOVIE_COLUMNS``). An earlier version of
        this method invented three cache keys
        (``plex/movies/genres_by_tmdb``, ``ml/movie_scores``,
        ``plex/collections/membership_by_tmdb``); none of the first two exist, and
        the third is only written when the default-OFF collections capability runs.
        All three returned ``{}``, so movies would have been silently absent from
        Tonight forever while TV worked - a failure with no error.

        Re-keyed from tmdb to the PLEX RATING KEY, because the plan must carry
        ratingKeys (write-back re-resolves against them) and history rows carry them
        too. Keying every map the same way removes the translation step where a
        tmdb/ratingKey mix-up would otherwise creep in.

        FRANCHISE PRECEDENCE is universe over collection: ``universe_name`` is the
        broader grouping ('mcu' spans several collections), and a weekday habit is
        far likelier to be "Saturday is MCU night" than "Saturday is Iron Man
        Collection night". Falls back to ``collection_name`` when no universe is set.

        NaN is not a score. The movie_files parquet round-trip yields float NaN for
        an unscored movie, and NaN silently poisons any comparison it touches - it
        must read as ABSENT so candidates.movie_candidates excludes it from the
        score-ranked groups rather than ranking it.
        """
        by_rk, fran, gens, score = {}, {}, {}, {}
        rk_by_tmdb = {}
        for tmdb, row in (movie_inv or {}).items():
            if isinstance(row, dict) and row.get("rating_key") is not None:
                rk_by_tmdb[str(tmdb)] = str(row["rating_key"])
                by_rk[str(row["rating_key"])] = row

        for m in (self._load_owned_movies() or []):
            if not isinstance(m, dict):
                continue
            rk = rk_by_tmdb.get(str(m.get("tmdb_id")))
            if rk is None:
                continue                    # owned by Radarr but not indexed by Plex
            f = _affinity(m.get("universe_name")) or _affinity(m.get("collection_name"))
            if f:
                fran[rk] = f
            g = _labels(m.get("genres"))
            if g:
                gens[rk] = g
            s = self._score(m)              # inherited: NaN -> None
            if s is not None:
                score[rk] = float(s)
        return by_rk, fran, gens, score

    # ── logging ──────────────────────────────────────────────────────────────
    def _preview(self, user, plan, groups, target):
        """Per-profile detail to the DEDICATED playlists.log, not the run log -
        a multi-profile preview would flood it. Real names stay here; the run
        log gets the de-identified handle."""
        if not (self.logger and hasattr(self.logger, "log_to_file")):
            return
        self.logger.log_to_file(
            "playlists",
            f"[Tonight] '{user.get('title')}' for {target:%A %d %b}: "
            f"{len(plan['items'])} item(s) from {len(groups)} group(s)")
        for it in plan["items"]:
            self.logger.log_to_file(
                "playlists",
                f"    {it['ordinal'] + 1:>2}. {it['group_key']:<28} "
                f"rk={it['rating_key']:<8} share={it['score']:.3f}  {it['reason']}")

    def _who(self, user) -> str:
        return str(user.get("safe_user") or user.get("title") or "?")

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

"""plex/playlists/affinity_builder.py — "Because You Watched", and the discovery signal behind it.

Closes the loop the household actually noticed: Iron Giant was surfaced by the
Anniversary shelf, watched, enjoyed - and then nothing followed up. The film
went into general history like any other play and the system never offered
anything adjacent to it.

    recommendations ledger ---+
                              |--> discovery.completions  (surfaced AND finished)
    Tautulli history ---------+          |
                                         +--> affinity_contributions  (published)
                                         +--> similar unwatched items -> a playlist

TWO OUTPUTS, DELIBERATELY SEPARATE.

1. A PUBLISHED SIGNAL (``ml/discovery/affinity``) - per profile, the attributes
   that discovery plays vouched for. It is published and reported, and it does
   NOT silently re-weight the scorer. `GLD-PLY-28` is filed to measure whether a
   discovery play predicts better than an ordinary one FIRST;
   ``discovery.DEFAULT_DISCOVERY_WEIGHT`` is 1.0 until it does. Wiring an
   unmeasured multiplier into live scoring is how the 0.28 day threshold shipped
   and stayed wrong by 4.5x for weeks.

2. A PLAYLIST - "Because You Watched", seeded from the completions themselves
   rather than from general history. That distinction is the entire point: the
   general affinity model is already dominated by what the household watches
   most, so a list drawn from it would recommend more of the same. This one
   answers "you finished the thing we suggested, here is what it implies".

WHY IT SEEDS FROM COMPLETIONS AND NOT FROM PLAYS. A play means the item was
started. A COMPLETION means it was finished. Following up on something abandoned
at 20% would teach the shelf to recommend more of a thing that was actively
rejected - the poster would read "because you watch horror" on the strength of a
horror film somebody turned off.

DEGRADES TO NOTHING. An empty ledger (nothing recorded yet), no completions, no
similar unwatched items - each produces no plan, and write-back then finds no
plan and leaves the playlist absent. A household that has not yet finished
anything a shelf suggested has no honest "because you watched" to show.
"""
from __future__ import annotations

from scripts.managers.machine_learning.labels import recommendations as R
from scripts.managers.machine_learning.playlists import coverage as COV
from scripts.managers.machine_learning.playlists import discovery as D
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager

_PLAN_KEY = "plex/playlists/affinity_plan"          # + /{safe_user}
_SIGNAL_KEY = "ml/discovery/affinity"               # published, not consumed by the scorer
_COVERAGE_KEY = "ml/discovery/coverage"             # per-profile blind-spot report
_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"

#: Surfaces whose completions seed this list. Up Next is excluded for the reason
#: given in discovery.DISCOVERY_SURFACES - the next episode of a show already
#: being watched is not a discovery.
_SEED_SURFACES = D.DISCOVERY_SURFACES

_DEFAULT_SIZE = 25

#: How many attributes one completion contributes. A film has many genres and a
#: long cast; taking all of them would let a single play dominate the profile.
_TOP_ATTRS = 4


class AffinityPlaylistBuilderManager(MoviePlaylistBuilderManager):
    """Builds ``Because You Watched`` and publishes the discovery signal."""

    parent_name = "PlexManager"

    def _cfg(self) -> dict:
        return (self._pl_cfg().get("because_you_watched", {}) or {})

    def _enabled(self) -> bool:
        """Default OFF. With no ledger rows this builds nothing anyway, so the
        gate is about intent rather than safety."""
        return bool(self._cfg().get("enabled", False))

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        stats = {"enabled": self._enabled(), "users": 0, "built": 0,
                 "no_completions": 0, "no_candidates": 0, "completions": 0}
        if not stats["enabled"]:
            return stats

        base_dir = self._base_dir()
        if base_dir is None:
            self.logger.log_warning(
                "[BecauseYouWatched] no cache base dir - the recommendation ledger "
                "cannot be read; nothing built.")
            return stats

        events = self._load_events(base_dir)
        if not len(events):
            self.logger.log_info(
                "[BecauseYouWatched] the recommendation ledger is empty - nothing has "
                "been recorded as surfaced yet, so there is no completion to build on.")
            return stats

        tracked = self._tracked_users()
        stats["users"] = len(tracked)
        movie_inv = self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {}
        tv_inv = self._cache_get(_TV_INVENTORY_KEY, {}) or {}
        entity_of_play = self._play_entity_resolver(movie_inv, tv_inv)
        attrs_by_entity, rows_by_entity = self._attribute_index(movie_inv)

        signal: dict = {}
        cover: dict = {}
        # {profile: {entity_id: earliest recommended_at}} - TEMPORAL coverage, so a
        # play only counts as covered when it landed AT OR AFTER the offer. The
        # snapshot form (a flat set) would credit every watch that preceded the
        # recommendation, which is the same causality error `discovery` guards.
        offered_by_profile: dict = {}
        for e in (events.to_dict("records") if hasattr(events, "to_dict") else events):
            if not isinstance(e, dict):
                continue
            prof, eid = e.get("profile"), e.get("entity_id")
            if prof in (None, "") or eid in (None, ""):
                continue
            ts = D._epoch(e.get("recommended_at"))
            if ts is None:
                continue
            bucket = offered_by_profile.setdefault(str(prof), {})
            prev = bucket.get(str(eid))
            if prev is None or ts < prev:
                bucket[str(eid)] = ts
        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                continue
            plays = self._history_rows(u.get("tautulli_user_id"), safe)
            done = D.completions(events, plays, entity_of_play=entity_of_play,
                                 surfaces=_SEED_SURFACES)
            done = [d for d in done if str(d.get("profile")) == str(safe)]

            # COVERAGE runs for EVERY profile, before the no-completions exit.
            # A profile with zero completions is exactly the one with a coverage
            # problem, and skipping it would measure only the households the
            # recommender is already working for.
            try:
                diag = COV.diagnose(
                    plays, offered_by_profile.get(str(safe), {}),
                    list(attrs_by_entity.keys()),
                    entity_of_play=entity_of_play,
                    attributes_of=lambda e: attrs_by_entity.get(str(e), ()),
                    min_pct=D.DEFAULT_MIN_PCT)
                cover[safe] = diag
                self.logger.log_info(f"[Coverage] '{safe}': {diag['summary']}")
                # Findings are EMPTY on an unusable read (see coverage.diagnose),
                # so this loop prints nothing rather than a plausible table.
                for f in diag["findings"][:3]:
                    self._detail(
                        f"[Coverage] '{safe}' {f['attribute']} — {f['lift']}x "
                        f"({f['missed_n']} missed, we show {f['offered_p']}, "
                        f"we own {f['library_p']}) → {f['verdict']}")
            except Exception as exc:
                self.logger.log_warning(
                    f"[Coverage] '{safe}' failed: {type(exc).__name__}: {exc}")

            if not done:
                stats["no_completions"] += 1
                continue
            stats["completions"] += len(done)

            contrib = D.affinity_contributions(
                done, attributes_of=lambda eid, _r, _a=attrs_by_entity: _a.get(str(eid), ()))
            if contrib.get(safe):
                signal[safe] = contrib[safe]

            items = self._similar(contrib.get(safe) or {}, done, movie_inv,
                                  self._watched_for(u.get("tautulli_user_id")))
            if not items:
                stats["no_candidates"] += 1
                continue
            plan = {"family": "because_you_watched", "considered": len(done),
                    "dropped_watched": 0, "truncated": 0, "coverage": {},
                    "seeds": [d.get("title") for d in done[:3]], "items": items}
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{safe}", plan)
            stats["built"] += 1
            self._detail(f"[BecauseYouWatched] '{safe}': {len(items)} item(s) from "
                         f"{len(done)} completion(s) - seeds "
                         f"{', '.join(str(s) for s in plan['seeds'])}")

        if signal and self.global_cache:
            # PUBLISHED, NOT APPLIED. See the module docstring - the weight is
            # unmeasured, so this is evidence for GLD-PLY-28 and an input a future
            # scorer change can consume, not a live multiplier.
            try:
                self.global_cache.set(_SIGNAL_KEY, signal)
            except Exception as e:
                self.logger.log_warning(f"[BecauseYouWatched] could not publish signal: {e}")

        if cover and self.global_cache:
            # The blind-spot report, published for inspection rather than acted on.
            # It is the only measurement here that can say what we are NOT
            # offering; every other number in this package asks whether an offer
            # was taken.
            try:
                self.global_cache.set(_COVERAGE_KEY, {
                    p: {"coverage": d["coverage"]["coverage_rate"],
                        "watched": d["coverage"]["watched"],
                        "missed": d["coverage"]["missed"],
                        "fidelity": d["coverage"]["fidelity"],
                        # Carried so a consumer cannot mistake a start-date zero
                        # for a measured zero. Anything reading this key must
                        # check it before treating `coverage` as a result.
                        "usable": d.get("usable", False),
                        "summary": d["summary"],
                        "findings": d["findings"][:10]}
                    for p, d in cover.items()})
            except Exception as e:
                self.logger.log_warning(f"[Coverage] could not publish report: {e}")

        self.logger.log_info(
            f"[BecauseYouWatched] {stats['built']}/{stats['users']} profile(s) built from "
            f"{stats['completions']} discovery completion(s) \u00b7 "
            f"{stats['no_completions']} with none yet \u00b7 "
            f"{stats['no_candidates']} with nothing similar left unwatched.")
        return stats

    # ── the join: a Tautulli play -> a ledger identity ───────────────────────
    def _play_entity_resolver(self, movie_inv, tv_inv):
        """``row -> [entity_id, ...]``, delegated to `playlists/identity`.

        This method used to invert the owned inventories itself. That only ever
        knows the CURRENT ratingKey, and measured on real data it resolved just
        **185 of 979 plays** - 81% of the household's viewing discarded before any
        matching ran. The ratingKey crosswalk
        (``tautulli/rating_key_crosswalk.json``) accumulates retired keys and was
        already in the cache, unused: 1036 films against 959 in the live
        inventory.

        Kept as a thin delegate rather than deleted so the call site still reads
        in place, but the LOOKUP now has exactly one implementation.
        """
        from scripts.managers.services.plex.playlists import identity
        return identity.build_resolver(
            base_dir=self._base_dir(), movie_inventory=movie_inv,
            episode_inventory=tv_inv)

    def _attribute_index(self, movie_inv):
        """``({entity_id: [attribute]}, {entity_id: row})`` for owned movies.

        Attributes are genres plus the universe/franchise - the axes the taste
        model already groups on. Capped at ``_TOP_ATTRS`` so one long cast list
        cannot dominate a profile built from a handful of completions.
        """
        attrs: dict = {}
        rows: dict = {}
        for m in (self._load_owned_movies() or []):
            if not isinstance(m, dict):
                continue
            tmdb = m.get("tmdb_id")
            if tmdb is None:
                continue
            eid = str(tmdb)
            rows[eid] = m
            vals = []
            g = m.get("genres")
            if isinstance(g, str):
                try:
                    import json
                    g = json.loads(g) if g.strip().startswith("[") else g.split(",")
                except ValueError:
                    g = g.split(",")
            for x in (g or ()):
                text = str(x).strip().strip("\"'")
                if text and text.lower() not in ("nan", "none", ""):
                    vals.append(f"genre:{text.lower()}")
            for field in ("universe_name", "collection_name"):
                v = m.get(field)
                if v is not None and not (isinstance(v, float) and v != v):
                    text = str(v).strip()
                    if text and text.lower() not in ("nan", "none", "universe", ""):
                        vals.append(f"franchise:{text.lower()}")
                        break
            attrs[eid] = vals[:_TOP_ATTRS]
        return attrs, rows

    def _similar(self, contrib: dict, done, movie_inv, watched) -> list:
        """Unwatched owned movies scoring highest against the discovery profile.

        Scored by ATTRIBUTE OVERLAP with what discovery already vouched for, then
        by watchability as the tie-break. The seeds themselves are excluded - a
        \"because you watched X\" list containing X is the joke that writes itself.
        """
        attrs, rows = self._attribute_index(movie_inv)
        seeds = {str(d.get("entity_id")) for d in (done or ())}
        seen = {str(w) for w in (watched or ())}
        rk_by_tmdb = {str(t): str(r.get("rating_key"))
                      for t, r in (movie_inv or {}).items()
                      if isinstance(r, dict) and r.get("rating_key") is not None}

        scored = []
        for eid, vals in attrs.items():
            if eid in seeds or not vals:
                continue
            rk = rk_by_tmdb.get(eid)
            if rk is None or rk in seen:
                continue
            overlap = sum(float(contrib.get(v, 0.0)) for v in vals)
            if overlap <= 0:
                continue                    # nothing discovery vouched for
            watchability = self._score(rows.get(eid) or {}) or 0.0
            scored.append((-overlap, -float(watchability), eid, rk))
        scored.sort()

        size = self._num_cfg("size", _DEFAULT_SIZE)
        out = []
        for ordinal, (neg_overlap, neg_w, eid, rk) in enumerate(scored[:size]):
            row = rows.get(eid) or {}
            out.append({"rating_key": str(rk), "ordinal": ordinal,
                        "tmdb_id": eid, "media": "movie",
                        "score": round(-neg_overlap, 4),
                        "group_key": f"affinity:{eid}",
                        "group_kind": "affinity",
                        "reason": f"matches {', '.join(attrs.get(eid, [])[:2])}",
                        "title": row.get("title")})
        return out

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _load_events(self, base_dir):
        """Every discovery surface's ledger, concatenated. Empty frame on any miss."""
        frames = []
        for surface in sorted(_SEED_SURFACES):
            try:
                df = R.load_events(base_dir, surface=surface)
            except Exception:
                continue
            if df is not None and not getattr(df, "empty", True):
                frames.append(df)
        if not frames:
            return []
        try:
            import pandas as pd
            return pd.concat(frames, ignore_index=True)
        except Exception:
            return [r for f in frames for r in f.to_dict("records")]

    def _history_rows(self, user_id, safe) -> list:
        """Raw Tautulli rows, tagged with the profile the ledger keys on."""
        if user_id is None or not self.registry:
            return []
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return []
        try:
            rows = list(hm.get_all_history_cached(user_id) or [])
        except Exception:
            return []
        for r in rows:
            if isinstance(r, dict):
                r.setdefault("profile", safe)
        return rows

    def _base_dir(self):
        kb = getattr(self.global_cache, "key_builder", None) if self.global_cache else None
        return getattr(kb, "base_dir", None) if kb is not None else None

    def _num_cfg(self, key, default):
        try:
            return type(default)(self._cfg().get(key, default))
        except (TypeError, ValueError):
            return default

    def _detail(self, msg):
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("playlists", msg)

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

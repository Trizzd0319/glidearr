"""
TraktPeopleMatrixManager
========================
Service adapter (FETCH/CACHE) for the pure ``machine_learning/people_matrix`` brain.

It reads the enrich-daemon's per-title people buckets (movies via
``TraktMovieCacheManager``, shows via ``TraktShowCacheManager`` — the established
readers, already hardened against 0-byte/stale files), hands the decoded credits
dicts to the PURE ``build_index`` builder, and caches the resulting forward map so the
watchability scorer (Group-C4) and the acquisition co-cast candidate source can read
the person↔media graph without re-opening thousands of gz files.

Two artifacts, cached SEPARATELY (see ``daemon_paths``): the forward map here is
LIBRARY-derived (stable); the household person-affinity weights are watched-set-derived
(volatile) and live in their own key so a watched-set change doesn't rebuild the matrix.

Zero Trakt calls — the daemon owns fetching. Building from buckets already on disk is a
cheap pure rebuild; the cache (a non-destructive derived annotation) is written even in
dry_run so the searchable index is available for a dry-run query.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.daemons.daemon_paths import (
    CACHE_TTL_S,
    MOVIE_BUCKETS,
    PEOPLE_AFFINITY_PATH,
    PEOPLE_MATRIX_PATH,
    SHOW_BUCKETS,
)
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class TraktPeopleMatrixManager(BaseManager, ComponentManagerMixin):
    DEFAULT_TTL = CACHE_TTL_S

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktMoviesManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.ttl     = int(kwargs.get("ttl", self.DEFAULT_TTL))
        self.matrix_path   = Path(kwargs.get("matrix_path", PEOPLE_MATRIX_PATH))
        self.affinity_path = Path(kwargs.get("affinity_path", PEOPLE_AFFINITY_PATH))
        self._movie_cache = None
        self._show_cache  = None

    # ── lazy bucket readers (the established, 0-byte-hardened cache managers) ────
    def _get_movie_cache(self):
        if self._movie_cache is None:
            try:
                from scripts.managers.services.trakt.movies.cache import TraktMovieCacheManager
                self._movie_cache = TraktMovieCacheManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry, dry_run=self.dry_run)
            except Exception as e:
                self.logger.log_debug(f"[PeopleMatrix] movie cache unavailable: {e}")
                self._movie_cache = False
        return self._movie_cache or None

    def _get_show_cache(self):
        if self._show_cache is None:
            try:
                from scripts.managers.services.trakt.shows.cache import TraktShowCacheManager
                self._show_cache = TraktShowCacheManager(
                    logger=self.logger, config=self.config,
                    global_cache=self.global_cache, registry=self.registry, dry_run=self.dry_run)
            except Exception as e:
                self.logger.log_debug(f"[PeopleMatrix] show cache unavailable: {e}")
                self._show_cache = False
        return self._show_cache or None

    @staticmethod
    def _ids_in(bucket_dir: Path):
        """Yield the int ids of the ``{id}.json.gz`` files in a people bucket dir."""
        if not bucket_dir.exists():
            return
        for f in bucket_dir.glob("*.json.gz"):
            try:
                yield int(f.name.split(".", 1)[0])
            except (ValueError, IndexError):
                continue

    def _iter_media_people(self):
        """Yield ``((medium, ext_id), credits)`` for every enriched title with people."""
        mc = self._get_movie_cache()
        if mc is not None:
            for tmdb in self._ids_in(MOVIE_BUCKETS["people"]):
                credits = mc.get_people(tmdb)
                if credits and (credits.get("cast") or credits.get("crew")):
                    yield ("movie", tmdb), credits
        sc = self._get_show_cache()
        if sc is not None:
            for tvdb in self._ids_in(SHOW_BUCKETS["people"]):
                credits = sc.get_people(tvdb)
                if credits and (credits.get("cast") or credits.get("crew")):
                    yield ("show", tvdb), credits

    # ── build + persist ─────────────────────────────────────────────────────────
    def build(self, media_people: dict | None = None) -> dict:
        """Read the people buckets, build the person↔media graph, cache the forward
        map, and log coverage. ``media_people`` may be injected (tests); otherwise it
        is assembled from the daemon buckets. Best-effort + never raises into the run."""
        from scripts.managers.machine_learning.people_matrix import build_index, serialize_forward
        from scripts.managers.machine_learning.affinity.genre_affinity import aggregate_person_affinity
        try:
            if media_people is None:
                media_people = dict(self._iter_media_people())
            person_index, fwd = build_index(media_people)

            total       = len(media_people)
            with_people = sum(1 for roles in fwd.values() if any(roles.values()))
            self._save_forward(fwd)
            if self.global_cache:
                try:
                    self.global_cache.set("people_matrix/forward", serialize_forward(fwd))
                except Exception:
                    pass

            # Household person-affinity (volatile — cached SEPARATELY from the forward
            # map). Derived from the SAME watched-set the C3 scorer uses (Trakt history
            # + Tautulli completions), so a watched-set change re-weights without
            # rebuilding the matrix.
            watched_keys = self._household_watched_keys()
            person_weights = aggregate_person_affinity(watched_keys, fwd)
            self._save_affinity(person_weights)
            if self.global_cache:
                try:
                    self.global_cache.set("people_matrix/affinity",
                                          {str(k): v for k, v in person_weights.items()})
                except Exception:
                    pass

            pct = (with_people / total * 100.0) if total else 0.0
            self.logger.log_info(
                f"[PeopleMatrix] indexed {with_people:,}/{total:,} title(s) carrying cast/crew "
                f"person-ids ({pct:.0f}% of enriched), {len(person_index):,} distinct people; "
                f"household affinity over {len(watched_keys):,} watched -> {len(person_weights):,} people.")
            stats = {"titles": total, "with_people": with_people,
                     "persons": len(person_index), "weighted_people": len(person_weights)}
            if self.global_cache:
                try:
                    self.global_cache.set("people_matrix/run_stats", stats)
                except Exception:
                    pass
            return stats
        except Exception as e:
            self.logger.log_warning(f"[PeopleMatrix] build skipped: {e}")
            return {"titles": 0, "with_people": 0, "persons": 0}

    def _household_watched_keys(self) -> set:
        """The household's watched titles as ``(medium, ext_id)`` keys, from the SAME
        cached signals the C3 scorer uses: Trakt movie history + per-group Tautulli
        tmdb completions. Movies-first (the matrix's TV half is daemon-gated); shows
        join in once a watched-tvdb signal is threaded. Empty when unconfigured."""
        keys: set = set()
        gc = self.global_cache
        if not gc:
            return keys
        try:
            for entry in (gc.get("trakt/history/movies") or []):
                tmdb = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
                if tmdb:
                    keys.add(("movie", int(tmdb)))
        except Exception:
            pass
        cfg = getattr(self, "config", None)
        try:
            groups = (cfg.get("rating_groups", {}) if cfg else {}) or {"household": {}}
        except Exception:
            groups = {"household": {}}
        for group in groups:
            try:
                for tmdb_str in (gc.get(f"tautulli/group/{group}/tmdb_completions") or {}):
                    try:
                        keys.add(("movie", int(tmdb_str)))
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass
        return keys

    def _save_forward(self, fwd: dict) -> None:
        """Atomic gz write of the serialized forward map. Written even in dry_run — a
        derived read-cache, never touches the library."""
        from scripts.managers.machine_learning.people_matrix import serialize_forward
        self._write_gz(self.matrix_path, serialize_forward(fwd))

    def _save_affinity(self, person_weights: dict) -> None:
        """Atomic gz write of the household person-affinity ({person_id: weight}); int
        keys are stringified for JSON and coerced back on read by the consumers."""
        self._write_gz(self.affinity_path, {str(k): v for k, v in person_weights.items()})

    def _write_gz(self, path: Path, payload) -> None:
        """Atomic gz write (temp + os.replace) so a hard kill never leaves a partial."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pmatrix_", suffix=".tmp")
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception as e:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self.logger.log_debug(f"[PeopleMatrix] cache write skipped ({path.name}): {e}")

    # ── load (downstream consumers: scorer C4, candidate source) ─────────────────
    def load_index(self):
        """Return ``(person_index, media_people_fwd)`` from cache, or ``(None, None)``
        if the matrix has never been built / is stale / unreadable. Prefers the live
        global_cache, falls back to the gz on disk."""
        from scripts.managers.machine_learning.people_matrix import deserialize_forward, invert_forward
        raw = None
        if self.global_cache:
            try:
                raw = self.global_cache.get("people_matrix/forward")
            except Exception:
                raw = None
        if raw is None:
            raw = self._read_forward_gz()
        if not raw:
            return None, None
        try:
            fwd = deserialize_forward(raw)
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] forward map parse error: {e}")
            return None, None
        return invert_forward(fwd), fwd

    def _read_forward_gz(self) -> dict | None:
        path = self.matrix_path
        try:
            st = path.stat()
        except OSError:
            return None
        if st.st_size == 0 or (time.time() - st.st_mtime) > self.ttl:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] cache read error: {e}")
            return None

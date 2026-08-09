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

WHERE THE CREDITS COME FROM
---------------------------
The MOVIE half is built from the Radarr RELATIONAL credits table
(``radarr/<inst>/relational/movie_person_relations.parquet``) — ONE parquet per
instance carrying, for every credited person: a stable ``person_tmdb_id``, a
``role_type`` covering all seven credited roles (actor / director / writer / producer /
composer / cinematographer / editor) and a real ``billing_order``. That is strictly
more than the daemon's per-title gz buckets expose (no cinematographer/editor
department, no consolidated ordering) and it reads in ~1s instead of ~100s of
individual gzip decompressions. It is also why the matrix is built from the relational
table rather than movie_files' ``cast_names``/``director_names``/… columns: those carry
NAMES only, and Group-C4 is id-keyed on purpose (immune to alias drift), so a
name-keyed graph could not feed it.

The SHOW half has no relational table, so it still reads the daemon's
``trakt/shows/{tvdb}.json.gz`` credit buckets — but INCREMENTALLY, through a
``{tvdb: mtime_ns}`` sidecar, so a repeat run only re-reads the shows the daemon
actually rewrote (cold ~25s, warm ~0.1s).

CHEAP ON REPEAT RUNS
--------------------
A two-part fingerprint (movie: relational parquet size+mtime per instance; show:
bucket count + newest mtime + total bytes) plus the role-weight/decay/revision
signature and the watched-set digest is persisted to ``people_matrix.state.json``.
An unchanged fingerprint skips the whole rebuild and reuses the cached artifacts.

Zero Trakt calls — the daemon owns fetching. Building from data already on disk is a
cheap pure rebuild; the cache (a non-destructive derived annotation) is written even in
dry_run so the searchable index is available for a dry-run query. Every stage is
fault-isolated: a failure anywhere logs and returns zero stats, never raising into the
run.
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
    PEOPLE_MATRIX_STATE,
    PEOPLE_NAMES_PATH,
    PEOPLE_MOVIES_SIDECAR,
    PEOPLE_SHOWS_SIDECAR,
    SHOW_BUCKETS,
)
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin

# Bumped when a change alters the SHAPE or WEIGHTING of the built matrix (a new role,
# a different billing decay, a different source). Part of the fingerprint, so the next
# run rebuilds instead of reusing artifacts produced by the previous definition.
MATRIX_REVISION = 2


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
        self.names_path    = Path(kwargs.get("names_path", PEOPLE_NAMES_PATH))
        self.shows_sidecar  = Path(kwargs.get("shows_sidecar", PEOPLE_SHOWS_SIDECAR))
        self.movies_sidecar = Path(kwargs.get("movies_sidecar", PEOPLE_MOVIES_SIDECAR))
        self.state_path    = Path(kwargs.get("state_path", PEOPLE_MATRIX_STATE))
        # Daemon credit-bucket dirs, overridable so a test can point them at an empty
        # temp dir instead of walking the real ~31k-file cache.
        self.movie_bucket  = Path(kwargs.get("movie_bucket", MOVIE_BUCKETS["people"]))
        self.show_bucket   = Path(kwargs.get("show_bucket", SHOW_BUCKETS["people"]))
        self._movie_cache = None
        self._show_cache  = None

    # ── config view ─────────────────────────────────────────────────────────────
    def _cfg(self) -> dict:
        raw = self.config.raw_data if hasattr(self.config, "raw_data") else (self.config or {})
        try:
            return (raw.get("people_matrix") or {}) if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _cache_base(self) -> "Path | None":
        """The parquet cache root (``…/support/cache``) via the global_cache key builder."""
        try:
            return Path(self.global_cache.key_builder.base_dir)
        except Exception:
            return None

    def _instance_parquets(self, leaf: str) -> "list[Path]":
        """Every ``radarr/<instance>/<leaf>`` that exists, sorted for a stable
        fingerprint. Discovered by scanning the cache tree rather than read from config
        so an instance renamed / added mid-life is picked up without a config edit."""
        base = self._cache_base()
        if base is None:
            return []
        try:
            return sorted(p for p in (base / "radarr").glob(f"*/{leaf}") if p.is_file())
        except Exception:
            return []

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
        """Yield ``((medium, ext_id), credits)`` for every enriched title with people.

        The FALLBACK source (used when no relational credits table exists yet — e.g. a
        fresh install whose first Radarr relational pull has not run). Reads every
        daemon gz bucket, which is why the primary path is the relational parquet."""
        mc = self._get_movie_cache()
        if mc is not None:
            for tmdb in self._ids_in(self.movie_bucket):
                credits = mc.get_people(tmdb)
                if credits and (credits.get("cast") or credits.get("crew")):
                    yield ("movie", tmdb), credits
        sc = self._get_show_cache()
        if sc is not None:
            for tvdb in self._ids_in(self.show_bucket):
                credits = sc.get_people(tvdb)
                if credits and (credits.get("cast") or credits.get("crew")):
                    yield ("show", tvdb), credits

    # ── MOVIE half — the relational credits table ───────────────────────────────
    # HISTORY — why the movie half reads the daemon buckets first.
    #
    # The enrich daemon used to call ``movies/{id}/people`` with a TMDB id, but that Trakt
    # path resolves a TRAKT id (or slug, or imdb id). Colliding numbers returned a
    # DIFFERENT title's credits, cached under the external id as if correct:
    #
    #   movies/65      (8 Mile, tmdb 65)     -> Eddie Murphy, Judge Reinhold  (Beverly Hills Cop)
    #   movies/1271    (300, tmdb 1271)      -> Daniel Brühl, Leonor Watling
    #   shows/75710    (Criminal Minds)      -> Ray Evernham, a NASCAR crew chief
    #
    # ~77% of movie buckets and ~84% of show buckets were wrong. Fixed in enrich_pool via
    # ``fetch_map`` (imdb/trakt id in the URL, external id on the bucket) and the whole
    # corpus was purged and refetched — 845/845 watchlist movies and 90/90 watchlist shows
    # verified correct afterwards, against Radarr's own tmdbId->title mapping.
    #
    # The relational parquet was NOT an independent check on any of that: it is built by
    # ``relational.build_relations_from_movies`` from ``TraktMoviePeopleManager``, which
    # reads these same buckets. It inherited every bad row and keeps serving them until
    # something regenerates it — which is exactly why the buckets now take precedence.

    def _movie_forward(self) -> dict:
        """``{("movie", tmdb): {role: [person_id]}}`` — the Radarr relational credit
        tables, SUPPLEMENTED by the daemon's own credit buckets for titles Radarr
        doesn't hold.

        All seven credited roles come through, cast ordered by ``billing_order``. A
        title present in several instances keeps whichever table actually has credits
        for it (see ``merge_forward``). Returns {} — never raises — when pandas or the
        tables are unavailable, so the show half and the fallback still run.

        WHY the supplement. The relational parquet is derived from Radarr, so it can only
        ever describe movies that are IN Radarr. Everything else in this class quietly
        inherited that ceiling: measured on a live watchlist, 574 of 845 watchlisted
        movies were in Radarr and 271 were not, and NONE of those 271 could enter the
        matrix no matter how thoroughly the enrich daemon fetched their credits. Those
        271 are exactly the acquisition candidates — the titles not yet added — so
        ``people_affinity`` and the co-occurrence proposer were both structurally blind
        to the set they exist to rank. The show half never had this problem because it
        reads its daemon bucket directly (``_show_forward``).

        The parquet stays PRIMARY: it carries Radarr's ``billing_order`` and
        ``role_type``, which the Trakt credit blobs approximate less precisely. The
        daemon bucket only fills ids the parquet said nothing about, so behaviour for
        every already-covered title is unchanged.
        """
        from scripts.managers.machine_learning.people_matrix import (
            forward_from_relations, merge_forward,
        )
        paths = self._instance_parquets("relational/movie_person_relations.parquet")
        if not paths:
            # P-B FIX (GLD-PPL-11): a missing parquet used to EARLY-RETURN {} past the
            # bucket supplement below — the exact inversion of this docstring's own
            # precedence ("buckets WIN … the AUTHORITATIVE source"). No relational
            # parquet has ever been produced on this deployment, so the movie half
            # silently vanished from every build: on 2026-08-07 the published forward
            # was SHOWS-ONLY (4,152 titles) while a fresh 1.5MB movie sidecar sat
            # unused, and the household person-affinity published as {} — the C4
            # people_affinity signal dead system-wide, the co-occurrence proposer
            # blind, and the billing experiment's join measuring an artifact with no
            # movies in it. No parquet ⇒ the buckets ARE the movie half.
            return self._movie_forward_from_buckets()
        try:
            import pandas as pd
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] pandas unavailable: {e}")
            return {}

        cols = ["tmdb_id", "person_tmdb_id", "role_type", "billing_order"]
        maps = []
        for path in paths:
            try:
                df = pd.read_parquet(path, columns=cols)
            except Exception as e:
                self.logger.log_debug(f"[PeopleMatrix] {path.parent.parent.name} relations unreadable: {e}")
                continue
            if df is None or df.empty:
                continue
            try:
                maps.append(forward_from_relations(df.to_dict("records")))
            except Exception as e:
                self.logger.log_debug(f"[PeopleMatrix] {path.parent.parent.name} relations unroutable: {e}")
        fwd = merge_forward(*maps) if maps else {}
        # Daemon buckets WIN over the parquet — see _movie_forward_from_buckets.
        fwd.update(self._movie_forward_from_buckets())
        return fwd

    def _movie_forward_from_buckets(self) -> dict:
        """Movie credits from the daemon's own gz buckets — the AUTHORITATIVE source.

        This used to be a gap-filler behind the Radarr relational parquet, on the belief
        that the parquet carried Radarr's own credits. It does not. ``relational.py``'s
        ``build_relations_from_movies`` is fed by ``TraktMoviePeopleManager``, which reads
        these very buckets — so the parquet is a DERIVATIVE of them, one hop further from
        the truth and free to go stale. It also holds no extra fidelity: its cast order
        comes from the same Trakt ``order`` field the buckets carry.

        So the precedence is now bucket-first, parquet only for titles the buckets have
        not reached. That matters during a refetch: after the id-collision fix the buckets
        are correct immediately, while the parquet keeps serving the old wrong credits
        until something regenerates it. Ranking the derived copy above its own source was
        the thing keeping known-good data out of the matrix.

        Incremental, via a ``{tmdb: mtime_ns}`` sidecar — the same trick ``_show_forward``
        uses. That is not premature: on a live cache the parquet held 12,987 tmdb ids
        against 25,007 in the daemon bucket, so the supplement is ~12k titles, and the
        build fingerprint now includes the movie bucket (it has to, or the supplement
        would freeze). The daemon rewrites that bucket continuously while it works the
        unowned backlog, so a non-incremental version would re-decompress 12k gz files
        every few minutes. Only files whose mtime moved are re-read.

        Best-effort: an unreadable blob is skipped rather than failing the build.
        """
        mc = self._get_movie_cache()
        if mc is None:
            return {}
        from scripts.managers.machine_learning.people_matrix import route_people

        current: dict[str, int] = {}
        try:
            with os.scandir(self.movie_bucket) as it:
                for entry in it:
                    if not entry.name.endswith(".json.gz"):
                        continue
                    try:
                        current[entry.name.split(".", 1)[0]] = entry.stat().st_mtime_ns
                    except OSError:
                        continue
        except (OSError, FileNotFoundError):
            return {}

        prior = self._read_gz(self.movies_sidecar, ignore_ttl=True) or {}
        prior_files = prior.get("files") or {}
        prior_roles = prior.get("roles") or {}

        roles_out: dict[str, dict] = {}
        reread = 0
        for mid, mtime in current.items():
            if prior_files.get(mid) == mtime and mid in prior_roles:
                roles_out[mid] = prior_roles[mid]
                continue
            reread += 1
            try:
                credits = mc.get_people(int(mid))
            except Exception:
                credits = None
            if not credits or not (credits.get("cast") or credits.get("crew")):
                roles_out[mid] = {}
                continue
            roles_out[mid] = route_people(credits)

        self._write_gz(self.movies_sidecar, {"files": current, "roles": roles_out})

        out: dict = {}
        for mid, roles in roles_out.items():
            if not roles:
                continue
            try:
                out[("movie", int(mid))] = roles
            except (TypeError, ValueError):
                continue
        if out:
            self.logger.log_info(
                f"[PeopleMatrix] {len(out):,} movies from daemon credits "
                f"({reread:,} re-read); these override the relational tables."
            )
        return out

    def _relational_names(self) -> dict:
        """``{person_tmdb_id: name}`` from the relational ``people.parquet`` tables — the
        id→name lookup for the movie half (the show half carries its own, routed from the
        daemon credits). Infra artifact only; no scorer reads it. Never raises."""
        paths = self._instance_parquets("relational/people.parquet")
        if not paths:
            return {}
        try:
            import pandas as pd
        except Exception:
            return {}
        out: dict = {}
        for path in paths:
            try:
                df = pd.read_parquet(path, columns=["person_tmdb_id", "name"])
            except Exception:
                continue
            for pid, name in zip(df["person_tmdb_id"], df["name"]):
                try:
                    if name:
                        out.setdefault(int(pid), str(name))
                except (TypeError, ValueError):
                    continue
        return out

    # ── SHOW half — incremental over the daemon credit buckets ──────────────────
    def _show_forward(self) -> "tuple[dict, dict, int]":
        """``({("show", tvdb): roles}, {tvdb: name}, n_reread)`` from the daemon show
        people buckets, re-reading ONLY the files whose mtime changed since the sidecar
        was written. Shows deleted from the bucket drop out of the map."""
        from scripts.managers.machine_learning.people_matrix import route_people, route_people_names
        bucket = self.show_bucket
        current: dict[str, int] = {}
        try:
            with os.scandir(bucket) as it:
                for entry in it:
                    if not entry.name.endswith(".json.gz"):
                        continue
                    try:
                        current[entry.name.split(".", 1)[0]] = entry.stat().st_mtime_ns
                    except OSError:
                        continue
        except (OSError, FileNotFoundError):
            return {}, {}, 0

        prior = self._read_gz(self.shows_sidecar, ignore_ttl=True) or {}
        prior_files = prior.get("files") or {}
        prior_roles = prior.get("roles") or {}
        prior_names = prior.get("names") or {}

        sc = self._get_show_cache()
        roles_out: dict[str, dict] = {}
        names_out: dict[str, str] = {}
        reread = 0
        for sid, mtime in current.items():
            if prior_files.get(sid) == mtime and sid in prior_roles:
                roles_out[sid] = prior_roles[sid]
                if sid in prior_names:
                    names_out[sid] = prior_names[sid]
                continue
            reread += 1
            credits = None
            if sc is not None:
                try:
                    credits = sc.get_people(int(sid))
                except Exception:
                    credits = None
            if not credits or not (credits.get("cast") or credits.get("crew")):
                roles_out[sid] = {}
                continue
            roles_out[sid] = route_people(credits)
            names_out[sid] = route_people_names(credits)

        self._write_gz(self.shows_sidecar,
                       {"files": current, "roles": roles_out, "names": names_out})

        fwd: dict = {}
        names: dict = {}
        for sid, roles in roles_out.items():
            if not roles:
                continue
            try:
                fwd[("show", int(sid))] = roles
            except (TypeError, ValueError):
                continue
            for pid, nm in (names_out.get(sid) or {}).items():
                try:
                    names.setdefault(int(pid), nm)
                except (TypeError, ValueError):
                    continue
        return fwd, names, reread

    # ── fingerprints ────────────────────────────────────────────────────────────
    @staticmethod
    def _bucket_sig(bucket) -> list:
        """``[file_count, newest_mtime_ns, total_bytes]`` for a daemon gz bucket — a cheap
        stat-only digest that changes whenever the daemon adds, rewrites or removes a
        credits blob. ``[0, 0, 0]`` for a missing/unreadable directory."""
        try:
            n = newest = total = 0
            with os.scandir(bucket) as it:
                for entry in it:
                    if not entry.name.endswith(".json.gz"):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    n += 1
                    total += st.st_size
                    newest = max(newest, st.st_mtime_ns)
            return [n, newest, total]
        except (OSError, FileNotFoundError):
            return [0, 0, 0]

    @staticmethod
    def _stat_sig(paths) -> list:
        sig = []
        for p in paths:
            try:
                st = p.stat()
                sig.append([str(p), st.st_size, st.st_mtime_ns])
            except OSError:
                continue
        return sig

    def _fingerprint(self, watched_digest) -> str:
        """One hash over everything that can change the built matrix: the relational
        credit tables, the movie_files tables the engagement comes from, the show
        bucket directory, the role-weight/decay signature and MATRIX_REVISION."""
        import hashlib
        from scripts.managers.machine_learning.people_matrix.build import (
            PERSON_BILLING_DECAY, PERSON_ROLE_WEIGHTS,
        )
        payload = [
            MATRIX_REVISION,
            sorted(PERSON_ROLE_WEIGHTS.items()), PERSON_BILLING_DECAY,
            self._stat_sig(self._instance_parquets("relational/movie_person_relations.parquet")),
            self._stat_sig(self._instance_parquets("movie_files.parquet")),
            self._bucket_sig(self.show_bucket),
            # The movie bucket now feeds _movie_forward_supplement, so the matrix must
            # rebuild when the daemon enriches a movie Radarr doesn't hold. Without this
            # the supplement would be computed once and then frozen behind a fingerprint
            # that could not see the very growth it depends on.
            self._bucket_sig(self.movie_bucket),
            watched_digest,
        ]
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8", "replace")
        ).hexdigest()

    def _read_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] state write skipped: {e}")

    def _artifacts_present(self) -> bool:
        """True when every artifact a skipped rebuild would have to reuse is on disk
        AND readable — a fingerprint match must never leave a consumer empty-handed."""
        try:
            return bool(self._read_gz(self.matrix_path, ignore_ttl=True)) and \
                   self._read_gz(self.affinity_path, ignore_ttl=True) is not None
        except Exception:
            return False

    def _republish(self) -> dict:
        """Re-publish the on-disk artifacts into global_cache after a skipped rebuild.
        global_cache is per-process, so a skip must still populate the keys the scorer
        and the acquisition candidate source read this run."""
        stats = {}
        try:
            fwd_raw = self._read_gz(self.matrix_path, ignore_ttl=True) or {}
            aff_raw = self._read_gz(self.affinity_path, ignore_ttl=True) or {}
            names_raw = self._read_gz(self.names_path, ignore_ttl=True) or {}
            if self.global_cache:
                self.global_cache.set("people_matrix/forward", fwd_raw)
                self.global_cache.set("people_matrix/affinity", aff_raw)
                if names_raw:
                    self.global_cache.set("people_matrix/names", names_raw)
            stats = {"titles": len(fwd_raw), "with_people": len(fwd_raw),
                     "persons": 0, "weighted_people": len(aff_raw),
                     "named_people": len(names_raw), "reused": True}
            if self.global_cache:
                self.global_cache.set("people_matrix/run_stats", stats)
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] republish skipped: {e}")
        return stats

    # ── build + persist ─────────────────────────────────────────────────────────
    def build(self, media_people: dict | None = None, *, force: bool = False) -> dict:
        """Build the person↔media graph, cache the forward map + household affinity, and
        log coverage.

        Sources, in order: the Radarr relational credits table (movie half, all seven
        roles with billing order) + the daemon show buckets (show half, incremental).
        ``media_people`` may be injected (tests) — that bypasses both sources and the
        fingerprint and builds straight from the supplied credits dicts. When neither
        relational tables nor injected credits are available the daemon gz buckets are
        the fallback.

        ``force=True`` rebuilds even when the fingerprint is unchanged.

        Best-effort + never raises into the run."""
        from scripts.managers.machine_learning.people_matrix import (
            build_index, invert_forward, merge_forward, serialize_forward, serialize_names)
        from scripts.managers.machine_learning.affinity.genre_affinity import aggregate_person_affinity
        try:
            watched_keys, engagement = self._household_watched()

            if media_people is not None:
                # Injected credits (tests / callers with their own source).
                person_index, fwd, names = build_index(media_people)
                total = len(media_people)
                src = "injected"
            else:
                _wd = [len(watched_keys), round(sum(engagement.values()), 4)]
                fp = self._fingerprint(_wd)
                if not force and fp and fp == self._read_state().get("fingerprint") \
                        and self._artifacts_present():
                    stats = self._republish()
                    self.logger.log_info(
                        f"[PeopleMatrix] unchanged since last build "
                        f"({stats.get('titles', 0):,} titles, "
                        f"{stats.get('weighted_people', 0):,} weighted people) — reused.")
                    return stats

                movie_fwd = self._movie_forward()
                show_fwd, show_names, reread = self._show_forward()
                src = f"movie graph({len(movie_fwd):,}) + show buckets({len(show_fwd):,}, {reread:,} re-read)"

                if not movie_fwd:
                    # No relational credits table yet (fresh install): fall back to the
                    # daemon gz buckets so the matrix still builds on day one.
                    fallback = dict(self._iter_media_people())
                    if fallback:
                        _, fb_fwd, fb_names = build_index(fallback)
                        movie_fwd = {k: v for k, v in fb_fwd.items() if k[0] == "movie"}
                        show_names = {**fb_names, **show_names}
                        src = f"daemon buckets fallback({len(movie_fwd):,})"

                fwd = merge_forward(movie_fwd, show_fwd)
                person_index = invert_forward(fwd)
                names = {**self._relational_names(), **(show_names or {})}
                total = len(fwd)
                self._write_state({"fingerprint": fp, "built_at": int(time.time())})

            with_people = sum(1 for roles in fwd.values() if any(roles.values()))
            self._save_forward(fwd)
            if self.global_cache:
                try:
                    self.global_cache.set("people_matrix/forward", serialize_forward(fwd))
                except Exception:
                    pass

            # Additive id→name lookup (infra). Persisted alongside the forward map so a
            # person id can be resolved to a label later; NOT read by the scorers (which
            # are id-keyed) or surfaced in any log. Fully isolated — a names-persistence
            # failure must NEVER abort the forward/affinity build it rides along with.
            try:
                self._save_names(names)
                if self.global_cache:
                    self.global_cache.set("people_matrix/names", serialize_names(names))
            except Exception:
                pass

            # Household person-affinity (volatile — cached SEPARATELY from the forward
            # map). Weighted three ways: by ROLE (a director outranks an editor), by
            # BILLING ORDER within the cast, and by how hard the household actually
            # ENGAGED with each title (finished vs abandoned, rewatched vs seen once).
            person_weights = aggregate_person_affinity(
                watched_keys, fwd, engagement=engagement)
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
                f"person-ids ({pct:.0f}% of enriched), {len(person_index):,} distinct people "
                f"[{src}]; household affinity over {len(watched_keys):,} watched "
                f"-> {len(person_weights):,} people.")
            stats = {"titles": total, "with_people": with_people,
                     "persons": len(person_index), "weighted_people": len(person_weights),
                     "named_people": len(names)}
            if self.global_cache:
                try:
                    self.global_cache.set("people_matrix/run_stats", stats)
                except Exception:
                    pass
            return stats
        except Exception as e:
            self.logger.log_warning(f"[PeopleMatrix] build skipped: {e}")
            return {"titles": 0, "with_people": 0, "persons": 0}

    def _household_watched(self) -> "tuple[set, dict]":
        """``(watched_keys, engagement)`` — the household's watched titles as
        ``(medium, ext_id)`` keys plus, per key, HOW HARD they were engaged with.

        Three sources, strongest first (the strongest engagement for a title wins):

        1. ``radarr/<inst>/movie_files.parquet`` — the only source with per-title
           ``watch_count`` AND ``percent_complete``, so it is the one that can tell a
           film rewatched five times from one abandoned at 20%. This is what makes the
           affinity weight say "the people behind what they actually finish", not just
           "the people behind what they once pressed play on".
        2. Trakt movie history — one entry per play, so repeated entries for the same
           tmdb id become rewatch credit.
        3. Per-group Tautulli tmdb completions — ``{tmdb: {pct, threshold}}``; the pct
           becomes the completion factor.

        Movies-first (the matrix's TV half has no watched-tvdb signal threaded yet;
        shows join in when one is). Empty when unconfigured — in which case the
        affinity vector is empty and ``resolve_person_affinity_inputs`` forces C4's cap
        to 0.0, exactly as before the matrix existed."""
        from scripts.managers.machine_learning.affinity.genre_affinity import (
            title_engagement_weight,
        )
        engagement: dict = {}

        def _bump(key, weight):
            if weight > 0 and weight > engagement.get(key, 0.0):
                engagement[key] = weight

        # 1 — owned movie rows (watch_count + percent_complete)
        try:
            import pandas as pd
            for path in self._instance_parquets("movie_files.parquet"):
                try:
                    df = pd.read_parquet(
                        path, columns=["tmdb_id", "watch_count", "percent_complete"])
                except Exception:
                    continue
                for tmdb, wc, pct in zip(df["tmdb_id"], df["watch_count"],
                                         df["percent_complete"]):
                    try:
                        if tmdb is None or tmdb != tmdb:      # NaN
                            continue
                        _bump(("movie", int(tmdb)), title_engagement_weight(wc, pct))
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] engagement from movie_files skipped: {e}")

        gc = self.global_cache
        if gc:
            # 2 — Trakt history: count plays per tmdb, then run them through the same
            # engagement curve (no completion column → treated as full plays).
            try:
                plays: dict = {}
                for entry in (gc.get("trakt/history/movies") or []):
                    tmdb = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
                    if tmdb:
                        plays[int(tmdb)] = plays.get(int(tmdb), 0) + 1
                for tmdb, n in plays.items():
                    _bump(("movie", tmdb), title_engagement_weight(n, None))
            except Exception:
                pass

            # 3 — Tautulli group completions
            cfg = getattr(self, "config", None)
            try:
                groups = (cfg.get("rating_groups", {}) if cfg else {}) or {"household": {}}
            except Exception:
                groups = {"household": {}}
            for group in groups:
                try:
                    raw = gc.get(f"tautulli/group/{group}/tmdb_completions") or {}
                    for tmdb_str, meta in raw.items():
                        try:
                            pct = (meta or {}).get("pct") if isinstance(meta, dict) else None
                            _bump(("movie", int(tmdb_str)), title_engagement_weight(1, pct))
                        except (ValueError, TypeError):
                            continue
                except Exception:
                    pass

        return set(engagement), engagement

    # Back-compat alias — the pre-engagement signature some callers/tests may use.
    def _household_watched_keys(self) -> set:
        return self._household_watched()[0]

    def _save_forward(self, fwd: dict) -> None:
        """Atomic gz write of the serialized forward map. Written even in dry_run — a
        derived read-cache, never touches the library."""
        from scripts.managers.machine_learning.people_matrix import serialize_forward
        self._write_gz(self.matrix_path, serialize_forward(fwd))

    def _save_affinity(self, person_weights: dict) -> None:
        """Atomic gz write of the household person-affinity ({person_id: weight}); int
        keys are stringified for JSON and coerced back on read by the consumers."""
        self._write_gz(self.affinity_path, {str(k): v for k, v in person_weights.items()})

    def _save_names(self, names: dict) -> None:
        """Atomic gz write of the id→name lookup ({person_id: name}); int keys stringified
        for JSON. Infra artifact — written even in dry_run (a derived read-cache)."""
        from scripts.managers.machine_learning.people_matrix import serialize_names
        self._write_gz(self.names_path, serialize_names(names))

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
        return self._read_gz(self.matrix_path)

    def load_names(self) -> dict:
        """Return the ``{tmdb_person_id: name}`` id→name lookup from cache (global_cache
        first, then the gz on disk), or ``{}`` when never built / stale / unreadable. Infra
        for any future consumer that wants to resolve a person id to a label."""
        from scripts.managers.machine_learning.people_matrix import deserialize_names
        raw = None
        if self.global_cache:
            try:
                raw = self.global_cache.get("people_matrix/names")
            except Exception:
                raw = None
        if raw is None:
            raw = self._read_gz(self.names_path)
        if not raw:
            return {}
        try:
            return deserialize_names(raw)
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] names parse error: {e}")
            return {}

    def _read_gz(self, path: Path, *, ignore_ttl: bool = False) -> dict | None:
        """Read a gz artifact. ``ignore_ttl`` is for the BUILD-side reads (the show
        sidecar, the fingerprint-skip republish): those are keyed on content
        fingerprints, not on wall-clock freshness, so a TTL expiry there would throw
        away a perfectly valid incremental cache and force a full re-read. The
        CONSUMER-side reads (load_index / load_names) keep the TTL."""
        try:
            st = path.stat()
        except OSError:
            return None
        if st.st_size == 0 or (not ignore_ttl and (time.time() - st.st_mtime) > self.ttl):
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.log_debug(f"[PeopleMatrix] cache read error: {e}")
            return None
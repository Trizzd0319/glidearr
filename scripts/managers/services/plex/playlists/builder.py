"""
plex/playlists/builder.py — per-user TV playlist BUILD + CACHE + dry-run preview.
================================================================================
The wrapper that makes personal TV playlists visible. For each tracked Home profile
it joins the data foundation and runs the brain, then CACHES the plan and LOGS a
dry-run preview grid. It writes NOTHING to Plex (write-back is a later, separately
gated phase) — this is the "see exactly what would be created" stage.

Up front it runs the readiness diagnosis (coverage + enrichment + daemon liveness)
and logs it prominently, so a shared install with partial coverage or an unfinished
enrichment daemon always understands its state (and that watchability ordering
resumes automatically once enrichment completes).

Gated behind ``plex.episodes.enabled`` (it needs the owned-episode inventory that
flag builds). The I/O gather is defensive — any missing piece degrades to empty
rather than raising; the tested core is ``_build_for_users`` (pure given its inputs).
"""
from __future__ import annotations

import json
import math
import os
from datetime import date

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.mdblist import client as mdblist_client
from scripts.managers.machine_learning.playlists.cert_gate import (
    cert_allowed,
    is_restricted,
    tier_level,
)
from scripts.managers.machine_learning.playlists.per_user import (
    GENRE_MATCH_MODES,
    genre_match,
    kids_household_affinity,
    priority_score,
)
from scripts.managers.machine_learning.playlists.rationale import explain_reason
from scripts.managers.services.plex._common import anon_label, metadata_items
from scripts.managers.services.plex.playlists.readiness import diagnose_tv_readiness
from scripts.managers.services.plex.playlists.tv_resolver import (
    build_tv_plan,
    watched_episode_keys,
    watched_episode_recency,
)
from scripts.managers.services.plex.playlists.universe_order import (
    CURATED_TV_FRANCHISES,
    _collection_norm,
    apply_universe_timeline,
    build_universe_maps,
    collection_group_key,
    collection_universe_key,
    detect_kometa,
    franchise_title_index,
    is_collection_noise,
    franchise_tier,
    is_stale,
    merge_movie_orders,
    movie_order_from_children,
    movie_universe_keys,
    saga_member_sets,
    series_order_from_children,
    split_list_media,
    tv_franchise_universes,
    tv_group_maps,
    universe_lists,
)

_INVENTORY_KEY = "plex/episodes/owned_inventory"
_STATS_KEY = "plex/episodes/resolution_stats"
_PLAN_KEY = "plex/playlists/tv_plan"          # + /{safe_user}
_UNIVERSE_SRC_KEY = "plex/playlists/universe_source"   # fetched universe lists (cache VOLUME)
_UNIVERSE_TTL_DAYS = 7                                  # re-fetch a universe list at most weekly
# Layer-2 cross-named TV-franchise catalog files (co-located with this package), in load order:
# the hand-vetted baked floor, then the generated catalog (overlays the floor). The
# `plex.playlists.tv_franchises` config key overlays both. See coordinator/tv_franchise_discovery.md.
_TV_FRANCHISE_FILES = ("tv_franchises.json", "tv_franchises.generated.json")
# In-universe MOVIE+SHOW watch order, generated from chronolists.com by
# `support/tools/generate_universe_timeline` (editorially sourced; movies keyed by tmdb, shows by tvdb).
# This LEADS the universe source — its full interleaved order replaces the movies-only mdblist list for
# every covered universe. Overlay/extend per install via `plex.playlists.universe_timeline.universes`.
_UNIVERSE_TIMELINE_FILE = "universe_timeline.json"


def _to_int(v):
    """Int-or-None — module-level so the BASE builder's universe helpers don't depend on the
    movie-subclass ``_coerce_int`` (the TV builder is a base instance)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finite_float(value, default: float) -> float:
    """float(value) but fail SAFE to default on non-numeric AND on non-finite (nan/inf).
    Bare float() accepts 'nan'/'inf' and json.load accepts unquoted Infinity/NaN, which
    would propagate NaN through the ranking weights and collapse order_items' sort into
    nondeterministic garbage. Keep all weights finite."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


class PlexPlaylistBuilderManager(BaseManager):
    parent_name = "PlexManager"
    _TIER_NAMES = ("little_kid", "older_kid", "teen", "adult")   # cert_gate level → label

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── run (I/O gather → tested core) ──────────────────────────────────────────
    def run(self) -> dict:
        tracked = self._tracked_users()
        owned_eps = self._load_owned_episodes()
        inventory = self._cache_get(_INVENTORY_KEY, {})
        resolution_stats = self._cache_get(_STATS_KEY, {})
        series_scores, series_genres = self._series_scores_and_genres()
        series_certs = self._series_certs()
        watched_by_user = {u["safe_user"]: self._watched_for(u.get("tautulli_user_id"))
                           for u in tracked}
        affinity_by_user = {u["safe_user"]: self._user_affinity(u.get("tautulli_username"))
                            for u in tracked}
        # TV-only playlist: let a Kometa user's custom SHOW-collection order lead (prefer_plex).
        franchise_by_series, series_timeline = self._tv_franchise_maps(owned_eps, prefer_plex=True)
        return self._build_for_users(
            tracked, owned_eps, inventory, resolution_stats, series_scores,
            watched_by_user, series_genres=series_genres, affinity_by_user=affinity_by_user,
            series_certs=series_certs, series_csm_ages=self._series_csm_ages(),
            daemon_enabled=self._daemon_enabled(), daemon_running=self._daemon_running(),
            franchise_by_series=franchise_by_series, series_timeline=series_timeline)

    def _build_for_users(self, tracked, owned_eps, inventory, resolution_stats,
                         series_scores, watched_by_user, *, series_genres=None,
                         affinity_by_user=None, series_certs=None, series_csm_ages=None,
                         daemon_enabled, daemon_running,
                         franchise_by_series=None, series_timeline=None) -> dict:
        """The orchestration core: diagnose readiness, then per user AGE-GATE (parental
        controls) + PERSONALIZE the series watchability by their genre affinity (tilt),
        build+cache the plan, and log a preview. Returns run stats. ``series_csm_ages``
        (series_id → Common Sense age) is the cert-gate fallback for uncertified series."""
        series_genres = series_genres or {}
        affinity_by_user = affinity_by_user or {}
        series_certs = series_certs or {}
        series_csm_ages = series_csm_ages or {}
        profile_ages = self._profile_ages()
        # Per-series ranking weights (affinity > JIT > household) + the household
        # normaliser + the per-user JIT-grabbed series (so an actively-watched show
        # outranks household-popular content for the member watching it).
        aff_w, hh_w, jit_w = self._priority_weights()
        hh_max = max((float(s) for s in series_scores.values() if s is not None), default=0.0) or 1.0
        jit_by_user = self._jit_series_by_user(tracked)
        owned_series = {ep.get("series_id") for ep in (owned_eps or [])
                        if ep.get("series_id") is not None}
        scored = sum(1 for sid in owned_series if series_scores.get(sid) is not None)

        diag = diagnose_tv_readiness(
            inventory_present=bool(inventory),
            resolution_pct=resolution_stats.get("resolution_pct"),
            max_pages_hit=bool(resolution_stats.get("max_pages_hit")),
            series_total=len(owned_series), series_scored=scored,
            daemon_enabled=daemon_enabled, daemon_running=daemon_running)
        for note in diag["notes"]:
            emit = self.logger.log_warning if note["level"] == "warn" else self.logger.log_info
            emit(f"[Playlists] {note['message']}")
        if not diag["can_build"]:
            return {"users": len(tracked), "built": 0, "can_build": False}

        display = self._display_map(inventory)
        built = 0
        for idx, u in enumerate(tracked, 1):
            watched = watched_by_user.get(u["safe_user"], set())
            user_aff = affinity_by_user.get(u["safe_user"]) or {}
            user_jit = jit_by_user.get(u["safe_user"], set())

            # AGE GATE (parental controls): for a restricted profile, keep ONLY series
            # whose certification fits the profile's tier — owner/adult sees everything.
            level = tier_level(u.get("restriction_profile"),
                               profile_ages.get(u.get("title")) or profile_ages.get(u.get("safe_user")))
            user_owned = owned_eps
            if is_restricted(level):
                user_owned = [ep for ep in owned_eps
                              if cert_allowed(series_certs.get(ep.get("series_id")), level,
                                              csm_age=series_csm_ages.get(ep.get("series_id")))]

            # Cold-start: a restricted profile with no affinity of its own inherits a prior from
            # the household's engagement with its age-appropriate owned content (parent co-views).
            user_aff = self._apply_cold_kids_prior(
                user_aff, level, self._series_genre_scores(user_owned, series_genres, series_scores))

            # One self-explaining diagnostic per user so per-user differentiation (or its
            # absence) is VISIBLE: which Tautulli account matched, how much affinity +
            # watch-history personalize the order, and the resolved parental-controls tier.
            # Without this, a shared top-20 dominated by one high-watchability series reads
            # as "identical playlists" even when the tail and gating actually differ.
            tier_name = self._TIER_NAMES[level] if 0 <= level < len(self._TIER_NAMES) else str(level)
            gate_note = (f", age-gated {len(user_owned)}/{len(owned_eps)} ep"
                         if is_restricted(level) else "")
            top_genres = ",".join(g for g, _ in sorted(
                user_aff.items(), key=lambda kv: -kv[1])[:3]) if user_aff else "-"
            # watched holds up to 3 identities per episode (ratingKey + (series,season,
            # episode) + (series,title)); count the ratingKey strings for the episode tally.
            n_watched = sum(1 for x in watched if isinstance(x, str))
            self.logger.log_info(
                f"[Playlists] {anon_label(u.get('title'), tier_name, idx)} -> "
                f"tautulli={'matched' if u.get('tautulli_username') else '-'}, "
                f"affinity={len(user_aff)} genre(s) [{top_genres}], watched={n_watched} ep, "
                f"jit={len(user_jit)}{gate_note}")

            # RANK each series for this user: user-affinity > JIT > household (weighted).
            # household is normalised so a household-favourite can't dominate by raw
            # magnitude; a series the user is actively watching (JIT) is lifted above
            # household-popular content but still loses to a strong affinity match. Rank
            # over the UNION of scored series and JIT'd series, scoring a JIT/affinity
            # series that has NO household score yet (a freshly JIT-acquired show the daemon
            # hasn't scored) with household_norm=0 — otherwise it would be dropped to last,
            # defeating JIT's whole purpose of surfacing what you're actively watching.
            user_scores = self._per_user_series_scores(
                series_scores, series_genres, user_aff, user_jit, hh_max, (aff_w, hh_w, jit_w),
                self._genre_match_opts())
            plan, stats = build_tv_plan(
                user_owned, inventory, watched, user_scores, family="up_next",
                episode_cap=self._episode_cap(), max_items=self._max_items(),
                franchise_by_series=franchise_by_series, series_timeline=series_timeline)
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{u['safe_user']}", self._serialize(plan))
            reasons = self._tv_reasons(user_owned, inventory, series_genres, user_aff, user_jit)
            self._log_preview(u, plan, stats, display, reasons, label="episode",
                              anon=anon_label(u.get("title"), tier_name, idx))
            built += 1
        self.logger.log_info(f"[Playlists] built {built} per-user TV plan(s) (dry-run — no Plex writes).")
        return {"users": len(tracked), "built": built, "can_build": True}

    # ── serialization + preview ─────────────────────────────────────────────────
    @staticmethod
    def _serialize(plan) -> dict:
        return {
            "family": plan.family, "considered": plan.considered,
            "dropped_watched": plan.dropped_watched, "truncated": plan.truncated,
            "coverage": plan.coverage,
            "items": [{"rating_key": i.rating_key, "ordinal": i.ordinal,
                       "group_key": i.group_key, "group_kind": i.group_kind,
                       "score": i.score, "reason": i.reason} for i in plan.items],
        }

    def _log_preview(self, user, plan, stats, display: dict, reasons=None, *,
                     kinds=None, label: str = "episode", family_label: str = "Up Next",
                     anon: str | None = None):
        """Preview grid: ``# | Title | [Kind] | Rank | Why``.

        ``Rank`` = the per-user priority_score the block is ordered on (affinity > JIT >
        household; 2dp so it stays discriminating). ``Why`` = the human rationale (from the
        ``reasons`` map keyed by ratingKey — genres/JIT/cast/crew/franchise — falling back
        to the brain's group reason). ``Kind`` (TV/Movie) only shows for the combined plan
        (``kinds`` given). ``label`` makes the header medium-correct (episode/movie/item)."""
        title = user.get("title") or user.get("safe_user") or "?"
        reasons = reasons or {}
        show_kind = kinds is not None
        rows = []
        for i in plan.items[:25]:
            rk = i.rating_key
            score = "" if i.score is None else f"{i.score:.2f}"
            why = reasons.get(rk) or i.reason or ""
            row = [str(i.ordinal + 1), display.get(rk, rk)]
            if show_kind:
                row.append(kinds.get(rk, "?"))
            rows.append(row + [score, why])
        # The SHAREABLE run log gets the de-identified handle (anon, e.g. 'T - adult 1'); the real
        # name only ever reaches the local playlists.log mirror below.
        who = anon or title
        header = (f"[dry-run] '{who}' {family_label} - {len(plan.items)} {label}(s), "
                  f"{stats.get('unresolved', 0)} unmatched")
        cols = ["#", "Title"] + (["Kind"] if show_kind else []) + ["Rank", "Why"]
        grid = getattr(self.logger, "log_grid", None)
        if callable(grid) and rows:
            grid(cols, rows, title=header, cap=44)
        else:
            self.logger.log_info(f"[Playlists] {header}")
        # Mirror the full preview into the dedicated, per-run support/logs/playlists.log so the
        # complete per-profile contents stay inspectable without bloating the main run log. This
        # file is a LOCAL operator drill-down (not shared), so it KEEPS the real profile name to
        # stay easy to validate by household member.
        to_file = getattr(self.logger, "log_to_file", None)
        if callable(to_file) and rows:
            file_header = (f"[dry-run] '{title}' {family_label} - {len(plan.items)} {label}(s), "
                           f"{stats.get('unresolved', 0)} unmatched")
            to_file("playlists", file_header)
            for r in rows:
                to_file("playlists", "  " + " | ".join(str(c) for c in r))

    @staticmethod
    def _per_user_series_scores(series_scores, series_genres, user_aff, user_jit, hh_max, weights,
                               gm_opts=None) -> dict:
        """{series_id: per-user priority_score} over the UNION of scored + JIT'd series
        (affinity > JIT > household). A JIT/affinity series with no household score is scored
        with household_norm=0 (not dropped); a series with no signal at all stays None.
        ``gm_opts`` (mode/soft_lambda/blend_weight) selects the genre_match shape."""
        aff_w, hh_w, jit_w = weights
        gm_opts = gm_opts or {}
        user_jit = user_jit or set()
        out: dict = {}
        for sid in set(series_scores) | user_jit:
            sc = series_scores.get(sid)
            gm = genre_match(series_genres.get(sid), user_aff, **gm_opts)
            jit = (int(sid) in user_jit) if sid is not None else False
            if sc is None and gm is None and not jit:
                out[sid] = None
                continue
            out[sid] = priority_score((float(sc) / hh_max) if sc is not None else 0.0,
                                      gm, is_jit=jit, affinity_weight=aff_w, jit_weight=jit_w,
                                      household_weight=hh_w)
        return out

    def _tv_reasons(self, user_owned, inventory, series_genres, user_aff, user_jit) -> dict:
        """{ratingKey: 'why'} for the TV preview — per-series genre + JIT rationale (all
        episodes of a series share their series' reason)."""
        out: dict = {}
        for ep in user_owned or []:
            jk = ep.get("tvdb_join_key")
            match = (inventory or {}).get(jk) if jk else None
            rk = str(match["rating_key"]) if (match and match.get("rating_key")) else None
            if rk is None or rk in out:
                continue
            sid = ep.get("series_id")
            out[rk] = explain_reason(
                series_genres.get(sid), user_aff,
                is_jit=(int(sid) in user_jit) if sid is not None else False)
        return out

    @staticmethod
    def _display_map(inventory: dict) -> dict:
        """rating_key → 'Series SxxExx' for a readable preview (season/episode parsed
        from the join key, which is '{tvdb}:{s}:{e}')."""
        out: dict = {}
        for jk, v in (inventory or {}).items():
            rk = v.get("rating_key")
            if not rk:
                continue
            parts = str(jk).split(":")
            se = f" S{parts[1]}E{parts[2]}" if len(parts) == 3 else ""
            out[str(rk)] = (f"{v.get('series_title', '')}{se}".strip()
                            or v.get("title", "") or str(rk))
        return out

    # ── universe / franchise timeline ordering (plex.playlists.universe_timeline.*) ──
    # Hybrid source: (1) fetch the SAME IMDb/mdblist universe lists Kometa uses, ourselves, via
    # mdblist → membership + saga order, cached with a TTL (auto-updates as new films release, no
    # Kometa + no container rebuild); (2) the operator's Kometa universe Plex COLLECTIONS, if any
    # (respects custom curation); (3) the bundled curated TV-franchise map. The list source is
    # primary; the others fill gaps. All inert when the feature flag is off → byte-identical.
    def _universe_timeline_enabled(self) -> bool:
        """plex.playlists.universe_timeline.enabled — default OFF. When off, the maps below are
        empty and the resolvers fall back to release/air date → byte-identical to today."""
        return bool((self._pl_cfg().get("universe_timeline", {}) or {}).get("enabled", False))

    def _mdblist_key(self) -> str:
        return ((self.config.get("mdblist", {}) if self.config else {}) or {}).get("apikey", "") or ""

    def _cfg_universe_lists(self) -> dict:
        return self._pl_cfg().get("universe_lists", {}) or {}

    def _universe_ttl_days(self) -> int:
        try:
            return int((self._pl_cfg().get("universe_timeline", {}) or {}).get("ttl_days", _UNIVERSE_TTL_DAYS))
        except (TypeError, ValueError):
            return _UNIVERSE_TTL_DAYS

    def _universe_source(self) -> dict:
        """The cached universe contents — ``{"universes": {key: {timeline, movies, shows}}}`` —
        fetched from the mdblist universe lists. Refreshes any STALE/never-fetched universe (TTL),
        keeping the LAST-GOOD entry on a fetch failure (a transient mdblist outage never wipes a
        working list). Returns ``{}`` when the feature is off. With no API key it serves whatever
        was last cached (so the feature survives a key being removed)."""
        if not self._universe_timeline_enabled():
            return {}
        cached = dict(self._cache_get(_UNIVERSE_SRC_KEY, {}) or {})
        universes = dict(cached.get("universes") or {})
        fetched = dict(cached.get("fetched") or {})
        key = self._mdblist_key()
        refreshed = 0
        if key:
            now = date.today().toordinal()
            ttl = self._universe_ttl_days()
            for uk, defn in universe_lists(self._cfg_universe_lists()).items():
                if not isinstance(defn, dict):     # a config typo (bare string) can't abort the run
                    continue
                if not is_stale(fetched.get(uk), now, ttl):
                    continue
                res = mdblist_client.list_items(key, defn)
                if res.get("ok") and res.get("items"):
                    universes[uk] = split_list_media(res["items"], bool(defn.get("timeline", True)),
                                                     titles=res.get("titles"))
                    fetched[uk] = now
                    refreshed += 1
                # else: leave the prior entry untouched (LAST-GOOD)
        # The ``timeline`` flag is config-authoritative: re-stamp each entry from the CURRENT
        # universe_lists() defn so flipping a list to ``timeline: False`` (a reverse-sorted list → order
        # by release date ascending) takes effect on the NEXT run, not only after the TTL re-fetch.
        defns = universe_lists(self._cfg_universe_lists())
        for uk in list(universes):
            d = defns.get(uk)
            if isinstance(universes[uk], dict) and isinstance(d, dict):
                universes[uk] = {**universes[uk], "timeline": bool(d.get("timeline", True))}
        # Let the baked chronolists timeline LEAD: replace each covered universe's entry with the full
        # in-universe MOVIE+SHOW order (films AND shows), demoting mdblist to a new-release top-up. Runs
        # here (live grouping + refresh-run cache write) AND in the synthetic refresh, so the interleaved
        # order is present no matter which path of the run touches the cache first.
        universes = apply_universe_timeline(universes, self._universe_timeline_catalog())
        if refreshed and self.global_cache:
            self.global_cache.set(_UNIVERSE_SRC_KEY, {"universes": universes, "fetched": fetched})
            self.logger.log_info(f"[UniverseOrder] refreshed {refreshed} universe list(s) from mdblist "
                                 f"({len(universes)} cached).")
        return {"universes": universes}

    def _movie_universe_order(self, movie_inventory, owned_movies=None, *, prefer_plex=False) -> dict:
        """``{tmdb_id: position}`` saga order — MERGED from the mdblist/chronolist universe order + the
        operator's Kometa universe Plex collections. ``{}`` when the feature is off → release date.

        ``prefer_plex`` picks the winner on overlap. The MOVIE-only playlist passes ``True`` so a Kometa
        user's hand-curated COLLECTION order leads. The COMBINED (movie+show) playlist keeps the default
        ``False`` so the chronolist bake leads — the bake is the only source with a UNIFIED movie+show
        rank, and a movies-only collection order winning there would bunch all films ahead of the shows."""
        if not self._universe_timeline_enabled():
            return {}
        owned_tmdbs = {t for m in (owned_movies or []) if (t := _to_int(m.get("tmdb_id"))) is not None}
        _, list_order, _, _ = build_universe_maps(self._universe_source(), owned_tmdbs, {})
        plex_order = self._plex_collection_order(movie_inventory, owned_movies)
        if prefer_plex:
            return {**list_order, **plex_order}        # Kometa Plex-collection curation wins
        return {**plex_order, **list_order}            # bake/list wins (interleave-safe)

    def _movie_universe_membership(self, owned_movies=None) -> dict:
        """``{tmdb_id: set(universe_keys)}`` GROUPING from the fetched universe lists — forms a
        universe block with NO Kometa ``universe_name`` tag required. ``{}`` when the feature is off."""
        if not self._universe_timeline_enabled():
            return {}
        owned_tmdbs = {t for m in (owned_movies or []) if (t := _to_int(m.get("tmdb_id"))) is not None}
        membership, _, _, _ = build_universe_maps(self._universe_source(), owned_tmdbs, {})
        return membership

    def _all_collections(self) -> list:
        """Every Plex collection across ALL library sections, as a flat list of metadata dicts.
        Collections are PER-SECTION on PMS — the global ``/library/collections`` endpoint returns
        nothing on modern servers — so iterate ``get_sections()`` and read each section's collections.
        ``[]`` with no Plex API / on error."""
        if not self.plex_api:
            return []
        try:
            secs = metadata_items(self.plex_api.get_sections())
        except Exception:
            return []
        out: list = []
        for s in secs:
            sid = s.get("key")
            if sid is None:
                continue
            try:
                out.extend(metadata_items(self.plex_api.get_collections(section_id=sid)))
            except Exception:
                continue
        return out

    def _plex_collection_order(self, movie_inventory, owned_movies=None) -> dict:
        """``{tmdb_id: position}`` from the operator's Kometa UNIVERSE Plex collections (read IN
        COLLECTION ORDER). A child film earns a saga index only if it belongs to THIS universe — proven
        by its Radarr ``universe_name`` tag OR (tag-free) by membership in the universe's canonical
        list/bake — so a Kometa user with ZERO universe tags still gets ordering, while a film mis-filed
        in the wrong Plex collection is excluded (it's in another universe's list). ``{}`` with no Plex
        API / no movie inventory. Secondary to the list source — honours a custom Plex curation."""
        if not self.plex_api or not movie_inventory:
            return {}
        rk_to_tmdb = self._inventory_rk_to_tmdb(movie_inventory)
        keys_by_tmdb = movie_universe_keys(owned_movies)           # Radarr universe_name tags (may be empty)
        members = saga_member_sets(self._universe_source())        # tag-free list/bake membership
        cols = self._all_collections()
        det = detect_kometa([d.get("title") for d in cols])
        if det["detected"]:
            self.logger.log_info(f"[UniverseOrder] Kometa Defaults detected "
                                 f"({len(det['separators'])} separator collection(s); "
                                 f"{len(det['universe_keys'])} universe collection(s) recognised).")
        orders, matched = [], 0
        for d in cols:
            rk = d.get("ratingKey")
            key = collection_universe_key(d.get("title")) if rk is not None else None
            if key is None:
                continue
            try:
                # get_collections is library-wide: TV-library universe collections (e.g. Arrowverse)
                # also match, but their SHOW ratingKeys aren't in rk_to_tmdb so they drop to {}.
                kids = metadata_items(self.plex_api.get_collection_children(rk))
            except Exception:
                continue
            child_rks = [str(c.get("ratingKey")) for c in kids if c.get("ratingKey") is not None]
            # Belong-to-this-universe guard, tag-free: the Radarr ``universe_name`` tag (if any) UNIONed
            # with the universe's canonical list membership — so a tag-less Kometa install still orders,
            # and a film in the wrong Plex collection is still excluded (it's not in THIS universe's list).
            allowed = {t for t, ks in keys_by_tmdb.items() if key in ks}
            allowed |= set((members.get(key) or {}).get("movies") or {})
            order = movie_order_from_children(child_rks, rk_to_tmdb, allowed_tmdbs=allowed)
            if order:
                orders.append(order)
                matched += 1
        merged = merge_movie_orders(orders)
        if merged:
            self.logger.log_info(f"[UniverseOrder] {matched} Plex universe collection(s) → "
                                 f"{len(merged)} owned movie(s).")
        return merged

    @staticmethod
    def _tvdb_from_guids(item) -> int | None:
        """tvdb id from a Plex item's external ``Guid[]`` (FREE parse, no Discover hop) — handles the
        modern ``tvdb://12345`` and the legacy ``com.plexapp.agents.thetvdb://12345?lang=en`` agent
        forms; ``None`` for a non-tvdb / bare ``plex://`` item (it simply won't get a saga position)."""
        cands = [g.get("id", "") for g in (item.get("Guid") or []) if isinstance(g, dict)]
        cands.append(item.get("guid", "") or "")
        for gid in cands:
            if "tvdb" not in gid:
                continue
            tail = gid.rsplit("/", 1)[-1].split("?", 1)[0]     # 12345 (modern + legacy agent form)
            if tail.isdigit():
                return int(tail)
        return None

    def _plex_tv_collection_order(self, tvdb_to_sid):
        """``({series_id: franchise}, {series_id: position})`` from the operator's Kometa UNIVERSE
        SHOW collections (read IN COLLECTION ORDER) — the TV analogue of :meth:`_plex_collection_order`.
        Owned episodes are Sonarr-sourced (no Plex show ratingKey), so each child show is joined to a
        ``series_id`` by free-parsing its tvdb from the Plex ``Guid[]`` and looking it up in
        ``tvdb_to_sid``. ``({}, {})`` with no Plex API / no owned series. Secondary to the curated/list
        source — present only to honour a custom Plex curation a Kometa user may have."""
        if not self.plex_api or not tvdb_to_sid:
            return {}, {}
        # Recognise both UNIVERSE collections (Arrowverse, MCU…) and FRANCHISE collections (One Chicago,
        # NCIS, Doctor Who…) by matching the title to a known glidearr group key — with a trailing
        # parenthetical stripped, so a custom "Arrowverse (Watch Order)" still resolves to 'arrow'.
        fidx = franchise_title_index({**self._tv_franchise_catalog(), **self._universe_timeline_catalog()},
                                     CURATED_TV_FRANCHISES)
        cols = self._all_collections()
        kometa = detect_kometa([d.get("title") for d in cols])["detected"]    # trust unknown collections only on Kometa
        fran_all, time_all, matched = {}, {}, 0
        for d in cols:
            rk = d.get("ratingKey")
            if rk is None:
                continue
            title = d.get("title")
            key = collection_group_key(title, fidx)
            tentative = key is None
            if tentative:
                # An unrecognised collection on a Kometa install IS a franchise when it isn't a
                # separator/streaming rollup AND has >=2 owned member shows — so CSI / Power / Yellowstone
                # group from the collection ITSELF, no hand-maintained key list. Single-show + noise skip.
                if not kometa or is_collection_noise(title):
                    continue
                key = _collection_norm(title)
                if not key:
                    continue
            try:
                kids = metadata_items(self.plex_api.get_collection_children(rk, include_guids=True))
            except Exception:
                continue
            ordered, rk_to_sid = [], {}
            for c in kids:
                crk = c.get("ratingKey")
                if crk is None:
                    continue
                crk = str(crk)
                ordered.append(crk)
                tvdb = self._tvdb_from_guids(c)
                sid = tvdb_to_sid.get(tvdb) if tvdb is not None else None
                if sid is not None:
                    rk_to_sid[crk] = sid
            if not rk_to_sid or (tentative and len(set(rk_to_sid.values())) < 2):
                continue                                       # no owned shows, or a single-show "franchise"
            fran, tmap = series_order_from_children(ordered, rk_to_sid, key, with_timeline=True)
            if fran:
                fran_all.update(fran)
                time_all.update(tmap)
                matched += 1
        if fran_all:
            self.logger.log_info(f"[UniverseOrder] {matched} Plex universe/franchise SHOW collection(s) → "
                                 f"{len(fran_all)} owned series.")
        return fran_all, time_all

    def _tv_franchise_maps(self, owned_eps, *, prefer_plex=False):
        """``({series_id: franchise}, {series_id: timeline_index})`` for owned series — the canonical
        merge of the bundled curated TV franchises (One Chicago, Law & Order, …) + the fetched
        universe lists (tvdb→series_id; e.g. Arrowverse / Star Trek TV), delegated to
        ``universe_order.tv_group_maps`` so the playlist builder and the acquisition prefetch read
        the SAME grouping + order. ``({}, {})`` when the feature is off → per-series fallback.

        ``prefer_plex`` picks the winner on overlap (mirrors the movie path): the TV-only playlist passes
        ``True`` so a Kometa user's custom SHOW-collection order LEADS; the COMBINED playlist keeps the
        default ``False`` so the bake leads (its unified movie+show rank drives the interleave)."""
        if not self._universe_timeline_enabled():
            return {}, {}
        # Regenerate the owned-inventory TV-franchise (tvfran:) entries into the universe-source
        # cache BEFORE we read it below — so the SAME synthetic franchises reach this live playlist
        # grouping AND the cache-reading consumers that run later this run (catch-up retention +
        # hybrid universe acquisition both read plex/playlists/universe_source directly).
        self._refresh_synthetic_universes(owned_eps)
        seen: dict = {}
        tvdb_to_sid: dict = {}
        for ep in owned_eps or []:
            sid = ep.get("series_id")
            if sid is None:
                continue
            seen.setdefault(sid, ep.get("series_title") or ep.get("title") or "")
            tv = _to_int(ep.get("series_tvdb_id"))
            if tv is not None:
                tvdb_to_sid.setdefault(tv, sid)
        fran, timeline = tv_group_maps(list(seen.items()), self._universe_source(), tvdb_to_sid)
        # Honour a Kometa user's custom SHOW-collection order. prefer_plex=True → it OVERRIDES the
        # curated/list order for any series in a Plex collection (custom curation wins). prefer_plex=False
        # → gap-fill only: add series the curated/list source didn't already group, so the bake leads.
        plex_fran, plex_time = self._plex_tv_collection_order(tvdb_to_sid)
        for sid, fkey in plex_fran.items():
            if sid in fran and not prefer_plex:
                continue
            fran[sid] = fkey
            if sid in plex_time:
                timeline[sid] = plex_time[sid]
            elif prefer_plex:
                timeline.pop(sid, None)
        if fran:
            self.logger.log_info(f"[UniverseOrder] {len(set(fran.values()))} TV franchise(s) → "
                                 f"{len(fran)} owned series grouped.")
        return fran, timeline

    def _refresh_synthetic_universes(self, owned_eps) -> None:
        """Merge the owned-inventory TV-franchise (``tvfran:``) entries into the cached universe
        source, regenerated EVERY run from current inventory. Stale ``tvfran:`` keys are stripped
        first (a removed/renamed family never lingers) and the mdblist universes + their TTL
        metadata are preserved verbatim — the synthetic seam never pollutes the last-good cache.
        Best-effort: a failure here never blocks playlist grouping. Reaches the live grouping below
        AND the later cache-reading consumers (catch-up retention, hybrid universe acquisition)."""
        if not self.global_cache:
            return
        try:
            rows, seen_tv = [], set()
            for ep in owned_eps or []:
                tv = _to_int(ep.get("series_tvdb_id"))
                if tv is None or tv in seen_tv:
                    continue
                seen_tv.add(tv)
                rows.append({"title": ep.get("series_title") or ep.get("title") or "",
                             "tvdbId": tv, "year": ep.get("series_year") or ep.get("year")})
            cached = dict(self._cache_get(_UNIVERSE_SRC_KEY, {}) or {})
            universes = {k: v for k, v in (cached.get("universes") or {}).items()
                         if not str(k).startswith("tvfran:")}            # strip prior synthetic
            # Let the baked chronolists timeline LEAD: rebuild each covered universe's entry from the full
            # in-universe MOVIE+SHOW order (MCU, Star Wars, Arrowverse, Buffy, …), demoting mdblist to a
            # new-release top-up. Regenerated every run; carries each member's title (so unowned films get
            # named) — reaching grouping, retention AND acquisition through the cache below.
            tl_catalog = self._universe_timeline_catalog()
            universes = apply_universe_timeline(universes, tl_catalog)
            # Universe show tvdbs — now INCLUDING the baked saga shows — so a TV franchise a universe
            # already groups (Arrowverse, Star Trek, the MCU/SW shows, AND chronolists' own TV franchises
            # like Buffy/One Chicago) is NOT re-emitted as a tvfran: entry (no double-grouping).
            deny = {ti for v in universes.values() for tv in (v.get("shows") or [])
                    if (ti := _to_int(tv)) is not None}
            catalog = self._tv_franchise_catalog()
            syn = tv_franchise_universes(rows, catalog,
                                         engaged_tvdbs=self._watchlisted_show_tvdbs(), deny_tvdbs=deny)
            fetched = {k: v for k, v in (cached.get("fetched") or {}).items()
                       if k != "__tvfran__"}                            # drop only our marker; mdblist TTL untouched
            universes.update(syn)
            if syn:
                fetched["__tvfran__"] = date.today().toordinal()         # bookkeeping; not read by the mdblist TTL loop
            self.global_cache.set(_UNIVERSE_SRC_KEY, {"universes": universes, "fetched": fetched})
            self._publish_saga_member_titles(rows, catalog)
            tl_keys = [k for k in (tl_catalog or {}) if k in universes]
            if tl_keys:
                shows = sum(len((universes[k] or {}).get("shows") or []) for k in tl_keys)
                self.logger.log_info(f"[UniverseOrder] {len(tl_keys)} chronolist universe(s) lead the "
                                     f"timeline ({shows} interleaved show(s) across them).")
            if syn:
                self.logger.log_info(f"[UniverseOrder] {len(syn)} engaged TV franchise(s) "
                                     f"(owned or watchlisted) feeding grouping, retention and acquisition.")
        except Exception as e:
            self.logger.log_debug(f"[UniverseOrder] synthetic franchise refresh skipped: {e}")

    def _publish_saga_member_titles(self, rows, catalog) -> None:
        """Cache ``plex/playlists/saga_member_titles`` = ``{str(tvdb): title}`` for every TV-franchise
        member — owned series (from the rows) + the TV catalog's UNOWNED siblings (its parallel
        titles/shows arrays). The universe-acquisition preview + the future GUI resolve a backfilled
        tvfran: member's id to a real title from this. (Chronolist-led universes carry their members'
        titles in the universe-source entry itself — read directly by the coordinator's ``_titles_map``
        — so they need no publish here.)"""
        if not self.global_cache:
            return
        try:
            titles: dict = {}
            for r in (rows or []):
                tv = _to_int(r.get("tvdbId"))
                if tv is not None and r.get("title"):
                    titles.setdefault(tv, r["title"])
            for entry in (catalog or {}).values():
                if not isinstance(entry, dict):
                    continue
                for t, s in zip(entry.get("titles") or [], entry.get("shows") or []):
                    si = _to_int(s)
                    if si is not None and t:
                        titles.setdefault(si, t)
            self.global_cache.set("plex/playlists/saga_member_titles", {str(k): v for k, v in titles.items()})
        except Exception as e:
            self.logger.log_debug(f"[UniverseOrder] saga-member-titles publish skipped: {e}")

    def _watchlisted_show_tvdbs(self) -> set:
        """Household-watchlisted SHOW tvdbs (intent to watch) from the watchlist union — these may be
        UNOWNED, so a watchlist add still scopes its whole franchise into the universe source (for
        completion + retention) even before any member is in the library. ``set()`` when absent."""
        out: set = set()
        if not self.global_cache:
            return out
        try:
            for it in (self.global_cache.get("plex/watchlist/union") or []):
                if isinstance(it, dict) and it.get("type") == "show":
                    tv = _to_int((it.get("ids") or {}).get("tvdb"))
                    if tv is not None:
                        out.add(tv)
        except Exception:
            pass
        return out

    def _tv_franchise_catalog(self) -> dict:
        """The Layer-2 cross-named TV-franchise catalog (tvdb-keyed) the same-stem clusterer can't
        derive — Grey's↔Station 19↔Private Practice, Buffy↔Angel, … — merged from the baked floor,
        the generated catalog (if present) and the ``plex.playlists.tv_franchises`` config overlay
        (each later source overlays the earlier). ``{}`` when none exist. Shape is
        ``{franchise_key: {"shows": [tvdb…], "titles": [...], "tier": int}}``, fed to
        :func:`tv_franchise_universes` alongside the owned-inventory clusters.

        Each entry carries a ``tier`` for ACQUISITION priority. A key in the hand-curated floor OR the
        config overlay is tier 0 (known/trusted — Grey's, One Chicago, …). A GENERATED family is
        auto-PROMOTED to tier 0 when it's cross-validated by ≥ ``tv_franchise_promote_min_sources``
        (default 2) independent edges (its ``sources`` list — e.g. a Wikidata spin-off AND a Wikipedia
        category agree); a single-source generated family stays tier 2 (unvetted). So the floor
        promotion is automatic + data-driven off the generated catalog — read it when present, ignore it
        when absent. Recomputed per call (the files are tiny + a manager is a long-lived singleton, so a
        stale memo would freeze a regenerated catalog)."""
        catalog: dict = {}
        curated_keys: set = set()
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        for fname in _TV_FRANCHISE_FILES:
            try:
                with open(os.path.join(pkg_dir, fname), encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "generated" not in fname:               # the baked floor is hand-curated/trusted
                        curated_keys.update(data.keys())
                    catalog.update(data)                       # generated overlays the baked floor
            except FileNotFoundError:
                continue
            except Exception as e:
                self.logger.log_debug(f"[UniverseOrder] tv-franchise catalog read skipped ({fname}): {e}")
        overlay = self._pl_cfg().get("tv_franchises", {})
        if isinstance(overlay, dict) and overlay:
            catalog.update(overlay)                            # config overlay wins (no rebuild)
            curated_keys.update(overlay.keys())                # operator-added → trusted
        try:
            min_src = int(self._pl_cfg().get("tv_franchise_promote_min_sources", 2))
        except (TypeError, ValueError):
            min_src = 2
        for k, v in catalog.items():                           # acquisition tier: curated, or auto-promote
            if isinstance(v, dict):                            # a cross-validated generated family to tier 0
                v["tier"] = franchise_tier(k in curated_keys, v.get("sources"), min_src)
        return catalog

    def _universe_timeline_catalog(self) -> dict:
        """The baked in-universe MOVIE+SHOW order (``universe_timeline.json``, generated from
        chronolists.com) — the full chronological interleave per universe — merged with the
        ``plex.playlists.universe_timeline.universes`` config overlay (which wins per key, so an operator
        re-orders or adds a whole universe with one config block, no rebuild). ``{}`` when neither
        exists. Shape ``{universe_key: {"display"?, "sources"?, "items": [{"media", "tmdb"|"tvdb",
        "title"?}…]}}``, fed to :func:`apply_universe_timeline`. Recomputed per call (tiny file; a manager
        is a long-lived singleton, so a stale memo would freeze a JSON/overlay edit)."""
        catalog: dict = {}
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(pkg_dir, _UNIVERSE_TIMELINE_FILE), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                catalog.update(data)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.log_debug(f"[UniverseOrder] universe-timeline overlay read skipped: {e}")
        overlay = (self._pl_cfg().get("universe_timeline", {}) or {}).get("universes", {})
        if isinstance(overlay, dict) and overlay:
            catalog.update(overlay)                            # config overlay wins per universe (no rebuild)
        return catalog

    # ── config knobs ────────────────────────────────────────────────────────────
    def _pl_cfg(self) -> dict:
        return ((self.config.get("plex", {}) if self.config else {}) or {}).get("playlists", {}) or {}

    def _episode_cap(self) -> int:
        try:
            return int(self._pl_cfg().get("episode_cap", 5))
        except (TypeError, ValueError):
            return 5

    def _max_items(self) -> int:
        try:
            return int(self._pl_cfg().get("max_items", 100))
        except (TypeError, ValueError):
            return 100

    def _genre_match_opts(self) -> dict:
        """genre_match shape knobs (plex.playlists.*) — passed into every per-user score so the
        whole household re-ranks together. ``genre_match_mode`` (precision | soft | coverage |
        blend; default 'precision' = legacy, byte-identical); ``genre_match_soft_lambda`` (soft
        denominator weight for off-taste genres, default 0.5); ``genre_match_blend_weight``
        (coverage share in blend, default 0.85). Easy A/B: flip the mode and re-run the dry-run."""
        pl = self._pl_cfg()
        mode = str(pl.get("genre_match_mode", "precision")).strip().lower()
        if mode not in GENRE_MATCH_MODES:
            mode = "precision"
        return {
            "mode": mode,
            "soft_lambda": _finite_float(pl.get("genre_match_soft_lambda", 0.5), 0.5),
            "blend_weight": _finite_float(pl.get("genre_match_blend_weight", 0.85), 0.85),
        }

    def _jit_weight(self) -> float:
        """Weight of the JIT (actively-watched) boost. plex.playlists.jit_weight, default
        0.65 — tuned on real watch history (temporal-holdout offline eval over 7 ranking
        metrics: jit_weight=0 was always worst; the best sat in [0.5,0.8] across users and
        methodology sweeps; 0.65 preserves an affinity-beats-JIT zone, vs 0.8 which lets
        JIT nearly always dominate). Clamped between the household and affinity weights in
        _priority_weights so the precedence user-affinity > JIT > household holds."""
        return _finite_float(self._pl_cfg().get("jit_weight", 0.65), 0.65)

    def _priority_weights(self):
        """(affinity_weight, household_weight, jit_weight) for priority_score, with the
        precedence affinity > JIT > household ENFORCED INTRINSICALLY — re-ordered after the
        fact so NO knob magnitude can invert it (a low personal_tilt used to flip the whole
        ranking to household-led). Defaults 0.9 / 0.1 / 0.65; each is a plex.playlists.*
        knob (affinity_weight, household_weight, jit_weight); personal_tilt is a legacy
        alias for affinity_weight (tilt/100). At the defaults a JIT-grabbed series outranks
        household-popular content but loses to a STRONG affinity match — affinity beats JIT
        when its genre match exceeds jit_w/aff_w (≈0.72), which thousands of real series clear."""
        pl = self._pl_cfg()
        aff_w = _finite_float(pl.get("affinity_weight"), self._personal_tilt() / 100.0)
        hh_w = min(max(_finite_float(pl.get("household_weight"), 0.1), 0.0), 1.0)
        jit_w = max(self._jit_weight(), 0.0)
        # Re-order with a margin so affinity > JIT > household ALWAYS holds, whatever the
        # configured magnitudes — the invariant is intrinsic, not magnitude-dependent.
        _GAP = 0.05
        jit_w = max(jit_w, hh_w + _GAP)
        aff_w = max(aff_w, jit_w + _GAP)
        return aff_w, hh_w, jit_w

    # ── cold-start prior for kid profiles (config plex.playlists.cold_start_kids_prior) ──
    def _cold_kids_prior_enabled(self) -> bool:
        """plex.playlists.cold_start_kids_prior — default OFF. When on, a RESTRICTED profile
        with no affinity of its own is seeded from the household's engagement with its
        age-appropriate content (a parent co-viewing kid shows) instead of a flat household
        order. Off → byte-identical to today."""
        return bool(self._pl_cfg().get("cold_start_kids_prior", False))

    def _apply_cold_kids_prior(self, user_aff, level, genre_score_pairs) -> dict:
        """Substitute a household-kids cold-start prior for a restricted profile that has NO
        affinity of its own (see :func:`kids_household_affinity`). A no-op — returns ``user_aff``
        unchanged — for adults, for any user that already has affinity, or when the feature is
        off, so the default ranking path is untouched."""
        if user_aff or not is_restricted(level) or not self._cold_kids_prior_enabled():
            return user_aff
        return kids_household_affinity(genre_score_pairs)

    @staticmethod
    def _series_genre_scores(eps, series_genres, series_scores) -> list:
        """``[(genres, household_score)]`` over the DISTINCT series in ``eps`` (so a long-running
        show contributes its genres ONCE, not per episode) — the TV input to the kids prior."""
        seen: dict = {}
        for e in eps or []:
            sid = e.get("series_id")
            if sid is not None and sid not in seen:
                seen[sid] = (series_genres.get(sid), series_scores.get(sid))
        return list(seen.values())

    # ── I/O gather (defensive — degrade to empty, never raise) ──────────────────
    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

    def _tracked_users(self) -> list:
        if not self.registry:
            return []
        um = self.registry.get("manager", "PlexUsersManager")
        return list(getattr(um, "tracked_users", []) or []) if um else []

    def _sonarr_instances(self) -> list:
        insts = [k for k, v in ((self.config.get("sonarr_instances", {}) if self.config else {}) or {}).items()
                 if k != "default_instance" and isinstance(v, dict)]
        return insts or ["sonarr"]

    def _jit_series_by_user(self, tracked) -> dict:
        """{safe_user: set(series_id)} — series JIT grabbed FOR each user. Intersects the
        per-instance ``sonarr/<i>/jit_grabbed`` set (what the JIT pass acquired/upgraded)
        with ``sonarr/<i>/jit_watchers`` (who recently watched each series) so a series is
        JIT-priority ONLY for the member(s) actually watching it, never the whole household."""
        out = {u["safe_user"]: set() for u in tracked}
        by_username = {}
        for u in tracked:
            un = u.get("tautulli_username")
            if un:
                by_username[str(un).strip().lower()] = u["safe_user"]
        if not by_username:
            return out
        for inst in self._sonarr_instances():
            grabbed = self._cache_get(f"sonarr/{inst}/jit_grabbed", []) or []
            watchers = self._cache_get(f"sonarr/{inst}/jit_watchers", {}) or {}
            for sid in grabbed:
                for un in (watchers.get(str(sid)) or []):
                    safe = by_username.get(str(un).strip().lower())
                    if safe is not None:
                        try:
                            out[safe].add(int(sid))
                        except (TypeError, ValueError):
                            pass
        return out

    def _load_owned_episodes(self) -> list:
        # Dedupe within a run. The TV builder (gate=episodes) and the combined builder
        # (gate=movies) each call this, and build_or_refresh re-iterates every owned series
        # and rewrites owned_episodes.parquet on every call (no short-circuit) — ~15s of
        # wasted work, twice per run. Memoize the built rows on the shared in-memory cache
        # (global_cache.memory): run-scoped (a fresh GlobalCacheManager per run) so the
        # parquet still rebuilds once per run on the FIRST caller, while every sibling
        # builder reuses the result. NOT persisted to disk -> no cross-run staleness.
        # Callers only read owned_eps (iterate / comprehension / len), so sharing the list
        # by reference is safe.
        _MEMO = "plex/_run/owned_episode_rows"
        mem = getattr(self.global_cache, "memory", None) if self.global_cache else None
        if mem is not None and mem.exists(_MEMO):
            return mem.get(_MEMO) or []
        rows = self._build_owned_episodes()
        if mem is not None:
            mem.set(_MEMO, rows)
        return rows

    def _build_owned_episodes(self) -> list:
        sonarr = self.registry.get("manager", "SonarrManager") if self.registry else None
        sonarr_cache = getattr(sonarr, "sonarr_cache", None)
        if sonarr_cache is None:
            return []
        try:
            from scripts.managers.services.sonarr.cache.owned_episodes import (
                SonarrCacheOwnedEpisodesManager,
            )
            mgr = SonarrCacheOwnedEpisodesManager(
                logger=self.logger, config=self.config, global_cache=self.global_cache,
                registry=self.registry, sonarr_cache=sonarr_cache, dry_run=self.dry_run)
            rows: list = []
            for inst in self._sonarr_instances():
                rows.extend(mgr.build_or_refresh(inst).to_dict("records"))
            return rows
        except Exception as e:
            self.logger.log_warning(f"[Playlists] owned-episode load failed: {e}")
            return []

    def _series_scores_and_genres(self):
        """(series_id→watchability_score, series_id→[genres]) read from the existing
        episode_files parquet; READ-only, never mutates the JIT/space artifact. Genres
        power the per-user affinity tilt."""
        scores: dict = {}
        genres: dict = {}
        if not (self.global_cache and getattr(self.global_cache, "key_builder", None)):
            return scores, genres
        import pandas as pd
        for inst in self._sonarr_instances():
            path = self.global_cache.key_builder.base_dir / "sonarr" / inst / "episode_files.parquet"
            try:
                if not path.exists():
                    continue
                df = pd.read_parquet(path, columns=["series_id", "watchability_score", "genres"])
            except Exception:
                try:                                    # older parquet without a genres column
                    df = pd.read_parquet(path, columns=["series_id", "watchability_score"])
                    df["genres"] = None
                except Exception:
                    continue
            for sid, grp in df.groupby("series_id"):
                vals = grp["watchability_score"].dropna()
                if len(vals):
                    scores[sid] = float(vals.iloc[0])
                for g in grp["genres"]:
                    gl = self._as_genre_list(g)
                    if gl:
                        genres[sid] = gl
                        break
        return scores, genres

    @staticmethod
    def _as_genre_list(g) -> list:
        """Normalize a parquet genres cell → clean list of genre strings.

        The cell may be a real list/numpy array, a JSON-encoded array STRING
        (``'["Animation", "Family"]'`` — how the Sonarr episode cache serializes it),
        or a plain comma string. CRITICAL: a naive ``split(",")`` on the JSON form
        leaves literal ``[`` ``]`` ``"`` stuck to each token, so NOTHING matches the
        per-user affinity vocab → the genre tilt silently degrades to a uniform floor
        scaling and every profile gets the same (household) order. Parse JSON first."""
        if g is None:
            return []
        if isinstance(g, str):
            s = g.strip()
            if s.startswith("["):
                try:
                    import json
                    return [str(x).strip() for x in json.loads(s) if str(x).strip()]
                except (ValueError, TypeError):
                    pass
            return [t for t in (x.strip().strip('[]"\'') for x in s.split(",")) if t]
        try:
            return [str(x).strip() for x in g if str(x).strip()]
        except TypeError:
            return []

    def _user_affinity(self, tautulli_username) -> dict:
        """A user's genre→weight affinity from Tautulli (tautulli/users/<safe>/affinity).
        {} when the profile is unmatched / has no history → no tilt (household order)."""
        if not tautulli_username or not self.global_cache:
            return {}
        import re
        safe = re.sub(r'[\\/:*?"<>|]', '_', str(tautulli_username)).strip()
        try:
            aff = self.global_cache.get(f"tautulli/users/{safe}/affinity")
        except Exception:
            return {}
        return ((aff.get("genres") if isinstance(aff, dict) else {}) or {})

    _DEFAULT_TILT = 90.0   # strong personalization; legacy alias for affinity_weight=0.9

    def _personal_tilt(self) -> float:
        """Legacy alias for the affinity weight as a 0-100 tilt (affinity_weight = tilt/100).
        plex.playlists.personal_tilt, default 90 (strong personalization). Prefer the explicit
        plex.playlists.affinity_weight knob — this is kept for back-compat. The precedence
        affinity > JIT > household no longer depends on this value (see _priority_weights)."""
        return _finite_float(self._pl_cfg().get("personal_tilt", self._DEFAULT_TILT),
                             self._DEFAULT_TILT)

    def _series_certs(self) -> dict:
        """series_id → certification (content rating) from the Sonarr series cache —
        the parental-controls age gate matches on it. {} when the cache is unavailable."""
        sonarr = self.registry.get("manager", "SonarrManager") if self.registry else None
        series_mgr = getattr(getattr(sonarr, "sonarr_cache", None), "series", None)
        if series_mgr is None:
            return {}
        out: dict = {}
        for inst in self._sonarr_instances():
            try:
                for s in series_mgr.iter_all_series(inst):
                    if isinstance(s, dict) and "id" in s and s.get("certification"):
                        out[s["id"]] = s.get("certification")
            except Exception:
                continue
        return out

    def _series_csm_ages(self) -> dict:
        """series_id → Common Sense age (int) — the cert-gate FALLBACK for series with no
        Sonarr certification, joined from the MDBList TV age cache (keyed by show tmdbId).
        Returns {} (no fallback, identical to the old behaviour) when the TV age cache is
        empty/unavailable — so this stays inert until the enrich daemon fills it."""
        try:
            from scripts.managers.services.mdblist import age_cache
            ages: dict = {}
            for k, v in (age_cache.load(age_cache.TV_AGE_CACHE_PATH) or {}).items():
                if isinstance(v, int):
                    try:
                        ages[int(k)] = v
                    except (TypeError, ValueError):
                        continue
        except Exception:
            return {}
        if not ages:
            return {}
        sonarr = self.registry.get("manager", "SonarrManager") if self.registry else None
        series_mgr = getattr(getattr(sonarr, "sonarr_cache", None), "series", None)
        if series_mgr is None:
            return {}
        out: dict = {}
        for inst in self._sonarr_instances():
            try:
                for s in series_mgr.iter_all_series(inst):
                    if not (isinstance(s, dict) and "id" in s):
                        continue
                    try:
                        age = ages.get(int(s.get("tmdbId")))
                    except (TypeError, ValueError):
                        continue
                    if age is not None:
                        out[s["id"]] = age
            except Exception:
                continue
        return out

    def _profile_ages(self) -> dict:
        """Operator overrides for a profile's age tier (little_kid / older_kid / teen /
        adult), keyed by profile title or safe_user — config plex.playlists.profile_ages.
        Wins over the auto-detected Plex restriction profile; useful when Plex doesn't
        expose the tier or the operator wants to override it."""
        pa = self._pl_cfg().get("profile_ages")
        return pa if isinstance(pa, dict) else {}

    def _watched_for(self, user_id) -> set:
        if user_id is None or not self.registry:
            return set()
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return set()
        try:
            return watched_episode_keys(hm.get_all_history_cached(user_id))
        except Exception:
            return set()

    def _watched_episode_recency_for(self, user_id) -> dict:
        """{episode-identity: latest unix watch ts} for this user — tv_inputs aggregates it per
        series into series_recency (The Long Glide's TV recency key). {} on any miss; the same
        24h-cached history fetch _watched_for uses (cache hit)."""
        if user_id is None or not self.registry:
            return {}
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return {}
        try:
            return watched_episode_recency(hm.get_all_history_cached(user_id))
        except Exception:
            return {}

    def _daemon_enabled(self) -> bool:
        d = ((self.config.get("daemons", {}) if self.config else {}) or {}).get("enrich", {}) or {}
        return bool(d.get("enabled"))

    def _daemon_running(self) -> bool:
        try:
            from scripts.managers.factories.daemons.supervisor import DaemonSupervisor
            return bool(DaemonSupervisor(logger=self.logger).is_running())
        except Exception:
            return False

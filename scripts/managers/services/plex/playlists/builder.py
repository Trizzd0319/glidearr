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
from datetime import date, datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.mdblist import client as mdblist_client
from scripts.managers.machine_learning.playlists.cert_gate import (
    ADULT,
    cert_allowed,
    cert_summary,
    is_restricted,
    tier_ceiling,
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
    trakt_episode_identities,
    merge_household_history,
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
# Per-user universe/franchise progress. A SEPARATE tracked artifact - read-only, feeds
# nothing in the pipeline, shaped for direct serialisation by a future web layer.
_SAGA_PROGRESS_KEY = "plex/playlists/saga_progress"
# Owned-movie inventory (tmdb -> {rating_key, title, year}); the same key movie_builder
# reads. Inverted here to resolve a Tautulli play's ratingKey back to a tmdb.
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"
# Durable Plex ratingKey -> native id. APPEND-ONLY: a key Plex retires on a re-scan keeps
# resolving, which is the only way a play recorded before that re-scan stays attributable.
_RK_CROSSWALK_KEY = "tautulli/rating_key_crosswalk"
# Per-profile record of which surfaced groups were watched vs walked past WHILE ACTIVE.
# Day-based, not run-based: see playlists.engagement.
_ENGAGEMENT_KEY = "plex/playlists/engagement"          # + /{safe_user}
_KOMETA_FRANCHISE_KEY = "plex/playlists/kometa_franchises"   # franchises LEARNED from the live Kometa collections
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

# ── run-scoped EXTERNAL-FETCH memo (shared ACROSS builder instances) ─────────────────
# The TV, MOVIE and COMBINED builders are three separate INSTANCES of this base handed the
# SAME ``global_cache``. Each of them independently walks the operator's Plex collections
# (one section listing + one children read per collection) and, when a universe list is
# stale, hits mdblist — so the same EXTERNAL reads happen 2-4x per run for zero new
# information (Plex/mdblist state does not change mid-run).
#
# WHERE the memo lives: ``global_cache.memory`` — the shared ``MemoryManager`` created in
# ``GlobalCacheManager.__init__``. LIFETIME: exactly one run (one GlobalCacheManager per
# process), in memory only, never persisted → no cross-run staleness, and a fresh run
# always refetches. With no global_cache (or no ``.memory``) every fetch runs live, i.e.
# byte-identical to the pre-memo behaviour. Mirrors the ``_load_owned_episodes`` memo.
#
# WHAT is memoized: ONLY the external fetch (the raw Plex/mdblist payload). Every LOCAL
# computation layered on top — the owned/watchlist filtering, the belongs-to-this-universe
# guard, the franchise maps, the Kometa learning/persist — stays per-call and unmemoized,
# so each builder still computes against ITS OWN current state and its output is unchanged
# (e.g. the TV pass' prefer_plex=True map and the combined pass' prefer_plex=False map keep
# differing exactly as they do today).
_RUN_MEMO = "plex/_run/universe"
_FETCH_STATS_KEY = f"{_RUN_MEMO}/fetch_stats"


def run_fetch_stats(global_cache) -> dict:
    """The run's external-fetch counters (``{stat: n}``) from the shared in-memory memo, or
    ``{}`` when there is no memo store / nothing was fetched. See :func:`log_run_fetch_stats`."""
    mem = getattr(global_cache, "memory", None) if global_cache else None
    if mem is None:
        return {}
    stats = mem.get(_FETCH_STATS_KEY)
    return dict(stats) if isinstance(stats, dict) else {}


def log_run_fetch_stats(logger, global_cache) -> None:
    """ONE line at the end of the Plex reconcile phase reporting how many EXTERNAL universe
    fetches the playlist builders made this run vs how many repeat calls the run-scoped memo
    served — so the de-duplication is visible in the run log (before: the counts equal the
    call counts; after: the fetch counts collapse and the hit counts carry the remainder).
    Silent (no line) when nothing was fetched."""
    stats = run_fetch_stats(global_cache)
    if not stats:
        return
    logger.log_info(
        "[UniverseOrder] external fetches this run — "
        f"Plex section/collection listings {stats.get('collection_list_fetches', 0)} "
        f"(+{stats.get('collection_list_hits', 0)} memo hit(s)), "
        f"Plex collection children {stats.get('collection_children_fetches', 0)} "
        f"(+{stats.get('collection_children_hits', 0)} memo hit(s)), "
        f"mdblist list refreshes {stats.get('mdblist_fetches', 0)} "
        f"(+{stats.get('mdblist_hits', 0)} memo hit(s)).")


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
        # Episode-level watch recency per user. Without this tv_inputs sees wrec={} and every
        # series_recency entry carries ts=0, so the resume ordering has nothing to sort on.
        resume_on, resume_order, resume_weight = self._resume_cfg()
        recency_by_user = ({u["safe_user"]: self._watched_episode_recency_for(u.get("tautulli_user_id"))
                            for u in tracked} if resume_on else {})
        # TV-only playlist: let a Kometa user's custom SHOW-collection order lead (prefer_plex).
        franchise_by_series, series_timeline = self._tv_franchise_maps(owned_eps, prefer_plex=True)
        return self._build_for_users(
            tracked, owned_eps, inventory, resolution_stats, series_scores,
            watched_by_user, series_genres=series_genres, affinity_by_user=affinity_by_user,
            series_certs=series_certs, series_csm_ages=self._series_csm_ages(),
            daemon_enabled=self._daemon_enabled(), daemon_running=self._daemon_running(),
            franchise_by_series=franchise_by_series, series_timeline=series_timeline,
            resume_boost=resume_on, resume_order=resume_order,
            resume_weight=resume_weight, recency_by_user=recency_by_user)

    def _build_for_users(self, tracked, owned_eps, inventory, resolution_stats,
                         series_scores, watched_by_user, *, series_genres=None,
                         affinity_by_user=None, series_certs=None, series_csm_ages=None,
                         daemon_enabled, daemon_running,
                         franchise_by_series=None, series_timeline=None,
                         resume_boost=False, resume_order="recency",
                         resume_weight=0.0, recency_by_user=None) -> dict:
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
        cert_by_rk = self._tv_cert_by_rk(owned_eps, inventory, series_certs)
        # HOUSEHOLD layer, fetched ONCE. Trakt is a master-profile feed with no per-user
        # attribution, so it is applied to every Home profile; age-gating still happens
        # above via cert_allowed on user_owned, so an out-of-tier show contributes no
        # candidates regardless of its household position.
        hh_watched, hh_recency, hh_series = self._trakt_household_episodes() if resume_boost else (set(), {}, {})
        self._begin_summary()
        built = 0
        for idx, u in enumerate(tracked, 1):
            watched = watched_by_user.get(u["safe_user"], set())
            user_recency = (recency_by_user or {}).get(u["safe_user"], {})
            # Engagement is measured against the plan SURFACED last run, so read it before
            # this run overwrites it below.
            _prior_plan = self._cache_get(f"{_PLAN_KEY}/{u['safe_user']}", None)
            _saga_b = self._saga_boost_for(u["safe_user"], watched, _prior_plan)
            if hh_series:
                watched, user_recency, _hh = merge_household_history(
                    watched, user_recency, hh_watched, hh_recency, hh_series)
                self.logger.log_debug(
                    f"[Playlists] {u.get('safe_user')}: household Trakt merge - "
                    f"{_hh['trakt_series']} series in feed, {_hh['held_series']} held at the "
                    f"user's own newer position, +{_hh['added']} episode identities.")
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
            recency_on, recency_window = self._recency_cfg()
            plan, stats = build_tv_plan(
                user_owned, inventory, watched, user_scores, family="up_next",
                episode_cap=self._episode_cap(), max_items=self._max_items(),
                franchise_by_series=franchise_by_series, series_timeline=series_timeline,
                recency_boost=recency_on, window_days=recency_window,
                watch_recency=user_recency,
                resume_boost=resume_boost, resume_order=resume_order,
                resume_weight=resume_weight, saga_boost=_saga_b)
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{u['safe_user']}", self._serialize(plan))
            reasons = self._tv_reasons(user_owned, inventory, series_genres, user_aff, user_jit)
            self._log_preview(u, plan, stats, display, reasons, label="episode",
                              anon=anon_label(u.get("title"), tier_name, idx),
                              certs=cert_by_rk, level=level)
            built += 1
        self._emit_summary_grid("[dry-run] TV playlists - per-profile summary")
        self.logger.log_info(f"[Playlists] built {built} per-user TV plan(s) (dry-run — no Plex writes).")
        # READ-ONLY projection. Wrapped so a progress failure can never cost the run the
        # playlists it just built.
        try:
            self._emit_saga_progress(tracked, watched_by_user, recency_by_user)
        except Exception as e:
            # WARNING, not debug: the first wiring of this failed on a relative cache path
            # and the whole feature vanished from the log with nothing to point at.
            self.logger.log_warning(f"[SagaProgress] skipped: {type(e).__name__}: {e}")
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
                     anon: str | None = None, certs=None, level: int = ADULT):
        """Record ONE summary row for this (playlist family x profile) and MIRROR the full
        per-item preview (``# | Title | [Kind] | Rank | Why``) into support/logs/playlists.log.

        Deliberately emits NO grid to the main run log: a household of N profiles times the
        playlist families each builder makes used to flood the shell with N x families 25-row
        grids saying little the operator could act on. The per-item detail is unchanged in the
        playlists.log mirror (the operator drill-down); the run log instead gets ONE compact
        table per builder from :meth:`_emit_summary_grid`.

        ``Rank`` = the per-user priority_score the block is ordered on (affinity > JIT >
        household; 2dp so it stays discriminating). ``Why`` = the human rationale (from the
        ``reasons`` map keyed by ratingKey — genres/JIT/cast/crew/franchise — falling back
        to the brain's group reason). ``Kind`` (TV/Movie) only shows for the combined plan
        (``kinds`` given). ``label`` makes the header medium-correct (episode/movie/item).
        ``certs`` (ratingKey -> certification) + ``level`` (the profile's resolved age tier)
        are the age-gate evidence for the summary row."""
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
        self._add_summary_row(family_label=family_label, who=anon or title, plan=plan,
                              certs=certs, level=level)
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

    # ── per-builder run-log SUMMARY (replaces the per-playlist preview grids) ────
    # ONE table per LIBRARY/MEDIUM (TV builder, MOVIE builder, COMBINED builder), emitted
    # after that builder's per-user loop and only when it actually produced plans. Each row
    # is one (playlist family x profile) and carries the CERTIFICATION EVIDENCE needed to
    # validate the parental-controls gating at a glance — the ceiling the profile permits
    # next to the strictest certification the generated plan actually contains.
    _SUMMARY_COLS = ("Playlist", "Profile", "Items", "Allowed", "Strictest", "Unrated", "Cert")
    _SUMMARY_CAPTION = (
        "One row per playlist x profile. Allowed = the certification ceiling this profile's "
        "parental-controls tier permits; Strictest = the most mature certification actually "
        "present in the generated plan; Unrated = items carrying no recognised certification "
        "(admitted via the Common Sense age fallback - never a violation, but the leak path "
        "worth watching); Cert = OK, or VIOLATION when the plan holds something the age gate "
        "should have rejected. Per-item previews are mirrored to support/logs/playlists.log.")

    def _begin_summary(self) -> None:
        """Start a fresh accumulation for THIS builder's summary table (call before the
        per-user loop, so a re-run of the same manager never doubles its rows)."""
        self._summary_rows: list = []

    def _add_summary_row(self, *, family_label, who, plan, certs=None, level: int = ADULT) -> None:
        """Accumulate ONE (playlist family x profile) row. ``certs`` maps a plan item's
        ratingKey to its certification; a missing/unrecognised entry lands in the unrated
        bucket (never a violation — see :func:`cert_summary`). ``certs=None`` means the
        medium can't reach certification here: the cert columns render as ``-`` rather than
        claiming a clean bill of health we didn't actually check."""
        rows = getattr(self, "_summary_rows", None)
        if rows is None:
            rows = self._summary_rows = []
        if certs is None:
            ev = {"ceiling": tier_ceiling(level), "strictest": "-", "unknown": "-",
                  "violations": 0}
            flag = "-"
        else:
            item_certs = [certs.get(i.rating_key, certs.get(str(i.rating_key)))
                          for i in plan.items]
            ev = cert_summary(item_certs, level)
            flag = "OK" if not ev["violations"] else f"VIOLATION x{ev['violations']}"
        rows.append([str(family_label), str(who), str(len(plan.items)),
                     ev["ceiling"], ev["strictest"], str(ev["unknown"]), flag])

    def _emit_summary_grid(self, title: str) -> None:
        """Emit this builder's ONE summary table. A no-op when the builder produced no plans
        (nothing accumulated), so a household with only one enabled medium sees only that
        medium's table. Falls back to one info line per row for a logger with no ``log_grid``."""
        rows = getattr(self, "_summary_rows", None)
        if not rows:
            return
        cols = list(self._SUMMARY_COLS)
        grid = getattr(self.logger, "log_grid", None)
        if not callable(grid):                    # a logger with no table support at all
            for r in rows:
                self.logger.log_info(f"[Playlists] {title} | " + " | ".join(r))
            return
        # ``caption`` is probed rather than assumed so a minimal logger stub predating it still
        # gets the table (probing beats try/except TypeError, which could re-emit on a genuine
        # TypeError raised INSIDE the logger).
        try:
            import inspect
            has_caption = "caption" in inspect.signature(grid).parameters
        except (TypeError, ValueError):
            has_caption = False
        if has_caption:
            grid(cols, rows, title=title, cap=44, caption=self._SUMMARY_CAPTION)
        else:
            grid(cols, rows, title=title, cap=44)

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
    def _tv_cert_by_rk(owned_eps, inventory, series_certs) -> dict:
        """``{ratingKey: certification}`` for the TV summary's cert evidence — the SAME
        ``series_certs`` map the age gate filtered on, re-keyed from series_id to the plan's
        ratingKeys through the owned-episode join (mirrors :meth:`_tv_reasons`). An episode
        of a series with no Sonarr certification maps to ``None`` → the unrated bucket."""
        out: dict = {}
        for ep in owned_eps or []:
            jk = ep.get("tvdb_join_key")
            match = (inventory or {}).get(jk) if jk else None
            rk = str(match["rating_key"]) if (match and match.get("rating_key")) else None
            if rk is None or rk in out:
                continue
            out[rk] = (series_certs or {}).get(ep.get("series_id"))
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
        was last cached (so the feature survives a key being removed).

        Called many times per run by several builders. Only its EXTERNAL leg — the mdblist HTTP
        fetch — is run-memoized (:meth:`_fetch_universe_list`); the cache read + merge here is
        deliberately NOT, because :meth:`_refresh_synthetic_universes` REWRITES this same cache
        key mid-run and every later reader must see that write."""
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
                res = self._fetch_universe_list(key, uk, defn)
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

    def _fetch_universe_list(self, api_key: str, universe_key: str, defn: dict) -> dict:
        """ONE mdblist list fetch (EXTERNAL HTTP), memoized for the run — the only I/O inside
        :meth:`_universe_source`. Keyed by the universe key + the list REF fields ``list_items``
        actually dispatches on (imdb / id / mdblist), so a re-pointed list is a different fetch.
        mdblist state doesn't change mid-run, so a sibling builder that reaches the same STALE
        universe reuses the response instead of re-hitting the API; the response is only ever
        read (``split_list_media``), never mutated. Everything else in ``_universe_source``
        (TTL staleness, the timeline re-stamp, the chronolist overlay, the cache write) is LOCAL
        and stays per-call — it must see the ``tvfran:`` entries a sibling wrote this run."""
        ref = f"{defn.get('imdb') or ''}|{defn.get('id') if defn.get('id') is not None else ''}|{defn.get('mdblist') or ''}"
        return self._memo_fetch(f"{_RUN_MEMO}/mdblist/{universe_key}/{ref}",
                                lambda: mdblist_client.list_items(api_key, defn),
                                stat="mdblist_fetches", hit_stat="mdblist_hits")

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

    # ── run-scoped external-fetch memo (see the _RUN_MEMO block at module top) ──────
    def _run_memo(self):
        """The SHARED in-run memo store (``global_cache.memory``) — ``None`` when there is no
        global_cache / no memory manager, in which case every fetch below runs live exactly as
        it did before the memo existed."""
        return getattr(self.global_cache, "memory", None) if self.global_cache else None

    def _bump_fetch_stat(self, name: str) -> None:
        """+1 on a run-scoped external-fetch counter (rendered by :func:`log_run_fetch_stats`)."""
        mem = self._run_memo()
        if mem is None:
            return
        stats = mem.get(_FETCH_STATS_KEY)
        stats = dict(stats) if isinstance(stats, dict) else {}
        stats[name] = stats.get(name, 0) + 1
        mem.set(_FETCH_STATS_KEY, stats)

    def _memo_fetch(self, key: str, fetch, *, stat: str, hit_stat: str):
        """Run-scoped memoization of ONE external fetch. ``fetch()`` is called at most once per
        ``key`` per run and its result is shared (by reference) with every sibling builder; a
        raising ``fetch`` is NOT memoized (it propagates to the caller's existing guard and the
        next caller retries) — identical to today's per-call failure handling."""
        mem = self._run_memo()
        if mem is None:
            return fetch()
        if mem.exists(key):
            self._bump_fetch_stat(hit_stat)
            return mem.get(key)
        val = fetch()
        mem.set(key, val)
        self._bump_fetch_stat(stat)
        return val

    def _all_collections(self) -> list:
        """Every Plex collection across ALL library sections, as a flat list of metadata dicts.
        ``[]`` with no Plex API / on error.

        EXTERNAL I/O, memoized for the run: this listing is pure fetch (no local state feeds it)
        and Plex's collections don't change mid-run, so the TV/MOVIE/COMBINED builders share ONE
        walk instead of four. Callers only READ the returned list (iterate / ``sorted``), so
        sharing it by reference is safe."""
        if not self.plex_api:
            return []
        return self._memo_fetch(f"{_RUN_MEMO}/collections", self._fetch_all_collections,
                                stat="collection_list_fetches", hit_stat="collection_list_hits")

    def _fetch_all_collections(self) -> list:
        """The live listing behind :meth:`_all_collections`. Collections are PER-SECTION on PMS —
        the global ``/library/collections`` endpoint returns nothing on modern servers — so iterate
        ``get_sections()`` and read each section's collections."""
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

    def _collection_children(self, rating_key, *, include_guids: bool = False) -> list:
        """The member items of ONE Plex collection — the single EXTERNAL fetch behind both
        collection readers, memoized for the run.

        Keyed by ``(ratingKey, include_guids)`` so each reader still gets exactly the payload
        shape it asked for (the SHOW reader needs the external ``Guid[]`` array, the movie reader
        doesn't request it). Raises through to the caller's ``except``/``continue`` on failure,
        and nothing is memoized in that case."""
        def _fetch():
            if include_guids:
                return metadata_items(
                    self.plex_api.get_collection_children(rating_key, include_guids=True))
            return metadata_items(self.plex_api.get_collection_children(rating_key))
        return self._memo_fetch(
            f"{_RUN_MEMO}/children/{rating_key}/{1 if include_guids else 0}", _fetch,
            stat="collection_children_fetches", hit_stat="collection_children_hits")

    def _plex_collection_order(self, movie_inventory, owned_movies=None) -> dict:
        """``{tmdb_id: position}`` from the operator's Kometa UNIVERSE Plex collections (read IN
        COLLECTION ORDER). A child film earns a saga index only if it belongs to THIS universe — proven
        by its Radarr ``universe_name`` tag OR (tag-free) by membership in the universe's canonical
        list/bake — so a Kometa user with ZERO universe tags still gets ordering, while a film mis-filed
        in the wrong Plex collection is excluded (it's in another universe's list). ``{}`` with no Plex
        API / no movie inventory. Secondary to the list source — honours a custom Plex curation.

        SIDE-EFFECT (movie twin of the show learning in :meth:`_plex_tv_collection_order`): each
        recognised universe collection's OWNED membership (tmdb ids, UNFILTERED by the order guard —
        Kometa's curation is the teacher) is learned and persisted via
        :meth:`_persist_kometa_movie_franchises`, so the tagless universe resolver
        (quality/universe_membership.gather_derived_maps) gains its franchise-map source for MOVIES.

        I/O vs LOCAL: the only external reads are :meth:`_all_collections` and
        :meth:`_collection_children`, both run-memoized and shared with the sibling builders.
        Everything below them (the rk→tmdb join, the Radarr-tag/list membership guard, the order
        merge, the learning + persist) is recomputed per call against THIS builder's owned set,
        so two builders with different owned movies still get different orders."""
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
        learn_rows: list = []                              # (norm_title, key, display, [(tmdb, title)…])
        for d in cols:
            rk = d.get("ratingKey")
            key = collection_universe_key(d.get("title")) if rk is not None else None
            if key is None:
                continue
            try:
                # get_collections is library-wide: TV-library universe collections (e.g. Arrowverse)
                # also match, but their SHOW ratingKeys aren't in rk_to_tmdb so they drop to {}.
                # Run-memoized fetch (the children payload); everything below it is LOCAL and
                # recomputed per call against THIS builder's owned set.
                kids = self._collection_children(rk)
            except Exception:
                continue
            child_rks = [str(c.get("ratingKey")) for c in kids if c.get("ratingKey") is not None]
            # Learn the collection's owned MOVIE membership (tmdb-resolvable children) for the
            # Kometa-independent persistence below. Deliberately NOT filtered by the ``allowed``
            # order guard: learning trusts the operator's curation (same as the show learning),
            # which is exactly what covers a standalone universe member no TMDB collection or
            # mdblist list knows. Derived membership is never protective, so the worst case of a
            # mis-filed film is a wrong bare-universe grouping label.
            mem = [(t, c.get("title")) for c in kids
                   if c.get("ratingKey") is not None
                   and (t := rk_to_tmdb.get(str(c.get("ratingKey")))) is not None]
            if mem:
                learn_rows.append((_collection_norm(d.get("title")), key, d.get("title"), mem))
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
        # Persist the learned movie franchises (first-wins dedup across collections in stable
        # title order — a film lands in ONE franchise, mirroring the show learning). Only when
        # we actually read some — never wipe.
        if learn_rows:
            learned_movies: dict = {}
            seen_tmdb: set = set()
            for _norm, key, display, mem in sorted(learn_rows, key=lambda r: r[0] or ""):
                ent = learned_movies.setdefault(
                    key, {"display": display, "movies": [], "movie_titles": [], "source": "kometa-plex"})
                for tmdb, name in mem:
                    if tmdb not in seen_tmdb:
                        seen_tmdb.add(tmdb)
                        ent["movies"].append(tmdb)
                        ent["movie_titles"].append(name or "")
            self._persist_kometa_movie_franchises(learned_movies)
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
        source — present only to honour a custom Plex curation a Kometa user may have.

        I/O vs LOCAL: the only external reads are :meth:`_all_collections` and
        :meth:`_collection_children` (run-memoized, shared with the sibling builders). The
        franchise-title index, the noise/Kometa gating, the tvdb→series_id join, the ordering and
        the learning + persist all stay per-call, computed against THIS builder's ``tvdb_to_sid``."""
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
        learned: dict = {}                                     # {key: {display, shows[tvdb], titles}} to PERSIST
        seen_tvdb: set = set()                                 # first-wins tiebreak: a show lands in ONE franchise
        for d in sorted(cols, key=lambda c: _collection_norm(c.get("title"))):   # stable order → stable tiebreak
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
                kids = self._collection_children(rk, include_guids=True)   # run-memoized fetch
            except Exception:
                continue
            ordered, rk_to_sid, members = [], {}, []
            for c in kids:
                crk = c.get("ratingKey")
                if crk is None:
                    continue
                crk = str(crk)
                ordered.append(crk)
                tvdb = self._tvdb_from_guids(c)
                if tvdb is None:
                    continue
                members.append((tvdb, c.get("title")))
                sid = tvdb_to_sid.get(tvdb)
                if sid is not None:
                    rk_to_sid[crk] = sid
            if not rk_to_sid or (tentative and len(set(rk_to_sid.values())) < 2):
                continue                                       # no owned shows, or a single-show "franchise"
            fran, tmap = series_order_from_children(ordered, rk_to_sid, key, with_timeline=True)
            if fran:
                fran_all.update(fran)
                time_all.update(tmap)
                matched += 1
            # Learn the franchise's full membership for Kometa-independent grouping (first-wins dedup).
            ent = learned.setdefault(key, {"display": title, "shows": [], "titles": [], "source": "kometa-plex"})
            for tvdb, name in members:
                if tvdb not in seen_tvdb:
                    seen_tvdb.add(tvdb)
                    ent["shows"].append(tvdb)
                    ent["titles"].append(name or "")
        if learned:
            self._persist_kometa_franchises(learned)           # only when we actually read some — never wipe
        if fran_all:
            self.logger.log_info(f"[UniverseOrder] {matched} Plex universe/franchise SHOW collection(s) → "
                                 f"{len(fran_all)} owned series.")
        return fran_all, time_all

    def _persist_kometa_franchises(self, learned) -> None:
        """Persist franchises LEARNED from the live Kometa collections (``{key: {shows[tvdb], titles,
        source}}``) so glidearr groups them even when Kometa is later absent — Kometa as a one-time
        teacher, not a runtime dependency. ``_tv_franchise_catalog`` reads this back as a trusted overlay.
        Best-effort; the caller only invokes it with a NON-empty catalog, so a Kometa-less run never wipes
        the learned list.

        MOVIE fields (``movies``/``movie_titles`` — written by the movie-teaching twin
        :meth:`_persist_kometa_movie_franchises` into the SAME catalog) are PRESERVED through this
        show-side write: a re-learned key keeps its movie lists, and a movie-only key (e.g. ``mcu``
        learned from a MOVIE-library collection this show pass never sees) survives as a shows-empty
        entry — so the two passes can never wipe each other's knowledge. Show semantics are otherwise
        unchanged: keys not re-learned (and carrying no movies) still drop."""
        if not self.global_cache:
            return
        try:
            prior = self._cache_get(_KOMETA_FRANCHISE_KEY, {})
            prior = prior if isinstance(prior, dict) else {}
            merged: dict = {}
            for key, ent in learned.items():
                p = prior.get(key)
                if isinstance(p, dict) and p.get("movies"):
                    ent = {**ent, "movies": p["movies"], "movie_titles": p.get("movie_titles") or []}
                merged[key] = ent
            for key, p in prior.items():                   # movie-only entries survive a show relearn
                if key not in merged and isinstance(p, dict) and p.get("movies"):
                    merged[key] = {"display": p.get("display"), "shows": [], "titles": [],
                                   "source": p.get("source") or "kometa-plex",
                                   "movies": p["movies"], "movie_titles": p.get("movie_titles") or []}
            self.global_cache.set(_KOMETA_FRANCHISE_KEY, merged)
            self.logger.log_info(f"[UniverseOrder] learned {len(learned)} franchise(s) "
                                 f"({sum(len(v['shows']) for v in learned.values())} shows) from Kometa "
                                 f"collections (persisted for Kometa-independent grouping).")
        except Exception as e:
            self.logger.log_debug(f"[UniverseOrder] could not persist learned Kometa franchises: {e}")

    def _persist_kometa_movie_franchises(self, learned) -> None:
        """Persist MOVIE franchises learned from the operator's Plex universe collections
        (``{key: {display, movies[tmdb], movie_titles, source}}``) into the SAME
        ``kometa_franchises`` catalog the show pass writes — the movie-teaching twin of
        :meth:`_persist_kometa_franchises`. The tagless universe resolver
        (quality/universe_membership.gather_derived_maps) reads each entry's optional
        ``movies`` list, so this write is what activates the franchise-map source for
        MOVIES (precedence: tags > TMDB collection > these maps > mdblist; derived
        membership is ALWAYS bare-universe — never the keep-universe pin).

        MERGE semantics (mirror of the show side): show fields on existing entries are
        preserved verbatim; movie fields are REPLACED wholesale by this learn (a deleted
        Plex collection stops teaching on the next learn), and entries not in this learn
        lose only their movie fields. Kometa-independent + best-effort; the caller only
        invokes it with a NON-empty learn, so a collection-less run never wipes the
        learned movie lists."""
        if not self.global_cache:
            return
        try:
            prior = self._cache_get(_KOMETA_FRANCHISE_KEY, {})
            prior = prior if isinstance(prior, dict) else {}
            merged: dict = {}
            for key, ent in prior.items():                 # keep show knowledge; strip old movie fields
                if not isinstance(ent, dict):
                    continue
                kept = {k: v for k, v in ent.items() if k not in ("movies", "movie_titles")}
                if kept.get("shows") or key in learned:    # neither shows nor a fresh movie learn → drop
                    merged[key] = kept
            for key, ent in learned.items():
                base = merged.setdefault(
                    key, {"display": ent.get("display"), "shows": [], "titles": [],
                          "source": "kometa-plex"})
                base.setdefault("shows", [])
                base.setdefault("titles", [])
                base["movies"] = list(ent.get("movies") or [])
                base["movie_titles"] = list(ent.get("movie_titles") or [])
            self.global_cache.set(_KOMETA_FRANCHISE_KEY, merged)
            self.logger.log_info(
                f"[UniverseOrder] learned {len(learned)} movie franchise(s) "
                f"({sum(len(v.get('movies') or []) for v in learned.values())} owned movies) from Plex "
                f"universe collections (persisted for Kometa-independent tagless membership).")
        except Exception as e:
            self.logger.log_debug(f"[UniverseOrder] could not persist learned movie franchises: {e}")

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
        the generated catalog (if present), the franchises LEARNED + persisted from the operator's live
        Kometa collections, and the ``plex.playlists.tv_franchises`` config overlay (each later source
        overlays the earlier; the learned Kometa families let grouping survive a Kometa-less run). Shape is
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
        learned = self._cache_get(_KOMETA_FRANCHISE_KEY, {})   # franchises LEARNED from the operator's Kometa
        if isinstance(learned, dict) and learned:              # collections — trusted (their own curation) and
            # Re-apply the noise filter on READ, not just on learn. The cache is MERGED with
            # prior state every run, so an entry captured before the filter existed would
            # otherwise persist forever - and one did: "720p Movies" was learned as a tier-0
            # franchise of 1368 unrelated shows, fusing most of the library into one saga.
            # Filtering here makes a stale cache self-heal instead of needing hand surgery.
            _clean, _dropped = {}, []
            for _k, _v in learned.items():
                _title = (_v or {}).get("display") if isinstance(_v, dict) else None
                if is_collection_noise(_title or _k):
                    _dropped.append(_title or _k)
                    continue
                _clean[_k] = _v
            if _dropped:
                self.logger.log_info(
                    f"[UniverseOrder] dropped {len(_dropped)} learned collection(s) that describe "
                    f"a library facet, not a franchise: {', '.join(sorted(_dropped)[:4])}"
                    f"{' …' if len(_dropped) > 4 else ''}")
            learned = _clean
        if isinstance(learned, dict) and learned:
            catalog.update(learned)                            # PERSISTED, so grouping survives a Kometa-less run
            curated_keys.update(learned.keys())
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

    def _recency_cfg(self):
        """(enabled, window_days) from plex.playlists.recency_boost — lift a group the viewer is
        CAUGHT UP on the instant its freshest member lands within window_days (e.g. a show you've
        finished whose new episode just aired). order_items gives this precedence over resume_boost.
        OFF (default) → byte-identical. Shared by the TV, movie, and combined Up Next builders."""
        rc = self._pl_cfg().get("recency_boost", {}) or {}
        try:
            window = int(rc.get("window_days", 30))
        except (TypeError, ValueError):
            window = 30
        return bool(rc.get("enabled", False)), max(0, window)

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

    def _trakt_household_episodes(self):
        """``(identities, recency, series_latest)`` from the household Trakt episode feed.

        Fetched ONCE per run and shared across profiles - Trakt has no per-user attribution,
        so there is nothing to fetch per user. Returns empty structures on any miss, which
        makes the merge downstream a no-op rather than a failure: a Trakt outage must not
        cost a profile the Tautulli history it already had.

        Raw rows deliberately, not ``history_dataframe``: the identities have to be built
        with the SAME ``_norm`` / ``_coerce_int`` the Tautulli path uses, or a Trakt identity
        and a Tautulli identity for one episode would differ and silently double-count
        instead of merging.
        """
        try:
            hm = self.registry.get("manager", "TraktHistoryManager") if self.registry else None
            if hm is None or not hasattr(hm, "get_full_watch_history_cached"):
                return set(), {}, {}
            # CACHED form. This called the uncached get_full_watch_history(), which
            # re-paginates the whole ~1,900-row episode history live -- so the household
            # feed cost a full Trakt sweep here on top of the one TraktManager.run() had
            # already made. Same data, served from trakt/history/episodes (24h).
            #
            # The `or []` is deliberate and stays: the cached call returns None when the
            # fetch failed AND no last-good copy exists, and this method's contract is to
            # degrade to a no-op merge rather than fail -- "a Trakt outage must not cost a
            # profile the Tautulli history it already had". Empty here means "no household
            # layer this run", not "nobody watched anything".
            ident, rec, ser = trakt_episode_identities(hm.get_full_watch_history_cached() or [])
            if ser:
                self.logger.log_info(
                    f"[Playlists] household Trakt episode feed: {len(ser)} series, "
                    f"{len(ident)} episode identities - applied to every profile, "
                    f"per-series precedence to each user's own newer position.")
            return ident, rec, ser
        except Exception as e:
            self.logger.log_debug(f"[Playlists] household Trakt feed unavailable: {e}")
            return set(), {}, {}

    # ── Saga progress (read-only projection) ──────────────────────────────────

    def _series_stats_for_progress(self) -> tuple:
        """``({tvdb: {title, episode_count, runtime_minutes}}, {norm_title: tvdb})``.

        Read straight off the Sonarr series LIBRARY cache shards, which carry
        ``statistics.episodeCount`` for EVERY episode that exists - owned or not. That is
        the denominator saga progress needs: neither parquet has it (episode_files is
        mostly pilot stubs; owned_episodes is a partial file inventory, 9 Blue Bloods rows
        against Sonarr's 293).

        The shards are first-letter keyed and include numeric and non-ASCII names, so the
        directory is globbed rather than enumerated - verified consistent across ascii,
        numeric and non-ASCII shards (always a list, tvdbId on 100% of rows).
        """
        import glob as _glob
        import gzip as _gzip
        import os as _os
        from scripts.managers.machine_learning.likelihood.saga_progress import _norm as _pnorm

        stats: dict = {}
        by_title: dict = {}
        inst = None
        try:
            _ef = self.registry.get("manager", "SonarrCacheEpisodeFilesManager") if self.registry else None
            inst = _ef._resolve_instance(None) if _ef else None
        except Exception:
            inst = None
        if not inst:
            self.logger.log_debug("[SagaProgress] no Sonarr instance resolved; skipping.")
            return stats, by_title
        # ABSOLUTE, via the cache key_builder - the same resolution _parquet_path uses.
        # A relative "scripts/support/cache/..." only resolves when the process CWD happens
        # to be the repo root; it silently globbed nothing and the caller early-returned
        # with no log at all.
        try:
            root = self.global_cache.key_builder.base_dir / "sonarr" / str(inst) / "library"
        except Exception as e:
            self.logger.log_debug(f"[SagaProgress] cache base_dir unavailable: {e}")
            return stats, by_title
        shards = sorted(_glob.glob(_os.path.join(str(root), "*.json.gz")))
        if not shards:
            self.logger.log_debug(f"[SagaProgress] no library shards under {root}")
            return stats, by_title
        for path in shards:
            try:
                with _gzip.open(path, "rt", encoding="utf-8") as f:
                    rows = json.load(f)
            except Exception:
                continue
            for r in (rows if isinstance(rows, list) else list((rows or {}).values())):
                if not isinstance(r, dict):
                    continue
                tv = r.get("tvdbId")
                try:
                    tv = int(tv)
                except (TypeError, ValueError):
                    continue
                st = r.get("statistics") or {}
                # totalEpisodeCount, NOT episodeCount. Sonarr's ``episodeCount`` counts
                # MONITORED episodes and tracks episodeFileCount exactly - on a pilot-only
                # series it is 1. Aqua Teen Hunger Force reports episodeCount=1 against
                # totalEpisodeCount=319; Powerpuff Girls 1 against 184. With ~93% of this
                # library pilot-only, using it made the denominator 1 for nearly every
                # show, which is the opposite of "progress through everything that exists,
                # owned or not".
                stats[tv] = {"title": r.get("title"),
                             "episode_count": (st.get("totalEpisodeCount")
                                               or st.get("episodeCount")),
                             "runtime_minutes": r.get("runtime")}
                t = _pnorm(r.get("title"))
                if t:
                    by_title.setdefault(t, tv)
        self.logger.log_info(
            f"[SagaProgress] series stats: {len(stats)} series from {len(shards)} shard(s).")
        return stats, by_title

    def _movie_stats_for_progress(self) -> tuple:
        """``({tmdb: {title, runtime_minutes}}, {watched tmdb}, {(norm_title, year): tmdb})``
        across EVERY Radarr instance.

        A universe's films can live on any instance (a 4K copy on ultra, the baseline on
        standard), so all are merged - reading one instance alone under-reports both the
        catalogue and what has been watched.

        The third map exists to resolve PER-USER film watches: Tautulli history identifies a
        film by ratingKey and ``(title, year)`` (see ``movie_resolver.watched_movie_keys``),
        never by tmdb, so the tmdb-keyed universe membership can only be joined through it.
        The second (household) set stays as the fallback for a profile with no Tautulli id.
        """
        from scripts.managers.services.plex.playlists.movie_resolver import _norm as _mnorm

        stats: dict = {}
        watched: set = set()
        by_title_year: dict = {}
        by_title: dict = {}
        try:
            mfm = self.registry.get("manager", "RadarrCacheMovieFilesManager") if self.registry else None
        except Exception:
            mfm = None
        if mfm is None or not hasattr(mfm, "load"):
            self.logger.log_debug("[SagaProgress] no movie-files manager; films unresolved.")
            return stats, watched, by_title_year, by_title, {}
        # Enumerate from CONFIG, the way plan_summary._instances does. An earlier version
        # called mfm._get_apis() - a method that exists on the *instance* managers but NOT
        # on RadarrCacheMovieFilesManager. It raised, the except swallowed it, `instances`
        # stayed empty, and EVERY film in EVERY universe silently read as unwatched.
        try:
            _cfg = (self.config or {}).get("radarr_instances", {}) or {}
            instances = [k for k, v in _cfg.items()
                         if k != "default_instance" and isinstance(v, dict)]
        except Exception:
            instances = []
        if not instances:
            try:
                instances = [mfm._resolve_instance(None)]
            except Exception:
                instances = []
        for inst in instances:
            try:
                df = mfm.load(inst)
            except Exception:
                continue
            if df is None or getattr(df, "empty", True) or "tmdb_id" not in df.columns:
                continue
            for row in df.itertuples():
                tm = getattr(row, "tmdb_id", None)
                try:
                    tm = int(tm)
                except (TypeError, ValueError):
                    continue
                title = getattr(row, "title", "") or ""
                if tm not in stats:
                    stats[tm] = {"title": title,
                                 "runtime_minutes": getattr(row, "runtime_minutes", None)}
                _y = getattr(row, "year", None)
                try:
                    _y = int(_y)
                except (TypeError, ValueError):
                    _y = None
                if title and _y is not None:
                    by_title_year.setdefault((_mnorm(title), _y), tm)
                if title:
                    by_title.setdefault(_mnorm(title), tm)
                if getattr(row, "is_watched", False) is True:
                    watched.add(tm)
        # ratingKey -> tmdb, inverted from the owned-movie inventory (tmdb -> ratingKey).
        # Supplements the title match; see _watched_film_tmdbs_for for why neither alone.
        by_rk: dict = {}
        for _tm, _v in (self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {}).items():
            _rk = (_v or {}).get("rating_key") if isinstance(_v, dict) else None
            if _rk is None:
                continue
            try:
                _tmi = int(_tm)
            except (TypeError, ValueError):
                continue
            by_rk.setdefault(str(_rk), _tmi)
            _t = (_v or {}).get("title") if isinstance(_v, dict) else None
            if _t:
                by_title.setdefault(_mnorm(_t), _tmi)   # inventory titles too
        self.logger.log_info(
            f"[SagaProgress] film stats: {len(stats)} film(s) across "
            f"{len(instances)} instance(s), {len(watched)} watched household-wide; "
            f"{len(by_title)} title / {len(by_rk)} ratingKey resolver(s).")
        return stats, watched, by_title_year, by_title, by_rk

    def _saga_boost_for(self, safe_user, watched, prior_plan) -> dict:
        """``{group_key: 0..1}`` completion boost for one profile, and persist the update.

        Reads the profile's OWN universe progress from the saga-progress artifact the last
        run published, so the boost is per-user rather than household-wide, then de-rates
        each group by its skip history.

        Keyed by the ordering's ``group_key``, which for a franchise group IS the universe
        key - so a saga's completion lifts every member series in it. A series-keyed group
        gets its own show's progress where that show belongs to a tracked universe.

        Returns ``{}`` on any miss, which makes ordering byte-identical.
        """
        try:
            from scripts.managers.machine_learning.playlists.engagement import (
                saga_boost, update_engagement,
            )
        except Exception as e:
            self.logger.log_debug(f"[Engagement] module unavailable: {e}")
            return {}
        try:
            key = f"{_ENGAGEMENT_KEY}/{safe_user}"
            state, st = update_engagement(
                self._cache_get(key, None), prior_plan, watched,
                now=datetime.now(tz=timezone.utc).isoformat())
            if self.global_cache:
                self.global_cache.set(key, state)
            if st["surfaced"]:
                self.logger.log_debug(
                    f"[Engagement] {safe_user}: {st['surfaced']} group(s) surfaced last run — "
                    f"{st['engaged']} engaged, {st['skipped']} skipped, {st['dormant']} dormant "
                    f"(activity {st['activity']}, window_due={st['window_due']}).")
            prog = ((self._cache_get(_SAGA_PROGRESS_KEY, {}) or {}).get("users") or {})
            mine = (prog.get(safe_user) or {}).get("universes") or []
            out: dict = {}
            for u in mine:
                b = saga_boost(u.get("pct"), state, u.get("universe"))
                if b > 0:
                    out[u.get("universe")] = b
                    # A franchise group is keyed by the universe; its member SERIES groups
                    # inherit the same boost so a saga lifts coherently rather than only
                    # when the ordering happened to group it as a franchise.
                    for s in (u.get("series") or []):
                        out.setdefault(str(s.get("tvdb")), b)
            return out
        except Exception as e:
            self.logger.log_warning(f"[Engagement] skipped: {type(e).__name__}: {e}")
            return {}

    def _refresh_rk_crosswalk(self, tracked, by_title=None) -> dict:
        """Merge today's resolvable ratingKeys into the durable crosswalk and persist it.

        Runs off the two owned inventories, which are rebuilt each run, so the crosswalk
        tracks Plex without a single extra API call. On the FIRST build it is additionally
        seeded from each tracked profile's history via ``by_title``: a live inventory can
        only teach it keys Plex still issues, and 84% of this household's movie plays were
        already pointing at retired ones.

        Returns the crosswalk; ``{}`` on any failure, which leaves every caller on its
        existing title fallback rather than losing a resolver.
        """
        try:
            from scripts.support.utilities.rating_key_crosswalk import (
                build_crosswalk, movie_rk_map, seed_from_history,
            )
            from scripts.managers.services.plex.playlists.movie_resolver import _norm as _mnorm
        except Exception as e:
            self.logger.log_debug(f"[Crosswalk] module unavailable: {e}")
            return {}
        try:
            prior = self._cache_get(_RK_CROSSWALK_KEY, None)
            first_build = not prior
            now = datetime.now(tz=timezone.utc).isoformat()
            cw, stats = build_crosswalk(
                prior,
                movie_inventory=self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {},
                episode_inventory=self._cache_get(_INVENTORY_KEY, {}) or {},
                now=now,
            )
            seeded = 0
            if first_build and by_title:
                # ONE-TIME bootstrap. Title matching is used here to capture mappings for
                # keys that churned before this existed; afterwards the mapping is permanent
                # and no longer depends on Plex and *arr agreeing on a title.
                for u in (tracked or []):
                    uid = u.get("tautulli_user_id")
                    if uid is None:
                        continue
                    try:
                        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
                        if hm is None:
                            taut = self.registry.get("manager", "TautulliManager")
                            hm = getattr(taut, "watch_history", None) if taut else None
                        if not hm or not hasattr(hm, "get_all_history_cached"):
                            continue
                        seeded += seed_from_history(
                            cw, hm.get_all_history_cached(uid) or [], by_title,
                            now=now, norm=_mnorm)["seeded"]
                    except Exception:
                        continue
            if self.global_cache:
                self.global_cache.set(_RK_CROSSWALK_KEY, cw)
            # Plain concatenation, not a nested f-string: same-quote nesting only parses
            # on 3.12+ (PEP 701) and this should not carry a version floor for a log line.
            _extra = ""
            if seeded:
                _extra += f", +{seeded} seeded from history"
            _recycled = stats["movies_conflict"] + stats["shows_conflict"]
            if _recycled:
                _extra += f", {_recycled} recycled key(s)"
            self.logger.log_info(
                f"[Crosswalk] ratingKey map: {stats['movies_total']} film(s) / "
                f"{stats['shows_total']} show(s) (+{stats['movies_new']} film / "
                f"+{stats['shows_new']} show new{_extra}).")
            return cw
        except Exception as e:
            self.logger.log_warning(f"[Crosswalk] refresh failed: {type(e).__name__}: {e}")
            return {}

    def _watched_film_tmdbs_for(self, user_id, by_title_year, by_title=None, by_rk=None) -> set:
        """``{tmdb}`` a single user has FINISHED, from their own Tautulli history.

        Three resolvers, because no single identity survives on its own:

        * **TITLE** — the workhorse. Tautulli history movie rows carry NO ``year`` field at
          all (0 of 282 on a real profile), so ``watched_movie_keys``' ``(title, year)``
          identity is NEVER produced for films and matching only on it resolved nothing for
          every user — which silently pushed all five onto the household fallback and made
          the progress table show everyone in the same place.
        * **ratingKey** — exact when fresh, but Plex re-scans churn it: only 19 of 178
          finished plays still resolved this way on a real profile, against 46 by title.
          Kept as a supplement since it catches titles that differ between Plex and Radarr.
        * **(title, year)** — retained for any source that does supply a year.

        Empty set on any miss; the caller then falls back to the household set.
        """
        if user_id is None or not self.registry:
            return set()
        try:
            from scripts.managers.services.plex.playlists.movie_resolver import (
                _norm as _mnorm, watched_movie_keys,
            )
            hm = self.registry.get("manager", "TautulliWatchHistoryManager")
            if hm is None:
                taut = self.registry.get("manager", "TautulliManager")
                hm = getattr(taut, "watch_history", None) if taut else None
            if not hm or not hasattr(hm, "get_all_history_cached"):
                return set()
            hist = hm.get_all_history_cached(user_id) or []
            keys = watched_movie_keys(hist) or set()
        except Exception:
            return set()
        out: set = set()
        for k in keys:
            if isinstance(k, tuple) and len(k) == 2:
                tm = (by_title_year or {}).get(k)
                if tm is not None:
                    out.add(tm)
            elif isinstance(k, str) and by_rk:
                tm = by_rk.get(k)
                if tm is not None:
                    out.add(tm)
        # Title pass, over the SAME finished rows watched_movie_keys admits.
        if by_title:
            try:
                _pct = 85.0
                for row in hist:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("media_type", "")).lower() != "movie":
                        continue
                    try:
                        if float(row.get("percent_complete") or 0) < _pct:
                            continue
                    except (TypeError, ValueError):
                        continue
                    tm = by_title.get(_mnorm(row.get("title")))
                    if tm is not None:
                        out.add(tm)
            except Exception:
                pass
        return out

    def _emit_saga_progress(self, tracked, watched_by_user, recency_by_user) -> None:
        """Build, persist and log the per-user universe progress artifact.

        Persisted under ONE stable key so a future web layer can render it without
        re-deriving anything - every value is a plain scalar or list.
        """
        from scripts.managers.machine_learning.likelihood.saga_progress import (
            build_progress_artifact,
        )
        from scripts.managers.services.plex.playlists.universe_order import saga_display_name
        series_stats, title_to_tvdb = self._series_stats_for_progress()
        if not series_stats:
            self.logger.log_warning(
                "[SagaProgress] no series stats - skipping universe progress.")
            return
        movie_stats, household_films, by_title_year, by_title, by_rk = \
            self._movie_stats_for_progress()
        # Durable ratingKey map, refreshed and persisted before the per-user resolve so
        # this run already benefits. It SUPERSETS the live inventory map built above:
        # every key valid today, plus every key ever seen valid on a previous run.
        try:
            from scripts.support.utilities.rating_key_crosswalk import movie_rk_map
            _cw = self._refresh_rk_crosswalk(tracked, by_title=by_title)
            _cw_rk = movie_rk_map(_cw) if _cw else {}
            if _cw_rk:
                _cw_rk.update(by_rk)      # a live key wins on any disagreement
                by_rk = _cw_rk
        except Exception as e:
            self.logger.log_debug(f"[Crosswalk] not applied to film resolve: {e}")
        universes = (self._cache_get(_UNIVERSE_SRC_KEY, {}) or {}).get("universes") or {}
        if not universes:
            self.logger.log_warning(
                "[SagaProgress] universe source cache is empty - skipping.")
            return
        names = [u["safe_user"] for u in (tracked or [])]
        # PER-USER film sets. A profile Tautulli can identify gets its own; one it cannot
        # (no tautulli_user_id, or no movie history yet) falls back to the HOUSEHOLD set,
        # which over-reports that individual but never under-reports the remainder.
        films_by_user: dict = {}
        _own = 0
        for u in (tracked or []):
            mine = self._watched_film_tmdbs_for(u.get("tautulli_user_id"), by_title_year,
                                                by_title, by_rk)
            if mine:
                _own += 1
            films_by_user[u["safe_user"]] = mine or household_films
        self.logger.log_debug(
            f"[SagaProgress] film watch-sets: {_own}/{len(names)} profile(s) resolved "
            f"per-user, remainder on the household set of {len(household_films)}.")
        art = build_progress_artifact(
            names, universes, series_stats,
            watched_by_user=watched_by_user,
            recency_by_user=recency_by_user,
            title_to_tvdb=title_to_tvdb,
            movie_stats=movie_stats,
            watched_tmdbs_by_user=films_by_user,
            # The project's OWN key->name map: 'star' -> "Star Wars Universe", not the
            # entry's chronologically-first member ("The Acolyte").
            canonical_name=saga_display_name,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        if self.global_cache:
            self.global_cache.set(_SAGA_PROGRESS_KEY, art)
        self._log_saga_progress(art, tracked)

    def _log_saga_progress(self, art, tracked, *, top=12, min_items=10) -> None:
        """Universe ledger: one parent row per universe, one indented row per user.

        Same shape as the space plan ledger - NBSP indents and an ASCII marker, because
        ordinary spaces are collapsed by _strip_decor and a unicode arrow is dropped by the
        cp1252 encoder AFTER the column width is measured, which leaves child rows short.
        """
        users = (art or {}).get("users") or {}
        if not users:
            return
        label = {u["safe_user"]: (u.get("title") or u["safe_user"]) for u in (tracked or [])}
        _L1 = "\u00a0\u00a0> "

        def _eta(days, rate):
            """Readable finish estimate. Past ~2 years the number is arithmetically correct
            and practically meaningless - 13028d is 36 years - so it collapses rather than
            implying a precision the trailing 90-day rate cannot support."""
            if days is None:
                return "no rate"
            if days > 3650:
                return f"10y+ @ {rate:g}/d"
            if days > 730:
                return f"{days / 365.0:.1f}y @ {rate:g}/d"
            return f"{days:.0f}d @ {rate:g}/d"
        # Universe totals are user-independent, but WHICH universes to show is not.
        # Ranking purely by size surfaced the twelve biggest - all of them untouched -
        # and pushed every universe anyone was actually part-way through off the table.
        # So: STARTED universes first (by best progress across the household), then the
        # unstarted by how much is left. A progress report should lead with progress.
        totals: dict = {}
        best_pct: dict = {}
        for _n, blob in users.items():
            for u in blob.get("universes") or []:
                totals.setdefault(u["universe"], u)
                _k = u["universe"]
                if u["pct"] > best_pct.get(_k, 0.0):
                    best_pct[_k] = u["pct"]
        ranked = [u for u in totals.values() if u["items_total"] >= min_items]
        ranked.sort(key=lambda u: (-(best_pct.get(u["universe"], 0.0) > 0),
                                   -best_pct.get(u["universe"], 0.0),
                                   -u["items_remaining"]))
        rows = []
        for uni in ranked[:top]:
            key = uni["universe"]
            # Parent row carries the universe TOTAL under its own header; the user rows
            # below carry what each has actually watched. An earlier version put the total
            # in the "watched" column, which read as though the household had seen all 953
            # Star Trek episodes.
            rows.append([uni["display"][:26],
                         f"{uni['shows_in_universe']}sh/{uni['movies_in_universe']}mv",
                         f"{uni['items_total']}", "", "", ""])
            for name, blob in sorted(users.items()):
                mine = next((x for x in (blob.get("universes") or [])
                             if x["universe"] == key), None)
                if mine is None:
                    continue
                rate = (blob.get("rate") or {}).get("episodes_per_day") or 0
                rows.append([f"{_L1}{label.get(name, name)[:20]}", "",
                             f"{mine['items_watched']}",
                             f"{mine['pct']:.1f}%",
                             f"{mine['remaining_hours']:.0f}h",
                             _eta(mine["eta_days"], rate)])
        if not rows:
            return
        try:
            self.logger.log_grid(
                ["universe / user", "members", "items / watched", "pct", "left", "finish in"],
                rows, title="Universe progress - per user", cap=28)
        except Exception as e:
            self.logger.log_debug(f"[SagaProgress] ledger skipped: {e}")

    def _resume_cfg(self):
        """(enabled, order, weight) from plex.playlists.resume_boost - lift an IN-PROGRESS
        series toward the front. Mirrors ``movie_builder._resume_cfg`` exactly so a saga and
        a show rank on the same axis. order in {recency (default), progress}; weight in [0,1]
        (default 0.35 = moderate: an in-progress show wins ties and gaps up to the weight, a
        clearly-higher-affinity standalone still overtakes). OFF -> byte-identical."""
        rc = self._pl_cfg().get("resume_boost", {}) or {}
        order = str(rc.get("order", "recency")).strip().lower()
        try:
            weight = min(1.0, max(0.0, float(rc.get("weight", 0.35))))
        except (TypeError, ValueError):
            weight = 0.35
        return (bool(rc.get("enabled", False)),
                (order if order in ("recency", "progress") else "recency"), weight)

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

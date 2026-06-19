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

import math

from scripts.managers.factories.base_manager import BaseManager
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
from scripts.managers.services.plex.playlists.readiness import diagnose_tv_readiness
from scripts.managers.services.plex.playlists.tv_resolver import (
    build_tv_plan,
    watched_episode_keys,
)

_INVENTORY_KEY = "plex/episodes/owned_inventory"
_STATS_KEY = "plex/episodes/resolution_stats"
_PLAN_KEY = "plex/playlists/tv_plan"          # + /{safe_user}


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
        return self._build_for_users(
            tracked, owned_eps, inventory, resolution_stats, series_scores,
            watched_by_user, series_genres=series_genres, affinity_by_user=affinity_by_user,
            series_certs=series_certs, series_csm_ages=self._series_csm_ages(),
            daemon_enabled=self._daemon_enabled(), daemon_running=self._daemon_running())

    def _build_for_users(self, tracked, owned_eps, inventory, resolution_stats,
                         series_scores, watched_by_user, *, series_genres=None,
                         affinity_by_user=None, series_certs=None, series_csm_ages=None,
                         daemon_enabled, daemon_running) -> dict:
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
        for u in tracked:
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
                f"[Playlists] '{u.get('title')}' -> tautulli={u.get('tautulli_username') or '-'}, "
                f"affinity={len(user_aff)} genre(s) [{top_genres}], watched={n_watched} ep, "
                f"jit={len(user_jit)}, tier={tier_name}{gate_note}")

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
                episode_cap=self._episode_cap(), max_items=self._max_items())
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{u['safe_user']}", self._serialize(plan))
            reasons = self._tv_reasons(user_owned, inventory, series_genres, user_aff, user_jit)
            self._log_preview(u, plan, stats, display, reasons, label="episode")
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
                     kinds=None, label: str = "episode", family_label: str = "Up Next"):
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
        header = (f"[dry-run] '{title}' {family_label} - {len(plan.items)} {label}(s), "
                  f"{stats.get('unresolved', 0)} unmatched")
        cols = ["#", "Title"] + (["Kind"] if show_kind else []) + ["Rank", "Why"]
        grid = getattr(self.logger, "log_grid", None)
        if callable(grid) and rows:
            grid(cols, rows, title=header, cap=44)
        else:
            self.logger.log_info(f"[Playlists] {header}")

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

    def _daemon_enabled(self) -> bool:
        d = ((self.config.get("daemons", {}) if self.config else {}) or {}).get("enrich", {}) or {}
        return bool(d.get("enabled"))

    def _daemon_running(self) -> bool:
        try:
            from scripts.managers.factories.daemons.supervisor import DaemonSupervisor
            return bool(DaemonSupervisor(logger=self.logger).is_running())
        except Exception:
            return False

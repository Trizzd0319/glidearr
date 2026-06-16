"""
RadarrQualityUniverseManager
============================
Manages quality-profile changes for universe-tagged movies.

Universe movies are part of a cinematic / shared universe (MCU, DC, etc.)
and are NEVER deleted.  Instead, when free space is tight they are
downgraded to a lower quality profile and when space is plentiful they
are upgraded back.

Tag conventions supported (Radarr uses hyphens, not underscores)
    "keep-universe"       → universe label "universe"
    "keep-universe-mcu"   → universe label "mcu"
    "keep-universe-dc"    → universe label "dc"
    Multiple universe tags on one movie are all captured.

Thresholds — the SAME band as the rest of space management (space.space_targets):
    downgrade →  free below the floor T (= free_space_limit, or 25% of the total drive
                 when unset). Under pressure, universe titles help reclaim by lowering
                 quality (they're never deleted, so quality is the only lever).
    upgrade   →  free above the band top U (= T + headroom). Holds in [T, U] (hysteresis).
                 The old hardcoded 10 GB downgrade / 50 GB upgrade floors are gone.

Quality-profile ranking
    Profiles are ranked by the highest resolution among their *allowed*
    quality items (ascending).  A ``min_rank`` floor (default 0) prevents
    downgrading past the cheapest profile.

Workflow
    1. evaluate_quality_actions(instance, free_space_gb)
       Writes "downgrade" / "upgrade" / None into the ``quality_action``
       Parquet column for all universe rows.  Does not call the API.

    2. apply_quality_actions(instance)
       Reads pending quality_action rows, GETs the full movie payload from
       Radarr, changes ``qualityProfileId``, PUTs it back, then clears the
       column.  Respects dry_run.

    3. run(instance, free_space_gb, ...)
       Convenience wrapper: evaluate → apply in one call.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.classification.keep_policy import FRANCHISE_HINTS
from scripts.managers.machine_learning.ledger.decision_ledger import stamp_universe_plan
from scripts.managers.machine_learning.space.universe_quality import (
    downgrade_single_rank,
    downgrade_target,
    universe_action,
    upgrade_target,
)
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import space_targets
from scripts.support.utilities.watch_likelihood import watch_likelihood


class RadarrQualityUniverseManager(BaseManager, ComponentManagerMixin):
    """
    Quality-change manager for universe-tagged movies.
    Never deletes; downgrades quality when space is tight,
    upgrades when space is plentiful.
    """

    # Universe quality follows the SAME band as everything else (space.space_targets):
    # downgrade below the floor T, upgrade above the band top U, hold in [T, U]. Both
    # triggers derive from free_space_limit (or 25% of the total drive when unset) —
    # NEVER a hardcoded GB floor. See evaluate_quality_actions.
    SCORE_4K_THRESHOLD   = 70     # score >= this → eligible for 4K (0-100 scale)
    _4K_MIN_RESOLUTION   = 2000

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrQualityManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"RadarrManager, or Main. Refusing to initialize without an explicit value "
                f"from config.json to prevent accidental destructive operations."
            )
        self.dry_run = bool(_dry_run)
        if self.dry_run:
            self.logger.log_debug(f"🛡️ {self.__class__.__name__} dry_run=True — no destructive operations will run")

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    # ── Instance resolution ──────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    # ── Registry helpers ─────────────────────────────────────────────────────────

    def _get_movie_files_manager(self):
        try:
            return self.registry.get("manager", "RadarrCacheMovieFilesManager")
        except Exception:
            return None

    # ── Quality profile ranking ──────────────────────────────────────────────────

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """
        Return quality profiles sorted from lowest to highest quality.

        Rank key: maximum resolution among all *allowed* quality items in the
        profile (including nested group items).  Profiles with no allowed
        items rank at 0 and sort to the bottom — they are never upgrade
        targets and are only downgrade targets as a last resort.
        """
        if self.radarr_api is None:
            return []

        raw = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []

        def _max_res(profile: dict) -> int:
            best = 0
            for item in (profile.get("items") or []):
                if not item.get("allowed", False):
                    continue
                quality = item.get("quality") or {}
                res = quality.get("resolution", 0)
                if isinstance(res, (int, float)):
                    best = max(best, int(res))
                for sub in (item.get("items") or []):
                    if not sub.get("allowed", False):
                        continue
                    sq  = sub.get("quality") or {}
                    sr  = sq.get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
            return best

        return sorted(raw, key=_max_res)

    def _get_adjacent_profile(self, current_profile_id, ranked_profiles, direction, min_rank=0):
        """Legacy single-step — kept for compatibility."""
        ids = [p["id"] for p in ranked_profiles]
        try:
            idx = ids.index(current_profile_id)
        except ValueError:
            return None
        if direction == "downgrade":
            ti = idx - 1
            return ranked_profiles[ti] if ti >= min_rank else None
        ti = idx + 1
        return ranked_profiles[ti] if ti < len(ranked_profiles) else None

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        best = 0
        for item in (profile.get("items") or []):
            if not item.get("allowed"):
                continue
            res = (item.get("quality") or {}).get("resolution", 0)
            if isinstance(res, (int, float)):
                best = max(best, int(res))
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
        return best

    @staticmethod
    def _profile_cutoff_quality_name(profile: dict) -> str | None:
        """The target *edition* name for the From->To grid: the profile's cutoff quality
        name (e.g. 'Remux-2160p', 'WEBDL-2160p'). Every 4K tier reports resolution 2160,
        so resolution alone can't tell a WEB-DL profile from a Remux one — the cutoff is
        the edition Radarr actually aims for. Falls back to the highest-resolution allowed
        quality name when the cutoff id can't be matched (older payloads), else None."""
        items  = profile.get("items") or []
        cutoff = profile.get("cutoff")
        if cutoff is not None:
            for item in items:
                # A quality GROUP (e.g. 'WEB 2160p') — cutoff may reference the group id.
                if item.get("id") == cutoff and item.get("name"):
                    return item["name"]
                q = item.get("quality") or {}
                if q.get("id") == cutoff and q.get("name"):
                    return q["name"]
                for sub in (item.get("items") or []):
                    sq = sub.get("quality") or {}
                    if sq.get("id") == cutoff and sq.get("name"):
                        return sq["name"]
        # Fallback: best allowed edition by resolution.
        best_res, best_name = -1, None
        for item in items:
            if item.get("allowed"):
                q = item.get("quality") or {}
                r = q.get("resolution", 0)
                if isinstance(r, (int, float)) and int(r) > best_res and q.get("name"):
                    best_res, best_name = int(r), q["name"]
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sq = sub.get("quality") or {}
                    r = sq.get("resolution", 0)
                    if isinstance(r, (int, float)) and int(r) > best_res and sq.get("name"):
                        best_res, best_name = int(r), sq["name"]
        return best_name

    @staticmethod
    def _quality_label(value, fallback_res: int = 0) -> str:
        """Human quality-name cell for the From->To grid: the file edition string (e.g.
        'WEBDL-2160p') when known, else the resolution ('2160p'), else '-'."""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return f"{fallback_res}p" if fallback_res else "-"

    def _get_target_profile(self, ranked_profiles, direction, current_profile_id,
                            min_rank=0, score=None, likelihood=None):
        """Thin delegation to space.universe_quality. Upgrade → likelihood-gated Radarr
        ladder step-UP; downgrade → one ranked-list rank down (the legacy/fallback step).
        Kept as a method for the existing call sites + tests."""
        if direction == "upgrade":
            L = likelihood if likelihood is not None else (score if score is not None else 0.0)
            return upgrade_target(ranked_profiles, current_profile_id, L, self.config, min_rank=min_rank)
        return downgrade_single_rank(ranked_profiles, current_profile_id, min_rank=min_rank)

    def _downgrade_target(self, row, ranked_profiles, current_profile_id, *, min_rank=0):
        """Thin delegation to space.universe_quality.downgrade_target — one resolution-tier
        step down (best-quality, runtime-sized), legacy single-rank fallback when the row's
        resolution is unknown."""
        return downgrade_target(row, ranked_profiles, current_profile_id, self.config, min_rank=min_rank)


    # ── Diagnostic: tag audit ──────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("audit_universe_tags")
    def audit_universe_tags(self, instance: str) -> dict:
        """
        Diagnostic: compare universe tags in Radarr vs the Parquet.

        Reports:
        - All tags in Radarr matching keep-universe* pattern
        - How many movies have those tags in the live API
        - How many rows in the Parquet have keep_policy='universe'
        - Any mismatch between the two

        Call this when universe_count=0 but you expect universe movies.
        """
        instance = self._resolve_instance(instance)
        report   = {
            "universe_tags_in_radarr":     [],
            "movies_tagged_in_radarr":     0,
            "movies_in_parquet":           0,
            "movies_only_in_radarr":       [],  # tagged in Radarr but not in Parquet
            "movies_only_in_parquet":      [],  # in Parquet but tag missing in Radarr
            "tag_label_matches":           {},
        }

        if self.radarr_api is None:
            return report

        # ── Live Radarr tags ──────────────────────────────────────────────────
        raw_tags = self.radarr_api._make_request(instance, "tag", fallback=[]) or []
        tag_label_map = {t["id"]: t["label"] for t in raw_tags if t.get("id") is not None}

        uni_tag_ids = {
            tid for tid, lbl in tag_label_map.items()
            if lbl.lower() == "keep-universe"
            or lbl.lower().startswith("keep-universe-")
            or lbl.lower() == "universe"
        }
        report["universe_tags_in_radarr"] = [
            {"id": tid, "label": tag_label_map[tid]} for tid in uni_tag_ids
        ]
        report["tag_label_matches"] = {
            tag_label_map[tid]: tid for tid in uni_tag_ids
        }

        if not uni_tag_ids:
            self.logger.log_warning(
                f"[Universe] Audit: NO universe tags found in Radarr '{instance}'. "
                f"Create tags named 'keep-universe', 'keep-universe-mcu', etc. "
                f"and apply them to your franchise movies."
            )
            self.logger.log_info(
                f"[Universe] All tags in '{instance}': "
                + ", ".join(f"'{v}' (id={k})" for k, v in sorted(tag_label_map.items(), key=lambda x: x[1]))
            )
        else:
            self.logger.log_info(
                f"[Universe] Universe tag(s) found: "
                + ", ".join(f"'{tag_label_map[tid]}' (id={tid})" for tid in uni_tag_ids)
            )

        # ── Per-universe summary (ONE table) ──────────────────────────────────
        # Pending quality-action marks (upgrade/downgrade) live only in the Parquet; owned /
        # missing / cutoff-unmet live only in the live Radarr payload. Load the Parquet's marks
        # up front keyed by movie_id, so the single live-anchored table below can fold them in
        # by an EXACT id-join (no fragile universe-label matching). The Parquet stores OWNED
        # movies only, so it can't report Missing/CutoffUnmet — hence the live anchor.
        mfm = self._get_movie_files_manager()
        _qa_by_id: dict[int, str] = {}
        if mfm:
            df = mfm.load(instance)
            if not df.empty and "keep_policy" in df.columns:
                parquet_uni = df[df["keep_policy"] == "universe"]
                report["movies_in_parquet"] = len(parquet_uni)
                if "quality_action" in df.columns and "movie_id" in df.columns:
                    for _, row in df[df["quality_action"].notna()].iterrows():
                        _mid = row.get("movie_id")
                        if pd.notna(_mid):
                            _qa_by_id[int(_mid)] = str(row.get("quality_action") or "").lower()

        if uni_tag_ids:
            all_movies = []
            if self.global_cache:
                all_movies = self.global_cache.get(f"radarr.movies.{instance}.full") or []
            if not all_movies:
                all_movies = self.radarr_api._make_request(instance, "movie", fallback=[]) or []

            tagged = [
                {"id": m.get("id"), "title": m.get("title"), "tags": m.get("tags", []),
                 "has_file": bool(m.get("hasFile")), "size": int(m.get("sizeOnDisk") or 0),
                 # TMDB collection name (e.g. 'The Conjuring Collection') — used to auto-split a
                 # bare-'universe' movie into its franchise when no explicit tag/hint is present.
                 "collection": ((m.get("collection") or {}).get("name") or ""),
                 # cutoff-unmet = an owned movie whose file is still below the profile cutoff
                 # (upgrade-eligible). Radarr exposes qualityCutoffNotMet on the movieFile
                 # sub-object (NOT the top-level movie) — same path find_cutoff_not_met reads.
                 "cutoff_unmet": bool((m.get("movieFile") or {}).get("qualityCutoffNotMet", False))}
                for m in all_movies
                if any(tid in uni_tag_ids for tid in (m.get("tags") or []))
            ]
            report["movies_tagged_in_radarr"] = len(tagged)

            def _uni_name(tag_ids, collection):
                # Universe label for the audit view (display-only; does NOT change keep behavior):
                #   1. explicit suffix:  'keep-universe-mcu' -> 'mcu'
                #   2. a short FRANCHISE_HINT tag ('mcu','dc','startrek',…) next to a bare
                #      'universe' tag -> that hint (the LESS AWKWARD split — no per-franchise tag).
                #      1+2 mirror classification.keep_policy, so they match the Parquet universe_name.
                #   3. else the movie's TMDB collection ('The Conjuring Collection' -> 'Conjuring')
                #      so franchises break out with ZERO tagging — automatic, but per-sub-franchise
                #   4. else the ungrouped 'universe' bucket
                labels = {(tag_label_map.get(tid) or "").lower() for tid in tag_ids}
                for lbl in labels:
                    if lbl.startswith("keep-universe-"):
                        return lbl[len("keep-universe-"):]
                hints = sorted(labels & FRANCHISE_HINTS)
                if hints:
                    return "|".join(hints)
                c = (collection or "").strip()
                if c.lower().endswith(" collection"):
                    c = c[: -len(" collection")].strip()
                return c or "universe"

            # ONE row per universe: total tagged, owned vs. still missing, owned-but-below-cutoff
            # (upgrade-eligible), how many are MARKED for a space-driven upgrade / downgrade
            # (folded in from the Parquet by movie_id), and the universe's footprint on disk.
            # Collapses the former 300+ row per-title dump AND the second Parquet table into one.
            _uni_agg: dict[str, dict] = {}
            for t in tagged:
                # full tag list (hint tags aren't in uni_tag_ids) + TMDB collection fallback
                uni = _uni_name(t["tags"], t["collection"])
                a = _uni_agg.setdefault(
                    uni, {"total": 0, "owned": 0, "cutoff": 0, "upg": 0, "dng": 0, "size": 0}
                )
                a["total"]  += 1
                a["owned"]  += 1 if t["has_file"] else 0
                a["cutoff"] += 1 if t["cutoff_unmet"] else 0
                _qa = _qa_by_id.get(int(t["id"])) if t["id"] is not None else None
                if _qa == "upgrade":
                    a["upg"] += 1
                elif _qa == "downgrade":
                    a["dng"] += 1
                a["size"]   += t["size"]
            self.logger.log_info(
                f"[Universe] {len(tagged)} movie(s) tagged with keep-universe* in Radarr "
                f"across {len(_uni_agg)} universe(s):"
            )

            def _uni_row(label, a):
                return [label, str(a["total"]), str(a["owned"]), str(a["total"] - a["owned"]),
                        str(a["cutoff"]), str(a["upg"]), str(a["dng"]), f"{a['size'] / 1e9:.0f}"]

            # biggest universes first (collection auto-grouping can produce many rows), ties alpha
            _uni_rows = [_uni_row(uni, a) for uni, a in
                         sorted(_uni_agg.items(), key=lambda kv: (-kv[1]["total"], kv[0].lower()))]
            if len(_uni_agg) > 1:   # totals row only earns its place once there's >1 universe
                _tot = {k: sum(a[k] for a in _uni_agg.values())
                        for k in ("total", "owned", "cutoff", "upg", "dng", "size")}
                _uni_rows.append(_uni_row("TOTAL", _tot))
            self.logger.log_grid(
                ["Universe", "Movies", "Owned", "Missing", "CutoffUnmet",
                 "Upgrade", "Downgrade", "Size GB"],
                _uni_rows, cap=28,
            )

        # ── Mismatch analysis ─────────────────────────────────────────────────
        if report["movies_in_parquet"] == 0 and report["movies_tagged_in_radarr"] == 0:
            self.logger.log_warning(
                f"[Universe] '{instance}': Neither Radarr tags nor Parquet rows found. "
                "Universe quality management is inactive. "
                "Add 'keep-universe' / 'keep-universe-mcu' tags in Radarr to enable it."
            )
        elif report["movies_in_parquet"] != report["movies_tagged_in_radarr"]:
            self.logger.log_warning(
                f"[Universe] Mismatch: {report['movies_tagged_in_radarr']} tagged in Radarr "
                f"vs {report['movies_in_parquet']} in Parquet. "
                f"Run movie_files.refresh() to sync the Parquet."
            )

        return report

    # ── Universe movie query ─────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_universe_movies")
    def get_universe_movies(self, instance: str) -> pd.DataFrame:
        """
        Return a DataFrame of all rows tagged with keep_policy='universe'
        from the movie_files Parquet for this instance.
        """
        mfm = self._get_movie_files_manager()
        if mfm is None:
            self.logger.log_debug("[Universe] movie_files manager unavailable")
            return pd.DataFrame()

        instance = self._resolve_instance(instance)
        df = mfm.load(instance)

        if df.empty or "keep_policy" not in df.columns:
            return pd.DataFrame()

        # Include both keep_universe (never-delete) and universe (deletable) rows
        uni_mask = df["keep_policy"].isin(["keep_universe", "universe"])
        return df[uni_mask].copy()

    @LoggerManager().log_function_entry
    @timeit("get_universe_summary")
    def get_universe_summary(self, instance: str) -> dict:
        """
        Return a summary dict of universe movies grouped by universe_name.
        Keys: universe label → list of {title, year, quality_profile_name, quality_action}.
        """
        df = self.get_universe_movies(instance)
        if df.empty:
            return {}

        summary: dict[str, list] = {}
        for _, row in df.iterrows():
            uni = row.get("universe_name") or "universe"
            for label in str(uni).split("|"):
                entry = {
                    "title":                row.get("title"),
                    "year":                 row.get("year"),
                    "quality_profile_name": row.get("quality_profile_name"),
                    "quality_action":       row.get("quality_action"),
                    "size_gb":              round(float(row.get("size_bytes") or 0) / 1e9, 2),
                }
                summary.setdefault(label, []).append(entry)
        return summary

    # ── Quality action evaluation ────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("evaluate_universe_quality_actions")
    def evaluate_quality_actions(
        self,
        instance: str,
        free_space_gb: float,
        downgrade_threshold_gb: float | None = None,
        upgrade_threshold_gb: float | None = None,
    ) -> dict:
        """
        Mark quality actions for all universe movies based on free space.

        free_space_gb < T (floor)       → mark "downgrade" (reclaim under pressure)
        free_space_gb > U (band top)    → mark "upgrade"
        in the hold band [T, U]         → clear any pending action (hysteresis)

        Writes changes to the Parquet; does NOT call the Radarr API.

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)

        # Universe quality follows the SAME band as the rest of space management
        # (space.space_targets): downgrade below the floor T, upgrade above the band top U,
        # hold in [T, U]. Both T and U derive from free_space_limit, or 25% of the TOTAL
        # drive when unset — NEVER a hardcoded GB floor.
        #   • DOWNGRADE below T: under genuine space pressure universe titles help reclaim
        #     (their quality is lowered) alongside everything else — they're never deleted,
        #     so quality is the only lever. (Was a fixed 10 GB deep-emergency floor.)
        #   • UPGRADE above U only (DEFECT 2): upgrading inside the pressure band would
        #     consume the space space-pressure is trying to free, and could re-inflate a
        #     bare-"universe" title space-pressure just downgraded (thrashing).
        # downgrade_threshold_gb / upgrade_threshold_gb remain accepted for explicit
        # per-call override; when None they default to T / U respectively.
        try:
            _total_gb = self.instance_manager.disk_total_gb(instance) if self.instance_manager else None
        except Exception:
            _total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Radarr", instance, _total_gb)
        floor_gb, effective_upgrade_gb = space_targets(self.config, total_gb=_total_gb)
        downgrade_threshold = downgrade_threshold_gb if downgrade_threshold_gb is not None \
            else floor_gb

        mfm = self._get_movie_files_manager()
        if mfm is None:
            self.logger.log_warning("[Universe] movie_files manager unavailable — cannot evaluate")
            return {}

        df = mfm.load(instance)
        if df.empty or "keep_policy" not in df.columns:
            return {"universe_count": 0, "downgrade_marked": 0, "upgrade_marked": 0, "cleared": 0}

        # Ensure columns exist
        if "quality_action" not in df.columns:
            df["quality_action"] = None
        if "universe_name" not in df.columns:
            df["universe_name"] = None

        universe_mask     = df["keep_policy"].isin(["keep_universe", "universe"])
        universe_count    = int(universe_mask.sum())

        stats = {
            "universe_count":   universe_count,
            "downgrade_marked": 0,
            "upgrade_marked":   0,
            "cleared":          0,
            "free_space_gb":    round(free_space_gb, 2),
        }

        if universe_count == 0:
            self.logger.log_debug(f"[Universe] No universe-tagged movies in '{instance}'.")
            return stats

        # Determine desired action via the brain (space.universe_quality.universe_action):
        # downgrade below the floor, upgrade above the band top, else hold.
        desired = universe_action(free_space_gb, downgrade_threshold, effective_upgrade_gb)

        for idx in df.index[universe_mask]:
            current = df.at[idx, "quality_action"]
            if desired == "downgrade":
                if current != "downgrade":
                    stats["downgrade_marked"] += 1
                df.at[idx, "quality_action"] = "downgrade"
            elif desired == "upgrade":
                if current != "upgrade":
                    stats["upgrade_marked"] += 1
                df.at[idx, "quality_action"] = "upgrade"
            else:
                if current is not None:
                    stats["cleared"] += 1
                df.at[idx, "quality_action"] = None

        threshold_desc = (
            f"below floor ({downgrade_threshold:.0f}GB) — reclaiming"
            if desired == "downgrade"
            else f"above upgrade band ({effective_upgrade_gb:.0f}GB)"
            if desired == "upgrade"
            else f"in hold band ({downgrade_threshold:.0f}–{effective_upgrade_gb:.0f}GB, no quality change under space pressure)"
        )
        self.logger.log_info(
            f"[Universe] '{instance}': {free_space_gb:.1f}GB free — {threshold_desc}. "
            f"{universe_count} universe movie(s): "
            f"{stats['downgrade_marked']} newly marked downgrade, "
            f"{stats['upgrade_marked']} newly marked upgrade, "
            f"{stats['cleared']} cleared."
        )

        # Persist the quality_action marks even in dry_run — apply_quality_actions
        # loads the Parquet fresh and only acts on rows with quality_action set, so
        # without this save the dry-run apply pass sees nothing and the universe
        # upgrades/downgrades never reach the decision ledger. Only quality_action is
        # written here (not quality_profile_id), so it's a safe, recomputed-each-run
        # annotation — apply stamps the signed plan_reclaim_gb.
        mfm.save(instance, df)

        return stats

    def _stamp_universe_plan(self, df, idx, action: str, target_profile: dict) -> None:
        """Stamp the decision ledger for a universe quality change with the SIGNED
        space impact (downgrade frees, upgrade consumes). Delegates to the brain
        (ledger.decision_ledger.stamp_universe_plan)."""
        stamp_universe_plan(df, idx, action, target_profile)

    # ── Quality action execution ─────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("apply_universe_quality_actions")
    def apply_quality_actions(self, instance: str, min_rank: int = 0) -> dict:
        """
        Execute all pending quality_action entries for universe movies.

        For each universe row with quality_action set:
          - GET the full movie record from Radarr
          - Resolve adjacent quality profile (up or down)
          - PUT the movie back with the new qualityProfileId
          - Clear quality_action in the Parquet

        Never deletes.  Never touches non-universe movies.

        ``min_rank`` prevents downgrading below a floor profile index in the
        ranked list (0 = no floor, 1 = never use the cheapest profile, etc.).

        Returns stats dict.
        """
        instance = self._resolve_instance(instance)
        stats = {
            "downgraded":        0,
            "upgraded":          0,
            "failed":            0,
            "skipped_at_limit":  0,
            "dry_run":           self.dry_run,
        }

        if self.radarr_api is None:
            self.logger.log_warning("[Universe] radarr_api unavailable — cannot apply quality actions")
            return stats

        mfm = self._get_movie_files_manager()
        if mfm is None:
            self.logger.log_warning("[Universe] movie_files manager unavailable — cannot apply quality actions")
            return stats

        df = mfm.load(instance)
        if df.empty:
            return stats

        if "quality_action" not in df.columns or "keep_policy" not in df.columns:
            return stats

        # Apply must cover the SAME set evaluate_quality_actions marks — both
        # keep_universe (never-deleted franchises) and bare universe (deletable). A prior
        # ``== "universe"`` here silently dropped keep_universe titles: they were marked
        # for up/downgrade by evaluate, then wiped by the stale-guard below as "non-
        # universe" and never applied. The upgrade tier logic is already keep-universe
        # aware, and _get_target_profile safely no-ops a title already at its floor.
        universe_mask = df["keep_policy"].isin(["keep_universe", "universe"])
        action_mask   = df["quality_action"].notna()
        pending_mask  = universe_mask & action_mask

        if not pending_mask.any():
            self.logger.log_info(f"[Universe] No pending quality actions for '{instance}'.")
            return stats

        # Guard: clear any quality_action on rows that are no longer universe-tagged
        # (neither keep_universe nor universe — e.g. a tag was removed from a movie like
        # Life of Pi) but quality_action was written in a previous run.
        stale_action_mask = (~universe_mask) & action_mask
        if stale_action_mask.any():
            stale_titles = df.loc[stale_action_mask, "title"].tolist()
            self.logger.log_warning(
                f"[Universe] Clearing stale quality_action on {len(stale_titles)} non-universe "
                f"row(s): {stale_titles[:10]}"
            )
            df.loc[stale_action_mask, "quality_action"] = None
            mfm.save(instance, df)
            # Re-evaluate pending after clearing stale
            action_mask  = df["quality_action"].notna()
            pending_mask = universe_mask & action_mask

        if not pending_mask.any():
            self.logger.log_info(f"[Universe] No pending quality actions for '{instance}' (after stale clear).")
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning(
                f"[Universe] Could not fetch quality profiles for '{instance}' — aborting."
            )
            return stats

        changed = False

        # Ledger: reset stale plan stamps THIS pass authored on a prior run (a movie
        # changed last run but no longer actionable would otherwise keep its stamp
        # forever); the loop re-stamps the currently-pending set. CRUCIAL: reap ONLY
        # universe-authored stamps — stamp_universe_plan writes plan_reason
        # "universe <action>". Space-pressure downgrades a bare-"universe" title under
        # pressure (reason "...deletable last-resort") in the SAME run, BEFORE this pass;
        # that downgrade must survive into the ledger even when this pass finds the title
        # already at its earned tier and never re-stamps it. Matching on the
        # universe-authored reason keeps the two managers' stamps from colliding. (Stale
        # space-pressure 'delete'/'downgrade' stamps are reset upstream each run in
        # RadarrCacheMovieFilesManager.run, so scoping here cannot leak one.)
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)
        _uni_plan = universe_mask & df["plan_reason"].isin(["universe upgrade", "universe downgrade"])
        if _uni_plan.any():
            df.loc[_uni_plan, "planned_action"]  = None
            df.loc[_uni_plan, "plan_reason"]     = None
            df.loc[_uni_plan, "plan_reclaim_gb"] = None
        _plan_changed = bool(_uni_plan.any())

        # Current-profile resolution lookup (for the grid's From->To column).
        _res_by_pid = {p["id"]: self._profile_max_resolution(p) for p in ranked_profiles}

        _rows: list[list[str]] = []
        for idx in df.index[pending_mask]:
            title      = df.at[idx, "title"] or f"movie {df.at[idx, 'movie_id']}"
            movie_id   = df.at[idx, "movie_id"]
            action     = df.at[idx, "quality_action"]
            current_qp = df.at[idx, "quality_profile_id"] if "quality_profile_id" in df.columns else None

            if pd.isna(movie_id) or pd.isna(current_qp):
                self.logger.log_warning(
                    f"[Universe] Skipping '{title}': missing movie_id or quality_profile_id."
                )
                stats["failed"] += 1
                continue

            movie_id   = int(movie_id)
            current_qp = int(current_qp)

            # Watch-likelihood gates the upgrade tier: 4K only for rewatched
            # content; untouched titles (even keep-universe) are capped below 4K.
            likelihood = watch_likelihood(df.loc[idx], config=self.config)
            if action == "upgrade":
                target_profile = self._get_target_profile(
                    ranked_profiles, "upgrade",
                    current_profile_id=current_qp,
                    min_rank=min_rank,
                    likelihood=likelihood,
                )
            else:
                # Downgrade: one resolution-tier step down, best-quality reduction,
                # runtime-sized — same logic as the movie/TV space-pressure passes.
                target_profile = self._downgrade_target(
                    df.loc[idx], ranked_profiles, current_qp, min_rank=min_rank,
                )

            if target_profile is None:
                _cur_res   = _res_by_pid.get(current_qp, 0)
                _cur_label = self._quality_label(
                    df.at[idx, "quality_name"] if "quality_name" in df.columns else None,
                    _cur_res,
                )
                _rows.append([
                    str(title)[:24],
                    "hold",
                    f"{_cur_label}->{_cur_label}" if _cur_label != "-" else "-",
                ])
                changed = True
                continue

            target_id   = target_profile["id"]
            target_name = target_profile.get("name", str(target_id))
            stat_key    = "downgraded" if action == "downgrade" else "upgraded"

            _cur_res   = _res_by_pid.get(current_qp, 0)
            _cur_label = self._quality_label(
                df.at[idx, "quality_name"] if "quality_name" in df.columns else None,
                _cur_res,
            )
            _tgt_label = self._profile_cutoff_quality_name(target_profile) or self._quality_label(
                None, self._profile_max_resolution(target_profile)
            )
            _from_to = f"{_cur_label}->{_tgt_label}" if (_cur_label != "-" or _tgt_label != "-") else "-"
            if self.dry_run:
                _rows.append([str(title)[:24], str(action), _from_to])
                # Ledger-only stamp. CRUCIAL: do NOT write quality_profile_id here —
                # persisting the speculative target would make the next REAL run think
                # the movie is already at that profile and SKIP the actual grab. We also
                # leave quality_action set (evaluate refreshes it each run).
                self._stamp_universe_plan(df, idx, action, target_profile)
                _plan_changed = True
                stats[stat_key] += 1
                continue

            # Fetch the full movie payload — Radarr requires the complete object for PUT
            movie_payload = self.radarr_api._make_request(
                instance, f"movie/{movie_id}", fallback=None
            )
            if not movie_payload or not isinstance(movie_payload, dict):
                self.logger.log_warning(
                    f"[Universe] Could not fetch movie payload for '{title}' (id={movie_id}) — skipping."
                )
                stats["failed"] += 1
                continue

            movie_payload["qualityProfileId"] = target_id

            try:
                self.radarr_api._make_request(
                    instance,
                    f"movie/{movie_id}",
                    method="PUT",
                    payload=movie_payload,
                )
                _rows.append([str(title)[:24], str(action), _from_to])
                df.at[idx, "quality_profile_id"]   = target_id
                df.at[idx, "quality_profile_name"] = target_name
                df.at[idx, "quality_action"]       = None
                self._stamp_universe_plan(df, idx, action, target_profile)
                stats[stat_key] += 1
                changed = True
            except Exception as e:
                self.logger.log_warning(
                    f"[Universe] Failed to {action} '{title}' (id={movie_id}): {e}"
                )
                stats["failed"] += 1

        # Persist: real changes in a live run, OR the ledger stamps/stale-clears in
        # dry_run (plan-only — no quality_profile_id was speculatively written above).
        if (changed and not self.dry_run) or (self.dry_run and _plan_changed):
            mfm.save(instance, df)

        # Route per-title detail into the consolidated end-of-run summary (one Radarr block,
        # one row per title) instead of dumping the grid inline. Falls back to the inline
        # grid when no run_summary collector is present.
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            _rs.add_rows("radarr", "Universe quality actions", instance,
                         ["Title", "Action", "From->To"], _rows, order=20)
        else:
            self.logger.log_grid(
                ["Title", "Action", "From->To"],
                _rows,
                title="Radarr universe quality actions" + (" [dry_run]" if self.dry_run else ""),
                cap=32,
            )

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"{prefix}[Universe] Quality action pass for '{instance}': "
            f"{stats['downgraded']} downgraded, {stats['upgraded']} upgraded, "
            f"{stats['skipped_at_limit']} at quality limit, {stats['failed']} failed."
        )

        return stats

    # ── Combined run ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_universe_quality")
    def run(
        self,
        instance: str,
        free_space_gb: float,
        downgrade_threshold_gb: float | None = None,
        upgrade_threshold_gb: float | None = None,
        min_rank: int = 0,
    ) -> dict:
        """
        Full universe quality-management pass:
          1. evaluate_quality_actions  — marks downgrade/upgrade in Parquet
          2. apply_quality_actions     — pushes profile changes to Radarr API

        Returns merged stats from both steps.
        """
        instance = self._resolve_instance(instance)
        eval_stats  = self.evaluate_quality_actions(
            instance,
            free_space_gb,
            downgrade_threshold_gb=downgrade_threshold_gb,
            upgrade_threshold_gb=upgrade_threshold_gb,
        )

        # If no universe movies found, run the tag audit to explain why
        if eval_stats.get("universe_count", 0) == 0:
            self.audit_universe_tags(instance)

        apply_stats = self.apply_quality_actions(instance, min_rank=min_rank)
        return {**eval_stats, **apply_stats}

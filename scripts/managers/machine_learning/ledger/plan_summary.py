"""
plan_summary.py — roll up the dry-run decision ledger into one readable summary.
================================================================================
After Sonarr/Radarr stamp each file's ``planned_action`` (+ reason + reclaim) and
``watchability_score`` into their Parquet caches, this reads both caches and logs
a compact "what the system would do" table — the headline value of running in
dry_run. Read-only; best-effort (never breaks the run).

RELOCATED into ledger/ (ML Step 6); ``scripts/managers/machine_learning/plan_summary.py``
is now a re-export shim (deleted at MIGRATION.md Step 10). This roll-up is the
system-level parity ORACLE for the migration.

    PlanSummary(registry=…, logger=…, config=…, global_cache=…).log()

Also the single per-run home of the calibrated-threshold shadow report
(``thresholds/report.run`` — see :meth:`log_thresholds`) and, immediately before
it, the one-shot fresh-install label reconstruction (``labels/first_run`` — see
:meth:`first_run_backfill`): this is the one place that already has both service
parquets open, runs after every phase has stamped its plans, and is otherwise
read-only by contract.
"""
from __future__ import annotations

from scripts.managers.machine_learning.ledger.pending_plan import fold_into, pending_rows


class PlanSummary:
    _SOURCES = (
        ("sonarr", "SonarrCacheEpisodeFilesManager"),
        ("radarr", "RadarrCacheMovieFilesManager"),
    )

    def __init__(self, registry=None, logger=None, config=None, global_cache=None):
        self.registry = registry
        self.logger = logger
        self.config = config
        self.global_cache = global_cache
        self._scores_by_service: dict = {}

    # ── helpers ───────────────────────────────────────────────────────────────
    def _cfg_get(self, key, default=None):
        getter = getattr(self.config, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        return default

    def _instances(self, service: str):
        insts = self._cfg_get(f"{service}_instances", {}) or {}
        return [k for k, v in insts.items()
                if k != "default_instance" and isinstance(v, dict)]

    def _mgr(self, name: str):
        if not self.registry:
            return None
        try:
            return self.registry.get("manager", name)
        except Exception:
            return None

    def _iter_frames(self):
        for service, mgr_name in self._SOURCES:
            mgr = self._mgr(mgr_name)
            if mgr is None or not hasattr(mgr, "load"):
                continue
            for inst in self._instances(service):
                try:
                    df = mgr.load(inst)
                except Exception:
                    continue
                if df is not None and not df.empty:
                    yield service, inst, df

    # ── aggregate + log ───────────────────────────────────────────────────────
    def summarize(self):
        try:
            import pandas as pd
        except Exception:
            return {}, []

        agg: dict = {}          # action -> [count, reclaim_gb_sum]
        # action -> service -> instance -> [count, gb]. Populated from the SAME groupby
        # pass as `agg`, so the subtotals below can never disagree with their parent row.
        detail: dict = {}
        scores: list = []
        by_service: dict = {}   # service -> scores, for the threshold shadow report
        for _service, _inst, df in self._iter_frames():
            if "planned_action" in df.columns:
                pa = df["planned_action"]
                sub = df[pa.notna() & (pa.astype(str) != "")]
                if not sub.empty:
                    rc = (pd.to_numeric(sub["plan_reclaim_gb"], errors="coerce")
                          if "plan_reclaim_gb" in sub.columns else None)
                    for action, grp in sub.groupby("planned_action"):
                        a = agg.setdefault(str(action), [0, 0.0])
                        a[0] += len(grp)
                        _gb = (float(rc.loc[grp.index].fillna(0).sum())
                               if rc is not None else 0.0)
                        a[1] += _gb
                        d = (detail.setdefault(str(action), {})
                                   .setdefault(str(_service), {})
                                   .setdefault(str(_inst), [0, 0.0]))
                        d[0] += len(grp)
                        d[1] += _gb
            if "watchability_score" in df.columns:
                vals = list(pd.to_numeric(df["watchability_score"], errors="coerce").dropna())
                scores += vals
                by_service.setdefault(_service, []).extend(vals)
        self._scores_by_service = by_service
        # Fold in planned actions on media with NO Parquet row -- acquisitions, which
        # cannot be stamped because the title is not in the library yet. Without this
        # the grid omits the largest byte flow in the run while its own title claims
        # "every planned action": the 2026-08-20 run reported TOTAL +242.7 GB (reads
        # as freeing space) against a true net of roughly -68 GB. See ledger/pending_plan.
        try:
            _n = fold_into(agg, detail, pending_rows(self.global_cache))
            if _n and self.logger is not None:
                self.logger.log_debug(
                    f"[PlanSummary] folded {_n} pending (no-row) planned action(s).")
        except Exception:
            pass
        self._plan_detail = detail
        return agg, scores

    def itemize(self, cap_per_group: int = 25) -> list:
        """Per-title change plan from the decision ledger: one row per planned
        action — ``[service, instance, action, title, GB, why]`` — grouped by
        (service, instance, action) and sorted by absolute space impact within
        each group. Groups larger than ``cap_per_group`` are truncated with a
        ``… +N more`` row carrying the remainder's summed GB, so the grid stays
        readable at 400+ planned deletes. Read-only; best-effort."""
        try:
            import pandas as pd
        except Exception:
            return []

        out: list = []
        for service, inst, df in self._iter_frames():
            if "planned_action" not in df.columns:
                continue
            pa  = df["planned_action"]
            sub = df[pa.notna() & (pa.astype(str) != "")].copy()
            if sub.empty:
                continue

            if "plan_reclaim_gb" in sub.columns:
                sub["_gb"] = pd.to_numeric(sub["plan_reclaim_gb"], errors="coerce")
            else:
                sub["_gb"] = float("nan")
            sub["_absgb"] = sub["_gb"].abs().fillna(0.0)

            def _title(r) -> str:
                if service == "sonarr":
                    t = r.get("series_title") or f"series {r.get('series_id')}"
                    sn, en = r.get("season_number"), r.get("episode_number")
                    try:
                        if pd.notna(sn) and pd.notna(en):
                            return f"{t} S{int(sn):02d}E{int(en):02d}"
                    except (TypeError, ValueError):
                        pass
                    return str(t)
                return str(r.get("title") or f"movie {r.get('movie_id')}")

            for action, grp in sub.groupby(sub["planned_action"].astype(str)):
                g     = grp.sort_values("_absgb", ascending=False)
                shown = g.head(cap_per_group)
                for _, r in shown.iterrows():
                    _g = r.get("_gb")
                    out.append([
                        service, inst, str(action), _title(r)[:44],
                        (f"{float(_g):+.1f}" if _g is not None and pd.notna(_g) else "-"),
                        str(r.get("plan_reason") or "")[:48],
                    ])
                extra = len(g) - len(shown)
                if extra > 0:
                    _rem = float(g["_gb"].iloc[cap_per_group:].fillna(0).sum())
                    out.append([service, inst, str(action),
                                f"... +{extra} more", f"{_rem:+.1f}",
                                f"top {cap_per_group} by GB shown"])
        # Per-title rows for media with no Parquet row (acquisitions). Same grouping
        # and cap as above, applied per (service, instance, action) so a 97-movie add
        # wave does not bury the rest of the grid.
        try:
            _pend: dict = {}
            for r in pending_rows(self.global_cache):
                _pend.setdefault(
                    (str(r.get("service") or "?"), str(r.get("instance") or "?"),
                     str(r.get("action") or "?")), []).append(r)
            for (svc, inst, action), grp in sorted(_pend.items()):
                grp.sort(key=lambda r: -abs(float(r.get("gb") or 0.0)))
                for r in grp[:cap_per_group]:
                    out.append([svc, inst, action, str(r.get("title") or "")[:44],
                                f"{float(r.get('gb') or 0.0):+.1f}",
                                str(r.get("reason") or "")[:48]])
                _extra = len(grp) - min(len(grp), cap_per_group)
                if _extra > 0:
                    _rem = sum(float(r.get("gb") or 0.0) for r in grp[cap_per_group:])
                    out.append([svc, inst, action, f"... +{_extra} more",
                                f"{_rem:+.1f}", f"top {cap_per_group} by GB shown"])
        except Exception:
            pass
        return out

    # ── next-watch reminder (the machine_learning/next_watch consumer) ────────
    def next_watch_rows(self, limit: int = 15) -> list:
        """``[[Kind, Title, Intent, Who, Why], …]`` — OWNED + WATCHLISTED + NEVER PLAYED.

        The user-facing consumer for ``machine_learning/next_watch``, which was fully built
        and tested but had ZERO callers: a ranker nobody can see is indistinguishable from
        a ranker that does not work. This is the minimum honest wiring — one table at the
        end of a run — rather than a new shelf, which would need its own per-profile age
        gate, Plex write-back and measurement loop (i.e. a second Hidden Gems).

        Scope, stated plainly rather than papered over: MOVIES are ranked by
        :func:`rank_next_watch` over the Plex union, because ``movie_files`` carries
        ``tmdb_id`` and the union is keyed the same way. SERIES are listed from the
        Group-A5 shield column (``watchlist_hold``) instead, because ``episode_files``
        carries no TVDb id to join the union on — so they get the same "owned, asked for,
        never started" verdict without an intent number. Read-only; best-effort."""
        try:
            import pandas as pd
            from scripts.managers.machine_learning.next_watch import rank_next_watch
        except Exception:
            return []
        union = []
        if self.global_cache is not None:
            try:
                union = self.global_cache.get("plex/watchlist/union") or []
            except Exception:
                union = []

        owned_unplayed: set = set()
        series_rows: list = []
        for service, _inst, df in self._iter_frames():
            try:
                if service == "radarr" and "tmdb_id" in df.columns:
                    sub = df
                    if "has_file" in df.columns:
                        sub = sub[sub["has_file"].fillna(True).astype(bool)]
                    played = pd.Series(False, index=sub.index)
                    if "is_watched" in sub.columns:
                        played = played | sub["is_watched"].fillna(False).astype(bool)
                    if "watch_count" in sub.columns:
                        played = played | (pd.to_numeric(sub["watch_count"], errors="coerce")
                                           .fillna(0) > 0)
                    for t in pd.to_numeric(sub.loc[~played, "tmdb_id"],
                                           errors="coerce").dropna().astype(int):
                        owned_unplayed.add(str(t))
                elif service == "sonarr" and "watchlist_hold" in df.columns:
                    held = df[df["watchlist_hold"].fillna(False).astype(bool)]
                    for _sid, grp in held.groupby("series_id", sort=False):
                        if "is_watched" in grp.columns and \
                                bool(grp["is_watched"].fillna(False).astype(bool).any()):
                            continue                        # already started → not a reminder
                        title = next((str(v) for v in grp.get("series_title", []) if v), None)
                        by = next((str(v) for v in grp.get("watchlist_hold_by", []) if v), "")
                        series_rows.append(["series", (title or f"series {_sid}")[:44],
                                            "-", by[:20], "owned, never started"])
            except Exception:
                continue

        rows: list = []
        for r in rank_next_watch(union, owned_ids=owned_unplayed):
            if not r.get("owned"):
                continue                                    # unowned watchlist → acquisition's job
            rows.append(["movie", str(r.get("title") or r.get("primary_id"))[:44],
                         f"{r.get('intent', 0):.0f}",
                         ", ".join(r.get("watchlisted_by") or [])[:20],
                         "owned, never played"])
        rows.extend(series_rows)
        return rows[:max(0, int(limit))]

    def log_next_watch(self, limit: int = 15) -> int:
        """Render :meth:`next_watch_rows` as one end-of-run table. Silent when empty."""
        if not self.logger:
            return 0
        try:
            rows = self.next_watch_rows(limit=limit)
        except Exception as e:
            self.logger.log_debug(f"[Plan] next-watch reminder skipped: {e}")
            return 0
        if not rows:
            return 0
        try:
            self.logger.log_grid(
                ["Kind", "Title", "Intent", "Asked by", "Why"], rows,
                title="Next watch — you own it, you asked for it, you have never played it",
                cap=48,
            )
        except Exception as e:
            self.logger.log_debug(f"[Plan] next-watch table skipped: {e}")
            return 0
        return len(rows)

    # ── first-run label reconstruction (ML Stage 1 backfill) ──────────────────
    def first_run_backfill(self) -> dict:
        """On a FRESH install only: replay the Tautulli history the household
        already owns into reconstructed snapshots, once, so the calibrated
        threshold report below has labels to work with on day one instead of a
        horizon later (``labels/first_run``).

        This is the only write on this class's path, and the only place in the
        run where it can happen: the truncated replay needs Tautulli history,
        ``movie_files.parquet`` and the daemon buckets all warm, which is true at
        the END of a run and not at startup or during onboarding. Guarded by a
        one-shot marker, bounded to a capped grid, config-gated
        (``ml.snapshots.backfill_on_first_run``, default true) and fully wrapped
        — a failure is a logged no-op."""
        try:
            from scripts.managers.machine_learning.labels import first_run as _first_run
            from scripts.managers.machine_learning.thresholds import registry as _t_reg
            return _first_run.maybe_backfill_on_first_run(
                self.config, global_cache=self.global_cache, logger=self.logger,
                horizon_days=_t_reg.horizon_days(self.config)) or {}
        except Exception as e:
            if self.logger:
                try:
                    self.logger.log_debug(f"[Plan] first-run backfill skipped: {e}")
                except Exception:
                    pass
            return {}

    # ── calibrated-threshold shadow report (MATH_FOUNDATION §9) ───────────────
    def log_thresholds(self) -> dict:
        """Derive every decision cutoff from calibrated P(watch within H), shrink
        it toward the hand-set constant by the evidence behind it, count the
        entities that WOULD flip, log one table and persist
        ``<cache>/ml/reports/thresholds_{date}.json``.

        Runs :meth:`first_run_backfill` first, so a brand-new install derives
        from the history it already has rather than reporting "no data" for two
        weeks; on every subsequent run that call is a marker check and returns
        immediately.

        SHADOW BY DEFAULT (``ml.thresholds.mode="shadow"``): report only, zero
        behaviour change. ``"off"`` skips the report entirely (no snapshot load).
        Fully wrapped — a failure can never affect the run."""
        self.first_run_backfill()
        try:
            from scripts.managers.machine_learning.thresholds import report as _t_report
            return _t_report.run(
                self.config, global_cache=self.global_cache, logger=self.logger,
                scores_by_service=self._scores_by_service) or {}
        except Exception as e:
            if self.logger:
                try:
                    self.logger.log_debug(f"[Plan] threshold shadow report skipped: {e}")
                except Exception:
                    pass
            return {}

    def log(self, detailed: bool = False) -> dict:
        agg, scores = self.summarize()
        if not self.logger:
            return agg
        if not agg and not scores:
            self.logger.log_debug("[Plan] No decision-ledger data yet (caches empty).")
            self.log_thresholds()
            return agg

        # ── Itemized change plan (detailed / end-of-run mode) ─────────────────
        # Rendered BEFORE the roll-up so the run closes on: every planned change
        # → the totals ledger → the score distribution.
        if detailed:
            try:
                items = self.itemize()
                if items:
                    # NOTE: log_grid's ``cap`` is the CELL-WIDTH truncation (chars),
                    # not a row cap — rows are already capped per group in itemize().
                    self.logger.log_grid(
                        ["Svc", "Instance", "Action", "Title", "GB (+free/-use)", "Why"],
                        items,
                        title="Change plan — every planned action this run",
                        cap=48,
                    )
            except Exception as e:
                self.logger.log_debug(f"[Plan] itemized change plan skipped: {e}")

        if agg:
            # MEDIA LABEL for the subtotal rows. The ledger is keyed by the *arr service,
            # which IS the media split - no extra derivation, and it cannot drift from the
            # frames the counts came from.
            _MEDIA = {"sonarr": "tv shows", "radarr": "movies"}
            _detail = getattr(self, "_plan_detail", {}) or {}
            # INDENT with NON-BREAKING spaces and an ASCII marker. Ordinary spaces are
            # collapsed by _strip_decor (re.sub(r"[ \t]{2,}", " ")) and a unicode arrow is
            # dropped by the cp1252 console encoder AFTER the column width was measured -
            # either one leaves the subtotal rows visibly short of their parent.
            _L1 = "\u00a0\u00a0> "
            _L2 = "\u00a0\u00a0\u00a0\u00a0> "
            rows = []
            net = 0.0
            total = 0
            for action in sorted(agg, key=lambda a: -agg[a][1]):
                cnt, gb = agg[action]
                # TOTAL sums the PARENT rows only - the subtotals beneath are the same
                # titles broken out, so folding them in would double-count every one.
                net += gb
                total += cnt
                rows.append([action, cnt, f"{gb:+.1f}"])
                _svcs = _detail.get(action, {})
                for _svc in sorted(_svcs, key=lambda s: -sum(v[1] for v in _svcs[s].values())):
                    _insts = _svcs[_svc]
                    _c = sum(v[0] for v in _insts.values())
                    _g = sum(v[1] for v in _insts.values())
                    rows.append([f"{_L1}{_MEDIA.get(_svc, _svc)}", _c, f"{_g:+.1f}"])
                    # Instance rows only where the media type actually has more than one -
                    # a lone Sonarr instance repeating its parent row is just noise.
                    if len(_insts) > 1:
                        for _inst in sorted(_insts, key=lambda i: -_insts[i][1]):
                            _ic, _ig = _insts[_inst]
                            rows.append([f"{_L2}{_inst}", _ic, f"{_ig:+.1f}"])
            rows.append(["TOTAL", total, f"{net:+.1f}"])
            try:
                # log_grid, not log_table: it sizes each column to its own widest cell and
                # pads with NBSP, which is the only path here whose alignment survives
                # _strip_decor intact.
                self.logger.log_grid(
                    ["planned action", "count", "GB (+free/-use)"],
                    rows, title="Dry-run plan ledger", cap=24,
                )
            except Exception:
                self.logger.log_info(f"[Plan] {dict((a, agg[a][0]) for a in agg)} (net {net:+.1f} GB)")

        if scores:
            import statistics as _st
            self.logger.log_info(
                f"[Plan] Watchability scores: n={len(scores)}, "
                f"min={min(scores):.0f}, median={_st.median(scores):.0f}, "
                f"max={max(scores):.0f} (lower = first to downgrade/delete)"
            )

        # The forward half of the ledger: what the household ASKED for and still has not
        # played. Rendered after the "what we would change" tables because it is the one
        # thing on this surface addressed to the viewer rather than the operator.
        self.log_next_watch()

        # What those same scores would mean as calibrated probabilities — the
        # shadow half of the §9 threshold-derivation rollout. Report only.
        self.log_thresholds()

        # Has the score AXIS moved under the cutoffs? Report only, and separate
        # from log_thresholds on purpose: that asks "what should the cutoff be?",
        # this asks "does the cutoff still mean what it meant?". A household with
        # no calibrator still needs the second question answered.
        self.log_axis_drift()
        return agg

    # ── axis drift ───────────────────────────────────────────────────

    #: Where the reviewed baseline lives. ONE key, not one per run — see the
    #: bootstrap rule in :meth:`log_axis_drift`.
    _ANCHOR_KEY = "ml/thresholds/axis_anchor"

    def _v2_cutoffs(self, service: str) -> dict:
        """The live cutoffs for *service* that read the PERSISTED
        ``watchability_score`` column — i.e. the ones on AXIS V2, which is the
        axis the scores collected here actually sit on.

        The AXIS LEGACY specs (``repair/anomaly.py``'s ``_score_owned``) must be
        excluded: their scores are recomputed on the fly from raw Radarr dicts
        and never enter this parquet, so comparing them against this distribution
        would report a translation that is really a change of population — the
        exact mistake ``drift.py``'s integration note warns about.

        Detection is a string check on ``spec.note`` because the axis is recorded
        there and nowhere structured. That is fragile; a ``ThresholdSpec.axis``
        field would be better (GLD-DRIFT-02).
        """
        try:
            from scripts.managers.machine_learning.thresholds.registry import (
                THRESHOLD_SPECS, resolve_constant,
            )
        except Exception:
            return {}
        out = {}
        for spec in THRESHOLD_SPECS:
            if spec.service != service or not str(spec.note or "").startswith("AXIS V2"):
                continue
            try:
                out[spec.name] = resolve_constant(spec, self.config)
            except Exception:
                continue
        return out

    def log_axis_drift(self):
        """Compare this run's score distribution against the reviewed baseline and
        log the verdict. Read-only, best-effort, never breaks the run.

        WHY THIS EXISTS: Group D v2 translated the persisted axis ~13 points and
        nothing noticed. The delete family needed re-anchoring 20 -> 17, and
        ``likelihood.untouched_base`` 12 -> 25 — the latter only after untouched
        titles reaching 1080p had collapsed 456 -> 8 (-98.2%), found by hand. See
        ``thresholds/drift.py`` and ``registry.py``'s delete block.

        THE ANCHOR IS NEVER AUTO-REFRESHED. It is captured once, on the first run
        that finds none, and then left alone: a baseline that regenerates each run
        measures nothing, because it drifts with the thing it is watching. After a
        DELIBERATE re-anchor, delete the key so the next run recaptures it.

        The bootstrap run therefore reports no verdict, and says so — a captured
        baseline is not a clean bill of health, and the distribution it records
        may already be drifted.
        """
        if not self.logger or not self.global_cache:
            return
        scores_by_service = getattr(self, "_scores_by_service", None) or {}
        if not scores_by_service:
            return

        try:
            from scripts.managers.machine_learning.thresholds import drift
        except Exception as e:
            self.logger.log_debug(f"[AxisDrift] unavailable: {e}")
            return

        try:
            stored = self.global_cache.get(self._ANCHOR_KEY)
        except Exception:
            stored = None
        stored = stored if isinstance(stored, dict) else {}

        fresh = {}
        for service, scores in sorted(scores_by_service.items()):
            cuts = self._v2_cutoffs(service)
            if not cuts:
                continue
            label = f"{service}.persisted_watchability"
            anchor = stored.get(label)

            if not isinstance(anchor, dict) or not anchor.get("fingerprint"):
                # BOOTSTRAP — capture, do not judge.
                fresh[label] = drift.build_anchor(scores, cuts, label=label)
                self.logger.log_info(
                    f"[AxisDrift] {label}: baseline CAPTURED (n={len(scores)}) — no "
                    f"comparison this run. This records the CURRENT distribution, "
                    f"which is not the same as a reviewed one. Re-capture (delete "
                    f"'{self._ANCHOR_KEY}') after any deliberate re-anchor."
                )
                continue

            try:
                verdict = drift.assess(drift.compare(anchor, scores, cutoffs=cuts))
            except Exception as e:
                self.logger.log_debug(f"[AxisDrift] {label} skipped: {e}")
                continue

            emit = (self.logger.log_warning if verdict.get("drifted")
                    else self.logger.log_info)
            for line in drift.format_lines(verdict):
                emit(line)
            if verdict.get("drifted"):
                emit(
                    "[AxisDrift] A cutoff no longer admits the share it was chosen "
                    "to admit. Re-derive the affected constants (registry.py's "
                    "delete block documents the reconstruction) and CHECK "
                    "likelihood.untouched_base too — it is on a different scale and "
                    "has no ThresholdSpec, so a specs-only pass will miss it."
                )

        if fresh:
            try:
                self.global_cache.set(self._ANCHOR_KEY, {**stored, **fresh})
            except Exception as e:
                self.logger.log_debug(f"[AxisDrift] baseline not persisted: {e}")

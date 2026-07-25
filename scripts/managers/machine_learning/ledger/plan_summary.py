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

    PlanSummary(registry=…, logger=…, config=…).log()
"""
from __future__ import annotations


class PlanSummary:
    _SOURCES = (
        ("sonarr", "SonarrCacheEpisodeFilesManager"),
        ("radarr", "RadarrCacheMovieFilesManager"),
    )

    def __init__(self, registry=None, logger=None, config=None):
        self.registry = registry
        self.logger = logger
        self.config = config

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
        scores: list = []
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
                        if rc is not None:
                            a[1] += float(rc.loc[grp.index].fillna(0).sum())
            if "watchability_score" in df.columns:
                scores += list(pd.to_numeric(df["watchability_score"], errors="coerce").dropna())
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
        return out

    def log(self, detailed: bool = False) -> dict:
        agg, scores = self.summarize()
        if not self.logger:
            return agg
        if not agg and not scores:
            self.logger.log_debug("[Plan] No decision-ledger data yet (caches empty).")
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
            rows = []
            net = 0.0
            total = 0
            for action in sorted(agg, key=lambda a: -agg[a][1]):
                cnt, gb = agg[action]
                net += gb
                total += cnt
                rows.append([action, cnt, f"{gb:+.1f}"])
            rows.append(["TOTAL", total, f"{net:+.1f}"])
            try:
                self.logger.log_table(
                    ["planned action", "count", "GB (+free/-use)"],
                    rows, title="Dry-run plan ledger",
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
        return agg

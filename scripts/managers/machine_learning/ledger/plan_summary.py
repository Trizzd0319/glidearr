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

    def log(self) -> dict:
        agg, scores = self.summarize()
        if not self.logger:
            return agg
        if not agg and not scores:
            self.logger.log_debug("[Plan] No decision-ledger data yet (caches empty).")
            return agg

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

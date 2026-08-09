"""
reorganize_config.py — one-shot config.json reorganizer (GLD-ACQ-24 enablement).
================================================================================
Rewrites scripts/support/config/config.json with keys grouped in an obvious,
stable order and adds the ``cold_tv_reclaim`` block (enabled). SAFE BY
CONSTRUCTION:

  * Timestamped backup of the ORIGINAL BYTES is written first
    (config.json.bak-YYYYMMDD-HHMMSS).
  * Every original key's VALUE is asserted deep-equal after the rewrite —
    ordering is the only change; exactly ONE key is added.
  * Any key this script's grouping doesn't know about is appended (never
    dropped) and reported.
  * Idempotent: an existing ``cold_tv_reclaim`` block keeps its values.

Run once from the repo root:
    python scripts\\support\\tools\\reorganize_config.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.json"

COLD_TV_DEFAULT = {"enabled": True, "score_floor": 20, "min_owned_days": 90,
                   "grace_days": 30, "max_series_per_run": 25}

GROUPS = [
    # 1. Run mode & safety consents
    ["dry_run", "firstRunCompleted", "deletions_consent", "relocation_consent",
     "cross_instance_move_consent", "cross_instance_dedup_consent",
     "cf_sync_overwrite_consent", "backup_before_destructive",
     "backup_deep_validate", "backup_max_age_hours"],
    # 2. Services & instances
    ["radarr_instances", "sonarr_instances", "radarr_instances_categorized",
     "sonarr_instances_categorized", "tautulli", "plex", "trakt", "tvdb",
     "mal", "mdblist", "notifications", "ignored_users"],
    # 3. Library layout & routing
    ["rootFolders", "movieRootFolders", "routing", "animeGenres",
     "documentaryGenres", "realityGenres", "radarr_quality_ladder",
     "large_file_gb"],
    # 4. Space & deletion
    ["free_space_limit", "space_pressure_headroom_ratio",
     "space_coordinator_enabled", "space_exhaustive_downgrade",
     "space_downgrade_max_regrabs_per_run", "space_downgrade_retry_days",
     "space_pressure_delete_enabled", "space_pressure_include_unwatched",
     "space_pressure_score_ceiling", "space_pressure_downgrade_before_delete",
     "tv_downgrade_enabled", "tv_space_pressure_score_ceiling",
     "tv_restore_score_threshold", "size_anomaly", "cold_tv_reclaim"],
    # 5. Owned-movie lifecycle
    ["owned_monitor_policy", "owned_monitor_score_threshold",
     "owned_demote_enabled", "owned_demote_score_threshold",
     "owned_demote_dwell_days", "owned_delete_enabled",
     "owned_delete_dwell_days", "owned_delete_min_dwell_days",
     "owned_restore_score_threshold", "owned_restore_min_age_days"],
    # 6. Series lifecycle
    ["series_monitor_score_threshold", "series_demote_score_threshold",
     "series_demote_dwell_days"],
    # 7. Watch tracking & retention
    ["watched_threshold", "episode_retention", "saga_retention"],
    # 8. Acquisition & pilots
    ["acquisition", "jit_per_episode_tiers", "pilot_floor_climb",
     "pilot_interactive", "pilot_best_tier_first", "pilot_hold_at_floor",
     "english_dub", "calendar"],
    # 9. Scoring & ML
    ["scoring", "watch_likelihood", "people_matrix", "ml"],
    # 10. Writeback
    ["trakt_writeback", "mal_writeback"],
    # 11. Daemons
    ["daemons"],
]


def main() -> int:
    raw = CONFIG.read_text(encoding="utf-8")
    cfg = json.loads(raw)          # strict parse of the live file

    new: dict = {}
    placed: set = set()
    for grp in GROUPS:
        for k in grp:
            if k == "cold_tv_reclaim":
                new[k] = cfg.get(k, dict(COLD_TV_DEFAULT))   # idempotent
                placed.add(k)
            elif k in cfg:
                new[k] = cfg[k]
                placed.add(k)

    leftovers = [k for k in cfg if k not in placed]
    for k in leftovers:            # NEVER drop an unknown key — append + report
        new[k] = cfg[k]

    # ── Safety assertions: values untouched, exactly one addition (max) ──────
    assert {k: new[k] for k in cfg} == cfg, "VALUE DRIFT — aborting, nothing written"
    added = set(new) - set(cfg)
    assert added <= {"cold_tv_reclaim"}, f"unexpected additions: {added}"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG.with_name(f"config.json.bak-{stamp}")
    backup.write_bytes(raw.encode("utf-8"))

    out = json.dumps(new, indent=4, ensure_ascii=False) + "\n"
    assert json.loads(out) == new  # round-trip
    CONFIG.write_text(out, encoding="utf-8")

    # ── Post-write verification against the backup ───────────────────────────
    re_cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    re_old = json.loads(backup.read_text(encoding="utf-8"))
    assert {k: re_cfg[k] for k in re_old} == re_old, "POST-WRITE MISMATCH"

    print(f"config.json reorganized: {len(re_cfg)} keys in 11 groups "
          f"({len(re_old)} originals value-identical"
          f"{', +cold_tv_reclaim ENABLED' if added else ', cold_tv_reclaim already present'}"
          f"{f'; {len(leftovers)} unknown key(s) preserved at end: {leftovers}' if leftovers else ''}). "
          f"Backup: {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

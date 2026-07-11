"""sonarr_upgrade_pilots_720.py -- raise EXISTING sub-720 pilots to 720p (never 1080), in place.

Policy: every TV pilot (S01E01 taster) sits at 720p, NOT 1080p, UNTIL the series earns a watchability
score; after scoring the normal watch-based upgrade path lifts it. New pilots already floor at 720
(resolver show-add cap + pilot interactive search at floor_res=720, held by jit pilot_floor_hold). This
tool fixes the EXISTING library: genuine on-disk sub-720 pilot stubs that predate the floor.

For each target it (1) reprofiles the series to its FAMILY's true 720 cap so it physically cannot grab
>720, then (2) EpisodeSearches ONLY the pilot episode so Sonarr UPGRADES 480/576 -> 720 in place. There
is NO delete: if no >=720 release exists the existing file is kept (soft floor -- SD-only shows are never
orphaned). SeriesSearch is deliberately avoided (it could grab a whole monitored season).

TARGET (all must hold -- conservative; the efc guard is what stops us capping a full 1080 series that
merely owns a 480p S01E01):
  - on-disk sub-720 pilot:   is_pilot & episode_file_id present & 0 <= resolution < 720   (from parquet)
  - genuine stub:            LIVE statistics.episodeFileCount <= --max-owned-eps (default 1)
  - NOT earned:              series not watched AND watchability_score < --scored-cap (default 75)
  - NOT keep-tagged:         keep_policy not in {keep_series, keep_season} and no keep_quality tag
  - family target exists:    anime -> [Anime] HD-720p ; live-action -> HD-720p (resolved by NAME, live)

Two work groups (both handled by default so "all pilots" reach 720):
  R  reprofile+search -- current profile can grab >720  (the "not 1080" fix)
  S  search-only      -- already on a <=720 cap but the file is still sub-720 (nudge to 720)

On --confirm a LARGE batch is spilled to the background pilot-search daemon (mode ``pilot_720``, gated by
``daemons.pilot_search.{enabled,threshold}``) which reprofiles + EpisodeSearches in paced chunks,
cooperatively yielding to time-sensitive JIT grabs and resuming from a ledger — instead of blasting
thousands of searches into Sonarr's queue at once. A small batch (or a disabled daemon) runs inline
through the same shared ``run_pilot_720_upgrade`` core.

DRY-RUN BY DEFAULT (prints the plan, counts, exclusions, a sample); --confirm to execute. Refuses to run
while a main.py run is active (would race the run's series cache/PUTs).

    python -m scripts.support.tools.sonarr_upgrade_pilots_720                 # dry-run (no writes)
    python -m scripts.support.tools.sonarr_upgrade_pilots_720 --confirm       # EXECUTE
    python -m scripts.support.tools.sonarr_upgrade_pilots_720 --no-search     # reprofile only
    python -m scripts.support.tools.sonarr_upgrade_pilots_720 --reprofiled-only  # skip group S
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader           # noqa: E402
from scripts.managers.factories.daemons import pilot_jobs                           # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH, PILOT_SPILL_THRESHOLD  # noqa: E402
from scripts.managers.factories.daemons.supervisor import PilotSearchDaemonSupervisor  # noqa: E402
# Reuse arr_rebuild's HTTP helpers (generous timeout + backoff retry a busy, re-scoring Sonarr needs)
# and force_pilot_research's run-sentinel so we never race a live main.py run.
from scripts.support.tools.arr_rebuild import _request, _get                       # noqa: E402
from scripts.support.tools.force_pilot_research import _run_active                 # noqa: E402
from scripts.managers.machine_learning.acquisition.pilot_stepping import profile_max_resolution  # noqa: E402
from scripts.managers.services.sonarr.cache.pilot_720_upgrade import run_pilot_720_upgrade  # noqa: E402


class _ToolLog:
    """Minimal logger the shared core + daemon supervisor expect, printing to the tool's stdout."""
    def log_info(self, m="", *a, **k): print(m)
    def log_warning(self, m="", *a, **k): print(m)
    def log_debug(self, *a, **k): pass

_CACHE = _REPO_ROOT / "scripts" / "support" / "cache"
FLOOR = 720
LIVE_TARGET = "HD-720p"               # live-action 720 cap (resolved by NAME at runtime)
ANIME_TARGET = "[Anime] HD-720p"      # anime 720 cap (x265-OK, anime-CF scored)
FREEZE_POLICY = {"keep_series", "keep_season"}
FREEZE_TAGS = {"keep_quality", "keep-quality", "keepquality"}
BATCH = 200                           # series ids per editor PUT (reprofile-only path)


def _resolve(cfg, instance):
    inst = ((cfg.get("sonarr_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _profile_by_name(profiles, name):
    tgt = name.strip().lower()
    for p in profiles:
        if str(p.get("name") or "").strip().lower() == tgt:
            return p
    return None


def _candidates(df, scored_cap):
    """series_id -> {title, res, season, episode}: genuine on-disk sub-720 pilots that have NOT earned an
    upgrade. Watched is judged at the SERIES level (any owned episode watched) so we never cap a series
    someone is actually watching; score/keep read from the pilot row (both are series-grain)."""
    is_pilot = df["is_pilot"].fillna(False).astype(bool)
    p = df[is_pilot & df["episode_file_id"].notna()].copy()
    p = p[p["resolution"].notna() & (p["resolution"] >= 0) & (p["resolution"] < FLOOR)]
    watched_sids = set(df.loc[df["is_watched"].fillna(False).astype(bool), "series_id"].astype(int))
    out = {}
    for _, r in p.iterrows():
        sid = int(r["series_id"])
        if sid in watched_sids:
            continue
        if int(r.get("watchability_score") or 0) >= scored_cap:
            continue
        if str(r.get("keep_policy") or "") in FREEZE_POLICY:
            continue
        out[sid] = {
            "title": r.get("series_title"),
            "res": int(r["resolution"]),
            "season": None if pd.isna(r.get("season_number")) else int(r["season_number"]),
            "episode": None if pd.isna(r.get("episode_number")) else int(r["episode_number"]),
        }
    return out


def run(cfg, args) -> bool:
    base, key = _resolve(cfg, args.instance)
    if not (base and key):
        print(f"sonarr/{args.instance}: not configured (no base_url/api).")
        return False

    pq = _CACHE / "sonarr" / args.instance / "episode_files.parquet"
    if not pq.exists():
        print(f"ABORT: parquet not found: {pq}")
        return False
    df = pd.read_parquet(pq)
    cands = _candidates(df, args.scored_cap)

    print("=" * 78)
    print(f"### sonarr/{args.instance}  ({base})")
    print(f"### MODE: {'APPLY (--confirm)' if args.confirm else 'DRY-RUN (no writes)'}")
    print(f"### on-disk sub-720 pilots not-yet-earned (from parquet): {len(cands)}")
    print("=" * 78)

    profiles = _get(base, key, "qualityprofile")
    by_id = {p["id"]: p for p in profiles}
    pid_name = {p["id"]: p["name"] for p in profiles}
    live_tgt = _profile_by_name(profiles, args.live_target)
    anime_tgt = _profile_by_name(profiles, args.anime_target)
    if live_tgt is None:
        print(f"ABORT: live-action target profile {args.live_target!r} not found on {args.instance}.")
        return False
    live_id, live_cap = live_tgt["id"], profile_max_resolution(live_tgt)
    anime_id = anime_tgt["id"] if anime_tgt else None
    anime_cap = profile_max_resolution(anime_tgt) if anime_tgt else FLOOR
    print(f"targets: live-action -> id {live_id} {args.live_target!r} (cap {live_cap}p); "
          f"anime -> {('id ' + str(anime_id) + ' ' + repr(args.anime_target)) if anime_id else 'MISSING (anime skipped)'}")

    # freeze tag ids (keep_quality family) from the live tag list
    try:
        freeze_tag_ids = {t["id"] for t in _get(base, key, "tag")
                          if str(t.get("label") or "").strip().lower() in FREEZE_TAGS}
    except Exception:
        freeze_tag_ids = set()

    series = _get(base, key, "series")
    live_by_id = {s["id"]: s for s in series}

    plan_R, plan_S = [], []            # (sid, target_id) reprofile+search / search-only
    excl = Counter()
    for sid, info in cands.items():
        s = live_by_id.get(sid)
        if s is None:
            excl["not_in_live_series"] += 1
            continue
        efc = (s.get("statistics") or {}).get("episodeFileCount") or 0
        if efc > args.max_owned_eps:
            excl["full_series(efc>max)"] += 1
            continue
        if freeze_tag_ids and set(s.get("tags") or []) & freeze_tag_ids:
            excl["keep_quality_tagged"] += 1
            continue
        is_anime = s.get("seriesType") == "anime"
        if is_anime and anime_id is None:
            excl["no_anime_720_profile"] += 1
            continue
        target_id = anime_id if is_anime else live_id
        target_cap = anime_cap if is_anime else live_cap
        cur = by_id.get(s.get("qualityProfileId"))
        cur_cap = profile_max_resolution(cur) if cur else 0
        rec = {
            "sid": sid, "title": info["title"], "res": info["res"],
            "family": "anime" if is_anime else "live", "target_id": target_id,
            "cur": pid_name.get(s.get("qualityProfileId"), s.get("qualityProfileId")),
            "cur_id": s.get("qualityProfileId"),
            "season": info["season"], "episode": info["episode"],
        }
        if cur_cap > target_cap:
            plan_R.append(rec)
        elif s.get("qualityProfileId") == target_id or cur_cap <= FLOOR:
            plan_S.append(rec)
        else:                          # cap between target and 720 (shouldn't happen with 720 targets)
            plan_R.append(rec)

    if args.reprofiled_only:
        plan_S = []
    if args.limit:
        plan_R = plan_R[: args.limit]
        plan_S = plan_S[: max(0, args.limit - len(plan_R))]

    search_set = plan_R + ([] if args.no_search else plan_S)
    _report(plan_R, plan_S, excl, args)

    if not args.confirm:
        print("\n(dry-run -- nothing changed. Re-run with --confirm to execute.)")
        return True

    # ---- EXECUTE ----
    # --no-search: just reprofile the >720 stubs to their 720 cap now (cheap, immediate); the normal
    # run's pass / a later --confirm queues the searches. No daemon needed for a pure reprofile.
    if args.no_search:
        by_target = defaultdict(list)
        for rec in plan_R:
            by_target[rec["target_id"]].append(rec["sid"])
        moved = 0
        for tid, sids in by_target.items():
            for i in range(0, len(sids), BATCH):
                chunk = sids[i:i + BATCH]
                _request("PUT", base, key, "series/editor",
                         json_body={"seriesIds": chunk, "qualityProfileId": tid})
                moved += len(chunk)
        print(f"  OK: reprofiled {moved} series to their 720 cap (--no-search: no EpisodeSearch queued).")
        return True

    # The reprofile + paced EpisodeSearch both run in the shared run_pilot_720_upgrade core: spill a
    # LARGE batch to the background pilot-search daemon (mode pilot_720 — paced chunk-by-chunk, yields
    # to time-sensitive JIT grabs, resumable) so we never blast thousands of searches into Sonarr's
    # queue at once. A small batch (or a disabled daemon) runs inline through the same core.
    items = [{"series_id": rec["sid"], "season": rec["season"], "episode": rec["episode"],
              "series_title": rec["title"], "target_profile_id": rec["target_id"],
              "current_profile_id": rec["cur_id"]} for rec in search_set]
    if not items:
        print("  nothing to upgrade.")
        return True
    if _maybe_offload_pilot_720(cfg, args.instance, items):
        return True
    _inline_pilot_720(base, key, args.instance, items)
    return True


def _daemon_cfg(cfg):
    return ((cfg.get("daemons", {}) or {}).get("pilot_search", {}) or {})


def _maybe_offload_pilot_720(cfg, instance, items) -> bool:
    """Spill a large pilot-720 batch to the standalone pilot-search daemon (mode 'pilot_720') so the
    searches drain OUT-OF-PROCESS, paced and yielding to JIT. Returns True when enqueued AND the daemon
    is running (caller is done). False — caller runs the capped inline path — when the daemon is
    disabled, the batch is at/below the spill threshold, or enqueue/spawn fails (the job is rolled back
    so we never both queue AND run inline)."""
    dcfg = _daemon_cfg(cfg)
    if not dcfg.get("enabled", True):
        return False
    try:
        threshold = int(dcfg.get("threshold", PILOT_SPILL_THRESHOLD))
    except (TypeError, ValueError):
        threshold = PILOT_SPILL_THRESHOLD
    if len(items) <= max(0, threshold):
        return False
    try:
        job = {"version": 1, "mode": "pilot_720", "instance": instance,
               "items": items, "cooldown_days": 7, "run_pid": os.getpid()}
        path = pilot_jobs.enqueue(instance, job)
        try:
            PilotSearchDaemonSupervisor(logger=_ToolLog()).ensure_running()
        except Exception:
            pilot_jobs.remove(path)          # don't orphan a job AND run inline (a double-search)
            raise
        print(f"  spilled {len(items)} pilot(s) to the pilot-search daemon (mode pilot_720, batch "
              f"> {threshold}); job {path.name}, log: pilot_search_daemon.log.")
        print("  The daemon reprofiles + EpisodeSearches in paced chunks, yielding to JIT grabs.")
        return True
    except Exception as e:
        print(f"  could not offload to the daemon ({e}); running the inline path instead.")
        return False


def _inline_pilot_720(base, key, instance, items):
    """Run the batch through the shared core in-process (small batch / daemon disabled)."""
    def _mk(inst, endpoint, method="GET", payload=None, fallback=None):
        try:
            if method == "GET":
                return _get(base, key, endpoint)
            r = _request(method, base, key, endpoint, json_body=payload,
                         retries=1 if method == "POST" else 4)
            return (r.json() if r.content else {}) if r is not None else fallback
        except Exception:
            return fallback

    result = run_pilot_720_upgrade(make_request=_mk, logger=_ToolLog(), global_cache=None,
                                   instance=instance, items=items, dry_run=False)
    print(f"  OK (inline): {result['reprofiled']} reprofiled, {result['searched']} searched, "
          f"{result['unresolved']} unresolved.")
    print("  Sonarr upgrades 480/576 -> 720 in place; no >=720 release => existing file kept (no orphan).")


def _report(plan_R, plan_S, excl, args):
    print(f"\nPLAN (720 cap = 'not 1080 until scored'):")
    print(f"  R  reprofile + search  (current profile can grab >720): {len(plan_R)}")
    print(f"  S  search-only         (already <=720 cap, file still sub-720): "
          f"{'SKIPPED (--reprofiled-only)' if args.reprofiled_only else len(plan_S)}")
    acted = plan_R + ([] if (args.no_search or args.reprofiled_only) else plan_S)
    fam = Counter(r["family"] for r in (plan_R + plan_S))
    res = Counter(r["res"] for r in (plan_R + plan_S))
    cur = Counter(r["cur"] for r in plan_R)
    print(f"  by family: {dict(fam)}   by current file res: {dict(res)}")
    if cur:
        print(f"  reprofile FROM (current profile -> 720 cap): {dict(cur)}")
    if excl:
        print(f"  excluded: {dict(excl)}")
    print(f"  => would reprofile {len(plan_R)}, search {0 if args.no_search else len(acted)} pilot(s).")
    sample = (plan_R + plan_S)[:25]
    if sample:
        print("  sample (up to 25):")
        for r in sample:
            grp = "R" if r in plan_R else "S"
            print(f"    [{grp}] {str(r['title'])[:48]:48s} {r['res']}p  {r['family']:5s} "
                  f"cur={r['cur']} -> id {r['target_id']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Upgrade existing sub-720 TV pilots to 720p (never 1080), in place.")
    ap.add_argument("--instance", default="standard", help="Sonarr instance name (default: standard).")
    ap.add_argument("--max-owned-eps", type=int, default=1,
                    help="Only act on stubs owning <= this many episode files (default 1; the guard that "
                         "stops capping a full series with a 480p pilot).")
    ap.add_argument("--scored-cap", type=int, default=75,
                    help="Skip series whose watchability_score >= this (already earned upgrades; default 75).")
    ap.add_argument("--reprofiled-only", action="store_true",
                    help="Only touch series that need a reprofile (>720 -> 720); skip already-capped search-only.")
    ap.add_argument("--no-search", action="store_true", help="Reprofile only; do not queue EpisodeSearch.")
    ap.add_argument("--limit", type=int, default=0, help="Cap number of series acted on (0 = no cap).")
    ap.add_argument("--live-target", default=LIVE_TARGET, help=f"Live-action 720 profile name (default {LIVE_TARGET!r}).")
    ap.add_argument("--anime-target", default=ANIME_TARGET, help=f"Anime 720 profile name (default {ANIME_TARGET!r}).")
    ap.add_argument("--confirm", action="store_true", help="EXECUTE the writes (default: dry-run).")
    args = ap.parse_args()

    active, detail = _run_active()
    if active:
        print(f"ABORT: {detail}. Let the run finish, then re-run this.")
        return 1

    cfg = ConfigLoader(CONFIG_PATH).load()
    ok = run(cfg, args)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

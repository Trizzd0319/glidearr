"""sonarr_regrab_deleted_pilots.py — re-acquire deleted PILOT (S01E01) files at the 720 cap.

After deleting over-cap (>720) pilots, this recovers the exact set from Sonarr history and re-grabs
them capped at 720: it reads ``episodeFileDeleted`` history events (so it needs no saved list), keeps
the pilots that are STILL missing, then for each:

    1. reprofile the series -> a 720 cap profile (--target-id, default 3 = HD-720p) so it grabs 720 at most,
    2. RescanSeries (so Sonarr re-confirms the on-disk state),
    3. EpisodeSearch the S01E01 episode (re-grab).

Watchability can later upgrade a watched pilot (the engine bumps the profile when warranted).

Selection (all of):
  * eventType ``episodeFileDeleted`` within the window (--days / --since, default today, LOCAL),
  * episode is S01E01, series is NOT anime (no <=720 anime profile to cap onto; handle anime separately),
  * the DELETED file's resolution was > --above (default 720) -- i.e. an over-cap pilot,
  * the episode is CURRENTLY file-less (so upgrades, which replace the file, are excluded),
  * deletion reason in --reason (default 'Manual' = API/UI deletes; 'MissingFromDisk' = removed on disk;
    'any' = both).

DRY-RUN BY DEFAULT -- prints the plan and changes NOTHING. Pass --confirm to execute. ASCII-only output.

    python -m scripts.support.tools.sonarr_regrab_deleted_pilots                       # dry-run (Manual, today)
    python -m scripts.support.tools.sonarr_regrab_deleted_pilots --confirm              # reprofile+rescan+search
    python -m scripts.support.tools.sonarr_regrab_deleted_pilots --reason any --confirm # incl. disk-deleted
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH       # noqa: E402
from scripts.support.tools.arr_rebuild import _request, _get                  # noqa: E402

_DELETE_EVENT = "episodeFileDeleted"


def _resolve(cfg, instance):
    inst = ((cfg.get("sonarr_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _is_pilot(e) -> bool:
    e = e or {}
    return e.get("seasonNumber") == 1 and e.get("episodeNumber") == 1


def _res(h):
    v = (((h.get("quality") or {}).get("quality") or {})).get("resolution")
    return int(v) if isinstance(v, (int, float)) else None


def _qname(h):
    return (((h.get("quality") or {}).get("quality") or {})).get("name")


def run(cfg, instance, *, confirm, days, since, above, target_id, reasons, do_rescan, do_search) -> bool:
    base, key = _resolve(cfg, instance)
    if not (base and key):
        print(f"sonarr/{instance}: not configured (no base_url/api)."); return False

    now_local = datetime.now().astimezone()
    today_local = now_local.date()
    window_start = date.fromisoformat(since) if since else today_local - timedelta(days=max(1, days) - 1)
    start_midnight = (now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                      - timedelta(days=(today_local - window_start).days))
    since_iso = start_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_desc = f"{window_start}..{today_local}" if window_start != today_local else f"{today_local}"
    reason_desc = "any" if reasons is None else ",".join(sorted(reasons))

    print("=" * 78)
    print(f"### sonarr/{instance}  ({base})")
    print(f"### RE-GRAB deleted over-{above} PILOTS at 720  |  window {window_desc}  reason={reason_desc}")
    print(f"### target profile id={target_id}  rescan={do_rescan}  search={do_search}")
    print(f"### MODE: {'APPLY (--confirm)' if confirm else 'DRY-RUN (no writes)'}")
    print("=" * 78)

    profiles = _get(base, key, "qualityprofile")
    pid_name = {p["id"]: p["name"] for p in profiles}
    series_map = {s["id"]: s for s in _get(base, key, "series")}
    hist = _get(base, key, f"history/since?date={since_iso}&includeEpisode=true")
    if not isinstance(hist, list):
        print("Unexpected history payload; aborting."); return False

    by_ep = {}      # episodeId -> entry (dedupe repeated delete events)
    skip_hasfile = 0
    for h in hist:
        if h.get("eventType") != _DELETE_EVENT:
            continue
        e = h.get("episode") or {}
        if not _is_pilot(e):
            continue
        s = series_map.get(h.get("seriesId")) or (h.get("series") or {})
        if s.get("seriesType") == "anime":
            continue
        if (_res(h) or 0) <= above:
            continue
        reason = (h.get("data") or {}).get("reason")
        if reasons is not None and reason not in reasons:
            continue
        if e.get("episodeFileId"):       # has a file again now -> not a still-deleted pilot
            skip_hasfile += 1; continue
        eid = e.get("id")
        if eid in by_ep:
            continue
        by_ep[eid] = {"seriesId": h.get("seriesId"), "episodeId": eid,
                      "title": s.get("title"), "profileId": s.get("qualityProfileId"),
                      "res": _res(h), "qname": _qname(h), "reason": reason}

    items = list(by_ep.values())
    if not items:
        print("\nNo still-deleted over-cap pilots match -- nothing to do.")
        return True

    by_reason = Counter(d["reason"] for d in items)
    by_prof = Counter(f"{pid_name.get(d['profileId'], d['profileId'])}" for d in items)
    need_reprofile = sorted({d["seriesId"] for d in items
                             if d["profileId"] != target_id and d["seriesId"] is not None})
    series_ids = sorted({d["seriesId"] for d in items if d["seriesId"] is not None})
    ep_ids = [d["episodeId"] for d in items if d["episodeId"] is not None]

    print(f"\nMATCHED {len(items)} still-deleted over-{above} pilot(s) across {len(series_ids)} series "
          f"(skipped {skip_hasfile} that already have a file again).")
    print(f"  by deletion reason: {dict(by_reason)}")
    print(f"  by CURRENT series profile: {dict(by_prof)}")
    print(f"  PLAN: reprofile {len(need_reprofile)} series not already on id {target_id} "
          f"({pid_name.get(target_id, target_id)}); "
          f"{'RescanSeries ' + str(len(series_ids)) if do_rescan else 'no rescan'}; "
          f"{'EpisodeSearch ' + str(len(ep_ids)) + ' episode(s)' if do_search else 'no search'}.")
    print(f"  sample (up to 25):")
    for d in sorted(items, key=lambda x: (-(x["res"] or 0), x["title"] or ""))[:25]:
        print(f"    [{(str(d['res']) + 'p') if d['res'] else '?':>5}] {str(d['title'])[:46]:46s} "
              f"{str(d['qname']):18s} from {pid_name.get(d['profileId'], d['profileId'])!r:20s} ({d['reason']})")
    if len(items) > 25:
        print(f"    ... and {len(items) - 25} more")

    if not confirm:
        print(f"\n(dry-run -- nothing changed. Re-run with --confirm to execute.)")
        return True

    # 1. reprofile (bulk editor) so the re-grab caps at 720
    if need_reprofile:
        moved = 0
        for i in range(0, len(need_reprofile), 200):
            chunk = need_reprofile[i:i + 200]
            _request("PUT", base, key, "series/editor",
                     json_body={"seriesIds": chunk, "qualityProfileId": target_id})
            moved += len(chunk)
        print(f"\n  OK: reprofiled {moved} series -> id {target_id} ({pid_name.get(target_id, target_id)})")
    else:
        print(f"\n  (all matched series already on id {target_id} -- no reprofile needed)")

    # 2. RescanSeries per series (fire-and-forget; command is non-idempotent so retries=1)
    if do_rescan:
        ok = 0
        for sid in series_ids:
            try:
                _request("POST", base, key, "command",
                         json_body={"name": "RescanSeries", "seriesId": sid}, retries=1)
                ok += 1
                if ok % 50 == 0:
                    print(f"  ... RescanSeries queued {ok}/{len(series_ids)}")
            except Exception as ex:
                print(f"  ! RescanSeries failed for series {sid} ({ex})")
        print(f"  OK: queued RescanSeries for {ok} series")

    # 3. EpisodeSearch the pilots (chunked)
    if do_search:
        searched = 0
        for i in range(0, len(ep_ids), 100):
            chunk = ep_ids[i:i + 100]
            try:
                _request("POST", base, key, "command",
                         json_body={"name": "EpisodeSearch", "episodeIds": chunk}, retries=1)
                searched += len(chunk)
            except Exception as ex:
                print(f"  ! EpisodeSearch failed for a chunk ({ex})")
        print(f"  OK: queued EpisodeSearch for {searched} pilot episode(s)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-grab deleted over-cap pilots (S01E01) at the 720 profile.")
    ap.add_argument("--instance", default="standard", help="Sonarr instance (default: standard).")
    ap.add_argument("--days", type=int, default=1, help="Window in LOCAL days back from today (default 1=today).")
    ap.add_argument("--since", default=None, help="Window start YYYY-MM-DD (LOCAL); overrides --days.")
    ap.add_argument("--above", type=int, default=720, help="Only pilots whose DELETED file resolution was > this (default 720).")
    ap.add_argument("--target-id", type=int, default=3, help="720-cap profile id to reprofile onto (default 3 = HD-720p).")
    ap.add_argument("--reason", choices=["Manual", "MissingFromDisk", "any"], default="Manual",
                    help="Which deletions to act on (default Manual = API/UI deletes).")
    ap.add_argument("--no-rescan", action="store_true", help="Skip RescanSeries.")
    ap.add_argument("--no-search", action="store_true", help="Skip EpisodeSearch.")
    ap.add_argument("--confirm", action="store_true", help="EXECUTE (default: dry-run).")
    args = ap.parse_args()

    reasons = None if args.reason == "any" else {args.reason}
    cfg = ConfigLoader(CONFIG_PATH).load()
    ok = run(cfg, args.instance, confirm=args.confirm, days=args.days, since=args.since, above=args.above,
             target_id=args.target_id, reasons=reasons, do_rescan=not args.no_rescan, do_search=not args.no_search)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""sonarr_delete_today_pilots.py — delete PILOT episode files (S01E01) by acquisition window or library-wide.

Use case: pilots were grabbed at the wrong quality (a mix of 1080p / SD) and you want to remove the
off-720 ones so they re-grab at the corrected 720p cap. This drives Sonarr's own REST API (the
supported way) -- it does NOT touch the filesystem directly.

Two selection modes:
  * WINDOW (default) -- pilots ACQUIRED in a local-day window, via
    ``GET /api/v3/history/since?date=<local-midnight-as-UTC>`` (downloadFolderImported events). Each
    resolves to the episode's CURRENT ``episodeFileId``; the file's ``dateAdded`` is re-confirmed to
    fall inside the window before deleting. Tune with --days / --since.
  * LIBRARY-WIDE (--library-wide) -- EVERY series' S01E01 file, regardless of when acquired. One
    ``episode?seriesId=..&seasonNumber=1&includeEpisodeFile=true`` call per file-bearing series,
    scanned concurrently (--scan-workers).

PILOT = season 1, episode 1. Scope by resolution with --res / --res-mode (default: delete pilots whose
resolution != 720, i.e. off-720). --all-resolutions targets every pilot.

What this tool does NOT do: it does NOT blocklist the deleted release. By default it does NOT change
the series quality profile either -- a deleted (missing) pilot is re-acquired by the normal run's
PILOT SEARCH (which caps at 720) for series already on a 720 profile; watchability can later upgrade a
watched pilot. Pass --reprofile-id N to also cap the deleted pilots' NON-ANIME series at profile N
(anime are never reprofiled: no <=720 anime profile + HD-720p bans x265). Pass --search to force an
immediate EpisodeSearch (re-grabs at each series' CURRENT profile -- not necessarily 720p).

DRY-RUN BY DEFAULT -- prints the full plan and deletes NOTHING. Pass --confirm to execute. ASCII-only.

    python -m scripts.support.tools.sonarr_delete_today_pilots                       # dry-run, today, off-720
    python -m scripts.support.tools.sonarr_delete_today_pilots --since 2026-06-26 --exclude-anime --reprofile-id 3 --confirm
    python -m scripts.support.tools.sonarr_delete_today_pilots --library-wide --exclude-anime          # dry-run sweep
    python -m scripts.support.tools.sonarr_delete_today_pilots --library-wide --exclude-anime --confirm # SWEEP DELETE
"""
from __future__ import annotations

import argparse
import concurrent.futures
import re
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

# Only "a release was imported" counts as an acquisition. seriesFolderImported is a manual library
# import (not a fresh download) and episodeFileRenamed/Deleted are not acquisitions -- excluded.
_IMPORT_EVENT = "downloadFolderImported"


def _resolve(cfg, instance):
    inst = ((cfg.get("sonarr_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _is_pilot(episode) -> bool:
    e = episode or {}
    return e.get("seasonNumber") == 1 and e.get("episodeNumber") == 1


def _local_date(iso_utc):
    """Parse a Sonarr UTC ISO timestamp and return its date in the machine's LOCAL timezone.
    None on anything unparseable (so it fails the window guard rather than risking a wrong delete)."""
    if not iso_utc:
        return None
    try:
        s = str(iso_utc).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)        # trim .NET 7-digit ticks -> 6 (fromisoformat <3.11)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date()
    except (ValueError, TypeError):
        return None


def _quality_res(obj) -> tuple:
    """(name, resolution) from a Sonarr object carrying a quality wrapper. Sonarr nests it two deep:
    ``obj['quality']['quality'] = {'name','resolution',...}``. resolution kept as int (SD reports 0,
    distinct from None=missing). Works on an episodefile and on an embedded episodeFile alike."""
    q = (((obj or {}).get("quality") or {}).get("quality") or {})
    res = q.get("resolution")
    return q.get("name"), (int(res) if isinstance(res, (int, float)) else None)


def _res_label(res) -> str:
    """Display token for a resolution: 'SD' for 0, '<n>p' for >0, '?' for unknown/None."""
    if res is None:
        return "?"
    return "SD" if res == 0 else f"{res}p"


def _eff_res(quality) -> int:
    """A quality's resolution with Raw-HD discounted to 0 (it is a 1080-labelled 720-tier quality),
    identified by source 'televisionRaw' / name 'Raw-HD'. Mirrors pilot_stepping so a 'HD-720p'
    profile reads as a true 720 cap."""
    if not isinstance(quality, dict):
        return 0
    if str(quality.get("source") or "").lower() == "televisionraw" or quality.get("name") == "Raw-HD":
        return 0
    r = quality.get("resolution", 0)
    return int(r) if isinstance(r, (int, float)) else 0


def _profile_cap(profile) -> int:
    """Highest allowed resolution a profile can grab (Raw-HD discounted) -- the cap a series on it
    would re-grab at."""
    best = 0
    for item in (profile.get("items") or []):
        if not item.get("allowed"):
            continue
        best = max(best, _eff_res(item.get("quality")))
        for sub in (item.get("items") or []):
            if sub.get("allowed"):
                best = max(best, _eff_res(sub.get("quality")))
    return best


def _res_predicate(res_mode, res_value):
    """Return f(resolution)->bool for the --res/--res-mode scope filter. res_value None -> match all."""
    if res_value is None:
        return lambda r: True
    rv = int(res_value)
    return {
        "above":   lambda r: r is not None and r > rv,
        "below":   lambda r: r is not None and r < rv,
        "not":     lambda r: r is None or r != rv,
        "exactly": lambda r: r == rv,
    }[res_mode]


def _human(n_bytes) -> str:
    f = float(n_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TiB"


def _entry(fid, seriesId, title, seriesType, res, qname, size, path, episodeId, pid, pid_cap):
    return {"fid": fid, "seriesId": seriesId, "title": title, "seriesType": seriesType,
            "res": res, "qname": qname, "size": size or 0, "path": path, "episodeId": episodeId,
            "profileId": pid, "regrab_cap": pid_cap.get(pid)}


def _candidates_from_history(base, key, pid_cap, res_ok, exclude_anime, res_mode, res_value,
                             since_iso, window_start):
    """WINDOW mode: pilots imported in [window_start, today], confirmed by each file's dateAdded."""
    series_map = {s["id"]: s for s in _get(base, key, "series")}
    hist = _get(base, key, f"history/since?date={since_iso}&includeEpisode=true")
    if not isinstance(hist, list):
        print("Unexpected history payload; aborting."); return None
    cands = [h for h in hist if h.get("eventType") == _IMPORT_EVENT and _is_pilot(h.get("episode"))]
    print(f"\nhistory/since: {len(hist)} events; pilot imports in window ({_IMPORT_EVENT}): {len(cands)}")

    fid_ctx, no_file = {}, 0
    for h in cands:
        e = h.get("episode") or {}
        fid = e.get("episodeFileId")
        if not fid:
            no_file += 1; continue          # imported then deleted/replaced-to-none -> nothing to delete
        sid = h.get("seriesId")
        s = series_map.get(sid) or (h.get("series") or {})
        fid_ctx.setdefault(fid, {"seriesId": sid, "episodeId": e.get("id"), "title": s.get("title"),
                                 "seriesType": s.get("seriesType"), "profileId": s.get("qualityProfileId")})
    print(f"  distinct current files: {len(fid_ctx)}  (skipped {no_file} import(s) whose file is already gone)")

    out, guard_dropped, filt_anime, filt_res = [], 0, 0, 0
    for fid, ctx in fid_ctx.items():
        try:
            ef = _get(base, key, f"episodefile/{fid}")
        except Exception as ex:
            print(f"  ! could not read episodefile/{fid} ({ex}) -- skipping"); continue
        if not isinstance(ef, dict):
            print(f"  ! episodefile/{fid} returned non-dict -- skipping"); continue
        wd = _local_date(ef.get("dateAdded"))
        if wd is None or wd < window_start:
            guard_dropped += 1; continue                       # not added in-window -> never delete
        if exclude_anime and ctx.get("seriesType") == "anime":
            filt_anime += 1; continue
        qname, res = _quality_res(ef)
        if not res_ok(res):
            filt_res += 1; continue
        out.append(_entry(fid, ctx.get("seriesId"), ctx.get("title"), ctx.get("seriesType"), res, qname,
                          ef.get("size"), ef.get("path"), ctx.get("episodeId"), ctx.get("profileId"), pid_cap))
    print(f"  passed acquired-in-window guard: {len(fid_ctx) - guard_dropped}  (dropped {guard_dropped} outside window)")
    if exclude_anime:
        print(f"  excluded anime: {filt_anime}")
    if res_value is not None:
        print(f"  excluded by scope (resolution {res_mode} {res_value}): {filt_res}")
    return out


def _scan_library_pilots(base, key, pid_cap, res_ok, exclude_anime, workers):
    """LIBRARY-WIDE mode: every series' S01E01 file. One episode call per file-bearing series
    (seasonNumber=1, includeEpisodeFile), read concurrently. Returns entries passing res_ok."""
    series_list = _get(base, key, "series")
    targets = [s for s in series_list
               if ((s.get("statistics") or {}).get("episodeFileCount") or 0) > 0
               and not (exclude_anime and s.get("seriesType") == "anime")]
    print(f"\nLIBRARY-WIDE scan: {len(series_list)} series; {len(targets)} have files -> scanning S01E01 "
          f"({workers} workers)...")

    def fetch(s):
        try:
            eps = _get(base, key, f"episode?seriesId={s['id']}&seasonNumber=1&includeEpisodeFile=true")
        except Exception:
            return ("error", s, None)
        if not isinstance(eps, list):
            return None
        for e in eps:
            if e.get("seasonNumber") == 1 and e.get("episodeNumber") == 1 and e.get("hasFile"):
                return ("hit", s, e)
        return None

    out, scanned, errors = [], 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(fetch, targets):
            scanned += 1
            if scanned % 1000 == 0:
                print(f"  ... scanned {scanned}/{len(targets)}")
            if not r:
                continue
            if r[0] == "error":
                errors += 1; continue
            _, s, e = r
            ef = e.get("episodeFile") or {}
            fid = ef.get("id")
            if not fid:
                continue
            qname, res = _quality_res(ef)
            if not res_ok(res):
                continue
            out.append(_entry(fid, s.get("id"), s.get("title"), s.get("seriesType"), res, qname,
                              ef.get("size"), ef.get("path"), e.get("id"), s.get("qualityProfileId"), pid_cap))
    if errors:
        print(f"  ! {errors} series failed to scan (skipped)")
    print(f"  S01E01 files matching scope: {len(out)}")
    return out


def run(cfg, instance, *, confirm, exclude_anime, res_mode, res_value, do_search, reprofile_id=None,
        days=1, since=None, library_wide=False, scan_workers=10) -> bool:
    base, key = _resolve(cfg, instance)
    if not (base and key):
        print(f"sonarr/{instance}: not configured (no base_url/api).")
        return False

    scope = "ALL resolutions" if res_value is None else f"resolution {res_mode} {res_value}"
    window_start = None
    print("=" * 78)
    print(f"### sonarr/{instance}  ({base})")
    if library_wide:
        print(f"### DELETE LIBRARY-WIDE PILOT (S01E01) files (every series, any acquisition date)")
    else:
        now_local = datetime.now().astimezone()
        today_local = now_local.date()
        window_start = date.fromisoformat(since) if since else today_local - timedelta(days=max(1, days) - 1)
        start_midnight_local = (now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                                - timedelta(days=(today_local - window_start).days))
        since_iso = start_midnight_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        window_desc = f"{window_start}..{today_local}" if window_start != today_local else f"{today_local}"
        print(f"### DELETE PILOT (S01E01) files acquired {window_desc}  |  since(UTC)={since_iso}")
    print(f"### SCOPE: {scope}{'  (exclude anime)' if exclude_anime else ''}")
    print(f"### MODE: {'APPLY (--confirm)' if confirm else 'DRY-RUN (no writes)'}")
    print("=" * 78)

    profiles = _get(base, key, "qualityprofile")
    pid_name = {p["id"]: p["name"] for p in profiles}
    pid_cap = {p["id"]: _profile_cap(p) for p in profiles}
    res_ok = _res_predicate(res_mode, res_value)

    if library_wide:
        to_delete = _scan_library_pilots(base, key, pid_cap, res_ok, exclude_anime, scan_workers)
    else:
        to_delete = _candidates_from_history(base, key, pid_cap, res_ok, exclude_anime,
                                             res_mode, res_value, since_iso, window_start)
    if to_delete is None:
        return False
    if not to_delete:
        print("\nNothing matches -- no files to delete.")
        return True

    by_q = Counter(f"{d['qname']} ({_res_label(d['res'])})" for d in to_delete)
    by_t = Counter(d["seriesType"] for d in to_delete)
    by_cap = Counter(f"{pid_name.get(d['profileId'], d['profileId'])} (re-grab {_res_label(d['regrab_cap'])})"
                     for d in to_delete)
    total_bytes = sum(d["size"] for d in to_delete)
    at_720 = sum(1 for d in to_delete if d["res"] == 720)
    regrab_over720 = sum(1 for d in to_delete if (d["regrab_cap"] or 0) > 720)

    print(f"\nTO DELETE: {len(to_delete)} pilot file(s), {_human(total_bytes)} total")
    print(f"  by series type: {dict(by_t)}")
    print(f"  already at exactly 720p (would re-grab ~same release): {at_720}")
    print(f"  by current file quality:")
    for q, n in by_q.most_common():
        print(f"    - {q}: {n}")
    print(f"  re-grab cap (series' CURRENT profile -> what a re-search would land):")
    for c, n in by_cap.most_common():
        print(f"    - {c}: {n}")
    if regrab_over720:
        print(f"    NOTE: {regrab_over720} target(s) sit on a >720 profile -> a re-grab would land ABOVE 720 "
              f"(reassign them to HD-720p first, or pass --reprofile-id 3).")
    print(f"  sample (up to 25, highest resolution first):")
    for d in sorted(to_delete, key=lambda x: (-(x["res"] if x["res"] is not None else -1), x["title"] or ""))[:25]:
        print(f"    [{_res_label(d['res']):>5}] {str(d['title'])[:46]:46s} {str(d['qname']):18s} "
              f"{_human(d['size']):>9}  file#{d['fid']}")
    if len(to_delete) > 25:
        print(f"    ... and {len(to_delete) - 25} more")

    # Optional bulk re-profile so the re-grab caps at 720. NEVER reprofile anime: no <=720 [Anime]
    # profile and HD-720p bans x265, which would break anime grabbing -- those are left as-is.
    reprofile_sids, anime_left = [], 0
    if reprofile_id is not None:
        reprofile_sids = sorted({d["seriesId"] for d in to_delete
                                 if d["seriesType"] != "anime" and d["profileId"] != reprofile_id
                                 and d["seriesId"] is not None})
        anime_left = len({d["seriesId"] for d in to_delete if d["seriesType"] == "anime"})
        print(f"\nRE-PROFILE: set {len(reprofile_sids)} non-anime series -> profile id {reprofile_id} "
              f"({pid_name.get(reprofile_id, reprofile_id)}) so the re-grab caps there.")
        if anime_left:
            print(f"  ({anime_left} anime series left on their current profile -- no <=720 anime profile / x265 ban)")

    if do_search:
        print("\n  WARNING: --search will EpisodeSearch the deleted pilots. Releases are NOT blocklisted, so "
              "a pilot re-grabs at its series' CURRENT profile cap (see above) -- not necessarily 720p.")
    elif reprofile_id is None:
        print("\n  NOTE: no --reprofile-id and no --search: deleted pilots are left missing for the normal run's "
              "pilot search to re-acquire (caps at 720 only for series on a 720 profile -- see re-grab cap above).")

    if not confirm:
        print(f"\n(dry-run -- nothing deleted. Re-run with --confirm to execute.)")
        return True

    # re-profile FIRST (before deleting) so the episode is already on the 720 cap when it goes missing
    if reprofile_id is not None and reprofile_sids:
        moved = 0
        for i in range(0, len(reprofile_sids), 200):
            chunk = reprofile_sids[i:i + 200]
            _request("PUT", base, key, "series/editor",
                     json_body={"seriesIds": chunk, "qualityProfileId": reprofile_id})
            moved += len(chunk)
        print(f"\n  OK: re-profiled {moved} non-anime series -> id {reprofile_id}")

    # ── EXECUTE DELETIONS ──────────────────────────────────────────────────────────────
    deleted = failed = 0
    ep_ids = []
    for d in to_delete:
        try:
            # single-attempt: DELETE is NOT idempotent under arr_rebuild._request's retry (a
            # timed-out-but-applied delete would re-fire and 404). Mirrors the non-idempotent POST path.
            _request("DELETE", base, key, f"episodefile/{d['fid']}", retries=1)
            deleted += 1
            if d.get("episodeId"):
                ep_ids.append(d["episodeId"])
            if deleted % 50 == 0:
                print(f"  ... deleted {deleted}/{len(to_delete)}")
        except Exception as ex:
            failed += 1
            print(f"  ! delete failed for file#{d['fid']} ({d['title']}): {ex}")
    print(f"\n  OK: deleted {deleted} pilot file(s)" + (f"; {failed} failed" if failed else ""))

    if do_search and ep_ids:
        for i in range(0, len(ep_ids), 100):
            chunk = ep_ids[i:i + 100]
            try:
                _request("POST", base, key, "command",
                         json_body={"name": "EpisodeSearch", "episodeIds": chunk}, retries=1)
            except Exception as ex:
                print(f"  ! EpisodeSearch failed for a chunk ({ex})")
        print(f"  OK: triggered EpisodeSearch for {len(ep_ids)} episode(s).")
    elif do_search:
        print("  (--search: no episodes to re-search)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete pilot (S01E01) episode files on Sonarr (window or library-wide).")
    ap.add_argument("--instance", default="standard", help="Sonarr instance name (default: standard).")
    ap.add_argument("--library-wide", action="store_true",
                    help="Scan EVERY series' S01E01 (any acquisition date), not just a history window.")
    ap.add_argument("--scan-workers", type=int, default=10, help="Concurrent series scans in --library-wide (default 10).")
    ap.add_argument("--days", type=int, default=1,
                    help="WINDOW mode: acquisition window in LOCAL days back from today (default 1=today). "
                         "Ignored with --library-wide or --since.")
    ap.add_argument("--since", default=None,
                    help="WINDOW mode: window start YYYY-MM-DD (LOCAL); overrides --days.")
    ap.add_argument("--exclude-anime", action="store_true", help="Skip seriesType=='anime' pilots.")
    ap.add_argument("--res", type=int, default=720, help="Resolution filter value (default 720). With --res-mode.")
    ap.add_argument("--res-mode", choices=["above", "below", "not", "exactly"], default="not",
                    help="How --res filters (default: not -> delete pilots whose resolution != --res = off-720).")
    ap.add_argument("--all-resolutions", action="store_true",
                    help="Ignore --res/--res-mode and target EVERY pilot (incl. correct 720p).")
    ap.add_argument("--reprofile-id", type=int, default=None,
                    help="Bulk-set each deleted pilot's NON-ANIME series to this profile id (e.g. 3 = HD-720p) so "
                         "the re-grab caps there. Anime never reprofiled. NOTE: caps the WHOLE series (blocks 1080 "
                         "upgrade) -- best for cold/new series, not owned ones you want upgradeable.")
    ap.add_argument("--search", action="store_true",
                    help="After deleting, trigger EpisodeSearch (re-grabs at current profile; usually unnecessary).")
    ap.add_argument("--confirm", action="store_true", help="EXECUTE the deletions (default: dry-run).")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    res_value = None if args.all_resolutions else args.res
    ok = run(cfg, args.instance, confirm=args.confirm, exclude_anime=args.exclude_anime,
             res_mode=args.res_mode, res_value=res_value, do_search=args.search,
             reprofile_id=args.reprofile_id, days=args.days, since=args.since,
             library_wide=args.library_wide, scan_workers=args.scan_workers)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

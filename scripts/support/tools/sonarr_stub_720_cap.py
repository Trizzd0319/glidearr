"""sonarr_stub_720_cap.py — cap STUB series at 720p on a live Sonarr instance.

Two coupled actions that together make 720-capping actually take effect:

  1. PROFILE FIX (default on) — un-allow **Raw-HD** on the target "HD-720p" profile
     (id 3). Raw-HD is a 1080-resolution raw-capture quality that Sonarr leaves enabled
     on the stock 720p profile, so the profile is NOT a true 720 cap: Sonarr will grab a
     1080p Raw-HD over a 720p WEBDL, and our pilot climb ladder reads the profile's max
     resolution as 1080 (collapsing the 720 rung). Un-allowing it makes id 3 a real 720
     cap. Idempotent — a no-op once Raw-HD is already disallowed.

  2. REASSIGN — move every STUB series (``statistics.episodeFileCount == 0``) that sits
     on a >720-allowing profile onto HD-720p (id 3). Hard guards:
       * OWNED series (episodeFileCount > 0) are NEVER touched.
       * ANIME stubs (``seriesType == 'anime'``) are skipped — a separate [Anime] 720p
         plan handles those.
       * the target profile itself and the [Anime] profiles are excluded as SOURCES.

DRY-RUN BY DEFAULT — prints the plan (the profile fix preview, reassignment counts by
source profile, and a "Billionaires' Bunker" spot-check) and writes NOTHING. Pass
``--confirm`` to execute. Writes use the bulk editor endpoint in chunks with the same
generous timeout + retry as ``arr_rebuild`` (a busy *arr re-scores after each edit).

    python -m scripts.support.tools.sonarr_stub_720_cap            # dry-run (no writes)
    python -m scripts.support.tools.sonarr_stub_720_cap --confirm   # EXECUTE
    python -m scripts.support.tools.sonarr_stub_720_cap --no-profile-fix --confirm  # reassign only
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH       # noqa: E402
# Reuse arr_rebuild's HTTP helpers verbatim — same generous timeout + backoff retry the
# bulk profile/editor calls need on a busy, re-scoring instance (see arr_rebuild._request).
from scripts.support.tools.arr_rebuild import _request, _get                  # noqa: E402

TARGET_PROFILE_ID = 3        # "HD-720p" — the 720 cap stubs are reassigned onto
RAW_HD = "Raw-HD"            # the 1080-res quality to un-allow on the target so it truly caps at 720
BATCH = 200                  # series ids per editor PUT (avoid one giant PUT on a ~12k-series instance)


def _resolve(cfg, instance):
    inst = ((cfg.get("sonarr_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _max_allowed_res(items) -> int:
    """Highest allowed resolution in a profile's item tree — HONEST (counts Raw-HD), i.e.
    the resolution Sonarr could actually grab. Used to find profiles that can pull >720."""
    best = 0
    for it in items or []:
        if not it.get("allowed"):
            continue
        q = it.get("quality") or {}
        if isinstance(q.get("resolution"), (int, float)):
            best = max(best, int(q["resolution"]))
        best = max(best, _max_allowed_res(it.get("items")))
    return best


def _unallow_raw_hd(profile) -> int:
    """Set Raw-HD ``allowed=False`` everywhere in the profile (leaf or nested). Mutates the
    profile dict in place; returns how many Raw-HD items were flipped from allowed to disallowed."""
    flipped = 0

    def walk(items):
        nonlocal flipped
        for it in items or []:
            if (it.get("quality") or {}).get("name") == RAW_HD and it.get("allowed"):
                it["allowed"] = False
                flipped += 1
            walk(it.get("items"))

    walk(profile.get("items"))
    return flipped


def _is_anime_profile(p) -> bool:
    return str(p.get("name") or "").strip().lower().startswith("[anime]")


def run(cfg, instance, *, target_id, batch, do_profile_fix, confirm) -> bool:
    base, key = _resolve(cfg, instance)
    if not (base and key):
        print(f"sonarr/{instance}: not configured (no base_url/api).")
        return False

    profiles = _get(base, key, "qualityprofile")
    by_id = {p["id"]: p for p in profiles}
    pid_name = {p["id"]: p["name"] for p in profiles}
    target = by_id.get(target_id)
    if target is None:
        print(f"ABORT: target profile id {target_id} not found on {instance}.")
        return False

    # Source profiles: anything (other than the target / the [Anime] profiles) that can grab >720.
    source_pids = {
        p["id"] for p in profiles
        if p["id"] != target_id and not _is_anime_profile(p) and _max_allowed_res(p.get("items")) > 720
    }

    print(f"{'=' * 78}\n### sonarr/{instance}  ({base})")
    print(f"### MODE: {'APPLY (--confirm)' if confirm else 'DRY-RUN (no writes)'}\n{'=' * 78}")

    # ── 1. profile fix preview ──────────────────────────────────────────────────────
    cur_max = _max_allowed_res(target.get("items"))
    raw_hd_allowed = any((it.get("quality") or {}).get("name") == RAW_HD and it.get("allowed")
                         for it in _flat(target.get("items")))
    print(f"\nPROFILE FIX -- target {pid_name[target_id]!r} (id {target_id}):")
    if not do_profile_fix:
        print("    (skipped -- --no-profile-fix)")
    elif not raw_hd_allowed:
        print(f"    Raw-HD already disallowed; max resolution {cur_max}p -- nothing to do.")
    else:
        # compute the post-fix max on a copy without mutating the live object yet
        import copy
        after = copy.deepcopy(target)
        _unallow_raw_hd(after)
        print(f"    un-allow Raw-HD: max resolution {cur_max}p -> {_max_allowed_res(after.get('items'))}p "
              f"(true 720 cap)")

    # ── 2. reassignment plan ────────────────────────────────────────────────────────
    series = _get(base, key, "series")
    by_src = Counter()
    move_ids = []
    skipped_anime = skipped_owned = 0
    for s in series:
        efc = (s.get("statistics") or {}).get("episodeFileCount") or 0
        spid = s.get("qualityProfileId")
        if efc > 0:                              # OWNED — never touch
            skipped_owned += 1
            continue
        if s.get("seriesType") == "anime":       # anime stub — separate plan
            skipped_anime += 1
            continue
        if spid in source_pids:                  # non-anime stub on a >720 profile → move
            move_ids.append(s["id"])
            by_src[spid] += 1

    print(f"\nREASSIGN -- non-anime STUBS (episodeFileCount==0) on >720 profiles -> "
          f"{pid_name[target_id]!r} (id {target_id}):")
    print(f"    source profiles (>720, non-anime, excl. target): "
          f"{sorted((i, pid_name[i]) for i in source_pids)}")
    for spid, n in sorted(by_src.items()):
        print(f"    - from id {spid} {pid_name.get(spid)!r}: {n}")
    print(f"    => {len(move_ids)} series to reassign  "
          f"(protected: {skipped_owned} owned, {skipped_anime} anime stubs skipped)")

    # ── 3. Billionaires' Bunker spot-check ──────────────────────────────────────────
    bb = next((s for s in series if (s.get("title") or "").strip().lower() == "billionaires' bunker"), None)
    if bb is not None:
        efc = (bb.get("statistics") or {}).get("episodeFileCount") or 0
        in_plan = bb["id"] in set(move_ids)
        print(f"\nSPOT-CHECK 'Billionaires' Bunker' (id {bb['id']}, year {bb.get('year')}): "
              f"profile {bb.get('qualityProfileId')} ({pid_name.get(bb.get('qualityProfileId'))!r}), "
              f"{efc} files, seriesType {bb.get('seriesType')!r} -> "
              f"{'WILL be reassigned to id ' + str(target_id) if in_plan else 'NOT in plan'}")

    if not confirm:
        print(f"\n(dry-run -- nothing changed. Re-run with --confirm to execute.)")
        return True

    # ── EXECUTE ─────────────────────────────────────────────────────────────────────
    if do_profile_fix and raw_hd_allowed:
        n = _unallow_raw_hd(target)
        _request("PUT", base, key, f"qualityprofile/{target_id}", json_body=target)
        print(f"\n  OK: profile fix applied: Raw-HD disallowed on id {target_id} "
              f"({n} item(s); max now {_max_allowed_res(target.get('items'))}p)")

    moved = 0
    for i in range(0, len(move_ids), batch):
        chunk = move_ids[i:i + batch]
        _request("PUT", base, key, "series/editor",
                 json_body={"seriesIds": chunk, "qualityProfileId": target_id})
        moved += len(chunk)
        print(f"  ... reassigned {moved}/{len(move_ids)}")
    print(f"\n  OK: reassigned {moved} series to {pid_name[target_id]!r} (id {target_id}).")

    if bb is not None:
        after_bb = _get(base, key, f"series/{bb['id']}")
        ap = after_bb.get("qualityProfileId")
        print(f"  OK: verify 'Billionaires' Bunker' now on profile {ap} ({pid_name.get(ap)!r})")
    return True


def _flat(items):
    for it in items or []:
        yield it
        yield from _flat(it.get("items"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Cap stub series at 720p on a Sonarr instance.")
    ap.add_argument("--instance", default="standard", help="Sonarr instance name (default: standard).")
    ap.add_argument("--target-id", type=int, default=TARGET_PROFILE_ID,
                    help=f"720-cap profile id to reassign onto (default: {TARGET_PROFILE_ID} = HD-720p).")
    ap.add_argument("--batch-size", type=int, default=BATCH,
                    help=f"series ids per editor PUT (default: {BATCH}).")
    ap.add_argument("--no-profile-fix", action="store_true",
                    help="Skip un-allowing Raw-HD on the target profile (reassign only).")
    ap.add_argument("--confirm", action="store_true", help="EXECUTE the writes (default: dry-run).")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    ok = run(cfg, args.instance, target_id=args.target_id, batch=args.batch_size,
             do_profile_fix=not args.no_profile_fix, confirm=args.confirm)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

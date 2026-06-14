"""
Auto-enroll EVERY non-English-origin Radarr film into the English dub lifecycle by
assigning the right English-locked quality profile. Runs against the whole library.

NON-DESTRUCTIVE by design (verified against Radarr source in an adversarial review):
  * Enrollment = quality-profile assignment ONLY, via PUT /movie/editor with body
    {"movieIds":[...],"qualityProfileId":N}. Radarr's MovieEditorController mutates
    only the fields present (HasValue guards), so monitored / tags / minimumAvailability
    / rootFolderPath are left untouched, and no file is moved or deleted (moveFiles /
    deleteFiles default false and are omitted).
  * Monitored status is never changed -> UNMONITORED films (the vast majority) get a pure
    policy change with ZERO downloads (Radarr never searches unmonitored movies).
  * Cam-replacement footgun avoided WITHOUT modifying any existing profile:
      - ALL MONITORED films (the only ones that can grab) -> CAM-FREE profile (id 15,
        'English - HD Bluray + WEB') -> they seek a REAL English release only, never a cam.
        Any existing file is replaced ONLY on successful import of an equal-or-better
        English release (Radarr's intended upgrade swap), never preemptively.
      - ALL UNMONITORED films -> id 12 ('English - HD-720p', English twin of their tier).
        Unmonitored => no grab, so id 12's cam tiers are irrelevant.
  * Skips films already on an 'English - *' profile (idempotent).

States (reported): PLACEHOLDER = no file; SEEK = file Radarr tags non-English;
HASDUB = file Radarr tags English; UNKNOWN = has file but no language data.

Usage:
    python _english_autoenroll.py            # DRY RUN (no writes)
    python _english_autoenroll.py --apply    # bulk-assign profiles (snapshot first)
    python _english_autoenroll.py --revert    # restore the latest snapshot's prior profiles
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import keyring
import requests

BASE = "http://192.168.1.110:8988"            # Radarr 'standard'
KR   = "radarr_instances.standard.api"
BULK_PROFILE_NAME    = "English - HD-720p"            # id 12 — unmonitored films (no grab)
CAMFREE_PROFILE_NAME = "English - HD Bluray + WEB"    # id 15 — cam-free, all monitored films
APPLY  = "--apply" in sys.argv
REVERT = "--revert" in sys.argv
SNAP_DIR = os.path.join(os.path.dirname(__file__), "_audio_lang_snapshots")


def _hdr():
    return {"X-Api-Key": keyring.get_password("glidearr", KR), "Content-Type": "application/json"}


def _get(ep):
    r = requests.get(f"{BASE}/api/v3/{ep}", headers=_hdr(), timeout=90); r.raise_for_status(); return r.json()


def _put(ep, body):
    r = requests.put(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=90)
    r.raise_for_status(); return r.json() if r.text else None


def radarr_says_english(mf):
    return any((l.get("name") or "").lower() == "english" for l in (mf.get("languages") or []))


def state_of(m):
    if not m.get("hasFile"):
        return "PLACEHOLDER"
    mf = m.get("movieFile") or {}
    if not (mf.get("languages")):
        return "UNKNOWN"                       # has a file but Radarr has no language data
    return "HASDUB" if radarr_says_english(mf) else "SEEK"


def _orig_lang(m):
    ol = m.get("originalLanguage")
    return (ol or {}).get("name") if isinstance(ol, dict) else None


# ── revert ────────────────────────────────────────────────────────────────────
def revert():
    snaps = sorted(glob.glob(os.path.join(SNAP_DIR, "autoenroll_*.json")))
    if not snaps:
        print("No autoenroll_*.json snapshot found — nothing to revert."); return
    snap = json.load(open(snaps[-1], encoding="utf-8"))
    print(f"REVERT: restoring {len(snap)} films from {os.path.basename(snaps[-1])}")
    by_prev = defaultdict(list)
    for e in snap:
        by_prev[int(e["prev_qualityProfileId"])].append(int(e["movie_id"]))
    for prev_id, ids in by_prev.items():
        _put("movie/editor", {"movieIds": ids, "qualityProfileId": prev_id})
        print(f"  restored {len(ids)} films -> profile {prev_id}")
    print("DONE. (Idempotent — restoring an unchanged film is a no-op.)")


def main():
    print(f"{'='*78}\nMODE: {'APPLY' if APPLY else 'DRY RUN (no writes)'}\n{'='*78}")
    profiles = _get("qualityprofile")
    by_name = {p["name"]: p["id"] for p in profiles}
    english_ids = {p["id"] for p in profiles if p["name"].startswith("English -")}
    bulk_id    = by_name.get(BULK_PROFILE_NAME)
    camfree_id = by_name.get(CAMFREE_PROFILE_NAME)
    if not bulk_id or not camfree_id:
        print(f"!! missing target profiles ({BULK_PROFILE_NAME}={bulk_id}, "
              f"{CAMFREE_PROFILE_NAME}={camfree_id}) — abort."); return

    movies = _get("movie")
    no_origlang = sum(1 for m in movies if not _orig_lang(m))
    foreign = [m for m in movies if (_orig_lang(m) or "English") != "English"]

    snap, to_move = [], defaultdict(list)
    skipped_already = 0
    breakdown = Counter()
    camfree_samples = []
    for m in foreign:
        if m.get("qualityProfileId") in english_ids:
            skipped_already += 1
            continue
        st  = state_of(m)
        mon = bool(m.get("monitored"))
        # Only monitored films can grab -> route them ALL cam-free. Unmonitored -> id 12.
        target = camfree_id if mon else bulk_id
        to_move[target].append(m["id"])
        snap.append({"movie_id": m["id"], "prev_qualityProfileId": m["qualityProfileId"],
                     "target_qualityProfileId": target})
        breakdown[(st, mon, "id%d cam-free" % camfree_id if target == camfree_id else "id%d bulk" % bulk_id)] += 1
        if mon and len(camfree_samples) < 25:
            camfree_samples.append(f"{(m.get('title') or '?')[:34]} [{_orig_lang(m)}] {st}")

    total = sum(len(v) for v in to_move.values())
    print(f"\nForeign films: {len(foreign)}  |  already English-enrolled (skip): {skipped_already}"
          f"  |  excluded (null originalLanguage): {no_origlang}")
    print(f"To enroll: {total}  -> id{bulk_id} bulk(unmon): {len(to_move[bulk_id])},  "
          f"id{camfree_id} cam-free(mon): {len(to_move[camfree_id])}")
    print("\nBreakdown (state, monitored, target):")
    for (st, mon, tgt), n in sorted(breakdown.items()):
        flag = "  <-- can grab (cam-free)" if mon else "  (unmonitored: policy only, no download)"
        print(f"   {st:<12} monitored={mon!s:<5} -> {tgt:<14} {n:>5}{flag}")
    print(f"\nMonitored films routed cam-free to id{camfree_id} (sample of {len(to_move[camfree_id])}):")
    for s in camfree_samples:
        print(f"     {s}")

    if not APPLY:
        print(f"\n(dry run — nothing changed. Re-run with --apply, or --revert to undo.)")
        return

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json.dump(snap, open(os.path.join(SNAP_DIR, f"autoenroll_{stamp}.json"), "w"))

    for target_id, ids in to_move.items():
        if not ids:
            continue
        _put("movie/editor", {"movieIds": ids, "qualityProfileId": target_id})
        print(f"  + moved {len(ids)} films -> profile {target_id}")
    print(f"\nDONE. Snapshot autoenroll_{stamp}.json — undo with: python {os.path.basename(__file__)} --revert")


if __name__ == "__main__":
    if REVERT:
        revert()
    else:
        main()

"""
Create a dedicated Radarr 'English - Theatrical OK' quality profile and assign
the Infinity Castle movie to it.

Purpose: theatrical-only titles where the only English release is a TELESYNC and
the high-quality release is Japanese-only. Quality-first ranking means a TELESYNC
can never out-rank a real WEBDL unless the non-English releases are removed from
contention -> so this profile hard-REQUIRES English, then elevates TELESYNC above
the low-res/cam options so the 1080p English TELESYNC is the chosen fallback.
Upgrades stay ON so it auto-replaces the TELESYNC with a real English release later.

Cloned from profile 8 ('Remux + WEB 1080p') to inherit the tuned custom-format
scores, then: language=English, TELESYNC moved just below HD-1080p, CAM/WORKPRINT
disallowed.

Usage:
    python _english_theatrical_profile.py            # DRY RUN (no writes)
    python _english_theatrical_profile.py --apply    # create profile + assign movie
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone

import keyring
import requests

BASE = "http://192.168.1.110:8988"          # Radarr 'standard'
KR   = "radarr_instances.standard.api"
CLONE_FROM_ID = 8                            # 'Remux + WEB 1080p'
NEW_NAME = "English - Theatrical OK"
MOVIE_ID = 1015                              # Demon Slayer: Infinity Castle (2025)
ENGLISH_LANG = {"id": 1, "name": "English"}
DISALLOW = {"CAM", "WORKPRINT"}
ELEVATE_BELOW = "HDTV-1080p"                 # put TELESYNC just under this tier
APPLY = "--apply" in sys.argv
SNAP_DIR = os.path.join(os.path.dirname(__file__), "_audio_lang_snapshots")


def _hdr():
    return {"X-Api-Key": keyring.get_password("glidearr", KR), "Content-Type": "application/json"}


def _get(ep):
    r = requests.get(f"{BASE}/api/v3/{ep}", headers=_hdr(), timeout=30); r.raise_for_status(); return r.json()


def _post(ep, body):
    r = requests.post(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=30)
    r.raise_for_status(); return r.json()


def _put(ep, body):
    r = requests.put(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=30)
    r.raise_for_status(); return r.json() if r.text else None


def qname(it):
    return it.get("name") or (it.get("quality") or {}).get("name")


def build_profile(src):
    new = copy.deepcopy(src)
    new.pop("id", None)
    new["name"] = NEW_NAME
    new["language"] = ENGLISH_LANG
    new["upgradeAllowed"] = True
    items = new["items"]
    for it in items:
        if qname(it) in DISALLOW:
            it["allowed"] = False
    ts = next((it for it in items if qname(it) == "TELESYNC"), None)
    if ts is not None:
        ts["allowed"] = True
        items.remove(ts)
        idx = next(i for i, it in enumerate(items) if qname(it) == ELEVATE_BELOW)
        items.insert(idx, ts)   # worst->best array: lands just below HDTV-1080p
    new["items"] = items
    return new


def show(new):
    print(f"\nNEW PROFILE: {new['name']!r}   language=REQUIRED:{new['language']['name']}   "
          f"upgradeAllowed={new['upgradeAllowed']}")
    print("  quality order (most-preferred first):")
    for it in reversed(new["items"]):
        mark = "OK " if it.get("allowed") else "xx "
        star = "   <-- ELEVATED" if qname(it) == "TELESYNC" and it.get("allowed") else ""
        print(f"     {mark} {qname(it)}{star}")
    fmt = {f.get("name"): f.get("score") for f in new.get("formatItems", [])
           if f.get("name") in ("English Audio", "Dual Audio") and f.get("score")}
    print(f"  English/Dual CF scores carried over: {fmt}")


def main():
    print(f"{'='*78}\nMODE: {'APPLY' if APPLY else 'DRY RUN (no writes)'}\n{'='*78}")
    src = _get(f"qualityprofile/{CLONE_FROM_ID}")
    new = build_profile(src)
    show(new)

    movie = _get(f"movie/{MOVIE_ID}")
    print(f"\nMOVIE: {movie['title']!r} ({movie.get('year')})  "
          f"currently on profileId={movie['qualityProfileId']}  -> will move to NEW profile")

    if not APPLY:
        print("\n(dry run — nothing created. Re-run with --apply.)")
        return

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json.dump({"movie_id": MOVIE_ID, "prev_qualityProfileId": movie["qualityProfileId"]},
              open(os.path.join(SNAP_DIR, f"infinity_movie_{stamp}.json"), "w"), indent=2)

    created = _post("qualityprofile", new)
    print(f"\n  + Created profile id={created['id']} {created['name']!r}")
    movie["qualityProfileId"] = created["id"]
    _put(f"movie/{MOVIE_ID}", movie)
    check = _get(f"movie/{MOVIE_ID}")
    ok = check["qualityProfileId"] == created["id"]
    print(f"  + Assigned movie -> profileId={check['qualityProfileId']}  {'OK' if ok else 'FAILED'}")
    print(f"\nDONE. (revert: reassign movie to profile {movie['qualityProfileId']} and delete the new profile)")


if __name__ == "__main__":
    main()

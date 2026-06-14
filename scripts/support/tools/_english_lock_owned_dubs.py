"""
English-lock owned foreign films that ALREADY have an English dub, so a
higher-quality original-language release can never silently overwrite the dub
(quality-first ranking would otherwise downgrade English -> original).

Scope (deliberately narrow + safe — zero downloads):
  * Radarr 'standard' only (ultra has no foreign films).
  * originalLanguage != English, hasFile == True, AND Radarr's own
    movieFile.languages includes English (gate already satisfied -> locking it
    will NOT trigger a re-grab; this is pure protection).
  * Skips films already on an 'English - *' profile (idempotent).

Mechanism: clone profile 3 ('HD-720p', the tier these films already sit on) into
an English-REQUIRED twin 'English - HD-720p' (same quality items, language gate
added, upgrades on so it still improves WITHIN English), then reassign each film.
The 2 SD-profile films get bumped to the 720p twin (SD is almost certainly a
default artifact; +res, never -res).

Usage:
    python _english_lock_owned_dubs.py            # DRY RUN (no writes)
    python _english_lock_owned_dubs.py --apply    # create twin + reassign films
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone

import keyring
import requests

BASE = "http://192.168.1.110:8988"
KR   = "radarr_instances.standard.api"
CLONE_FROM_ID = 3                  # 'HD-720p' — the tier the dub films already use
NEW_NAME = "English - HD-720p"
ENGLISH_LANG = {"id": 1, "name": "English"}
ALREADY_LOCKED_PREFIX = "english"  # skip any profile whose name starts with 'English'
APPLY = "--apply" in sys.argv
SNAP_DIR = os.path.join(os.path.dirname(__file__), "_audio_lang_snapshots")


def _hdr():
    return {"X-Api-Key": keyring.get_password("glidearr", KR), "Content-Type": "application/json"}


def _get(ep):
    r = requests.get(f"{BASE}/api/v3/{ep}", headers=_hdr(), timeout=60); r.raise_for_status(); return r.json()


def _post(ep, body):
    r = requests.post(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=30)
    r.raise_for_status(); return r.json()


def _put(ep, body):
    r = requests.put(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=30)
    r.raise_for_status(); return r.json() if r.text else None


def radarr_says_english(mf):
    """Gate-accurate: does Radarr's own movieFile.languages include English?"""
    return any((l.get("name") or "").lower() == "english" for l in (mf.get("languages") or []))


def main():
    print(f"{'='*78}\nMODE: {'APPLY' if APPLY else 'DRY RUN (no writes)'}\n{'='*78}")
    profiles = _get("qualityprofile")
    qpname = {p["id"]: p["name"] for p in profiles}
    english_profile_ids = {p["id"] for p in profiles
                           if p["name"].lower().startswith(ALREADY_LOCKED_PREFIX)}

    src = next(p for p in profiles if p["id"] == CLONE_FROM_ID)
    twin = copy.deepcopy(src)
    twin.pop("id", None)
    twin["name"] = NEW_NAME
    twin["language"] = ENGLISH_LANG
    twin["upgradeAllowed"] = True
    existing_twin = next((p for p in profiles if p["name"] == NEW_NAME), None)

    movies = _get("movie")
    targets = []
    for m in movies:
        if ((m.get("originalLanguage") or {}).get("name") or "English") == "English":
            continue
        if not m.get("hasFile") or not radarr_says_english(m.get("movieFile") or {}):
            continue
        if m.get("qualityProfileId") in english_profile_ids:
            continue   # already English-locked
        targets.append(m)

    print(f"\nNEW PROFILE: {NEW_NAME!r}  (clone of {qpname[CLONE_FROM_ID]!r} + language=REQUIRED:English)"
          f"{'  [already exists]' if existing_twin else ''}")
    print(f"\n{len(targets)} owned-dub foreign films to English-lock. Current profiles:")
    from collections import Counter
    for pid, n in Counter(m.get("qualityProfileId") for m in targets).most_common():
        print(f"   p{pid} ({qpname.get(pid)!r}): {n}")
    print("\n  sample reassignments:")
    for m in targets[:12]:
        mf = m.get("movieFile") or {}
        al = (mf.get("mediaInfo") or {}).get("audioLanguages")
        print(f"     {m.get('title')[:36]:<36} [{(m.get('originalLanguage') or {}).get('name')}] "
              f"p{m.get('qualityProfileId')} -> {NEW_NAME}   (audio={al})")

    if not APPLY:
        print(f"\n(dry run — nothing changed. {len(targets)} films would move. Re-run with --apply.)")
        return

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = [{"movie_id": m["id"], "title": m["title"], "prev_qualityProfileId": m["qualityProfileId"]}
            for m in targets]
    json.dump(snap, open(os.path.join(SNAP_DIR, f"english_lock_{stamp}.json"), "w"), indent=2)

    twin_id = existing_twin["id"] if existing_twin else _post("qualityprofile", twin)["id"]
    print(f"\n  + profile {NEW_NAME!r} id={twin_id}")
    ok = 0
    for m in targets:
        full = _get(f"movie/{m['id']}")
        full["qualityProfileId"] = twin_id
        _put(f"movie/{m['id']}", full)
        ok += 1
    print(f"  + reassigned {ok}/{len(targets)} films to profile {twin_id}")
    print(f"\nDONE. Snapshot: english_lock_{stamp}.json  (revert: restore prev_qualityProfileId per movie)")


if __name__ == "__main__":
    main()

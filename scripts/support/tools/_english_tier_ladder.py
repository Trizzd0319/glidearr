"""
Create English-only (language=English) twins of every Radarr quality-ladder tier,
so the watchability upgrader can promote an English-locked film up a parallel
ENGLISH ladder instead of dropping it onto a mixed-language profile at each step.

Ladder (config radarr_quality_ladder) maps watch-likelihood -> profile id:
    [[0,3],[20,4],[30,6],[40,7],[55,8],[65,5],[70,9],[85,10]]   (all on 'standard')

For each tier profile: clone it verbatim, add language=English (hard gate),
keep upgradeAllowed. Idempotent by name ('English - <original name>'); the tier-3
twin already exists as 'English - HD-720p' (id 12) and is reused.

Prints the resulting ENGLISH ladder array (likelihood -> english-twin id) for
wiring the upgrader later.

Usage:
    python _english_tier_ladder.py            # DRY RUN (no writes)
    python _english_tier_ladder.py --apply    # create the twins
"""
from __future__ import annotations

import copy
import json
import os
import sys

import keyring
import requests

BASE = "http://192.168.1.110:8988"          # Radarr 'standard'
KR   = "radarr_instances.standard.api"
LADDER = [[0, 3], [20, 4], [30, 6], [40, 7], [55, 8], [65, 5], [70, 9], [85, 10]]
ENGLISH_LANG = {"id": 1, "name": "English"}
APPLY = "--apply" in sys.argv


def _hdr():
    return {"X-Api-Key": keyring.get_password("glidearr", KR), "Content-Type": "application/json"}


def _get(ep):
    r = requests.get(f"{BASE}/api/v3/{ep}", headers=_hdr(), timeout=30); r.raise_for_status(); return r.json()


def _post(ep, body):
    r = requests.post(f"{BASE}/api/v3/{ep}", headers=_hdr(), data=json.dumps(body), timeout=30)
    r.raise_for_status(); return r.json()


def twin_name(src_name):
    return f"English - {src_name}"


def main():
    print(f"{'='*78}\nMODE: {'APPLY' if APPLY else 'DRY RUN (no writes)'}\n{'='*78}")
    profiles = _get("qualityprofile")
    by_id = {p["id"]: p for p in profiles}
    by_name = {p["name"]: p for p in profiles}

    english_ladder = []
    print("\nTier -> English twin:")
    for min_like, pid in LADDER:
        src = by_id.get(pid)
        if not src:
            print(f"   !! ladder profile id {pid} not found — skipped"); continue
        tname = twin_name(src["name"])
        existing = by_name.get(tname)
        if existing:
            twin_id = existing["id"]
            print(f"   >={min_like:>2}  {src['name']:<26} -> {tname!r} (exists, id {twin_id})")
        elif APPLY:
            twin = copy.deepcopy(src); twin.pop("id", None)
            twin["name"] = tname
            twin["language"] = ENGLISH_LANG
            twin["upgradeAllowed"] = True
            twin_id = _post("qualityprofile", twin)["id"]
            print(f"   >={min_like:>2}  {src['name']:<26} -> {tname!r} (CREATED id {twin_id})")
        else:
            twin_id = None
            print(f"   >={min_like:>2}  {src['name']:<26} -> {tname!r} (would create)")
        english_ladder.append([min_like, twin_id])

    print("\nResulting ENGLISH ladder (likelihood -> english-twin profile id):")
    print("  radarr_quality_ladder_english =", json.dumps(english_ladder))
    if not APPLY:
        print("\n(dry run — nothing created. Re-run with --apply.)")
    else:
        print("\nDONE. Use the english ladder above to route English-locked films on upgrade.")


if __name__ == "__main__":
    main()

"""
Set up English-audio prioritization across all Sonarr/Radarr profiles.

Design (per the user's choices: strong scored preference, no hard blocks,
"dub if it exists, else original"):

  1. Create custom format "English Audio"  (LanguageSpecification=English, required)
       -> +ENGLISH_SCORE in EVERY quality profile.
  2. Create custom format "Dual Audio"     (English AND Original both required)
       -> +DUAL_SCORE in every profile (STACKS on English Audio, so a release that
          carries BOTH the original track and an English track ranks highest;
          English-only dub next; original-only still grabbed as fallback).
  3. Relax the hard language gates so the score actually decides and foreign
     content is never silently rejected:
       - Radarr 'standard': profile language English -> Any
       - Radarr 'ultra'   : profile language Original -> Any
  4. Sonarr only: zero the "Language: Not Original" -10000 hard penalty
     (it blocks English dubs of foreign content). CF left in place, just inert.

NOT touched (surgical): "Dubs Only" / "VOSTFR" -10000 in anime profiles stay as-is,
so anime keeps original lossless audio (dual-audio preferred, dub-only still avoided).

Quality-first mechanic: *arr ranks Quality tier FIRST, custom-format score only as a
tiebreaker WITHIN a quality. For English-origin titles English==Original so every
release gets the bonus uniformly (cancels out -> your audio/tier ladder is untouched).
The bonus only becomes decisive for foreign/anime titles, exactly as intended.

Usage:
    python _audio_lang_apply.py            # DRY RUN (no writes) — default
    python _audio_lang_apply.py --apply    # snapshot, then write to live instances
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import keyring
import requests

# ── Tunables ──────────────────────────────────────────────────────────────────
ENGLISH_SCORE = 1500   # added to every profile for releases with an English track
DUAL_SCORE    = 500    # extra, stacks on English -> dual-audio releases sit at 2000
ENABLE_DUAL   = True

EN_ID, ORIGINAL_ID, ANY_ID = 1, -2, -1

# Hard-block penalties to neutralize (set score 0) so nothing English is rejected
# outright — incl. anime dub-only ("Dubs Only") and French-subbed ("VOSTFR").
ZERO_BLOCKERS = {"language: not original", "dubs only", "vostfr"}

INSTANCES = [
    ("sonarr",   "sonarr",  "http://192.168.1.110:8990", "sonarr_instances.sonarr.api"),
    ("standard", "radarr",  "http://192.168.1.110:8988", "radarr_instances.standard.api"),
    ("ultra",    "radarr",  "http://192.168.1.110:8989", "radarr_instances.ultra.api"),
]

APPLY = "--apply" in sys.argv
REVERT = "--revert" in sys.argv
SNAP_DIR = os.path.join(os.path.dirname(__file__), "_audio_lang_snapshots")


def _delete(base, key, ep):
    r = requests.delete(f"{base}/api/v3/{ep}", headers=_hdr(key), timeout=30)
    r.raise_for_status()


def revert():
    """Restore each instance's most recent pre-change snapshot, then delete the
    two custom formats we created. One-command, full undo."""
    print(f"\n{'='*78}\nMODE: REVERT (restoring latest snapshots)\n{'='*78}")
    for disp, kind, base, kr in INSTANCES:
        key = keyring.get_password("glidearr", kr)
        snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.startswith(disp + "_"))
        if not snaps:
            print(f"!! {disp}: no snapshot found — skipped"); continue
        snap = json.load(open(os.path.join(SNAP_DIR, snaps[-1]), encoding="utf-8"))
        # 1) restore every profile to its pre-change state (gates, blockers, no new CFs)
        for p in snap["qualityprofile"]:
            _put(base, key, f"qualityprofile/{p['id']}", p)
        # 2) remove the CFs we added (auto-strips them from all profiles)
        live = _get(base, key, "customformat")
        for nm in ("English Audio", "Dual Audio"):
            cf = find_cf(live, nm)
            if cf:
                _delete(base, key, f"customformat/{cf['id']}")
        print(f"   {disp}: restored {snaps[-1]} + removed English Audio / Dual Audio CFs")
    print("\nDONE. Instances restored to pre-change state.")


def _hdr(key):
    return {"X-Api-Key": key, "Content-Type": "application/json"}


def _get(base, key, ep):
    r = requests.get(f"{base}/api/v3/{ep}", headers=_hdr(key), timeout=30)
    r.raise_for_status()
    return r.json()


def _post(base, key, ep, body):
    r = requests.post(f"{base}/api/v3/{ep}", headers=_hdr(key), data=json.dumps(body), timeout=30)
    r.raise_for_status()
    return r.json()


def _put(base, key, ep, body):
    r = requests.put(f"{base}/api/v3/{ep}", headers=_hdr(key), data=json.dumps(body), timeout=30)
    r.raise_for_status()
    return r.json() if r.text else None


def lang_spec(name, value):
    return {
        "name": name, "implementation": "LanguageSpecification",
        "negate": False, "required": True,
        "fields": [{"name": "value", "value": value}, {"name": "exceptLanguage", "value": False}],
    }


def english_cf():
    return {"name": "English Audio", "includeCustomFormatWhenRenaming": False,
            "specifications": [lang_spec("English", EN_ID)]}


def dual_cf():
    # Both required -> AND. For foreign content this is a true dual-audio release;
    # for English-origin content Original==English so it simply also fires (uniform).
    return {"name": "Dual Audio", "includeCustomFormatWhenRenaming": False,
            "specifications": [lang_spec("English", EN_ID), lang_spec("Original", ORIGINAL_ID)]}


def find_cf(cfs, name):
    return next((c for c in cfs if c.get("name", "").lower() == name.lower()), None)


def ensure_cf(base, key, cfs, payload, plan):
    """Return cf id (creating it in --apply mode). In dry-run returns existing id or None."""
    existing = find_cf(cfs, payload["name"])
    if existing:
        return existing["id"]
    if APPLY:
        created = _post(base, key, "customformat", payload)
        plan.append(f'      + CREATE custom format "{payload["name"]}" (id {created.get("id")})')
        return created["id"]
    plan.append(f'      + CREATE custom format "{payload["name"]}" (new)')
    return None


def set_score(profile, cf_id, cf_name, score, plan, label):
    """Set the formatItem score for cf_id (match by id, else by name, else append)."""
    items = profile.get("formatItems", [])
    target = None
    for it in items:
        fid = it.get("format")
        fid = fid.get("id") if isinstance(fid, dict) else fid
        if (cf_id is not None and fid == cf_id) or it.get("name", "").lower() == cf_name.lower():
            target = it
            break
    old = target.get("score") if target else None
    if old == score:
        return
    if target is None:
        items.append({"format": cf_id, "name": cf_name, "score": score})
        profile["formatItems"] = items
    else:
        target["score"] = score
    plan.append(f'      ~ {label:<22} {old if old is not None else "-":>7} -> {score}')


def main():
    print(f"\n{'='*78}\nMODE: {'APPLY (writing to live instances)' if APPLY else 'DRY RUN (no writes)'}"
          f"   English=+{ENGLISH_SCORE}  Dual=+{DUAL_SCORE if ENABLE_DUAL else 0}\n{'='*78}")
    if APPLY:
        os.makedirs(SNAP_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for disp, kind, base, kr in INSTANCES:
        key = keyring.get_password("glidearr", kr)
        if not key:
            print(f"!! {disp}: no API key — skipped"); continue

        cfs = _get(base, key, "customformat")
        profiles = _get(base, key, "qualityprofile")

        if APPLY:
            snap = {"customformat": cfs, "qualityprofile": profiles}
            with open(os.path.join(SNAP_DIR, f"{disp}_{stamp}.json"), "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=2)

        print(f"\n#### {disp}  ({kind})  {base}")
        plan_global = []
        en_id  = ensure_cf(base, key, cfs, english_cf(), plan_global)
        dl_id  = ensure_cf(base, key, cfs, dual_cf(), plan_global) if ENABLE_DUAL else None
        for line in plan_global:
            print(line)

        # In apply mode, re-fetch profiles so the freshly-created CFs appear in formatItems.
        if APPLY:
            profiles = _get(base, key, "qualityprofile")

        for p in profiles:
            plan = []
            # gate relaxation (Radarr only)
            if kind == "radarr":
                lang = p.get("language") or {}
                if lang.get("id") not in (None, ANY_ID):
                    plan.append(f'      ~ profile language        {lang.get("name"):>7} -> Any')
                    p["language"] = {"id": ANY_ID, "name": "Any"}
            # Neutralize hard-block penalties (Not Original / Dubs Only / VOSTFR)
            for it in p.get("formatItems", []):
                if it.get("name", "").lower() in ZERO_BLOCKERS and it.get("score", 0) != 0:
                    plan.append(f'      ~ {it.get("name"):<22} {it.get("score"):>7} -> 0')
                    it["score"] = 0
            # English + Dual scores
            set_score(p, en_id, "English Audio", ENGLISH_SCORE, plan, "English Audio")
            if ENABLE_DUAL:
                set_score(p, dl_id, "Dual Audio", DUAL_SCORE, plan, "Dual Audio")

            if plan:
                print(f"\n   profile [{p.get('id')}] {p.get('name')!r}")
                for line in plan:
                    print(line)
                if APPLY:
                    _put(base, key, f"qualityprofile/{p['id']}", p)

    if APPLY:
        print(f"\nDONE. Snapshots saved under {SNAP_DIR} (revert source).")
    else:
        print(f"\n(dry run complete — nothing was changed. Re-run with --apply to write.)")


if __name__ == "__main__":
    if REVERT:
        revert()
    else:
        main()

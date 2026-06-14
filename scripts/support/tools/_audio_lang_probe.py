"""
READ-ONLY diagnostic: dump quality profiles + custom formats from the live
Sonarr/Radarr instances, focused on audio-language prioritization.

No writes. Pulls /system/status, /qualityprofile, /customformat from each instance,
prints a human summary, and saves raw JSON snapshots under a temp dir for analysis.
"""
from __future__ import annotations

import json
import os
import sys

import keyring
import requests

INSTANCES = [
    ("sonarr",   "http://192.168.1.110:8990", "sonarr_instances.sonarr.api"),
    ("standard", "http://192.168.1.110:8988", "radarr_instances.standard.api"),
    ("ultra",    "http://192.168.1.110:8989", "radarr_instances.ultra.api"),
]

OUT_DIR = os.path.join(os.path.dirname(__file__), "_audio_lang_probe_out")
os.makedirs(OUT_DIR, exist_ok=True)


def _get(base, key, endpoint):
    r = requests.get(f"{base}/api/v3/{endpoint}", headers={"X-Api-Key": key}, timeout=20)
    r.raise_for_status()
    return r.json()


def _lang_in_spec(spec):
    """Return a label if a specification is a Language implementation."""
    impl = (spec.get("implementation") or "").lower()
    if "language" in impl:
        # value is usually a language id; pull the human field if present
        vals = []
        for f in spec.get("fields", []):
            if f.get("name") in ("value", "language"):
                vals.append(f.get("value"))
        return f"{spec.get('implementation')}={vals}"
    return None


def main():
    summary = {}
    for name, base, kr in INSTANCES:
        key = keyring.get_password("glidearr", kr)
        if not key:
            print(f"!! {name}: no API key")
            continue
        try:
            status = _get(base, key, "system/status")
            qps = _get(base, key, "qualityprofile")
            cfs = _get(base, key, "customformat")
        except Exception as e:
            print(f"!! {name}: {e}")
            continue

        json.dump(qps, open(os.path.join(OUT_DIR, f"{name}_qualityprofiles.json"), "w"), indent=2)
        json.dump(cfs, open(os.path.join(OUT_DIR, f"{name}_customformats.json"), "w"), indent=2)

        app = status.get("appName", "?")
        ver = status.get("version", "?")
        print("=" * 78)
        print(f"INSTANCE: {name}  ({app} v{ver})  {base}")
        print("=" * 78)

        # ---- Custom formats: flag any language-related ones ----
        print(f"\n  CUSTOM FORMATS ({len(cfs)}):")
        lang_cfs = []
        for cf in cfs:
            lang_hits = []
            for spec in cf.get("specifications", []):
                lab = _lang_in_spec(spec)
                if lab:
                    lang_hits.append(lab)
            marker = "  <-- LANGUAGE" if lang_hits else ""
            print(f"    [{cf.get('id'):>3}] {cf.get('name')}{marker}")
            if lang_hits:
                lang_cfs.append(cf.get("id"))
                for h in lang_hits:
                    print(f"           {h}")

        # ---- Quality profiles: name, language, format scoring ----
        print(f"\n  QUALITY PROFILES ({len(qps)}):")
        prof_rows = []
        for p in qps:
            lang = p.get("language")
            lang_str = lang.get("name") if isinstance(lang, dict) else lang
            scored = [
                (fi.get("name"), fi.get("score"))
                for fi in p.get("formatItems", [])
                if fi.get("score", 0) != 0
            ]
            print(f"    [{p.get('id'):>3}] {p.get('name')!r}  language={lang_str}  "
                  f"upgradeAllowed={p.get('upgradeAllowed')}  cutoffScore={p.get('cutoffFormatScore')}  "
                  f"minScore={p.get('minFormatScore')}")
            if scored:
                for fn, sc in scored:
                    print(f"           score {sc:>6}  {fn}")
            else:
                print(f"           (no custom-format scores set)")
            prof_rows.append({
                "id": p.get("id"), "name": p.get("name"), "language": lang_str,
                "scored_formats": scored,
            })

        summary[name] = {
            "app": app, "version": ver,
            "n_custom_formats": len(cfs),
            "language_custom_format_ids": lang_cfs,
            "profiles": prof_rows,
        }
        print()

    json.dump(summary, open(os.path.join(OUT_DIR, "_summary.json"), "w"), indent=2)
    print(f"\nRaw snapshots + summary written to: {OUT_DIR}")


if __name__ == "__main__":
    main()

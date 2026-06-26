"""
build_anime_profiles.py — derive per-tier ANIME quality profiles from your existing setup.
================================================================================
The pilot's floor-first search flips a series to the lowest RESOLUTION-tier profile (SD / HD-720p /
…). Those profiles score ``x265 = -10000`` (correct for live-action, WRONG for anime, which is
overwhelmingly x265) — so anime series land on them and never grab. This tool builds a parallel
ladder of ANIME profiles, one per resolution tier:

    [Anime] SD        = the SD tier's allowed qualities      + your anime CF scoring
    [Anime] HD-720p   = the HD-720p tier's allowed qualities + your anime CF scoring
    [Anime] HD-1080p  = the HD-1080p tier's allowed qualities+ your anime CF scoring
    [Anime] Ultra-HD  = the Ultra-HD tier's allowed qualities+ your anime CF scoring

Quality items come from your resolution-tier profiles; the custom-format scoring + min/cutoff format
score come from your existing ``[Anime] …`` profile (which does NOT penalize x265 and scores anime
sources positively). The pilot then uses this ladder for anime series.

    python scripts/support/tools/build_anime_profiles.py            # DRY-RUN — print what it would create
    python scripts/support/tools/build_anime_profiles.py --apply    # actually POST the new profiles
    python scripts/support/tools/build_anime_profiles.py --instance standard

Idempotent: skips a tier whose ``[Anime] <tier>`` profile already exists.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader   # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH     # noqa: E402

# The resolution tiers to mirror → the anime profile name to create. Source profiles are matched by
# name (case-insensitive) with a max-resolution fallback.
_TIERS = [("SD", "[Anime] SD"), ("HD-720p", "[Anime] HD-720p"),
          ("HD-1080p", "[Anime] HD-1080p"), ("Ultra-HD", "[Anime] Ultra-HD")]


def _endpoint(icfg):
    raw = (icfg.get("base_url") or icfg.get("url") or "").strip()
    if raw and not raw.startswith(("http://", "https://")):
        proto = "https" if icfg.get("ssl", True) else "http"
        raw = f"{proto}://{raw}"
        port = icfg.get("port")
        if port and f":{port}" not in raw.split("://", 1)[-1]:
            raw = f"{raw}:{port}"
    return raw.rstrip("/"), (icfg.get("api") or "").strip()


def _max_res(items):
    best = 0
    for it in items or []:
        if it.get("items"):
            for s in it["items"]:
                if s.get("allowed"):
                    best = max(best, (s.get("quality") or {}).get("resolution", 0) or 0)
        elif it.get("allowed"):
            best = max(best, (it.get("quality") or {}).get("resolution", 0) or 0)
    return best


def _allowed_names(items):
    out = []
    for it in items or []:
        subs = it.get("items") or [it]
        for s in subs:
            if s.get("allowed"):
                q = s.get("quality") or {}
                out.append(f"{q.get('name')}({q.get('resolution')}p)")
    return out


def _has_x265_penalty(prof):
    return any("x265" in (fi.get("name") or "").lower() and (fi.get("score") or 0) < 0
               for fi in (prof.get("formatItems") or []))


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive per-tier anime quality profiles")
    ap.add_argument("--instance", default="standard")
    ap.add_argument("--apply", action="store_true", help="Actually create the profiles (default: dry-run)")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    ic = (cfg.get("sonarr_instances", {}) or {})
    icfg = ic.get(args.instance) or ic.get((ic.get("default_instance") or {}).get("name")) or {}
    base, api = _endpoint(icfg)
    if not base or not api:
        print(f"ABORT: Sonarr instance '{args.instance}' not configured.")
        return 1

    def get(ep):
        r = requests.get(f"{base}/api/v3/{ep}", headers={"X-Api-Key": api}, timeout=60)
        r.raise_for_status()
        return r.json()

    profs = get("qualityprofile")
    by_name = {p["name"].lower(): p for p in profs}

    # The anime CF/scoring source: the existing "[Anime] …" profile that does NOT penalize x265.
    anime_src = next((p for p in profs
                      if "anime" in p["name"].lower() and not _has_x265_penalty(p)), None)
    if anime_src is None:
        anime_src = next((p for p in profs if "anime" in p["name"].lower()), None)
    if anime_src is None:
        print("ABORT: no existing '[Anime] …' profile to copy CF scoring from. Create one first.")
        return 1
    print(f"CF scoring source: '{anime_src['name']}'  (minFormatScore={anime_src.get('minFormatScore')}, "
          f"x265-penalty={_has_x265_penalty(anime_src)})\n")

    to_create = []
    for src_name, new_name in _TIERS:
        src = by_name.get(src_name.lower())
        if src is None:  # name miss → fall back to closest by max-resolution
            target = {"sd": 576, "hd-720p": 720, "hd-1080p": 1080, "ultra-hd": 2160}[src_name.lower()]
            src = min(profs, key=lambda p: abs(_max_res(p.get("items")) - target))
            print(f"  note: tier '{src_name}' not found by name — using '{src['name']}' (max {_max_res(src['items'])}p)")
        if new_name.lower() in by_name:
            print(f"  ✓ '{new_name}' already exists — skipping")
            continue
        # Inherit EVERY setting from the anime profile (CF scoring, min/cutoff/upgrade format scores,
        # language, etc. — so we never miss a Sonarr-required field), then override only the quality
        # ladder + name + cutoff to this resolution tier.
        payload = dict(anime_src)
        payload.pop("id", None)
        payload["name"] = new_name
        payload["items"] = src.get("items")          # this tier's allowed qualities
        payload["cutoff"] = src.get("cutoff")        # a cutoff valid within this tier's qualities
        to_create.append(payload)
        allowed = _allowed_names(src.get("items"))
        print(f"  + '{new_name}'  (from '{src['name']}', {len(allowed)} qualities ≤{_max_res(src['items'])}p, "
              f"minFormatScore={payload['minFormatScore']}, x265 NOT penalized)")

    if not to_create:
        print("\nNothing to create.")
        return 0

    if not args.apply:
        print(f"\nDRY-RUN — would create {len(to_create)} anime profile(s). Re-run with --apply to write them.")
        return 0

    print()
    for payload in to_create:
        r = requests.post(f"{base}/api/v3/qualityprofile", headers={"X-Api-Key": api}, json=payload, timeout=60)
        if r.status_code in (200, 201):
            print(f"  CREATED '{payload['name']}' (id={r.json().get('id')})")
        else:
            print(f"  FAILED '{payload['name']}': {r.status_code} {r.text[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

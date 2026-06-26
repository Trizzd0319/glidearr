"""arr_profiles_build.py — generate the merged, deduped, codec-aware quality-profile blueprint.

Offline + non-destructive. Produces the rebuild SPEC the apply tool will install:
  • keeps your DEDUPED base tiers (drops the English- clones + Any/SD; English priority becomes
    universal CF scoring instead);
  • PRESERVES your tuned per-tier CF scores (audio/streaming/codec) from the export — TRaSH supplies
    the canonical CF *definitions*, your *scores* stay;
  • clones the main tiers into codec/device variants (Universal-H264 / HEVC / HEVC+DV / AV1) via a
    thin score overlay (our items win);
  • bakes English priority in (English Audio / Dual Audio + , Language: Not Original − ).

CF scores are carried by NAME; the apply tool maps name→id against the live schema after installing
the CFs. Run:  python -m scripts.support.tools.arr_profiles_build
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
PROF = _REPO_ROOT / "scripts" / "support" / "profiles"

# ── taxonomy ──────────────────────────────────────────────────────────────────
# Deduped agnostic base tiers (TRaSH compat-first scoring preserved) — the quality ladder lives here.
RADARR_BASE = ["HD-720p", "HD-1080p", "HD - 720p/1080p", "Ultra-HD",
               "HD Bluray + WEB", "Remux + WEB 1080p", "Remux 2160p (Combined)", "UHD Bluray + WEB"]
SONARR_BASE = ["HD-720p", "HD-1080p", "HD - 720p/1080p", "Ultra-HD", "WEB-1080p", "WEB-2160p (Combined)",
               "[Anime] HD-1080p", "[Anime] Ultra-HD", "[Anime] Remux-1080p"]

# Which main tiers get codec/device variants, and which codecs (4K tiers drop H264, add the DV split).
RADARR_CODEC = {"HD Bluray + WEB": ["H264", "HEVC", "AV1"],
                "Remux + WEB 1080p": ["H264", "HEVC", "AV1"],
                "Remux 2160p (Combined)": ["HEVC", "HEVC+DV", "AV1"],
                "UHD Bluray + WEB": ["HEVC", "HEVC+DV", "AV1"]}
SONARR_CODEC = {"WEB-1080p": ["H264", "HEVC", "AV1"],
                "WEB-2160p (Combined)": ["HEVC", "HEVC+DV", "AV1"]}

# Codec/device score overlay (CF name → score), layered on the cloned base tier. Our items win.
_BLOCK = -35000
CODEC_OVERLAY = {
    "H264":    {"AVC/x264": 1500, "HEVC": _BLOCK, "x265 (HD)": _BLOCK, "x265 (no HDR/DV)": _BLOCK,
                "AV1": _BLOCK, "HDR or DV": _BLOCK, "Dolby Vision (steer)": _BLOCK, "10bit": -500},
    "HEVC":    {"HEVC": 1500, "x265 (HD)": 1000, "AVC/x264": 500, "AV1": _BLOCK,
                "Dolby Vision (steer)": _BLOCK},
    "HEVC+DV": {"HEVC": 1500, "x265 (HD)": 1000, "AVC/x264": 500, "AV1": _BLOCK},
    "AV1":     {"AV1": 1500, "HEVC": 800, "AVC/x264": 300, "Dolby Vision (steer)": _BLOCK},
}
_LABEL = {"H264": "H264", "HEVC": "HEVC", "HEVC+DV": "HEVC-DV", "AV1": "AV1"}

# English priority overlay (universal — replaces the English- clones). Anime keeps it soft (handled
# by leaving the anime profiles' existing dub/sub CF scoring intact + a gentler not-original).
ENGLISH_OVERLAY = {"English Audio": 1500, "Dual Audio": 500, "Language: Not Original": -200}


def _quality_items(p):
    """Return (items_by_name, cutoff_name) preserved from the source profile."""
    cutoff = p.get("cutoff")
    cutoff_name, items = None, []
    for it in p.get("items", []) or []:
        if it.get("items"):
            if it.get("id") == cutoff:
                cutoff_name = f"[{it.get('name')}]"
            items.append({"group": it.get("name"), "allowed": bool(it.get("allowed")),
                          "members": [{"name": (s.get("quality") or {}).get("name"),
                                       "allowed": bool(s.get("allowed"))} for s in it["items"]]})
        else:
            q = it.get("quality") or {}
            if q.get("id") == cutoff:
                cutoff_name = q.get("name")
            items.append({"name": q.get("name"), "allowed": bool(it.get("allowed"))})
    return items, cutoff_name


def _base_scores(p):
    """{cf_name: score} for the nonzero CF scores on a source profile (preserves the user's tuning)."""
    return {fi.get("name"): fi.get("score") for fi in (p.get("formatItems") or []) if fi.get("score")}


def _profile_spec(p, name, codec=None, anime=False):
    items, cutoff = _quality_items(p)
    scores = dict(_base_scores(p))
    if not anime:                                  # anime keeps its own dub/sub scoring untouched
        scores.update(ENGLISH_OVERLAY)
    if codec:
        scores.update(CODEC_OVERLAY[codec])
    return {
        "name": name,
        "upgradeAllowed": p.get("upgradeAllowed", True),
        "cutoff": cutoff,
        "minFormatScore": p.get("minFormatScore", 0),
        "cutoffFormatScore": p.get("cutoffFormatScore", 0),
        "minUpgradeFormatScore": p.get("minUpgradeFormatScore", 1),
        "language": "English" if (not anime and codec) else (p.get("language") or {}).get("name", "Any"),
        "items": items,
        "cf_scores": scores,
    }


def build(service: str):
    src = json.loads((PROF / service / "standard" / "qualityprofiles.json").read_text())
    by_name = {p["name"]: p for p in src}
    base_names = RADARR_BASE if service == "radarr" else SONARR_BASE
    codec_map = RADARR_CODEC if service == "radarr" else SONARR_CODEC

    profiles = []
    for nm in base_names:
        p = by_name.get(nm)
        if not p:
            print(f"  [warn] {service}: base tier {nm!r} not in export — skipped")
            continue
        profiles.append(_profile_spec(p, nm, anime=nm.startswith("[Anime]")))
    for tier, codecs in codec_map.items():
        p = by_name.get(tier)
        if not p:
            continue
        for c in codecs:
            profiles.append(_profile_spec(p, f"{tier} ({_LABEL[c]})", codec=c))

    # the CF install set = every CF any profile scores (nonzero) + our codec CFs.
    referenced = {n for pr in profiles for n in pr["cf_scores"]}
    out = PROF / "blueprint" / f"{service}_profiles.json"
    out.write_text(json.dumps({"service": service, "profiles": profiles}, indent=2), encoding="utf-8")
    print(f"  {service}: {len(profiles)} profiles "
          f"({len(base_names)} base + {sum(len(v) for v in codec_map.values())} codec variants), "
          f"{len(referenced)} CFs referenced  ->  {out.relative_to(_REPO_ROOT)}")
    return profiles, referenced


def main() -> int:
    total = 0
    for svc in ("radarr", "sonarr"):
        profiles, _ = build(svc)
        total += len(profiles)
    print(f"TOTAL: {total} quality profiles across both services.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

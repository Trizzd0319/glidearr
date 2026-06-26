"""
Create a true SD→720p Sonarr quality profile so the JIT/quality engine has a profile
to land on when a low-engagement, unwatched next-up episode earns a <=720p cap. Without
it, ``choose_jit_profile`` finds no profile with max-resolution <= the cap (every existing
Sonarr profile floors at 1080p — even the one *named* "HD-720p" allows Raw-HD/1080) and the
grab is silently skipped, misreported as a disk-reserve breach.

The new profile enables every quality at resolution <= 720 (SDTV / DVD / 480p / 576p / 720p,
plus 'Unknown' for odd old releases that never upscaled) and disables everything above 720,
so its max resolution is a true 720. Cutoff = Bluray-720p (targets 720, accepts down to SD).

Built by cloning an existing profile (default id 3) so the item structure matches THIS Sonarr
version exactly. Idempotent by name. Loads the instance URL + API key via the app's ConfigLoader
(env var -> OS keyring), so it works wherever the engine does.

Usage:
    python -m scripts.support.tools.sonarr_sd720_profile            # DRY RUN (no writes)
    python -m scripts.support.tools.sonarr_sd720_profile --apply    # create the profile
    python -m scripts.support.tools.sonarr_sd720_profile --instance standard --name "SD-720p"
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import requests

from scripts.managers.factories.config.config_loader import ConfigLoader

CONFIG_PATH = Path("scripts/support/config/config.json")
MAX_RES = 720          # everything strictly above this is disabled
CUTOFF_QUALITY_ID = 6  # Bluray-720p — the target the profile upgrades toward


def _cap_items(items: list) -> bool:
    """Recursively enable every quality leaf at resolution <= MAX_RES, disable the rest.
    A group is allowed iff any child is. Returns whether THIS list has any allowed leaf."""
    any_allowed = False
    for it in items:
        q = it.get("quality")
        if q is not None:                       # leaf quality
            res = q.get("resolution")
            it["allowed"] = res is not None and int(res) <= MAX_RES
            any_allowed = any_allowed or it["allowed"]
        else:                                   # group
            child_allowed = _cap_items(it.get("items", []))
            it["allowed"] = child_allowed
            any_allowed = any_allowed or child_allowed
    return any_allowed


def _max_allowed_res(items: list) -> int:
    best = 0
    for it in items:
        if it.get("allowed"):
            q = it.get("quality")
            if q and q.get("resolution"):
                best = max(best, int(q["resolution"]))
            if it.get("items"):
                best = max(best, _max_allowed_res(it["items"]))
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="create the profile (default: dry run)")
    ap.add_argument("--instance", default="standard")
    ap.add_argument("--name", default="SD-720p")
    ap.add_argument("--clone-from", type=int, default=3, help="source profile id to clone item structure from")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    inst = cfg["sonarr_instances"][args.instance]
    base = inst.get("base_url") or f"http://{inst['url']}:{inst['port']}"
    hdr = {"X-Api-Key": inst.get("api") or "", "Content-Type": "application/json"}

    print(f"{'='*72}\nMODE: {'APPLY' if args.apply else 'DRY RUN (no writes)'}  |  Sonarr '{args.instance}' {base}\n{'='*72}")

    profiles = requests.get(f"{base}/api/v3/qualityprofile", headers=hdr, timeout=30).json()
    by_name = {p["name"]: p for p in profiles}
    if args.name in by_name:
        print(f"Profile {args.name!r} already exists (id {by_name[args.name]['id']}) — nothing to do.")
        return

    src = next((p for p in profiles if p["id"] == args.clone_from), None) or profiles[0]
    new = copy.deepcopy(src)
    new.pop("id", None)
    new["name"] = args.name
    new["upgradeAllowed"] = True
    _cap_items(new["items"])
    new["cutoff"] = CUTOFF_QUALITY_ID

    allowed = [it["quality"]["name"] for it in _flat(new["items"]) if it.get("allowed") and it.get("quality")]
    print(f"cloned from {src['name']!r} (id {src['id']})")
    print(f"resulting max-allowed resolution: {_max_allowed_res(new['items'])}p  (target/cutoff: Bluray-720p)")
    print("allowed qualities:", ", ".join(allowed))

    if not args.apply:
        print("\n(dry run — nothing created. Re-run with --apply.)")
        return
    r = requests.post(f"{base}/api/v3/qualityprofile", headers=hdr, data=json.dumps(new), timeout=30)
    r.raise_for_status()
    print(f"\nCREATED profile {args.name!r} (id {r.json()['id']}).")


def _flat(items):
    for it in items:
        yield it
        if it.get("items"):
            yield from _flat(it["items"])


if __name__ == "__main__":
    main()

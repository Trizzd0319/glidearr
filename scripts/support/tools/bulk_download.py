"""
bulk_download.py
================
Iterates every monitored Sonarr series, sets each one to the lowest available
quality profile (SD if present, otherwise HD-720p), then triggers a season
or episode search so Sonarr grabs whatever is available at that quality.

The script uses two separate passes so profile changes are batched before
searches fire:

  Pass 1 — Profile assignment
    For every series whose current profile is NOT already the target, PUT the
    series record with the target profile ID.  Progress shown via tqdm.

  Pass 2 — Search trigger
    Trigger SeriesSearch for every series that was changed (or that has
    missing episodes even though it's already on the target profile).

Usage
-----
  python bulk_download.py [--dry-run] [--instance 720] [--search-missing-only]

Flags
-----
  --dry-run             Print what would happen without making any API calls.
  --instance NAME       Sonarr instance name from config (default: first one).
  --search-missing-only Skip series that already have the target profile set
                        and have at least one file — only search truly missing.
  --skip-profile-change Don't change the profile, just trigger searches at
                        whatever profile the series already has.

The script reads config from:
  scripts/support/config/config.json

Parquet used for context (not required — falls back to live API if absent):
  scripts/support/cache/sonarr/{instance}/episode_files.parquet

Exit codes
----------
  0  All series processed successfully (or dry-run).
  1  Sonarr unreachable or credential error.
  2  No quality profiles found.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR.parent / "config" / "config.json"  # scripts/support/config/

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"[bulk_download] Config not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_instance(cfg: dict, name: str | None) -> tuple[str, str, str]:
    """Return (instance_name, base_url, api_key)."""
    instances = cfg.get("sonarr_instances", {})
    if not name:
        name = instances.get("default_instance")
        if not name or not isinstance(name, str):
            name = next((k for k in instances if k != "default_instance"), None)
    if not name or name not in instances:
        sys.exit(f"[bulk_download] Sonarr instance '{name}' not found in config.")
    inst = instances[name]
    return name, inst["base_url"].rstrip("/"), inst["api"]

# ── Sonarr API ────────────────────────────────────────────────────────────────

class SonarrClient:
    def __init__(self, base_url: str, api_key: str, dry_run: bool = False):
        self.base    = base_url
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        self.dry_run = dry_run

    def get(self, endpoint: str) -> list | dict:
        r = requests.get(f"{self.base}/api/v3/{endpoint}", headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def put(self, endpoint: str, payload: dict) -> dict | None:
        if self.dry_run:
            return payload
        r = requests.put(
            f"{self.base}/api/v3/{endpoint}",
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def post_command(self, name: str, **kwargs) -> None:
        if self.dry_run:
            return
        payload = {"name": name, **kwargs}
        requests.post(
            f"{self.base}/api/v3/command",
            headers=self.headers,
            json=payload,
            timeout=60,
        ).raise_for_status()

# ── Profile selection ─────────────────────────────────────────────────────────

def _max_res(profile: dict) -> int:
    best = 0
    for item in (profile.get("items") or []):
        if not item.get("allowed"):
            continue
        res = (item.get("quality") or {}).get("resolution", 0)
        if isinstance(res, (int, float)):
            best = max(best, int(res))
        for sub in (item.get("items") or []):
            if sub.get("allowed"):
                sr = (sub.get("quality") or {}).get("resolution", 0)
                if isinstance(sr, (int, float)):
                    best = max(best, int(sr))
    return best


def pick_target_profile(profiles: list[dict]) -> dict:
    """
    Pick the best starting profile:
      1. A profile whose name contains 'SD' (case-insensitive).
      2. Otherwise the lowest-resolution profile available.
      3. If resolution tie-break, prefer name containing '720'.
    """
    # Prefer an explicitly named SD profile
    sd_profiles = [p for p in profiles if "sd" in (p.get("name") or "").lower()]
    if sd_profiles:
        # Among SD profiles, pick the lowest max-resolution one
        return sorted(sd_profiles, key=_max_res)[0]

    # Fall back to lowest resolution overall
    ranked = sorted(profiles, key=_max_res)
    if not ranked:
        sys.exit("[bulk_download] No quality profiles found in Sonarr.")
    return ranked[0]

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bulk-download all Sonarr series at SD/720p")
    parser.add_argument("--dry-run",           action="store_true")
    parser.add_argument("--instance",          default=None)
    parser.add_argument("--search-missing-only", action="store_true",
                        help="Only search series with no files at all")
    parser.add_argument("--skip-profile-change", action="store_true",
                        help="Don't change quality profiles — just trigger searches")
    args = parser.parse_args()

    cfg             = load_config()
    inst_name, url, key = get_instance(cfg, args.instance)
    client          = SonarrClient(url, key, dry_run=args.dry_run)

    print(f"[bulk_download] Instance: {inst_name} @ {url}")
    print(f"[bulk_download] dry_run={args.dry_run}")

    # ── Verify connectivity ────────────────────────────────────────────────────
    try:
        status  = client.get("system/status")
        version = status.get("version", "?")
        print(f"[bulk_download] Sonarr v{version} — connected ✅")
    except Exception as e:
        sys.exit(f"[bulk_download] ❌ Cannot reach Sonarr: {e}")

    # ── Quality profiles ───────────────────────────────────────────────────────
    try:
        profiles = client.get("qualityprofile")
    except Exception as e:
        sys.exit(f"[bulk_download] ❌ Cannot fetch quality profiles: {e}")

    target = pick_target_profile(profiles)
    target_id   = target["id"]
    target_name = target.get("name", str(target_id))
    print(f"\n[bulk_download] Target profile: '{target_name}' (id={target_id}, "
          f"max_res={_max_res(target)}p)")

    # ── Fetch all series ───────────────────────────────────────────────────────
    try:
        all_series = client.get("series")
    except Exception as e:
        sys.exit(f"[bulk_download] ❌ Cannot fetch series list: {e}")

    monitored = [s for s in all_series if s.get("monitored")]
    print(f"[bulk_download] {len(monitored):,} monitored series out of {len(all_series):,} total\n")

    if not monitored:
        print("[bulk_download] Nothing to do.")
        return

    # ── tqdm ───────────────────────────────────────────────────────────────────
    try:
        from tqdm import tqdm
    except ImportError:
        # No tqdm available — degrade to a no-op shim (no progress bar) rather
        # than auto-installing at runtime (unpinned/unhashed pip = supply risk).
        class tqdm:                              # noqa: N801 (mirror tqdm name)
            def __init__(self, iterable=None, *a, **k):
                self._iterable = iterable if iterable is not None else []

            def __iter__(self):
                return iter(self._iterable)

            def update(self, *a, **k):
                pass

            def close(self, *a, **k):
                pass

            def set_description(self, *a, **k):
                pass

            @staticmethod
            def write(msg="", *a, **k):
                print(msg)

    # ── Pass 1: Profile assignment ─────────────────────────────────────────────
    changed_ids: list[int] = []   # series IDs that got a profile change
    error_count = 0

    if not args.skip_profile_change:
        print("Pass 1 — setting quality profiles:")
        for series in tqdm(monitored, unit="series", dynamic_ncols=True, leave=True):
            sid         = series["id"]
            title       = series.get("title", f"series {sid}")
            current_pid = series.get("qualityProfileId")

            if current_pid == target_id:
                # Already on target profile — include in search pass if missing files
                stats = series.get("statistics", {})
                if stats.get("episodeFileCount", 0) == 0:
                    changed_ids.append(sid)   # no files → still needs a search
                continue

            if args.search_missing_only:
                stats = series.get("statistics", {})
                if stats.get("episodeFileCount", 0) > 0:
                    continue   # has some files, skip profile change

            try:
                series["qualityProfileId"] = target_id
                client.put(f"series/{sid}", series)
                changed_ids.append(sid)
            except Exception as e:
                tqdm.write(f"  ⚠️ Profile change failed for '{title}': {e}")
                error_count += 1

        if args.dry_run:
            print(f"\n[dry_run] Would update {len(changed_ids)} series to '{target_name}'.")
        else:
            print(f"\n  Profile updated: {len(changed_ids):,} series  |  errors: {error_count}")
    else:
        # No profile change — search everything (or just missing)
        for series in monitored:
            stats = series.get("statistics", {})
            if args.search_missing_only and stats.get("episodeFileCount", 0) > 0:
                continue
            changed_ids.append(series["id"])
        print(f"[bulk_download] Skipping profile changes — {len(changed_ids):,} series queued for search.\n")

    if not changed_ids:
        print("[bulk_download] No series need searching. All done.")
        return

    # ── Pass 2: Trigger searches ───────────────────────────────────────────────
    # SeriesSearch is expensive; batch in groups of 50 to avoid flooding Sonarr.
    BATCH     = 50
    SLEEP_S   = 2.0   # seconds between batches — gives Sonarr time to breathe
    searched  = 0
    s_errors  = 0

    id_to_title = {s["id"]: s.get("title", str(s["id"])) for s in monitored}
    batches     = [changed_ids[i:i + BATCH] for i in range(0, len(changed_ids), BATCH)]
    n_batches   = len(batches)

    print(f"Pass 2 — triggering searches ({len(changed_ids):,} series, {n_batches} batch(es)):")

    for b_idx, batch in enumerate(tqdm(batches, unit="batch", dynamic_ncols=True, leave=True), 1):
        try:
            client.post_command("SeriesSearch", seriesIds=batch)
            searched += len(batch)
            if b_idx < n_batches:
                time.sleep(SLEEP_S)
        except Exception as e:
            tqdm.write(f"  ⚠️ Search batch {b_idx}/{n_batches} failed: {e}")
            s_errors += len(batch)

    # ── Summary ────────────────────────────────────────────────────────────────
    prefix = "[dry_run] " if args.dry_run else ""
    print(f"\n{prefix}Done.")
    print(f"  Profile '{target_name}' applied to : {len(changed_ids):,} series")
    print(f"  Searches triggered                 : {searched:,} series")
    if error_count or s_errors:
        print(f"  Errors (profile / search)          : {error_count} / {s_errors}")
    print()
    print("Sonarr will now grab whatever releases it finds at this quality profile.")
    print("Run the main Glidearr pipeline to begin upgrading from there.")


if __name__ == "__main__":
    main()

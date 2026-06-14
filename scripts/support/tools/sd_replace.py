"""
sd_replace.py
=============
For every non-kids monitored Sonarr series that has existing episode files:

  1. Find the SD quality profile (or HD-720p if SD doesn't exist).
  2. Skip series already at or below that quality (no point deleting).
  3. Skip kids series (by root folder path or certification).
  4. Set the series quality profile to the target.
  5. DELETE all episodefile records via the Sonarr API.
  6. Trigger SeriesSearch so Sonarr grabs the SD versions.

Note on "no SD available":
  We do NOT pre-check indexers for each series — that would take hours.
  Instead, if Sonarr can't find an SD release after searching, the episode
  simply stays missing. The Glidearr pilot search will then step the
  quality back up automatically over subsequent runs until something lands.

Kids detection — series is skipped if ANY is true:
  • Root folder path contains: kids, kid, children, family, cartoon, bluey
  • Certification is one of: G, PG, TV-G, TV-Y, TV-Y7

Usage
-----
  cd scripts/support/tools/
  python sd_replace.py --dry-run          # preview only, no changes
  python sd_replace.py --limit 10         # test on 10 series first
  python sd_replace.py                    # full run (prompts YES to confirm)
  python sd_replace.py --confirm          # full run, skip confirmation prompt

Flags
-----
  --dry-run     Print what would happen without making any API calls.
  --instance    Sonarr instance name from config (default: first configured).
  --limit N     Process at most N series.
  --confirm     Skip the interactive YES prompt.

Exit codes: 0=success, 1=connection error, 2=no profiles found.
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

# ── Kids detection ────────────────────────────────────────────────────────────

_KIDS_PATH_MARKERS = frozenset({
    "kids", "kid", "children", "family", "cartoon", "bluey",
})
_KIDS_CERTS = frozenset({
    "G", "PG", "TV-G", "TV-Y", "TV-Y7",
})


def is_kids(series: dict) -> bool:
    path  = (series.get("rootFolderPath") or series.get("path") or "").lower()
    parts = {p for seg in path.replace("\\", "/").split("/") for p in [seg.strip()]}
    if parts & _KIDS_PATH_MARKERS:
        return True
    cert = (series.get("certification") or "").strip().upper()
    return cert in _KIDS_CERTS

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Category → root-folder resolution (shared by router_show / router_movie) ───
#
# The rest of Glidearr treats the LIVE *arr instance as the source of truth for
# which root folders exist (see resolver._pick_root_folder, gateway.root_folders and
# the *_api.get_root_folders calls): config ``rootFolders`` / ``movieRootFolders`` is
# only a routing HINT, honoured when it points at a folder the instance actually has
# registered. The routers used to hard-require config==registered, which broke the
# moment the live layout moved (e.g. ``/media/tv`` → ``/data/media/tv``). This helper
# resolves each category the app's way so the routers follow the live instance instead
# of forcing config to match them.

def _rf_norm(p: str | None) -> str:
    """Normalise a path for comparison: forward slashes, no trailing slash, lower."""
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _rf_leaf(p: str | None) -> str:
    """Last path segment, normalised (e.g. ``/data/media/tv/anime`` → ``anime``)."""
    return _rf_norm(p).rsplit("/", 1)[-1]


def resolve_category_roots(registered, categories, config_roots, default_category,
                           aliases=None):
    """
    Map each library category to a usable target root-folder PATH, the way the rest
    of the app does: the live instance's registered folders are authoritative and
    ``config_roots`` is only an override honoured when it is itself registered.

    Precedence per category (first hit wins):
      1. ``config_roots[cat]`` when set AND registered on the instance (explicit override),
      2. a registered folder whose leaf-name matches ``cat`` or one of its aliases,
      3. inherit ``default_category`` (e.g. TV kids→series, movie 4k→standard),
         resolved by the same two rules — mirrors resolver._pick_root_folder's
         ``rootFolders.get(cat) or rootFolders.get('series')`` precedence.

    Args:
        registered: the *arr ``GET /rootfolder`` payload (list of ``{"path": ...}``).
        categories: ordered iterable of category names (CATEGORY_ORDER / MOVIE_CATEGORY_ORDER).
        config_roots: config ``rootFolders`` / ``movieRootFolders`` dict (hint only).
        default_category: the sink category every other inherits (``series`` / ``standard``).
        aliases: optional ``{cat: (extra-leaf, …)}`` for folder names that differ from the
                 category name (e.g. ``documentary`` → live folder ``documentaries``).

    Returns:
        ``{cat: {"path": str|None, "via": "own"|"inherit"|None, "reason": str|None}}``
        in ``categories`` order. ``path`` is None only for categories with no own folder
        and no ``default_category`` folder to inherit.
    """
    aliases = aliases or {}
    config_roots = config_roots or {}
    reg_paths = [rf.get("path") for rf in (registered or [])
                 if isinstance(rf, dict) and rf.get("path")]
    reg_norm = {_rf_norm(p) for p in reg_paths}
    leaf_to_path: dict[str, str] = {}
    for p in reg_paths:
        leaf_to_path.setdefault(_rf_leaf(p), p)   # first registered folder per leaf wins

    def _own(cat: str):
        hint = (config_roots.get(cat) or "").strip()
        if hint and _rf_norm(hint) in reg_norm:
            return hint                                  # explicit, still-valid config override
        for cand in (cat, *aliases.get(cat, ())):
            match = leaf_to_path.get(_rf_leaf(cand))
            if match:
                return match                             # live folder matched by leaf-name
        return None

    own = {cat: _own(cat) for cat in categories}
    default_path = own.get(default_category) or _own(default_category)

    out: dict[str, dict] = {}
    for cat in categories:
        if own.get(cat):
            out[cat] = {"path": own[cat], "via": "own", "reason": None}
        elif default_path:
            out[cat] = {"path": default_path, "via": "inherit", "reason": None}
        else:
            out[cat] = {"path": None, "via": None,
                        "reason": (f"no '{cat}' root folder on the instance and no "
                                   f"'{default_category}' folder to inherit")}
    return out


def get_instance(cfg: dict, name: str | None) -> tuple[str, str, str]:
    instances = cfg.get("sonarr_instances", {})
    if not name:
        default = instances.get("default_instance")
        name    = str(default) if default else None
    if not name:
        name = next((k for k in instances if k != "default_instance"), None)
    if not name or str(name) not in instances:
        sys.exit(f"Sonarr instance '{name}' not found in config.")
    inst = instances[str(name)]
    return str(name), inst["base_url"].rstrip("/"), inst["api"]

# ── Sonarr client ─────────────────────────────────────────────────────────────

class SonarrClient:
    def __init__(self, base: str, key: str, dry_run: bool = False):
        self.base    = base
        self.headers = {"X-Api-Key": key, "Content-Type": "application/json"}
        self.dry_run = dry_run
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    def get(self, ep: str, params: dict | None = None) -> list | dict:
        r = self._session.get(
            f"{self.base}/api/v3/{ep}", params=params, timeout=60
        )
        r.raise_for_status()
        return r.json()

    def put(self, ep: str, payload: dict) -> dict:
        if self.dry_run:
            return payload
        r = self._session.put(
            f"{self.base}/api/v3/{ep}", json=payload, timeout=60
        )
        r.raise_for_status()
        return r.json()

    def delete(self, ep: str) -> None:
        if self.dry_run:
            return
        self._session.delete(
            f"{self.base}/api/v3/{ep}", timeout=30
        ).raise_for_status()

    def post_command(self, name: str, **kwargs) -> None:
        if self.dry_run:
            return
        self._session.post(
            f"{self.base}/api/v3/command",
            json={"name": name, **kwargs}, timeout=60,
        ).raise_for_status()

# ── Quality helpers ───────────────────────────────────────────────────────────

def _max_res(profile: dict) -> int:
    """Return the highest allowed resolution in a quality profile."""
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


def pick_target(profiles: list[dict]) -> dict:
    """
    Select the best starting-point quality profile:
      1. A profile whose name contains 'SD' (case-insensitive).
      2. Otherwise the lowest-resolution profile available.
    """
    sd = [p for p in profiles if "sd" in (p.get("name") or "").lower()]
    if sd:
        return sorted(sd, key=_max_res)[0]
    ranked = sorted(profiles, key=_max_res)
    if not ranked:
        sys.exit("No quality profiles found in Sonarr. Cannot continue.")
    return ranked[0]

# ── Episode file count ────────────────────────────────────────────────────────

def ep_file_count(series: dict) -> int:
    return (series.get("statistics") or {}).get("episodeFileCount", 0)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Replace all non-kids episode files with SD/720p versions."
    )
    ap.add_argument("--dry-run",  action="store_true",
                    help="Preview only — no API calls that modify data.")
    ap.add_argument("--instance", default=None,
                    help="Sonarr instance name (default: first in config).")
    ap.add_argument("--limit",    type=int, default=None,
                    help="Process at most N series (for testing).")
    ap.add_argument("--confirm",  action="store_true",
                    help="Skip the interactive YES confirmation prompt.")
    args = ap.parse_args()

    cfg                 = load_config()
    inst_name, url, key = get_instance(cfg, args.instance)
    client              = SonarrClient(url, key, dry_run=args.dry_run)

    DRY = "[dry-run] " if args.dry_run else ""

    print(f"\n{'='*62}")
    print(f"  sd_replace.py  {'(DRY RUN)' if args.dry_run else '*** LIVE MODE ***'}")
    print(f"  Instance : {inst_name} @ {url}")
    print(f"{'='*62}\n")

    # ── Connectivity check ────────────────────────────────────────────────────
    try:
        ver = client.get("system/status").get("version", "?")
        print(f"Sonarr v{ver} ✅\n")
    except Exception as e:
        sys.exit(f"❌ Cannot reach Sonarr: {e}")

    # ── Quality profiles ──────────────────────────────────────────────────────
    try:
        profiles = client.get("qualityprofile")
    except Exception as e:
        sys.exit(f"❌ Cannot fetch quality profiles: {e}")

    target     = pick_target(profiles)
    target_id  = target["id"]
    target_res = _max_res(target)
    target_nm  = target.get("name", str(target_id))
    # SD threshold: 576p is the canonical SD ceiling; 720p if no SD profile
    sd_ceiling = max(target_res, 576)

    print("Quality profiles available:")
    for p in sorted(profiles, key=_max_res):
        marker = " ◀ TARGET" if p["id"] == target_id else ""
        print(f"  [{p['id']:2d}] {p['name']:<30} max={_max_res(p)}p{marker}")
    print(f"\nTarget : '{target_nm}' (id={target_id}, max={target_res}p)\n")

    # ── Fetch all series ──────────────────────────────────────────────────────
    try:
        all_series = client.get("series")
    except Exception as e:
        sys.exit(f"❌ Cannot fetch series list: {e}")

    monitored  = [s for s in all_series if s.get("monitored")]
    has_files  = [s for s in monitored  if ep_file_count(s) > 0]

    # Split into categories
    kids_list     = [s for s in has_files if is_kids(s)]
    non_kids      = [s for s in has_files if not is_kids(s)]

    # Already at or below SD ceiling — skip (nothing to downgrade)
    already_sd    = []
    needs_replace = []
    for s in non_kids:
        cur_pid = s.get("qualityProfileId")
        cur_p   = next((p for p in profiles if p["id"] == cur_pid), None)
        cur_res = _max_res(cur_p) if cur_p else 9999
        if cur_res <= sd_ceiling:
            already_sd.append(s)
        else:
            needs_replace.append(s)

    print(f"Series totals:")
    print(f"  All series              : {len(all_series):>6,}")
    print(f"  Monitored               : {len(monitored):>6,}")
    print(f"  Monitored + has files   : {len(has_files):>6,}")
    print(f"  Kids (will be skipped)  : {len(kids_list):>6,}")
    print(f"  Already SD/below        : {len(already_sd):>6,}")
    print(f"  ➜ Will process          : {len(needs_replace):>6,}")
    print()

    if args.limit:
        needs_replace = needs_replace[:args.limit]
        print(f"--limit applied: processing {len(needs_replace)} series.\n")

    if not needs_replace:
        print("Nothing to process. Exiting.")
        return

    # ── Sample preview ────────────────────────────────────────────────────────
    print(f"Sample of series that will be processed (first 20):")
    for s in needs_replace[:20]:
        cpid  = s.get("qualityProfileId")
        cp    = next((p for p in profiles if p["id"] == cpid), None)
        cur_q = cp.get("name", "?") if cp else "?"
        nf    = ep_file_count(s)
        print(f"  {s.get('title','?'):<45}  {cur_q} → {target_nm}  ({nf} files)")
    if len(needs_replace) > 20:
        print(f"  … and {len(needs_replace)-20:,} more")
    print()

    # ── Confirmation ──────────────────────────────────────────────────────────
    if not args.dry_run and not args.confirm:
        total_files = sum(ep_file_count(s) for s in needs_replace)
        print(f"⚠️  About to permanently DELETE approximately {total_files:,} episode")
        print(f"   files across {len(needs_replace):,} series and re-download at")
        print(f"   '{target_nm}' quality.  This cannot be undone.")
        print()
        answer = input('Type "YES" to proceed, anything else to cancel: ').strip()
        if answer != "YES":
            print("Cancelled — no changes made.")
            return
        print()

    # ── tqdm ──────────────────────────────────────────────────────────────────
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

    # ── Pass 1: Set quality profile ───────────────────────────────────────────
    p_changed = 0
    p_errors  = 0
    print(f"Pass 1/3 — {DRY}setting quality profile to '{target_nm}':")

    for series in tqdm(needs_replace, unit="series", dynamic_ncols=True, leave=True):
        if series.get("qualityProfileId") == target_id:
            p_changed += 1   # already correct, still count it
            continue
        try:
            series["qualityProfileId"] = target_id
            client.put(f"series/{series['id']}", series)
            p_changed += 1
        except Exception as e:
            tqdm.write(f"  ⚠️ Profile set failed — '{series.get('title')}': {e}")
            p_errors += 1

    print(f"  {DRY}Profile updated: {p_changed:,}  |  errors: {p_errors}\n")

    # ── Pass 2: Delete episode files ──────────────────────────────────────────
    d_files  = 0
    d_errors = 0
    print(f"Pass 2/3 — {DRY}deleting episode files:")

    for series in tqdm(needs_replace, unit="series", dynamic_ncols=True, leave=True):
        sid   = series["id"]
        title = series.get("title", str(sid))
        try:
            ef_list = client.get("episodefile", params={"seriesId": sid})
            for ef in (ef_list or []):
                fid = ef.get("id")
                if fid:
                    client.delete(f"episodefile/{fid}")
                    d_files += 1
        except Exception as e:
            tqdm.write(f"  ⚠️ Delete failed — '{title}': {e}")
            d_errors += 1

    print(f"  {DRY}Files deleted: {d_files:,}  |  errors: {d_errors}\n")

    # ── Pass 3: Trigger SeriesSearch ──────────────────────────────────────────
    BATCH    = 50
    SLEEP_S  = 2.0
    s_done   = 0
    s_errors = 0

    ids     = [s["id"] for s in needs_replace]
    batches = [ids[i:i+BATCH] for i in range(0, len(ids), BATCH)]
    print(f"Pass 3/3 — {DRY}triggering searches "
          f"({len(ids):,} series, {len(batches)} batch(es)):")

    for b_i, batch in enumerate(
        tqdm(batches, unit="batch", dynamic_ncols=True, leave=True), 1
    ):
        try:
            client.post_command("SeriesSearch", seriesIds=batch)
            s_done += len(batch)
            if b_i < len(batches):
                time.sleep(SLEEP_S)
        except Exception as e:
            tqdm.write(f"  ⚠️ Search batch {b_i}/{len(batches)} failed: {e}")
            s_errors += len(batch)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  {DRY}Complete")
    print(f"  Series processed         : {len(needs_replace):>6,}")
    print(f"  Episode files deleted    : {d_files:>6,}")
    print(f"  Searches triggered       : {s_done:>6,}")
    print(f"  Kids series skipped      : {len(kids_list):>6,}")
    print(f"  Already SD (skipped)     : {len(already_sd):>6,}")
    if p_errors or d_errors or s_errors:
        print(f"  Errors (profile/del/srch): {p_errors}/{d_errors}/{s_errors}")
    print(f"{'='*62}")
    if not args.dry_run:
        print()
        print("Sonarr is now searching for SD releases.")
        print("Glidearr will upgrade from SD as episodes are watched.")


if __name__ == "__main__":
    main()

"""
router_movie.py
===============
Re-sort the EXISTING Radarr library so every movie lives in the root folder for
its correct Plex library — kids → kids, anime → anime, 2160p/UHD → 4k, everything
else → standard.

It classifies each movie with the shared standard in
``support/utilities/library_classifier.py`` (movie precedence: kids → anime → 4k
→ standard — CONTENT wins over resolution), compares that to where the movie
currently sits, and applies the difference via Radarr's ``movie/editor`` endpoint
with ``moveFiles=true`` (Radarr relocates the file on disk).

This is the movie counterpart to ``router_show.py`` (TV / Sonarr).

Classification (precedence — first match wins):
  • anime    — an explicit anime genre, or Japanese/Korean animation (animated AND
               originalLanguage Japanese/Korean). (A 'Preschool' GENRE beats anime → kids.)
  • kids     — a Children/Kids/Preschool GENRE, OR the 'Family' genre when kid-safe
               rated (≤ PG, or unrated incl. 'NR') AND free of a war/crime/thriller/horror
               genre. A bare G/PG CERTIFICATE does NOT route a movie to Kids — rating
               inflation puts classics/war epics/franchises at G/PG, so movie Kids routing
               is GENRE-driven (unlike TV, which keeps its TV-G/TV-Y cert route).
  • 4k       — the movie FILE is 2160p/UHD and the movie is neither kids nor anime.
  • standard — anything else (the default movie library).

The 4k bucket is a RESOLUTION axis, so it depends on the file on disk: a movie
with no file (or a sub-2160p file) can only be kids/anime/standard. Once a UHD
file lands, a later run relocates it to 4k.

Scope / safety:
  • Standalone: reads NOTHING from config.json. Radarr base URLs come from the
    built-in ``RADARR_INSTANCES`` map (env-overridable); API keys come from the OS
    keyring / ``RECOMMENDARR_*`` env vars; classification uses the library_classifier's
    own tight built-in genre/cert defaults.
  • Same-instance only. It moves a movie between root folders on the instance it
    already lives on; it does NOT migrate across Radarr instances.
  • Move targets follow the LIVE Radarr instance: each category routes to the
    registered root folder whose name matches it (anime→anime, kids→kids, 4k→4k).
    A category with no folder of its own inherits the ``standard`` library; a
    category with no usable folder at all is skipped.
  • Dry-run safe: nothing changes without an explicit confirmation.

Usage
-----
  cd scripts/support/tools/
  python router_movie.py --dry-run            # preview only, no changes
  python router_movie.py --dry-run --explain  # preview + per-movie reason
  python router_movie.py --limit 10           # apply to 10 movies (test)
  python router_movie.py                       # full run (prompts YES)
  python router_movie.py --confirm             # full run, skip the prompt

Flags
-----
  --dry-run     Print the planned actions without calling Radarr.
  --explain     List every planned action with the matched signal (e.g.
                "[anime:japanese-animation]"), instead of a 20-row sample.
  --instance    Radarr instance name from config (default: first configured).
  --limit N     Apply to at most N movies (after grouping; for testing).
  --batch N     Movies per editor/move call (default 100).
  --confirm     Skip the interactive YES prompt.

Exit codes: 0=success, 1=connection error, 2=no usable target folders.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import requests

# Standalone bootstrap: this file lives at scripts/support/tools/ — put scripts/
# on sys.path so the bare `support.*` imports resolve from any invocation cwd.
# (`sd_replace` is same-dir, so sys.path[0] already covers it.)
_SCRIPTS_DIR = Path(__file__).resolve().parents[2]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the canonical helpers used by the other standalone scripts (same dir).
# NOTE: load_config is intentionally NOT imported — this tool is STANDALONE and reads
# nothing from config.json (connection map + classifier defaults are below).
from sd_replace import resolve_category_roots
from support.utilities.library_classifier import (
    MOVIE_CATEGORY_ORDER,
    classify_movie_explained,
    is_uhd_resolution,
)

# ── Standalone configuration (NO config.json) ─────────────────────────────────
# Radarr instances on the homelab, name → base URL. API keys are NOT stored here:
# they come from the OS keyring / RECOMMENDARR_* env vars via SecretStore, so this
# file carries no secrets. Override a base URL at runtime with the matching env var
# RECOMMENDARR_RADARR_INSTANCES_<NAME>_BASE_URL if your setup differs.
RADARR_INSTANCES = {
    "standard": "http://192.168.1.110:8988",
    "ultra":    "http://192.168.1.110:8989",
}
DEFAULT_RADARR_INSTANCE = "standard"

# Optional Radarr tag label that pins a tagged movie to the Kids library regardless
# of genre/cert (was config 'forceKidsTag'). Empty = disabled.
FORCE_KIDS_TAG = ""

# Live-folder leaf aliases for movie categories whose Radarr root folder may be
# named differently from the category (e.g. a '4k' library called 'uhd'/'2160p').
MOVIE_ROOT_ALIASES = {"4k": ("uhd", "2160", "2160p")}

# Classification uses the library_classifier's OWN built-in genre/cert defaults
# (tight); the router no longer feeds it config genre lists.


# ── instance + secret resolution ──────────────────────────────────────────────

def _load_secret_store():
    """
    Load SecretStore straight from its file, bypassing the config package
    ``__init__`` (which uses ``scripts.``-prefixed imports that don't resolve when
    this script is run from inside ``scripts/``). Returns None if unavailable.
    """
    path = _SCRIPTS_DIR / "managers" / "factories" / "config" / "secret_store.py"
    try:
        spec = importlib.util.spec_from_file_location("_mr_secret_store", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.SecretStore()
    except Exception:
        return None


def resolve_instance(name: str | None) -> tuple[str, str, str]:
    """
    Return ``(name, base_url, api_key)`` for a Radarr instance — STANDALONE, no
    config.json. The base URL comes from the hardcoded ``RADARR_INSTANCES`` map
    (override per instance via ``RECOMMENDARR_RADARR_INSTANCES_<NAME>_BASE_URL``); the
    API key comes from the OS keyring / ``RECOMMENDARR_RADARR_INSTANCES_<NAME>_API``
    env var via SecretStore — so no secret is stored in this file.
    """
    name = str(name or DEFAULT_RADARR_INSTANCE)
    base = (os.environ.get(f"RECOMMENDARR_RADARR_INSTANCES_{name.upper()}_BASE_URL")
            or RADARR_INSTANCES.get(name) or "").rstrip("/")
    if not base:
        sys.exit(f"Unknown Radarr instance '{name}'. Known: {', '.join(RADARR_INSTANCES)}.")

    api = ""
    store = _load_secret_store()
    if store is not None:
        api = store.get(f"radarr_instances.{name}.api") or ""
    if not api:
        api = os.environ.get(f"RECOMMENDARR_RADARR_INSTANCES_{name.upper()}_API") or ""
    return name, base, api


# ── Radarr client ─────────────────────────────────────────────────────────────

class RadarrClient:
    def __init__(self, base: str, key: str, dry_run: bool = False):
        self.base = base
        self.dry_run = dry_run
        self._session = requests.Session()
        self._session.headers.update(
            {"X-Api-Key": key, "Content-Type": "application/json"}
        )

    def get(self, ep: str, params: dict | None = None) -> list | dict:
        r = self._session.get(f"{self.base}/api/v3/{ep}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def put(self, ep: str, payload: dict) -> dict | list:
        if self.dry_run:
            return payload
        r = self._session.put(f"{self.base}/api/v3/{ep}", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()


# ── path helpers ──────────────────────────────────────────────────────────────

def _norm(p: str | None) -> str:
    """Normalise a path for comparison: forward slashes, no trailing slash, lower."""
    return (p or "").replace("\\", "/").rstrip("/").lower()


def current_root(movie: dict, registered_norm: set[str]) -> str:
    """Best guess at the movie's current root folder (normalised)."""
    rfp = _norm(movie.get("rootFolderPath"))
    if rfp:
        return rfp
    path = _norm(movie.get("path"))
    best = ""
    for r in registered_norm:
        if r and (path == r or path.startswith(r + "/")) and len(r) > len(best):
            best = r
    return best


# ── classification ────────────────────────────────────────────────────────────

def _olang(movie: dict):
    ol = movie.get("originalLanguage")
    return ol.get("name") if isinstance(ol, dict) else ol


def _is_uhd(movie: dict) -> bool:
    """UHD if the movie's file is 2160p — by resolution, pixel height, or quality name."""
    mf = movie.get("movieFile") or {}
    if not mf:
        return False
    quality = ((mf.get("quality") or {}).get("quality")) or {}
    media = mf.get("mediaInfo") or {}
    return is_uhd_resolution(
        height=media.get("height"),
        resolution=quality.get("resolution"),
        quality_name=quality.get("name"),
    )


# Common Sense Media recommended-age cache (tmdbId → age|null), populated by
# enrich_csm_ages.py from MDBList. PRIMARY kids signal; a missing/null entry lets the
# classifier fall back to the studio/animation heuristics. Loaded once, lazily.
KIDS_AGE_MAX = 11                            # CSM age at/under which a movie is 'kids'
                                             # (= oldest genuine Pixar/Disney animation; 12+ = live-action outliers)
_CSM_AGE_CACHE: "dict | None" = None


def _csm_age(tmdb_id) -> "int | None":
    global _CSM_AGE_CACHE
    if _CSM_AGE_CACHE is None:
        path = _SCRIPTS_DIR / "support" / "cache" / "mdblist" / "age_ratings.json"
        try:
            with open(path, encoding="utf-8") as f:
                _CSM_AGE_CACHE = json.load(f) or {}
            print(f"Common Sense ages: {sum(1 for v in _CSM_AGE_CACHE.values() if isinstance(v, int)):,} "
                  f"cached (of {len(_CSM_AGE_CACHE):,} looked up).")
        except Exception:
            _CSM_AGE_CACHE = {}
            print("Common Sense ages: no cache yet (run enrich_csm_ages.py) — using studio/animation only.")
    if not tmdb_id:
        return None
    v = _CSM_AGE_CACHE.get(str(tmdb_id))
    return v if isinstance(v, int) else None


def classify(movie: dict) -> tuple[str, str]:
    """Return ``(category, reason)`` for a Radarr movie dict, using the classifier's
    built-in (tight) defaults plus the Common Sense age cache as the primary signal."""
    return classify_movie_explained(
        genres=movie.get("genres"),
        certification=movie.get("certification"),
        original_language=_olang(movie),
        studio=movie.get("studio"),          # kids/family studio allowlist (fallback)
        recommended_age=_csm_age(movie.get("tmdbId")),   # Common Sense age (primary)
        kids_age_max=KIDS_AGE_MAX,
        is_anime_hint=False,                 # Radarr carries no source hint
        is_uhd=_is_uhd(movie),
    )


def _action_line(a: dict) -> str:
    tgt = a["target"] or "(stay)"
    return f"  {a['title'][:40]:<40} <{a.get('cert', '?'):<6}> {a['cur_root_disp']} → {tgt}  [{a['cat']}:{a['reason']}]"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Route Radarr movies into the correct library root folder (kids/anime/4k/standard)."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only — no API calls that modify data.")
    ap.add_argument("--explain", action="store_true",
                    help="List every planned action with its matched signal.")
    ap.add_argument("--instance", default=None,
                    help="Radarr instance name from the built-in RADARR_INSTANCES map "
                         f"(default: {DEFAULT_RADARR_INSTANCE}).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Apply to at most N movies (for testing).")
    ap.add_argument("--batch", type=int, default=100,
                    help="Movies per editor/move call. Smaller = more resumable. Default 100.")
    ap.add_argument("--confirm", action="store_true",
                    help="Skip the interactive YES confirmation prompt.")
    ap.add_argument("--library", default=None,
                    help="Only consider movies CURRENTLY in this library/root folder. "
                         "Accepts a category (kids/anime/4k/standard) or a literal "
                         "path. Filters both the report and the moves.")
    ap.add_argument("--list", action="store_true",
                    help="List the movies registered to each root folder (title + year) "
                         "and exit — no classification, no moves. Combine with --library "
                         "to list just one folder.")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and choke on the status emoji below.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    inst_name, url, key = resolve_instance(args.instance)
    if not key:
        env_hint = f"RECOMMENDARR_RADARR_INSTANCES_{inst_name.upper()}_API"
        sys.exit(
            f"❌ No Radarr API key for '{inst_name}'. The SecretStore (keyring) returned "
            f"nothing.\n   Store it in the keyring (path 'radarr_instances.{inst_name}.api') "
            f"or export {env_hint}."
        )
    client = RadarrClient(url, key, dry_run=args.dry_run)

    DRY = "[dry-run] " if args.dry_run else ""

    print(f"\n{'='*64}")
    print(f"  router_movie.py  {'(DRY RUN)' if args.dry_run else '*** LIVE MODE ***'}")
    print(f"  Instance : {inst_name} @ {url}")
    print(f"{'='*64}\n")

    # ── Connectivity ──────────────────────────────────────────────────────────
    try:
        ver = client.get("system/status").get("version", "?")
        print(f"Radarr v{ver} ✅\n")
    except Exception as e:
        sys.exit(f"❌ Cannot reach Radarr: {e}")

    # ── Resolve target folders from the LIVE Radarr instance ──────────────────
    try:
        registered = client.get("rootfolder") or []
    except Exception as e:
        sys.exit(f"❌ Cannot fetch root folders: {e}")
    registered_norm = {_norm(rf.get("path")) for rf in registered if rf.get("path")}

    # ── Force-kids override tag: a Radarr tag that pins a movie to the Kids library
    #    regardless of genre/cert (for unrated/mis-rated kid content). ────────────
    force_tag_id = None
    force_tag_label = (FORCE_KIDS_TAG or "").strip().lower()
    if force_tag_label:
        try:
            for tg in client.get("tag") or []:
                if (tg.get("label") or "").strip().lower() == force_tag_label:
                    force_tag_id = tg.get("id")
                    break
        except Exception:
            force_tag_id = None

    # Resolve each category purely against the LIVE Radarr folders (no config hint);
    # a category with no folder of its own inherits 'standard', exactly like
    # resolver._pick_root_folder.
    roots = resolve_category_roots(
        registered, MOVIE_CATEGORY_ORDER, {},
        default_category="standard", aliases=MOVIE_ROOT_ALIASES,
    )
    targets = {cat: r["path"] for cat, r in roots.items() if r["path"]}

    if not targets and not args.list:
        sys.exit(
            f"❌ No usable target root folders. Instance '{inst_name}' has no registered "
            "root folder matching any library category — add root folders in Radarr. (exit 2)"
        )

    if not args.list:
        print("Target root folders (live Radarr folders, matched by library category):")
        for cat in MOVIE_CATEGORY_ORDER:
            r = roots[cat]
            if r["via"] == "own":
                print(f"  ✅ {cat:<10} → {r['path']}")
            elif r["via"] == "inherit":
                print(f"  ↳  {cat:<10} → {r['path']}  (no '{cat}' folder; inherits standard)")
            else:
                print(f"  ⏭️  {cat:<10} → moves skipped ({r['reason']})")
        print()
        if force_tag_label:
            if force_tag_id is not None:
                print(f"Force-kids tag: '{force_tag_label}' (id={force_tag_id}) — tagged movies are pinned to Kids.\n")
            else:
                print(f"Force-kids tag: '{force_tag_label}' not found in Radarr — no overrides applied.\n")

    # ── Fetch + classify all movies ───────────────────────────────────────────
    try:
        all_movies = client.get("movie")
    except Exception as e:
        sys.exit(f"❌ Cannot fetch movie list: {e}")

    # ── Optional: restrict to movies currently in one library/root folder ─────
    if args.library:
        sel = args.library.strip()
        sel_path = targets.get(sel) or sel       # category → live path, else literal path
        sel_norm = _norm(sel_path)
        before = len(all_movies)
        all_movies = [m for m in all_movies if current_root(m, registered_norm) == sel_norm]
        print(f"--library '{sel}' → {sel_path}: {len(all_movies):,} of {before:,} movies currently in this folder.\n")
        if not all_movies:
            print("No movies currently in that library. Exiting.")
            return

    # ── List-only mode: print the movies registered to each root folder ───────
    if args.list:
        by_root: dict[str, list] = defaultdict(list)
        for m in all_movies:
            root_disp = m.get("rootFolderPath") or current_root(m, registered_norm) or "(unknown)"
            by_root[root_disp].append(m)
        total = 0
        for root in sorted(by_root):
            items = sorted(by_root[root], key=lambda x: (x.get("title") or "").lower())
            print(f"{root}  ({len(items):,}):")
            for m in items:
                yr = m.get("year")
                genres = ", ".join(m.get("genres") or []) or "—"
                cert = (m.get("certification") or "").strip() or "?"
                print(f"  {m.get('title', '?')}{f' ({yr})' if yr else ''}  <{cert}>  [{genres}]")
            print()
            total += len(items)
        print(f"Total: {total:,} movie(s).")
        return

    counts = defaultdict(int)              # category → total movies
    n_move = defaultdict(int)              # category → movies needing a move
    n_ok = defaultdict(int)               # category → already in the right place
    blocked = defaultdict(int)            # category → would move but folder unusable
    actions: list[dict] = []              # movies needing a move

    for m in all_movies:
        if force_tag_id is not None and force_tag_id in (m.get("tags") or []):
            cat, reason = "kids", "force:tag"      # pinned to Kids by the force-kids tag
        else:
            cat, reason = classify(m)
        counts[cat] += 1
        target = targets.get(cat)
        cur = current_root(m, registered_norm)

        needs_move = bool(target) and cur != _norm(target)

        if not target:
            blocked[cat] += 1
        elif needs_move:
            n_move[cat] += 1
        else:
            n_ok[cat] += 1

        if needs_move:
            actions.append({
                "id": m.get("id"),
                "title": m.get("title", "?"),
                "cat": cat,
                "reason": reason,
                "cert": (m.get("certification") or "").strip() or "?",
                "cur_root_disp": m.get("rootFolderPath") or m.get("path") or "?",
                "target": target,
            })

    order = {c: i for i, c in enumerate(MOVIE_CATEGORY_ORDER)}
    actions.sort(key=lambda a: (order.get(a["cat"], 99), a["title"].lower()))

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"Library classification of {len(all_movies):,} movies:")
    for cat in MOVIE_CATEGORY_ORDER:
        line = f"  {cat:<10} total={counts[cat]:>5,}  ok={n_ok[cat]:>5,}  move={n_move[cat]:>5,}"
        if blocked[cat]:
            line += f"  blocked={blocked[cat]:,} (no usable folder)"
        print(line)
    print(f"\n  ➜ Movies to move: {len(actions):,}\n")

    if not actions:
        print("Nothing to do. Library already sorted. Exiting.")
        return

    # ── Preview ───────────────────────────────────────────────────────────────
    if args.explain:
        print("Planned actions (--explain):")
        for a in actions:
            print(_action_line(a))
        print()
    else:
        print("Sample of planned actions (first 20; use --explain for all + reasons):")
        for a in actions[:20]:
            print(_action_line(a))
        if len(actions) > 20:
            print(f"  … and {len(actions)-20:,} more (use --explain to see all)")
        print()

    # ── Apply --limit ─────────────────────────────────────────────────────────
    if args.limit:
        actions = actions[:args.limit]
        print(f"--limit applied: moving at most {args.limit} movies.\n")

    # ── Confirmation ──────────────────────────────────────────────────────────
    if not args.dry_run and not args.confirm:
        print(f"⚠️  About to move {len(actions):,} movies on '{inst_name}' (moveFiles=true).")
        print( "   Radarr relocates files on disk; ensure the Plex libraries scan these folders.")
        print()
        if input('Type "YES" to proceed, anything else to cancel: ').strip() != "YES":
            print("Cancelled — no changes made.")
            return
        print()

    # ── Execute: editor calls per target rootFolder, CHUNKED into small batches.
    #    The whole run is resumable — if interrupted, just re-run and only the
    #    movies that still need moving are touched. ─────────────────────────────
    groups: dict[str, list[int]] = defaultdict(list)
    for a in actions:
        if a["id"] is not None:
            groups[a["target"]].append(a["id"])

    total = len(actions)
    done = 0
    errors = 0
    for root, ids in groups.items():
        for i in range(0, len(ids), args.batch):
            chunk = ids[i:i + args.batch]
            payload = {"movieIds": chunk, "moveFiles": True, "rootFolderPath": root}
            try:
                client.put("movie/editor", payload)
                done += len(chunk)
                print(f"  {DRY}{done:>5,}/{total:,}  (+{len(chunk)})  → {root}")
            except Exception as e:
                errors += len(chunk)
                print(f"  ⚠️ editor batch failed ({len(chunk)} movies → {root}): {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*64}")
    print(f"  {DRY}Complete")
    print(f"  Movies moved (requested) : {done:>6,}")
    if errors:
        print(f"  Errors                   : {errors:>6,}")
    print(f"{'='*64}")
    if not args.dry_run:
        print("\nRadarr is relocating files in the background.")
        print("Re-scan the affected Plex libraries once the moves finish.")


if __name__ == "__main__":
    main()

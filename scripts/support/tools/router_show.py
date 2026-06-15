"""
router_show.py
=================
Re-sort the EXISTING Sonarr library so every series lives in the root folder for
its correct Plex library — anime → anime, kids → kids, reality → reality,
documentary → documentaries, everything else → series — and correct any wrong
``seriesType`` along the way.

It classifies each series with the shared standard in
``support/utilities/library_classifier.py`` (precedence: preschool → anime → CSM age →
kids → reality → documentary → series), compares that to where the series currently sits,
and applies the difference via Sonarr's ``series/editor`` endpoint:
  • mis-filed series are moved (``moveFiles=true`` — Sonarr relocates on disk);
  • a wrong ``seriesType`` is corrected (anime-classified → ``anime``; a series
    mistyped ``anime`` that is NOT anime → ``standard``).

Classification (precedence — first match wins):
  • preschool     — the 'Preschool' GENRE: genuine toddler content, beats anime.
  • anime         — anime-language animation (animated AND originalLanguage
                    Japanese/Korean/Chinese), an explicit anime genre, or a bare
                    seriesType="anime" that nothing contradicts. A Western cartoon
                    mistyped as anime (e.g. Curious George) is NOT anime.
  • CSM age       — Common Sense Media recommended age (cached by tmdbId): at/under
                    the kids cutoff → Kids; older → NOT Kids. PRIMARY signal when known.
  • kids (genre)  — Children/Family/Kids routes to Kids (used when no CSM age applies).
  • reality       — Reality / Game Show / Talk Show.
  • documentary   — Documentary / Biography / Nature.
  • kids (cert)   — TV-Y/TV-Y7/TV-G/G/PG, applied LAST (a cert never overrides
                    anime/reality/documentary).
  • series        — anything else.

seriesType is corrected from a SEPARATE "is this anime media" test, so a
children-genre anime routed to Kids still keeps seriesType=anime; only genuinely
non-anime shows mistyped as anime are reset to standard.

Scope / safety:
  • Standalone: reads NOTHING from config.json. Sonarr base URLs come from the
    built-in ``SONARR_INSTANCES`` map (env-overridable); API keys come from the OS
    keyring / ``RECOMMENDARR_*`` env vars; classification uses the library_classifier's
    own tight built-in genre/cert defaults (documentary == documentary/biography/nature
    only — scripted crime/war/history dramas are NOT swept into Documentaries).
  • Same-instance only. It moves a series between root folders on the instance it
    already lives on; it does NOT migrate across Sonarr instances.
  • Move targets follow the LIVE Sonarr instance: each category routes to the
    registered root folder whose name matches it (anime→anime, … documentary→
    documentaries). A category with no folder of its own inherits the ``series``
    library; a category with no usable folder at all is skipped (seriesType is still
    corrected even then).
  • Dry-run safe: nothing changes without an explicit confirmation.

Usage
-----
  cd scripts/support/tools/
  python router_show.py --dry-run            # preview only, no changes
  python router_show.py --dry-run --explain  # preview + per-series reason
  python router_show.py --limit 10           # apply to 10 series (test)
  python router_show.py                       # full run (prompts YES)
  python router_show.py --confirm             # full run, skip the prompt

Flags
-----
  --dry-run     Print the planned actions without calling Sonarr.
  --explain     List every planned action with the matched signal (e.g.
                "[anime:japanese-animation]"), instead of a 20-row sample.
  --instance    Sonarr instance name from config (default: first configured).
  --limit N     Apply to at most N series (after grouping; for testing).
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

# Standalone bootstrap: this file lives at scripts/support/tools/ — put scripts/
# on sys.path so the bare `support.*` imports resolve from any invocation cwd.
# (`sd_replace` is same-dir, so sys.path[0] already covers it.)
_SCRIPTS_DIR = Path(__file__).resolve().parents[2]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the stable, dependency-light helpers from sd_replace.py (same dir).
# NOTE: load_config is intentionally NOT imported — this tool is STANDALONE and reads
# nothing from config.json (connection map + classifier defaults are below).
from sd_replace import SonarrClient, resolve_category_roots
from support.utilities.library_classifier import (
    CATEGORY_ORDER,
    classify_show_explained,
    is_anime_media,
)

# ── Standalone configuration (NO config.json) ─────────────────────────────────
# Sonarr instances on the homelab, name → base URL. API keys are NOT stored here:
# they come from the OS keyring / RECOMMENDARR_* env vars via SecretStore, so this
# file carries no secrets. Override a base URL at runtime with the matching env var
# RECOMMENDARR_SONARR_INSTANCES_<NAME>_BASE_URL if your setup differs.
SONARR_INSTANCES = {
    "sonarr": "http://192.168.1.110:8990",
}
DEFAULT_SONARR_INSTANCE = "sonarr"

# Optional Sonarr tag label that pins a tagged series to the Kids library regardless
# of genre/cert (was config 'forceKidsTag'). Empty = disabled.
FORCE_KIDS_TAG = ""

# Live-folder leaf aliases for categories whose Sonarr root folder is named
# differently from the category (the 'documentary' library folder is pluralised).
SONARR_ROOT_ALIASES = {"documentary": ("documentaries", "docs")}

# Classification uses the library_classifier's OWN built-in genre/cert defaults
# (deliberately TIGHT: documentary == documentary/biography/nature ONLY). The router
# no longer feeds it config genre lists, so a polluted config can't sweep crime / war
# / history dramas into the Documentaries library.

# ── instance + secret resolution ──────────────────────────────────────────────

def _load_secret_store():
    """
    Load SecretStore straight from its file, bypassing the config package
    ``__init__`` (which uses ``scripts.``-prefixed imports that don't resolve when
    this script is run from inside ``scripts/``). Returns None if unavailable.
    """
    path = _SCRIPTS_DIR / "managers" / "factories" / "config" / "secret_store.py"
    try:
        spec = importlib.util.spec_from_file_location("_lr_secret_store", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.SecretStore()
    except Exception:
        return None


def resolve_instance(name: str | None) -> tuple[str, str, str]:
    """
    Return ``(name, base_url, api_key)`` for a Sonarr instance — STANDALONE, no
    config.json. The base URL comes from the hardcoded ``SONARR_INSTANCES`` map
    (override per instance via ``RECOMMENDARR_SONARR_INSTANCES_<NAME>_BASE_URL``); the
    API key comes from the OS keyring / ``RECOMMENDARR_SONARR_INSTANCES_<NAME>_API``
    env var via SecretStore — so no secret is stored in this file.
    """
    name = str(name or DEFAULT_SONARR_INSTANCE)
    base = (os.environ.get(f"RECOMMENDARR_SONARR_INSTANCES_{name.upper()}_BASE_URL")
            or SONARR_INSTANCES.get(name) or "").rstrip("/")
    if not base:
        sys.exit(f"Unknown Sonarr instance '{name}'. Known: {', '.join(SONARR_INSTANCES)}.")

    api = ""
    store = _load_secret_store()
    if store is not None:
        api = store.get(f"sonarr_instances.{name}.api") or ""
    if not api:
        api = os.environ.get(f"RECOMMENDARR_SONARR_INSTANCES_{name.upper()}_API") or ""
    return name, base, api


# ── path helpers ──────────────────────────────────────────────────────────────

def _norm(p: str | None) -> str:
    """Normalise a path for comparison: forward slashes, no trailing slash, lower."""
    return (p or "").replace("\\", "/").rstrip("/").lower()


def current_root(series: dict, registered_norm: set[str]) -> str:
    """Best guess at the series' current root folder (normalised)."""
    rfp = _norm(series.get("rootFolderPath"))
    if rfp:
        return rfp
    path = _norm(series.get("path"))
    best = ""
    for r in registered_norm:
        if r and (path == r or path.startswith(r + "/")) and len(r) > len(best):
            best = r
    return best


# ── classification ────────────────────────────────────────────────────────────

def _olang(series: dict):
    ol = series.get("originalLanguage")
    return ol.get("name") if isinstance(ol, dict) else ol


# Common Sense Media recommended-age cache for SHOWS (tmdbId → age|null), populated by
# enrich_csm_ages.py / the enrich daemon from MDBList. PRIMARY kids signal; a missing/null
# entry lets the classifier fall back to its genre/cert heuristics. Read directly from the
# JSON (no ``scripts.``-prefixed import) to keep this tool standalone — mirrors
# router_movie._csm_age, but pointed at the SEPARATE TV cache file (show and movie tmdbIds
# share an integer space, so they can't share a {tmdbId: age} dict). Loaded once, lazily.
KIDS_AGE_MAX = 11                            # CSM age at/under which a show is 'kids' (matches router_movie)
_CSM_AGE_CACHE: "dict | None" = None


def _csm_age(tmdb_id) -> "int | None":
    global _CSM_AGE_CACHE
    if _CSM_AGE_CACHE is None:
        path = _SCRIPTS_DIR / "support" / "cache" / "mdblist" / "age_ratings_tv.json"
        try:
            with open(path, encoding="utf-8") as f:
                _CSM_AGE_CACHE = json.load(f) or {}
            print(f"Common Sense ages: {sum(1 for v in _CSM_AGE_CACHE.values() if isinstance(v, int)):,} "
                  f"cached (of {len(_CSM_AGE_CACHE):,} looked up).")
        except Exception:
            _CSM_AGE_CACHE = {}
            print("Common Sense ages: no TV cache yet (run enrich_csm_ages.py --media tv) — using genre/cert only.")
    if not tmdb_id:
        return None
    v = _CSM_AGE_CACHE.get(str(tmdb_id))
    return v if isinstance(v, int) else None


def classify(series: dict) -> tuple[str, str]:
    """Return ``(category, reason)`` for a Sonarr series dict, using the classifier's
    built-in (tight) genre/cert defaults — no config lists are passed. Common Sense Media
    age (when cached for this series' tmdbId) is the PRIMARY kids signal, matching the
    add-time resolver and router_movie."""
    return classify_show_explained(
        genres=series.get("genres"),
        certification=series.get("certification"),
        series_type=series.get("seriesType"),
        original_language=_olang(series),
        is_anime_hint=False,                 # Sonarr carries no source hint
        recommended_age=_csm_age(series.get("tmdbId")),   # Common Sense age (primary)
        kids_age_max=KIDS_AGE_MAX,
    )


def anime_media(series: dict) -> bool:
    """Genuine-anime test for seriesType, independent of the library bucket."""
    return is_anime_media(
        genres=series.get("genres"),
        series_type=series.get("seriesType"),
        original_language=_olang(series),
    )


def _action_line(a: dict) -> str:
    tgt = a["target"] or "(stay)"
    st = f"  type {a['cur_stype']}→{a['new_stype']}" if a["new_stype"] else ""
    return f"  {a['title'][:40]:<40} <{a.get('cert', '?'):<6}> {a['cur_root_disp']} → {tgt}  [{a['cat']}:{a['reason']}]{st}"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Route Sonarr series into the correct library root folder and fix seriesType."
    )
    ap.add_argument("--dry-run",  action="store_true",
                    help="Preview only — no API calls that modify data.")
    ap.add_argument("--explain",  action="store_true",
                    help="List every planned action with its matched signal.")
    ap.add_argument("--instance", default=None,
                    help="Sonarr instance name from the built-in SONARR_INSTANCES map "
                         f"(default: {DEFAULT_SONARR_INSTANCE}).")
    ap.add_argument("--limit",    type=int, default=None,
                    help="Apply to at most N series (for testing).")
    ap.add_argument("--batch",    type=int, default=100,
                    help="Series per editor/move call. Smaller = more resumable "
                         "and each Sonarr move command stays small. Default 100.")
    ap.add_argument("--confirm",  action="store_true",
                    help="Skip the interactive YES confirmation prompt.")
    ap.add_argument("--library",  default=None,
                    help="Only consider series CURRENTLY in this library/root folder. "
                         "Accepts a category (series/anime/kids/reality/documentary) "
                         "or a literal path. Filters the report and moves.")
    ap.add_argument("--list",     action="store_true",
                    help="List the series registered to each root folder (title + year) "
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
        env_hint = f"RECOMMENDARR_SONARR_INSTANCES_{inst_name.upper()}_API"
        sys.exit(
            f"❌ No Sonarr API key for '{inst_name}'. The SecretStore (keyring) returned "
            f"nothing.\n   Store it in the keyring (path 'sonarr_instances.{inst_name}.api') "
            f"or export {env_hint}."
        )
    client              = SonarrClient(url, key, dry_run=args.dry_run)

    DRY = "[dry-run] " if args.dry_run else ""

    print(f"\n{'='*64}")
    print(f"  router_show.py  {'(DRY RUN)' if args.dry_run else '*** LIVE MODE ***'}")
    print(f"  Instance : {inst_name} @ {url}")
    print(f"{'='*64}\n")

    # ── Connectivity ──────────────────────────────────────────────────────────
    try:
        ver = client.get("system/status").get("version", "?")
        print(f"Sonarr v{ver} ✅\n")
    except Exception as e:
        sys.exit(f"❌ Cannot reach Sonarr: {e}")

    # ── Resolve target folders from the LIVE Sonarr instance ──────────────────
    try:
        registered = client.get("rootfolder") or []
    except Exception as e:
        sys.exit(f"❌ Cannot fetch root folders: {e}")
    registered_norm = {_norm(rf.get("path")) for rf in registered if rf.get("path")}

    # ── Resolve the force-kids override tag: a Sonarr tag that pins a series to the
    #    Kids library regardless of genre/cert (for unrated/mis-rated kid content). ─
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

    # Resolve each category purely against the LIVE Sonarr folders (no config hint);
    # a category with no folder of its own inherits 'series', exactly like
    # resolver._pick_root_folder.
    roots = resolve_category_roots(
        registered, CATEGORY_ORDER, {},
        default_category="series", aliases=SONARR_ROOT_ALIASES,
    )
    targets = {cat: r["path"] for cat, r in roots.items() if r["path"]}

    if not targets and not args.list:
        sys.exit(
            f"❌ No usable target root folders. Instance '{inst_name}' has no registered "
            "root folder matching any library category — add root folders in Sonarr. (exit 2)"
        )

    if not args.list:
        print("Target root folders (live Sonarr folders, matched by library category):")
        for cat in CATEGORY_ORDER:
            r = roots[cat]
            if r["via"] == "own":
                print(f"  ✅ {cat:<12} → {r['path']}")
            elif r["via"] == "inherit":
                print(f"  ↳  {cat:<12} → {r['path']}  (no '{cat}' folder; inherits series)")
            else:
                print(f"  ⏭️  {cat:<12} → moves skipped ({r['reason']})")
        print()
        if force_tag_label:
            if force_tag_id is not None:
                print(f"Force-kids tag: '{force_tag_label}' (id={force_tag_id}) — tagged series are pinned to Kids.\n")
            else:
                print(f"Force-kids tag: '{force_tag_label}' not found in Sonarr — no overrides applied.\n")

    # ── Fetch + classify all series ───────────────────────────────────────────
    try:
        all_series = client.get("series")
    except Exception as e:
        sys.exit(f"❌ Cannot fetch series list: {e}")

    # ── Optional: restrict to series currently in one library/root folder ─────
    if args.library:
        sel = args.library.strip()
        sel_path = targets.get(sel) or sel       # category → live path, else literal path
        sel_norm = _norm(sel_path)
        before = len(all_series)
        all_series = [s for s in all_series if current_root(s, registered_norm) == sel_norm]
        print(f"--library '{sel}' → {sel_path}: {len(all_series):,} of {before:,} series currently in this folder.\n")
        if not all_series:
            print("No series currently in that library. Exiting.")
            return

    # ── List-only mode: print the series registered to each root folder ───────
    if args.list:
        by_root: dict[str, list] = defaultdict(list)
        for s in all_series:
            root_disp = s.get("rootFolderPath") or current_root(s, registered_norm) or "(unknown)"
            by_root[root_disp].append(s)
        total = 0
        for root in sorted(by_root):
            items = sorted(by_root[root], key=lambda x: (x.get("title") or "").lower())
            print(f"{root}  ({len(items):,}):")
            for s in items:
                yr = s.get("year")
                genres = ", ".join(s.get("genres") or []) or "—"
                cert = (s.get("certification") or "").strip() or "?"
                print(f"  {s.get('title', '?')}{f' ({yr})' if yr else ''}  <{cert}>  [{genres}]")
            print()
            total += len(items)
        print(f"Total: {total:,} series.")
        return

    counts = defaultdict(int)              # category → total series
    n_move = defaultdict(int)              # category → series needing a move
    n_ok = defaultdict(int)               # category → already in the right place
    blocked = defaultdict(int)            # category → would move but folder unusable
    fix_to_standard = 0                   # mistyped anime → standard
    fix_to_anime = 0                      # anime-classified, not typed anime
    actions: list[dict] = []              # series needing a move and/or a type fix

    for s in all_series:
        if force_tag_id is not None and force_tag_id in (s.get("tags") or []):
            cat, reason = "kids", "force:tag"      # pinned to Kids by the force-kids tag
        else:
            cat, reason = classify(s)
        counts[cat] += 1
        target = targets.get(cat)
        cur_root = current_root(s, registered_norm)
        cur_stype = (s.get("seriesType") or "standard").strip().lower()

        if anime_media(s):
            desired_stype = "anime"        # genuine anime keeps anime parsing,
                                           # even when routed to the Kids library
        elif cur_stype == "anime":
            desired_stype = "standard"     # mistyped non-anime → correct it
        else:
            desired_stype = None           # leave standard/daily untouched

        needs_move = bool(target) and cur_root != _norm(target)
        needs_type = desired_stype is not None and desired_stype != cur_stype

        if not target and cat != "series":
            blocked[cat] += 1
        elif needs_move:
            n_move[cat] += 1
        else:
            n_ok[cat] += 1

        if needs_type:
            if desired_stype == "standard":
                fix_to_standard += 1
            else:
                fix_to_anime += 1

        if needs_move or needs_type:
            actions.append({
                "id": s.get("id"),
                "title": s.get("title", "?"),
                "cat": cat,
                "reason": reason,
                "cert": (s.get("certification") or "").strip() or "?",
                "cur_root_disp": s.get("rootFolderPath") or s.get("path") or "?",
                "target": target if needs_move else None,
                "cur_stype": cur_stype,
                "new_stype": desired_stype if needs_type else None,
            })

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    actions.sort(key=lambda a: (order.get(a["cat"], 99), a["title"].lower()))

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"Library classification of {len(all_series):,} series:")
    for cat in CATEGORY_ORDER:
        line = f"  {cat:<12} total={counts[cat]:>5,}  ok={n_ok[cat]:>5,}  move={n_move[cat]:>5,}"
        if blocked[cat]:
            line += f"  blocked={blocked[cat]:,} (no usable folder)"
        print(line)
    print(f"\n  seriesType fixes: anime→standard={fix_to_standard:,}, →anime={fix_to_anime:,}")
    print(f"  ➜ Series to update (move and/or seriesType): {len(actions):,}\n")

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
        print(f"--limit applied: updating at most {args.limit} series.\n")

    # ── Confirmation ──────────────────────────────────────────────────────────
    n_will_move = sum(1 for a in actions if a["target"])
    n_will_type = sum(1 for a in actions if a["new_stype"])
    if not args.dry_run and not args.confirm:
        print(f"⚠️  About to update {len(actions):,} series on '{inst_name}':")
        print(f"     • move files for {n_will_move:,} (moveFiles=true)")
        print(f"     • correct seriesType for {n_will_type:,}")
        print( "   Sonarr relocates files on disk; ensure the Plex libraries scan these folders.")
        print()
        if input('Type "YES" to proceed, anything else to cancel: ').strip() != "YES":
            print("Cancelled — no changes made.")
            return
        print()

    # ── Execute: editor calls per (rootFolder, seriesType), CHUNKED into small
    #    batches. Each Sonarr move command stays small and drains fast, and the
    #    whole run is resumable — if it's interrupted, just run it again and only
    #    the series that still need moving are touched (no orphaning). ──────────
    groups: dict[tuple, list[int]] = defaultdict(list)
    for a in actions:
        if a["id"] is not None:
            groups[(a["target"], a["new_stype"])].append(a["id"])

    total = len(actions)
    done = 0
    errors = 0
    for (root, stype), ids in groups.items():
        label = []
        if root:
            label.append(f"→ {root}")
        if stype:
            label.append(f"seriesType={stype}")
        for i in range(0, len(ids), args.batch):
            chunk = ids[i:i + args.batch]
            payload: dict = {"seriesIds": chunk, "moveFiles": bool(root)}
            if root:
                payload["rootFolderPath"] = root
            if stype:
                payload["seriesType"] = stype
            try:
                client.put("series/editor", payload)
                done += len(chunk)
                print(f"  {DRY}{done:>5,}/{total:,}  (+{len(chunk)})  {'  '.join(label)}")
            except Exception as e:
                errors += len(chunk)
                print(f"  ⚠️ editor batch failed ({len(chunk)} series, {' '.join(label)}): {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*64}")
    print(f"  {DRY}Complete")
    print(f"  Series updated (requested) : {done:>6,}")
    if errors:
        print(f"  Errors                     : {errors:>6,}")
    print(f"{'='*64}")
    if not args.dry_run:
        print("\nSonarr is relocating files in the background.")
        print("Re-scan the affected Plex libraries once the moves finish.")


if __name__ == "__main__":
    main()

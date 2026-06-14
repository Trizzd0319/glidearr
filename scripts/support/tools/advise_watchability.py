"""
advise_watchability.py — explain WHY a title earned its watchability tier.
================================================================================
The watchability score (0-100) is built from independently-capped signal groups
A-G (see trakt.movies.scorer / trakt.shows.scorer). ``refresh_scores`` now
persists the full per-signal-group BREAKDOWN next to the score, as a small flat
JSON in the ``watchability_breakdown`` column of:

    {cache}/radarr/{instance}/movie_files.parquet      (one row per movie)
    {cache}/sonarr/{instance}/episode_files.parquet    (per-series, broadcast)

This read-only tool surfaces that breakdown. For each title it prints the top
positive and top negative contributors (sorted by magnitude), a per-group
rollup, and — because an UPGRADE decision maps a *likelihood* (not the raw
score) to a quality profile — the likelihood derivation: whether the ENGAGEMENT
floor or the AFFINITY propensity won, and which profile/resolution that earns.

So you can answer, straight from the cache:
    'Predator' 13%: never watched (A2=0, A3=0), no affinity (B*=0), F1 critic
                    +8 … → low watchability → floor profile (id 3, HD-720p).
    'Rocky'    50%: watched once → engagement floor 50 > affinity 12 → 1080p.

Usage
-----
    python scripts/support/tools/advise_watchability.py                      # whole library, lowest score first
    python scripts/support/tools/advise_watchability.py --title predator     # titles matching "predator"
    python scripts/support/tools/advise_watchability.py --universe rocky      # one universe (movies)
    python scripts/support/tools/advise_watchability.py --service show        # TV only
    python scripts/support/tools/advise_watchability.py --upgrades-only       # only titles whose tier would change
    python scripts/support/tools/advise_watchability.py --top 8 --limit 50    # contributors per side / titles cap

No network, no writes — reads the Parquet cache the pipeline already produced.
Run ``refresh_scores`` first (it populates the breakdown column) if a title
prints "no breakdown".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
def _red(s):    return f"\033[31m{s}\033[0m" if _COLOR else s
def _yellow(s): return f"\033[33m{s}\033[0m" if _COLOR else s
def _green(s):  return f"\033[32m{s}\033[0m" if _COLOR else s
def _cyan(s):   return f"\033[36m{s}\033[0m" if _COLOR else s
def _dim(s):    return f"\033[2m{s}\033[0m"  if _COLOR else s
def _bold(s):   return f"\033[1m{s}\033[0m"  if _COLOR else s

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/*/<name>.py)
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder  # noqa: E402
from scripts.support.utilities.watch_likelihood import (                  # noqa: E402
    explain_likelihood,
    ladder_rank,
    profile_id_for_likelihood,
    radarr_ladder,
    resolution_cap_for_likelihood,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.json"  # scripts/support/config/

# Human label for each Radarr ladder profile id (from the default ladder documented
# in watch_likelihood). Used only when the parquet doesn't carry the target name.
_RADARR_PROFILE_LABELS = {
    3:  "HD-720p (floor)",
    4:  "HD-1080p",
    6:  "HD-720p/1080p",
    7:  "HD Bluray+WEB (1080p)",
    8:  "Remux+WEB-1080p",
    5:  "Ultra-HD (low-4K)",
    9:  "Remux 2160p (high-4K)",
    10: "UHD Bluray+WEB (top-4K)",
}


# ── Config / discovery ─────────────────────────────────────────────────────────
def _load_config() -> dict:
    """Best-effort config load (for the ladder/cutoff knobs). The watch_likelihood
    helpers fall back to their baked-in defaults if config is missing, so a failed
    load only means non-default ladders aren't honoured — never a crash."""
    try:
        from scripts.managers.factories.config.config_loader import ConfigLoader
        if CONFIG.exists():
            return ConfigLoader(CONFIG).load() or {}
    except Exception as e:
        print(_yellow(f"  (config not loaded — using default ladder: {e})"))
    return {}


def _discover(base: Path, service: str, instance: str | None) -> list[tuple[str, str, Path]]:
    """Return [(service, instance, parquet_path)] for the requested service(s)."""
    out: list[tuple[str, str, Path]] = []
    specs = []
    if service in ("movie", "all"):
        specs.append(("movie", base / "radarr", "movie_files.parquet"))
    if service in ("show", "all"):
        specs.append(("show", base / "sonarr", "episode_files.parquet"))
    for svc, root, fname in specs:
        if not root.is_dir():
            continue
        for inst_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if instance and inst_dir.name != instance:
                continue
            p = inst_dir / fname
            if p.exists():
                out.append((svc, inst_dir.name, p))
    return out


# ── Breakdown helpers ────────────────────────────────────────────────────────
def _parse_breakdown(raw) -> dict | None:
    """watchability_breakdown cell → flat dict (or None if absent/unparseable)."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


_GROUP_NAMES = {
    "A": "Household Intent", "B": "Affinity", "C": "Collection/Universe",
    "D": "Device/Playback", "E": "Audience", "F": "Content Quality", "G": "Penalties",
}


def _group_rollup(contribs: dict[str, float]) -> str:
    """One-line per-group (A-G) net contribution, e.g. 'A+0 B+0 C+1 F+10 G-0'."""
    sums: dict[str, float] = {}
    for k, v in contribs.items():
        g = k[0]
        sums[g] = sums.get(g, 0.0) + float(v)
    parts = []
    for g in "ABCDEFG":
        if g not in sums:
            continue
        v = sums[g]
        tag = f"{g}{'+' if v >= 0 else ''}{_fmt(v)}"
        parts.append(_green(tag) if v > 0 else (_red(tag) if v < 0 else _dim(tag)))
    return "  ".join(parts)


def _fmt(v: float) -> str:
    """Compact number: drop a trailing .0 (so '+8.0' prints as '+8')."""
    v = round(float(v), 2)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _narrative(contribs: dict[str, float]) -> str:
    """Short plain-English tag for the common zero patterns the score hinges on."""
    bits = []
    a2 = contribs.get("A2_completion", contribs.get("A2_engagement", 0.0))
    a3 = contribs.get("A3_rewatch", 0.0)
    if a2 == 0 and a3 == 0:
        bits.append("never watched (A2=0, A3=0)")
    b_terms = [v for k, v in contribs.items() if k.startswith("B")]
    if b_terms and not any(v > 0 for v in b_terms):
        bits.append("no affinity (B*=0)")
    f1 = contribs.get("F1_critic_consensus", 0.0)
    if f1 > 0:
        bits.append(f"critic-carried (F1 +{_fmt(f1)})")
    return "; ".join(bits)


def _likelihood_row(row: "pd.Series") -> dict:
    """Pull the watch-likelihood inputs out of a parquet row (all optional)."""
    def g(col):
        return row[col] if col in row.index and pd.notna(row[col]) else None
    return {
        "watch_count": g("watch_count"),
        "percent_complete": g("percent_complete"),
        "is_watched": bool(row["is_watched"]) if ("is_watched" in row.index and pd.notna(row["is_watched"])) else None,
        "watchability_score": g("watchability_score"),
        "watchability_percentile": g("watchability_percentile"),
    }


def _likelihood_line(svc: str, row: "pd.Series", config: dict) -> tuple[str, bool]:
    """Render the likelihood-derivation line. Returns (text, would_change_tier)."""
    ex = explain_likelihood(_likelihood_row(row), config=config)
    L = ex["likelihood"]
    tier, winner = ex["engagement_tier"], ex["winner"]
    eng, aff = ex["engagement"], ex["affinity"]
    cmp = ">" if aff > eng else ("<" if aff < eng else "=")
    deriv = (f"{tier}: affinity {_fmt(aff)} {cmp} engagement floor {_fmt(eng)} "
             f"→ winner: {_bold(winner)}")

    if svc == "movie":
        target_pid = profile_id_for_likelihood(L, config=config)
        label = _RADARR_PROFILE_LABELS.get(target_pid, "")
        cur_pid = int(row["quality_profile_id"]) if ("quality_profile_id" in row.index and pd.notna(row["quality_profile_id"])) else None
        cur_name = (row["quality_profile_name"] if ("quality_profile_name" in row.index and pd.notna(row["quality_profile_name"])) else "?")
        t_rank = ladder_rank(target_pid, config=config)
        c_rank = ladder_rank(cur_pid, config=config) if cur_pid is not None else -1
        if t_rank > c_rank:
            verdict, changed = _green(f"UPGRADE → profile id {target_pid} {label}"), True
        elif t_rank == c_rank:
            verdict, changed = _dim(f"at earned tier (profile id {target_pid} {label})"), False
        else:
            verdict, changed = _dim(f"above earned tier (earns id {target_pid} {label}; no downgrade here)"), False
        target = f"{verdict}  (current: {cur_name})"
    else:
        target_res = resolution_cap_for_likelihood(L, config=config)
        target = _cyan(f"earns max resolution {target_res}p")
        changed = False  # shows have no per-series current profile in this cache to diff

    return (f"likelihood {_bold(f'{L:.0f}%')}  ({deriv})\n      → {target}", changed)


# ── Rendering ──────────────────────────────────────────────────────────────────
def _render_title(svc: str, row: "pd.Series", config: dict, top: int) -> tuple[str, bool]:
    title = row.get("title") if "title" in row.index else None
    if not title or pd.isna(title):
        title = row.get("series_title") if "series_title" in row.index else None
    title = str(title) if title is not None and not pd.isna(title) else "(untitled)"

    score = row["watchability_score"] if ("watchability_score" in row.index and pd.notna(row["watchability_score"])) else None
    pct = row["watchability_percentile"] if ("watchability_percentile" in row.index and pd.notna(row["watchability_percentile"])) else None
    uni = row["universe_name"] if ("universe_name" in row.index and pd.notna(row["universe_name"])) else None

    head = _bold(f"{title}")
    meta = []
    if score is not None:
        meta.append(f"score={_fmt(score)}")
    if pct is not None:
        meta.append(f"pctile={_fmt(pct)}")
    if uni:
        meta.append(f"universe={uni}")
    lines = [f"{head}  " + _dim("  ".join(meta))]

    bd = _parse_breakdown(row.get("watchability_breakdown") if "watchability_breakdown" in row.index else None)
    if bd is None:
        lines.append("  " + _yellow("no breakdown — run refresh_scores to populate it"))
        like_line, changed = _likelihood_line(svc, row, config)
        lines.append("  " + like_line)
        return "\n".join(lines), changed

    contribs = {k: float(v) for k, v in bd.items() if not k.startswith("_")}
    like_line, changed = _likelihood_line(svc, row, config)
    lines.append("  " + like_line)

    pos = sorted([(k, v) for k, v in contribs.items() if v > 0], key=lambda kv: -kv[1])[:top]
    neg = sorted([(k, v) for k, v in contribs.items() if v < 0], key=lambda kv: kv[1])[:top]
    if pos:
        lines.append("  " + _green("+ ") + "  ".join(f"{k} +{_fmt(v)}" for k, v in pos))
    else:
        lines.append("  " + _dim("+ (no positive contributors)"))
    if neg:
        lines.append("  " + _red("− ") + "  ".join(f"{k} {_fmt(v)}" for k, v in neg))
    else:
        lines.append("  " + _dim("− (no penalties)"))

    lines.append("  " + _dim("groups: ") + _group_rollup(contribs))
    narr = _narrative(contribs)
    if narr:
        lines.append("  " + _dim("· " + narr))
    return "\n".join(lines), changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Explain WHY a title earned its watchability tier (reads the Parquet cache).")
    ap.add_argument("--service", choices=("movie", "show", "all"), default="all")
    ap.add_argument("--instance", help="limit to one instance (folder name under radarr/ or sonarr/)")
    ap.add_argument("--title", help="case-insensitive substring filter on the title")
    ap.add_argument("--universe", help="filter to a universe_name (movies)")
    ap.add_argument("--top", type=int, default=6, help="contributors per side (default 6)")
    ap.add_argument("--limit", type=int, default=30, help="max titles to print (default 30)")
    ap.add_argument("--upgrades-only", action="store_true",
                    help="only titles whose earned tier is ABOVE their current profile (movies)")
    ap.add_argument("--desc", action="store_true", help="sort highest score first (default lowest first)")
    ap.add_argument("--cache-dir", help="override the cache base dir (default: scripts/support/cache)")
    args = ap.parse_args()

    base = Path(args.cache_dir).resolve() if args.cache_dir else CacheKeyBuilder().base_dir
    if not base.is_dir():
        print(_red(f"ERROR: cache dir not found: {base}"))
        return 1

    config = _load_config()
    sources = _discover(base, args.service, args.instance)
    if not sources:
        print(_red(f"No parquet caches found under {base} for service={args.service}"
                   f"{f' instance={args.instance}' if args.instance else ''}."))
        return 1

    printed = 0
    for svc, inst, path in sources:
        try:
            df = pd.read_parquet(path)
        except Exception as e:
            print(_red(f"  could not read {path}: {e}"))
            continue
        if "watchability_score" not in df.columns:
            print(_yellow(f"[{svc}/{inst}] no watchability_score column — run refresh_scores."))
            continue

        # Shows broadcast the per-series score onto every episode row — collapse to
        # one row per series so we advise each series once.
        if svc == "show" and "series_id" in df.columns:
            df = df.sort_values("series_id").drop_duplicates(subset="series_id", keep="first")

        if args.title:
            tcol = "title" if "title" in df.columns else ("series_title" if "series_title" in df.columns else None)
            if tcol:
                df = df[df[tcol].astype(str).str.contains(args.title, case=False, na=False)]
        if args.universe and "universe_name" in df.columns:
            df = df[df["universe_name"].astype(str).str.contains(args.universe, case=False, na=False)]

        df = df[pd.to_numeric(df["watchability_score"], errors="coerce").notna()]
        if df.empty:
            continue
        df = df.sort_values("watchability_score", ascending=not args.desc,
                            kind="stable", na_position="last")

        print(_bold(f"\n══ {svc}/{inst} — {len(df)} title(s) ══"))
        for _, row in df.iterrows():
            if printed >= args.limit:
                print(_dim(f"\n  … limit {args.limit} reached; narrow with --title/--universe or raise --limit."))
                return 0
            text, changed = _render_title(svc, row, config, args.top)
            if args.upgrades_only and not changed:
                continue
            print("\n" + text)
            printed += 1

    if printed == 0:
        print(_yellow("\nNothing matched the filters."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

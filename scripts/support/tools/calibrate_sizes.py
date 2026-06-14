"""
calibrate_sizes.py — measure real MiB/min per quality from your *arr libraries.
================================================================================
Pulls the size + runtime + quality of every file Sonarr/Radarr already knows
about (read-only: only HTTP GETs, never a grab or a write) and computes the
measured MiB/min per quality tier — the ground truth that calibrates
``scripts/support/utilities/size_model.CALIBRATED_MB_PER_MIN``.

Why this beats pasting ``ls -lah``: it uses thousands of real files, reads the
exact per-file runtime from Sonarr/Radarr's own mediaInfo, and can split anime
vs live-action (anime encodes far smaller at the same quality label).

Auth is identical to the app: the script loads config through ``ConfigLoader``,
so the per-instance API keys are overlaid from env vars / the OS keyring (they
are blank in config.json on disk).

Usage
-----
    python scripts/support/tools/calibrate_sizes.py                 # report + suggested table
    python scripts/support/tools/calibrate_sizes.py --json out.json # also dump raw stats
    python scripts/support/tools/calibrate_sizes.py --min-samples 5 # trust threshold (default 3)

Output
------
1. Per-instance file counts.
2. Per-quality measured MiB/min (n, p10, median, mean, p90) — overall.
3. Anime vs live-action split where the gap is material.
4. A ready-to-paste ``CALIBRATED_MB_PER_MIN`` dict: measured value wherever a
   quality has >= --min-samples files, else the current table value (so tiers
   you don't own are left untouched).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Console: UTF-8 safe, colour-coded (red=error, yellow=caution, green=good),
# honours NO_COLOR / non-TTY — mirrors migrate_secrets.py.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
def _red(s):    return f"\033[31m{s}\033[0m" if _COLOR else s
def _yellow(s): return f"\033[33m{s}\033[0m" if _COLOR else s
def _green(s):  return f"\033[32m{s}\033[0m" if _COLOR else s
def _bold(s):   return f"\033[1m{s}\033[0m"  if _COLOR else s

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/*/<name>.py)
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from scripts.managers.factories.config.config_loader import ConfigLoader  # noqa: E402
from scripts.support.utilities.size_model import (                        # noqa: E402
    CALIBRATED_MB_PER_MIN, MIN_MB_PER_MIN, MAX_MB_PER_MIN,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.json"  # scripts/support/config/
HTTP_TIMEOUT = 60       # per-series / small calls
LIST_TIMEOUT = 180      # big list payloads (/movie, /series) on large libraries
HTTP_RETRIES = 3        # retry transient timeouts/connection errors with backoff
SONARR_WORKERS = 6
MIN_RUNTIME_MIN = 3.0   # ignore files whose runtime is implausibly short (corrupt mediaInfo)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _parse_runtime_min(raw, fallback_min: float | None) -> float | None:
    """mediaInfo.runTime → minutes. Accepts seconds (number), 'MM:SS', 'H:MM:SS';
    falls back to the series/movie runtime (already in minutes)."""
    if raw is not None:
        if isinstance(raw, (int, float)):
            if raw > 0:
                # Sonarr/Radarr mediaInfo.runTime numbers are seconds.
                return float(raw) / 60.0
        else:
            s = str(raw).strip()
            if ":" in s:
                parts = s.split(":")
                try:
                    if len(parts) == 2:
                        secs = float(parts[0]) * 60 + float(parts[1])
                    elif len(parts) == 3:
                        secs = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                    else:
                        secs = 0.0
                    if secs > 0:
                        return secs / 60.0
                except (ValueError, TypeError):
                    pass
            else:
                try:
                    v = float(s)
                    if v > 0:
                        return v / 60.0
                except (ValueError, TypeError):
                    pass
    try:
        fb = float(fallback_min or 0)
        return fb if fb > 0 else None
    except (TypeError, ValueError):
        return None


def _quality_name(quality_block: dict) -> str | None:
    q = (quality_block or {}).get("quality") or {}
    return q.get("name")


def _mbpm(size_bytes, runtime_min) -> float | None:
    try:
        sb = float(size_bytes)
        rt = float(runtime_min)
    except (TypeError, ValueError):
        return None
    if sb <= 0 or rt < MIN_RUNTIME_MIN:
        return None
    v = (sb / (1024 ** 2)) / rt
    if v < MIN_MB_PER_MIN or v > MAX_MB_PER_MIN:
        return None   # drop corrupt rows, same guard as size_model.measured_mb_per_min
    return v


def _get(base_url: str, api: str, endpoint: str, timeout: int = HTTP_TIMEOUT,
         retries: int = HTTP_RETRIES):
    last = None
    for attempt in range(max(1, retries)):
        try:
            r = requests.get(f"{base_url}/api/v3/{endpoint}",
                             headers={"X-Api-Key": api}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))   # 2s, 4s, … backoff
        except Exception:
            raise
    raise last if last else RuntimeError(f"GET {endpoint} failed")


def _percentiles(vals: list[float]) -> dict:
    xs = sorted(vals)
    n = len(xs)

    def pct(p):
        if n == 1:
            return xs[0]
        idx = p / 100.0 * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return {
        "n": n,
        "p10": pct(10),
        "median": pct(50),
        "mean": sum(xs) / n,
        "p90": pct(90),
        "max": xs[-1],
    }


# ── Collectors (one row per file) ─────────────────────────────────────────────
def collect_radarr(name: str, inst_cfg: dict, anime_genres: set[str]) -> list[dict]:
    base, api = inst_cfg.get("base_url"), inst_cfg.get("api")
    if not base or not api:
        print(_yellow(f"  radarr/{name}: missing base_url or API key — skipped "
                      f"(set RECOMMENDARR_RADARR_INSTANCES_{name.upper()}_API or keyring)."))
        return []
    try:
        movies = _get(base, api, "movie", timeout=LIST_TIMEOUT) or []
    except Exception as e:
        print(_red(f"  radarr/{name}: GET /movie failed: {e}"))
        return []

    rows = []
    for m in movies:
        mf = m.get("movieFile") or {}
        size = mf.get("size")
        if not size:
            continue
        qname = _quality_name(mf.get("quality") or {})
        if not qname:
            continue
        media = mf.get("mediaInfo") or {}
        runtime_min = _parse_runtime_min(media.get("runTime"), m.get("runtime"))
        mbpm = _mbpm(size, runtime_min)
        if mbpm is None:
            continue
        genres = {str(g).lower() for g in (m.get("genres") or [])}
        is_anime = bool(genres & anime_genres) or (
            str((m.get("originalLanguage") or {}).get("name", "")).lower() in ("japanese",)
            and "animation" in genres
        )
        rows.append({"service": "radarr", "instance": name, "quality": qname,
                     "content": "anime" if is_anime else "live", "mbpm": mbpm,
                     "size_gb": size / 1e9, "runtime_min": runtime_min,
                     "title": m.get("title")})
    print(_green(f"  radarr/{name}: {len(rows)} movie file(s) measured "
                 f"(of {len(movies)} movies)."))
    return rows


def collect_sonarr(name: str, inst_cfg: dict) -> list[dict]:
    base, api = inst_cfg.get("base_url"), inst_cfg.get("api")
    if not base or not api:
        print(_yellow(f"  sonarr/{name}: missing base_url or API key — skipped "
                      f"(set RECOMMENDARR_SONARR_INSTANCES_{name.upper()}_API or keyring)."))
        return []
    try:
        series = _get(base, api, "series", timeout=LIST_TIMEOUT) or []
    except Exception as e:
        print(_red(f"  sonarr/{name}: GET /series failed: {e}"))
        return []

    meta = {s["id"]: {"runtime": s.get("runtime"),
                      "anime": (s.get("seriesType") == "anime"),
                      "title": s.get("title")}
            for s in series if s.get("id") is not None}

    def _fetch(sid):
        try:
            return sid, (_get(base, api, f"episodefile?seriesId={sid}") or [])
        except Exception:
            return sid, []

    rows = []
    with ThreadPoolExecutor(max_workers=SONARR_WORKERS) as ex:
        futs = [ex.submit(_fetch, sid) for sid in meta]
        done = 0
        for fut in as_completed(futs):
            sid, files = fut.result()
            done += 1
            if done % 50 == 0 or done == len(futs):
                print(f"\r  sonarr/{name}: {done}/{len(futs)} series scanned…",
                      end="", flush=True)
            info = meta.get(sid, {})
            for f in files:
                size = f.get("size")
                if not size:
                    continue
                qname = _quality_name(f.get("quality") or {})
                if not qname:
                    continue
                media = f.get("mediaInfo") or {}
                runtime_min = _parse_runtime_min(media.get("runTime"), info.get("runtime"))
                mbpm = _mbpm(size, runtime_min)
                if mbpm is None:
                    continue
                rows.append({"service": "sonarr", "instance": name, "quality": qname,
                             "content": "anime" if info.get("anime") else "live",
                             "mbpm": mbpm, "size_gb": size / 1e9,
                             "runtime_min": runtime_min, "title": info.get("title")})
    print(_green(f"\r  sonarr/{name}: {len(rows)} episode file(s) measured "
                 f"(of {len(meta)} series).{' ' * 20}"))
    return rows


# ── Reporting ─────────────────────────────────────────────────────────────────
def _table(rows: list[dict], key=lambda r: r["quality"]) -> dict:
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r["mbpm"])
    return {k: _percentiles(v) for k, v in buckets.items()}


def _print_quality_table(stats: dict, title: str):
    print(f"\n{_bold(title)}")
    print(f"  {'quality':22} {'n':>5} {'p10':>7} {'median':>7} {'mean':>7} {'p90':>7} {'max':>7}")
    for q in sorted(stats, key=lambda k: stats[k]["median"]):
        s = stats[q]
        print(f"  {q:22} {s['n']:>5} {s['p10']:>7.1f} {s['median']:>7.1f} "
              f"{s['mean']:>7.1f} {s['p90']:>7.1f} {s['max']:>7.1f}")


def _print_anime_split(rows: list[dict], min_samples: int):
    by = _table(rows, key=lambda r: (r["content"], r["quality"]))
    qualities = sorted({q for (_c, q) in by})
    interesting = []
    for q in qualities:
        a = by.get(("anime", q))
        l = by.get(("live", q))
        if a and l and a["n"] >= min_samples and l["n"] >= min_samples:
            ratio = l["median"] / a["median"] if a["median"] else 0
            if ratio >= 1.3 or ratio <= 0.77:
                interesting.append((q, a, l, ratio))
    if not interesting:
        print(f"\n{_bold('Anime vs live-action:')} no material gap at >= "
              f"{min_samples} samples each (single table per quality is fine).")
        return
    print(f"\n{_bold('Anime vs live-action (material gaps):')}")
    print(f"  {'quality':22} {'anime med':>10} {'live med':>10} {'live/anime':>11}")
    for q, a, l, ratio in interesting:
        print(f"  {q:22} {a['median']:>9.1f} ({a['n']}) {l['median']:>9.1f} ({l['n']}) "
              f"{ratio:>10.2f}x")
    print(_yellow("  → consider splitting these tiers by content type in size_model."))


def _suggested_table(stats: dict, min_samples: int) -> dict:
    """measured value (round) where n >= min_samples, else current table value."""
    out = dict(CALIBRATED_MB_PER_MIN)
    for q, s in stats.items():
        if s["n"] >= min_samples:
            # mean leans slightly above median for heavy-tailed sizes — a realistic
            # "expected grab". Round to 1 dp.
            out[q] = round(s["mean"], 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure real MiB/min per quality from your *arr libraries.")
    ap.add_argument("--json", metavar="PATH", help="also write raw per-quality stats to this JSON file")
    ap.add_argument("--min-samples", type=int, default=3,
                    help="min files before a measured quality overrides the table (default 3)")
    args = ap.parse_args()

    if not CONFIG.exists():
        print(_red(f"ERROR: config not found: {CONFIG}"))
        return 1
    cfg = ConfigLoader(CONFIG).load()

    anime_genres = {str(g).lower() for g in (cfg.get("animeGenres") or []) if g}

    print(_bold("Scanning instances (read-only)…"))
    rows: list[dict] = []
    for name, inst in (cfg.get("radarr_instances") or {}).items():
        if name == "default_instance" or not isinstance(inst, dict):
            continue
        rows += collect_radarr(name, inst, anime_genres)
    for name, inst in (cfg.get("sonarr_instances") or {}).items():
        if name == "default_instance" or not isinstance(inst, dict):
            continue
        rows += collect_sonarr(name, inst)

    if not rows:
        print(_red("\nNo measurable files found. Check that instances are reachable and "
                   "API keys are set (env RECOMMENDARR_*_API or the OS keyring)."))
        return 1

    print(_green(f"\nTotal measured files: {len(rows)}"))

    overall = _table(rows)
    _print_quality_table(overall, "Measured MiB/min per quality (all content):")
    _print_anime_split(rows, args.min_samples)

    suggested = _suggested_table(overall, args.min_samples)
    measured_qs = {q for q, s in overall.items() if s["n"] >= args.min_samples}
    print(f"\n{_bold('Suggested CALIBRATED_MB_PER_MIN')} "
          f"(measured override where n >= {args.min_samples}; "
          f"{len(measured_qs)} tier(s) measured, rest kept):")
    print("CALIBRATED_MB_PER_MIN = {")
    for q in sorted(suggested, key=lambda k: suggested[k]):
        tag = "  # measured" if q in measured_qs else ""
        print(f'    {q!r:28}: {suggested[q]},{tag}')
    print("}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"overall": overall,
             "by_content": {f"{c}/{q}": s for (c, q), s in _table(
                 rows, key=lambda r: (r["content"], r["quality"])).items()},
             "suggested": suggested}, indent=2), encoding="utf-8")
        print(_green(f"\nRaw stats written to {args.json}"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

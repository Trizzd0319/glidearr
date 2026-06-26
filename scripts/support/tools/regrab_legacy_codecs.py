"""
regrab_legacy_codecs.py — replace owned legacy-codec TV files with a modern-codec release.
=========================================================================================
Old DVD-era encodes (XviD / DivX / MPEG-2 / MPEG-4 ASP / WMV / VC-1) cannot be direct-
played by most modern Plex clients, so EVERY play of them transcodes — regardless of
resolution or bandwidth. This is the one cleanly *arr-fixable slice of the transcode
picture: when an x264/x265 release of the same episode exists at the same (or better)
resolution, swapping to it eliminates the transcode at ZERO quality loss.

This tool finds those legacy-codec episode files, runs ONE Sonarr interactive search per
file to CONFIRM a modern-codec release is actually available at >= the current resolution,
and (with --apply) grabs that specific release. Sonarr replaces the file on import —
nothing is deleted first, so an episode that has NO modern replacement (only XviD exists)
is left untouched and never lost.

It pairs with the profile change that scores AVC/x264 above legacy codecs (see
blueprint/sonarr_profiles.json), which stops NEW grabs from re-introducing these files —
apply that via arr_rebuild first so the re-grab lands on x264.

Detection reads the engine's synced episode_files.parquet when present (instant); if it's
missing it falls back to scanning Sonarr over the API (slower — a few minutes on a large
library). Each candidate then costs one live interactive search, so --limit defaults low.

    python scripts/support/tools/regrab_legacy_codecs.py                  # DRY-RUN preview
    python scripts/support/tools/regrab_legacy_codecs.py --series enterprise --limit 30
    python scripts/support/tools/regrab_legacy_codecs.py --apply          # grab replacements

Read-only by default. --apply grabs releases (prompts YES unless --confirm).
Exit codes: 0=ok, 1=connection/config error.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
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
from scripts.managers.machine_learning.quality_analytics.legacy_codec import (   # noqa: E402
    best_modern_release,
    is_legacy_codec as _is_legacy,
    normalize_codec as _norm_codec,
    release_resolution as _rel_res,
)


def _endpoint(icfg):
    raw = (icfg.get("base_url") or icfg.get("url") or "").strip()
    if raw and not raw.startswith(("http://", "https://")):
        proto = "https" if icfg.get("ssl", True) else "http"
        raw = f"{proto}://{raw}"
        port = icfg.get("port")
        if port and f":{port}" not in raw.split("://", 1)[-1]:
            raw = f"{raw}:{port}"
    return raw.rstrip("/"), (icfg.get("api") or "").strip()


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── Sonarr client ─────────────────────────────────────────────────────────────

class SonarrClient:
    def __init__(self, base, key, apply=False):
        self.base = base
        self.apply = apply
        self._s = requests.Session()
        self._s.headers.update({"X-Api-Key": key, "Content-Type": "application/json"})

    def get(self, ep, params=None):
        r = self._s.get(f"{self.base}/api/v3/{ep}", params=params, timeout=120)
        r.raise_for_status()
        return r.json()

    def grab(self, guid, indexer_id):
        """Push a specific release to the download client (the interactive 'grab' button). Sonarr
        imports it and replaces the existing episode file on a successful, higher-scored grab — so
        we never pre-delete. No-op unless --apply."""
        if not self.apply:
            return None
        r = self._s.post(f"{self.base}/api/v3/release",
                         json={"guid": guid, "indexerId": indexer_id}, timeout=120)
        r.raise_for_status()
        return r.json()


# ── tqdm (optional) ─────────────────────────────────────────────────────────────

def _progress(iterable, desc):
    try:
        from tqdm import tqdm
        return tqdm(iterable, desc=desc, unit="item", dynamic_ncols=True, leave=False)
    except ImportError:
        return iterable


# ── Detection ─────────────────────────────────────────────────────────────────

def _parquet_path(instance):
    # Mirrors CacheKeyBuilder's default base_dir (<repo>/scripts/support/cache).
    return Path(__file__).resolve().parents[1] / "cache" / "sonarr" / instance / "episode_files.parquet"


def legacy_from_parquet(instance, series_filter):
    """Read the engine's synced episode_files.parquet and return the legacy-codec rows. None when the
    parquet is absent or lacks the columns (caller falls back to the API)."""
    path = _parquet_path(instance)
    if not path.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except Exception:
        return None
    need = {"series_id", "series_title", "episode_file_id", "video_codec", "resolution"}
    if not need <= set(df.columns):
        return None
    sub = df[df["video_codec"].map(_is_legacy)]
    if series_filter:
        sub = sub[sub["series_title"].astype(str).str.contains(series_filter, case=False, na=False)]
    rows = []
    for _, r in sub.iterrows():
        sid, fid = _safe_int(r.get("series_id")), _safe_int(r.get("episode_file_id"))
        if sid is None or fid is None:
            continue
        rows.append({
            "series_id": sid, "series_title": str(r.get("series_title") or "?"),
            "episode_file_id": fid, "season": _safe_int(r.get("season_number")),
            "episode": _safe_int(r.get("episode_number")), "codec": str(r.get("video_codec") or "?"),
            "resolution": _safe_int(r.get("resolution")) or 0,
            "size_bytes": _safe_int(r.get("size_bytes")) or 0,
        })
    return rows


def legacy_from_api(client, series_filter):
    """Fallback: scan Sonarr over the API (one GET /episodefile per series that has files)."""
    series = client.get("series")
    have = [s for s in series if (s.get("statistics") or {}).get("episodeFileCount", 0) > 0]
    if series_filter:
        have = [s for s in have if series_filter.lower() in (s.get("title") or "").lower()]
    rows = []
    for s in _progress(have, "scanning series"):
        try:
            efs = client.get("episodefile", params={"seriesId": s["id"]})
        except Exception:
            continue
        for ef in (efs or []):
            mi = ef.get("mediaInfo") or {}
            if not _is_legacy(mi.get("videoCodec")):
                continue
            rows.append({
                "series_id": s["id"], "series_title": s.get("title") or "?",
                "episode_file_id": ef.get("id"), "season": None, "episode": None,
                "codec": mi.get("videoCodec") or "?",
                "resolution": _safe_int(((ef.get("quality") or {}).get("quality") or {}).get("resolution"))
                or _safe_int(mi.get("height")) or 0,
                "size_bytes": _safe_int(ef.get("size")) or 0,
            })
    return rows


# ── Replacement selection ───────────────────────────────────────────────────────

def episode_map(client, sid, cache):
    if sid in cache:
        return cache[sid]
    try:
        eps = client.get("episode", params={"seriesId": sid})
    except Exception:
        eps = []
    m = {}
    for e in (eps or []):
        fid = _safe_int(e.get("episodeFileId"))
        if fid:
            m.setdefault(fid, e)
    cache[sid] = m
    return m


def _ep_label(row):
    t = str(row.get("series_title") or "?")[:24]
    s, e = row.get("season"), row.get("episode")
    if s is not None and e is not None:
        try:
            return f"{t} S{int(s):02d}E{int(e):02d}"
        except (TypeError, ValueError):
            pass
    return t


def _print_report(replaceable, no_repl):
    if replaceable:
        print(f"\nReplaceable ({len(replaceable)}) — current → modern release we'd grab:")
        for row, rel, _eid in replaceable[:40]:
            cur = f"{_norm_codec(row['codec'])}@{row['resolution'] or '?'}"
            relt = (rel.get("title") or "?")[:50]
            print(f"  {_ep_label(row):<32} {cur:<12} → {relt}  [{_rel_res(rel) or '?'}p]")
        if len(replaceable) > 40:
            print(f"  … and {len(replaceable) - 40} more")
    if no_repl:
        reasons = Counter(reason for _r, reason in no_repl)
        print(f"\nLeft as-is ({len(no_repl)}): "
              + ", ".join(f"{r} ×{n}" for r, n in reasons.most_common()))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Replace legacy-codec TV files with a modern-codec release.")
    ap.add_argument("--instance", default="standard")
    ap.add_argument("--apply", action="store_true", help="Grab the confirmed modern releases (default: dry-run).")
    ap.add_argument("--confirm", action="store_true", help="Skip the YES prompt under --apply.")
    ap.add_argument("--limit", type=int, default=40,
                    help="Check at most N legacy files this run (one interactive search each; default 40).")
    ap.add_argument("--series", default=None, help="Only series whose title contains this substring.")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    ic = cfg.get("sonarr_instances", {}) or {}
    icfg = ic.get(args.instance) or ic.get((ic.get("default_instance") or {}).get("name")) or {}
    base, api = _endpoint(icfg)
    if not base or not api:
        print(f"ABORT: Sonarr instance '{args.instance}' not configured (base/api missing).")
        return 1
    client = SonarrClient(base, api, apply=args.apply)

    try:
        ver = client.get("system/status").get("version", "?")
    except Exception as e:
        print(f"ABORT: cannot reach Sonarr at {base}: {e}")
        return 1

    DRY = "" if args.apply else "[dry-run] "
    print(f"\nregrab_legacy_codecs  {'*** LIVE ***' if args.apply else '(dry-run)'}  Sonarr v{ver} @ {base}\n")

    legacy = legacy_from_parquet(args.instance, args.series)
    src = "episode_files.parquet"
    if legacy is None:
        print("(episode_files.parquet not found — scanning Sonarr via API; this can take a few minutes)")
        legacy = legacy_from_api(client, args.series)
        src = "Sonarr API"
    print(f"Found {len(legacy)} legacy-codec episode file(s) via {src}"
          + (f" matching '{args.series}'" if args.series else "") + ".")
    if not legacy:
        print("Nothing to do.")
        return 0

    by_codec = Counter(_norm_codec(r["codec"]) for r in legacy)
    total_gb = sum(r["size_bytes"] for r in legacy) / (1024 ** 3)
    print("  by codec: " + ", ".join(f"{c}×{n}" for c, n in by_codec.most_common())
          + f"   |   total {total_gb:.1f} GB")

    work = legacy[:args.limit]
    if len(legacy) > args.limit:
        print(f"  --limit {args.limit}: checking the first {args.limit} for a modern replacement "
              f"({len(legacy) - args.limit} more not checked this run).")

    ep_cache: dict = {}
    replaceable: list = []
    no_repl: list = []
    print(f"\nInteractive search (one per file) — confirming a modern replacement exists…")
    for row in _progress(work, "searching"):
        emap = episode_map(client, row["series_id"], ep_cache)
        ep = emap.get(row["episode_file_id"])
        if ep:
            if row.get("season") is None:
                row["season"] = ep.get("seasonNumber")
            if row.get("episode") is None:
                row["episode"] = ep.get("episodeNumber")
        eid = _safe_int(ep.get("id")) if ep else None
        if not eid:
            no_repl.append((row, "no episode id"))
            continue
        try:
            releases = client.get("release", params={"episodeId": eid})
        except Exception:
            no_repl.append((row, "search error"))
            continue
        best = best_modern_release(releases, row["resolution"])
        if best:
            replaceable.append((row, best, eid))
        else:
            no_repl.append((row, "no modern release >= current res"))

    _print_report(replaceable, no_repl)
    print(f"\n{DRY}{len(replaceable)} replaceable, {len(no_repl)} left as-is (of {len(work)} checked).")

    if not args.apply:
        print("\nDry-run only — nothing grabbed. Re-run with --apply to grab the modern releases above.")
        print("Tip: apply the AVC/x264 profile change (arr_rebuild) first so the re-grab prefers x264.")
        return 0
    if not replaceable:
        print("No replacements to grab.")
        return 0
    if not args.confirm:
        ans = input(f'\nGrab {len(replaceable)} modern release(s)? Sonarr replaces each file on import. '
                    f'Type "YES" to proceed: ').strip()
        if ans != "YES":
            print("Cancelled — nothing grabbed.")
            return 0

    grabbed = errors = 0
    for row, rel, _eid in _progress(replaceable, "grabbing"):
        try:
            client.grab(rel.get("guid"), rel.get("indexerId"))
            grabbed += 1
            time.sleep(0.5)
        except Exception as e:
            errors += 1
            print(f"  grab failed — {row['series_title']} / {(rel.get('title') or '?')[:40]}: {e}")
    print(f"\nGrabbed {grabbed} release(s)" + (f" ({errors} errors)" if errors else "")
          + ". Sonarr will import and replace the legacy files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

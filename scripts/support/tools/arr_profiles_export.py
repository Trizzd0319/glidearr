"""arr_profiles_export.py — snapshot Radarr/Sonarr Quality Profiles + Custom Formats to a portable store.

READ-ONLY against the live instance. Writes, under ``scripts/support/profiles/<service>/<instance>/``:
  • Re-importable raw JSON — qualityprofiles / customformats / qualitydefinitions / languages, exactly as
    the *arr API returns them (the source of truth an apply tool replays onto a fresh instance);
  • Flat "accountability" parquet tables — one row per custom format, per quality profile, per
    (profile × scored custom format), and per (profile × quality item) — so the whole setup is auditable
    / diffable without re-reading the live server;
  • A manifest.json (instance, server version, per-endpoint counts, timestamp).

The API key + base_url are loaded from the live config (env var / OS keyring overlay) — nothing secret is
written to the export. Run:
    python -m scripts.support.tools.arr_profiles_export --service radarr --instance standard
    python -m scripts.support.tools.arr_profiles_export --all
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader          # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH           # noqa: E402

PROFILES_ROOT = _REPO_ROOT / "scripts" / "support" / "profiles"

# *arr v3 endpoints we snapshot → the raw-JSON filename each lands in.
_ENDPOINTS = {
    "qualityprofile": "qualityprofiles.json",
    "customformat": "customformats.json",
    "qualitydefinition": "qualitydefinitions.json",
    "language": "languages.json",
}


def _get(base: str, key: str, path: str):
    r = requests.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, timeout=60)
    r.raise_for_status()
    return r.json()


def _resolve(cfg, service: str, instance: str) -> tuple[str, str]:
    inst = ((cfg.get(f"{service}_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _quality_rows(qp: dict):
    """(quality_name, allowed, is_cutoff) per quality item of a profile, flattening groups."""
    cutoff = qp.get("cutoff")
    out = []
    for it in qp.get("items", []) or []:
        if it.get("items"):                                   # a quality GROUP
            gid, gname, gallowed = it.get("id"), it.get("name"), bool(it.get("allowed"))
            out.append((f"[{gname}]", gallowed, gid == cutoff))
            for sub in it["items"]:
                q = sub.get("quality") or {}
                out.append((q.get("name"), bool(sub.get("allowed")), q.get("id") == cutoff))
        else:                                                 # a single quality
            q = it.get("quality") or {}
            out.append((q.get("name"), bool(it.get("allowed")), q.get("id") == cutoff))
    return out


def _write_tables(out: Path, data: dict):
    """Flatten the raw JSON into auditable parquet (CSV fallback) tables."""
    try:
        import pandas as pd
    except Exception as e:                                    # pandas should be present; degrade loudly
        print(f"  [warn] pandas unavailable ({e}) — skipping parquet tables (raw JSON still written).")
        return

    cfs = data.get("customformat", []) or []
    qps = data.get("qualityprofile", []) or []
    cf_id_to_name = {c.get("id"): c.get("name") for c in cfs}

    cf_rows = [{
        "id": c.get("id"),
        "name": c.get("name"),
        "n_specs": len(c.get("specifications", []) or []),
        "spec_impls": ",".join(sorted({s.get("implementation", "") for s in c.get("specifications", []) or []})),
        "include_when_renaming": c.get("includeCustomFormatWhenRenaming"),
    } for c in cfs]

    qp_rows, score_rows, qual_rows = [], [], []
    for p in qps:
        fmt_items = p.get("formatItems", []) or []
        scored = [(fi.get("name") or cf_id_to_name.get(fi.get("format")), fi.get("score"))
                  for fi in fmt_items]
        nonzero = [(n, s) for n, s in scored if s]
        cutoff_name = next((q for q, _a, c in _quality_rows(p) if c), None)
        qp_rows.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "cutoff": cutoff_name,
            "upgrade_allowed": p.get("upgradeAllowed"),
            "min_format_score": p.get("minFormatScore"),
            "cutoff_format_score": p.get("cutoffFormatScore"),
            "min_upgrade_format_score": p.get("minUpgradeFormatScore"),
            "language": (p.get("language") or {}).get("name"),
            "n_qualities_allowed": sum(1 for _q, a, _c in _quality_rows(p) if a and not _q.startswith("[")),
            "n_cf_scored_nonzero": len(nonzero),
        })
        for name, score in nonzero:
            score_rows.append({"profile": p.get("name"), "custom_format": name, "score": score})
        for qname, allowed, is_cut in _quality_rows(p):
            qual_rows.append({"profile": p.get("name"), "quality": qname,
                              "allowed": allowed, "is_cutoff": is_cut})

    for fname, rows in (("customformats.parquet", cf_rows),
                        ("qualityprofiles.parquet", qp_rows),
                        ("qp_cf_scores.parquet", score_rows),
                        ("qp_qualities.parquet", qual_rows)):
        df = pd.DataFrame(rows)
        path = out / fname
        try:
            df.to_parquet(path, index=False)
        except Exception as e:
            print(f"  [warn] parquet write failed for {fname} ({e}) — writing CSV instead.")
            df.to_csv(path.with_suffix(".csv"), index=False)


def export_instance(cfg, service: str, instance: str):
    base, key = _resolve(cfg, service, instance)
    if not (base and key):
        print(f"  [skip] {service}/{instance}: no base_url/api in config")
        return None
    out = PROFILES_ROOT / service / instance
    out.mkdir(parents=True, exist_ok=True)

    data = {}
    for ep, fname in _ENDPOINTS.items():
        try:
            data[ep] = _get(base, key, ep)
        except Exception as e:
            print(f"  [warn] {service}/{instance} GET {ep}: {e}")
            data[ep] = []
        (out / fname).write_text(json.dumps(data[ep], indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        version = (_get(base, key, "system/status") or {}).get("version")
    except Exception:
        version = None

    manifest = {
        "service": service,
        "instance": instance,
        "base_url": base,
        "version": version,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {ep: len(data.get(ep, []) or []) for ep in _ENDPOINTS},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_tables(out, data)
    print(f"  [ok] {service}/{instance} (v{version}): "
          + ", ".join(f"{ep}={len(data.get(ep, []) or [])}" for ep in _ENDPOINTS)
          + f"  ->  {out.relative_to(_REPO_ROOT)}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Radarr/Sonarr quality profiles + custom formats.")
    ap.add_argument("--service", choices=["radarr", "sonarr"], help="Which service (omit with --all).")
    ap.add_argument("--instance", default="standard", help="Instance name in config (default: standard).")
    ap.add_argument("--all", action="store_true", help="Export every configured radarr + sonarr instance.")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    targets: list[tuple[str, str]] = []
    if args.all:
        for service in ("radarr", "sonarr"):
            for name in (cfg.get(f"{service}_instances", {}) or {}):
                if name == "default_instance":
                    continue
                targets.append((service, name))
    elif args.service:
        targets.append((args.service, args.instance))
    else:
        ap.error("pass --service or --all")

    print(f"Exporting to {PROFILES_ROOT.relative_to(_REPO_ROOT)} ...")
    any_ok = False
    for service, instance in targets:
        if export_instance(cfg, service, instance):
            any_ok = True
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())

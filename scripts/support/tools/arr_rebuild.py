"""arr_rebuild.py — wipe + rebuild Radarr/Sonarr custom formats + quality profiles from the blueprint.

DRY-RUN BY DEFAULT — prints the full plan and changes NOTHING. ``--apply`` executes in a SAFE,
additive-then-cleanup order so no movie/series is ever left without a profile mid-flight:
    snapshot → install merged CFs → install new profiles → reassign every item to its new profile
    → delete the old profiles → delete the now-unreferenced old CFs → (radarr) rewrite the ladder.

CF definitions resolve OURS > TRaSH(canonical) > your-current-export (preserves bespoke CFs);
CF scores come from the blueprint (your tuning + codec/English overlays). Run:
    python -m scripts.support.tools.arr_rebuild                  # dry-run, both services
    python -m scripts.support.tools.arr_rebuild --service radarr # dry-run, one
    python -m scripts.support.tools.arr_rebuild --apply          # EXECUTE (after reviewing dry-run)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH       # noqa: E402

PROF = _REPO_ROOT / "scripts" / "support" / "profiles"
_ITEM_EP = {"radarr": "movie", "sonarr": "series"}


def _resolve(cfg, svc, instance="standard"):
    inst = ((cfg.get(f"{svc}_instances", {}) or {}).get(instance, {}) or {})
    return inst.get("base_url") or "", inst.get("api") or ""


def _get(base, key, path):
    r = requests.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, timeout=60)
    r.raise_for_status()
    return r.json()


def _strip_spec(s):
    s = {k: v for k, v in s.items() if k != "id"}
    return s


def _strip_cf(c):
    return {"name": c["name"],
            "includeCustomFormatWhenRenaming": c.get("includeCustomFormatWhenRenaming", False),
            "specifications": [_strip_spec(s) for s in c.get("specifications", []) or []]}


def _cf_index(svc):
    ours = {c["name"]: _strip_cf(c) for c in
            json.loads((PROF / "blueprint" / "custom_formats.json").read_text(encoding="utf-8"))["custom_formats"]}
    trash = {}
    for f in sorted((PROF / "trash" / svc / "cf").glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        trash[c["name"]] = _strip_cf(c)
    current = {c["name"]: _strip_cf(c) for c in
               json.loads((PROF / svc / "standard" / "customformats.json").read_text(encoding="utf-8"))}
    return ours, trash, current


def _resolve_install_cfs(svc, referenced):
    ours, trash, current = _cf_index(svc)
    install, source, missing = {}, {"ours": 0, "trash": 0, "current": 0}, []
    for nm in sorted(referenced):
        if nm in ours:
            install[nm] = ours[nm]; source["ours"] += 1
        elif nm in trash:
            install[nm] = trash[nm]; source["trash"] += 1
        elif nm in current:
            install[nm] = current[nm]; source["current"] += 1
        else:
            missing.append(nm)
    return install, source, missing


def _ladder_rewrite(cfg, live_qps):
    """For radarr: map the configured radarr_quality_ladder (pct → old id) to pct → profile NAME via
    the live export, so after rebuild it re-resolves to the new ids by the (unchanged) base names."""
    ladder = cfg.get("radarr_quality_ladder") or []
    by_id = {p["id"]: p["name"] for p in live_qps}
    return [(pct, by_id.get(pid, f"<id {pid}>")) for pct, pid in ladder]


def dry_run(cfg, svc, instance="standard") -> bool:
    base, key = _resolve(cfg, svc, instance)
    if not (base and key):
        print(f"\n### {svc}: not configured — skipped"); return False
    bp = json.loads((PROF / "blueprint" / f"{svc}_profiles.json").read_text(encoding="utf-8"))["profiles"]
    referenced = {n for p in bp for n in p["cf_scores"]}
    install, source, missing = _resolve_install_cfs(svc, referenced)
    live_cfs = _get(base, key, "customformat")
    live_qps = _get(base, key, "qualityprofile")
    n_items = len(_get(base, key, _ITEM_EP[svc]))

    print(f"\n{'='*78}\n### {svc.upper()}  ({base})\n{'='*78}")
    print(f"CURRENT TIERS ({len(live_qps)} profiles, {len(live_cfs)} custom formats, {n_items} items assigned):")
    for p in live_qps:
        cut = next((q.get("name") for it in p.get("items", []) for q in [it.get("quality") or {}]
                    if q.get("id") == p.get("cutoff")), p.get("cutoff"))
        nz = sum(1 for fi in p.get("formatItems", []) if fi.get("score"))
        print(f"    - {p['name']:34s} (cutoff {str(cut):16s} · {nz} CF scores)")

    print(f"\nPLAN (dry-run — nothing changed):")
    print(f"    DELETE  {len(live_qps)} old profiles + {len(live_cfs)} old custom formats")
    print(f"    INSTALL {len(install)} custom formats  "
          f"(ours={source['ours']}, trash={source['trash']}, preserved-current={source['current']})")
    print(f"    INSTALL {len(bp)} new profiles:")
    for p in bp:
        print(f"    + {p['name']:40s} (cutoff {str(p['cutoff']):16s} · lang {p['language']} · "
              f"{len(p['cf_scores'])} CF scores)")
    print(f"    REASSIGN {n_items} {_ITEM_EP[svc]}(s) to their new profile, then drop the old ones")
    if svc == "radarr":
        print(f"    LADDER rewrite (radarr_quality_ladder → re-resolved by name to new ids):")
        for pct, nm in _ladder_rewrite(cfg, live_qps):
            keep = "✓ kept" if any(p["name"] == nm for p in bp) else "✗ MISSING in new set"
            print(f"        >= {pct:3d}%  ->  {nm}   [{keep}]")
    if missing:
        print(f"\n    ⚠ {len(missing)} referenced CF(s) have NO definition (ours/trash/current) — "
              f"would be skipped:\n        {missing}")
    else:
        print(f"\n    ✓ all {len(referenced)} referenced custom formats resolve to a definition.")
    return not missing


def _post(base, key, path, payload):
    r = requests.post(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, json=payload, timeout=60)
    r.raise_for_status()
    return r.json() if r.content else None


def _put(base, key, path, payload):
    r = requests.put(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, json=payload, timeout=120)
    r.raise_for_status()
    return r.json() if r.content else None


def _delete(base, key, path):
    r = requests.delete(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, timeout=60)
    r.raise_for_status()


def _schema_quality_index(schema):
    """name → cutoff id from a /qualityprofile/schema template (group names AND leaf quality names)."""
    idx = {}
    for it in schema.get("items", []) or []:
        if it.get("items"):                                  # a group
            if it.get("name") is not None:
                idx[it["name"]] = it.get("id")
                idx[f"[{it['name']}]"] = it.get("id")        # blueprint marks group cutoffs as "[name]"
            for sub in it["items"]:
                q = sub.get("quality") or {}
                if q.get("name"):
                    idx[q["name"]] = q.get("id")
        else:
            q = it.get("quality") or {}
            if q.get("name"):
                idx[q["name"]] = q.get("id")
    return idx


def _lang_index(base, key):
    try:
        return {l.get("name"): l.get("id") for l in _get(base, key, "language")}
    except Exception:
        return {}


def _reassign_map(bp_names, live_names, any_sd_default):
    """old profile name → new profile name. Base names 1:1; 'English - X' → 'X'; everything else
    (Any/SD/orphans) → the configured default. Anything already a new name stays."""
    out = {}
    for nm in live_names:
        if nm in bp_names:
            out[nm] = nm
        elif nm.startswith("English - ") and nm[len("English - "):] in bp_names:
            out[nm] = nm[len("English - "):]
        else:
            out[nm] = any_sd_default
    return out


def validate(cfg, svc, instance="standard") -> bool:
    """NON-WRITING: prove every blueprint profile builds against the LIVE schema — cutoff, language and
    all CF-score names resolve — and the reassignment map is total. Catches everything --apply would hit."""
    base, key = _resolve(cfg, svc, instance)
    bp = json.loads((PROF / "blueprint" / f"{svc}_profiles.json").read_text(encoding="utf-8"))["profiles"]
    bp_names = {p["name"] for p in bp}
    referenced = {n for p in bp for n in p["cf_scores"]}
    install, _src, missing_cf = _resolve_install_cfs(svc, referenced)
    schema = _get(base, key, "qualityprofile/schema")
    qidx = _schema_quality_index(schema)
    lidx = _lang_index(base, key)
    live_names = [p["name"] for p in _get(base, key, "qualityprofile")]
    default = "HD - 720p/1080p"

    print(f"\n### {svc.upper()} — validate (no writes)")
    bad = []
    for p in bp:
        # Leaf-quality cutoffs must exist in the schema; a bracketed "[group]" is a custom quality
        # group cloned verbatim from the source profile (apply copies source items), so it's fine.
        cut = p.get("cutoff")
        if cut and not str(cut).startswith("[") and cut not in qidx:
            bad.append(f"  {p['name']}: cutoff {cut!r} not in schema")
        if p.get("language") and p["language"] not in lidx and p["language"] != "Any":
            bad.append(f"  {p['name']}: language {p['language']!r} not found")
    install_names = set(install)
    cf_unresolved = sorted(n for n in referenced if n not in install_names)
    rmap = _reassign_map(bp_names, live_names, default)
    unmapped = [nm for nm, tgt in rmap.items() if tgt not in bp_names]

    print(f"  profiles to build:   {len(bp)}  (cutoff+language resolve: {'OK' if not bad else 'FAIL'})")
    print(f"  CFs to install:      {len(install)}  (referenced-but-missing: {len(cf_unresolved)})")
    print(f"  reassignment map:    {len(rmap)} old→new, default={default!r}  (targets all valid: "
          f"{'OK' if not unmapped else 'FAIL '+str(unmapped)})")
    for b in bad[:10]:
        print(b)
    if cf_unresolved:
        print(f"  ⚠ unresolved CFs: {cf_unresolved}")
    ok = not bad and not cf_unresolved and not unmapped
    print(f"  => {'READY to --apply' if ok else 'NOT READY — fix the above'}")
    return ok


def _src_name(name):
    """The base-tier source profile a blueprint profile clones items from (strip the codec suffix)."""
    for suf in (" (H264)", " (HEVC-DV)", " (HEVC)", " (AV1)"):
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def apply(cfg, svc, instance) -> bool:
    """EXECUTE the rebuild on ONE instance. Merge-style (PUT existing names, POST new) so items are never
    left profile-less: snapshot → upsert CFs → upsert profiles (clone source items, full formatItems) →
    reassign items off dropped profiles → delete dropped profiles → delete dropped CFs."""
    from collections import defaultdict
    base, key = _resolve(cfg, svc, instance)
    if not (base and key):
        print(f"{svc}/{instance}: not configured"); return False
    bp = json.loads((PROF / "blueprint" / f"{svc}_profiles.json").read_text(encoding="utf-8"))["profiles"]
    bp_names = {p["name"] for p in bp}
    referenced = {n for p in bp for n in p["cf_scores"]}
    install, _src, missing = _resolve_install_cfs(svc, referenced)
    if missing:
        print(f"ABORT {svc}/{instance}: unresolved CFs {missing}"); return False
    src_raw = {p["name"]: p for p in
               json.loads((PROF / svc / "standard" / "qualityprofiles.json").read_text(encoding="utf-8"))}

    print(f"\n### APPLY → {svc}/{instance} ({base})")
    old_cfs = _get(base, key, "customformat")
    old_qps = _get(base, key, "qualityprofile")
    snap = PROF / svc / instance / "_pre_apply_snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "customformats.json").write_text(json.dumps(old_cfs, indent=2), encoding="utf-8")
    (snap / "qualityprofiles.json").write_text(json.dumps(old_qps, indent=2), encoding="utf-8")
    print(f"  snapshot: {len(old_cfs)} CFs + {len(old_qps)} profiles -> {snap.relative_to(_REPO_ROOT)}")

    # 1. upsert CFs — PUT existing by name, POST new. Resilient: if an update is rejected we KEEP the
    # existing definition (its id still scores fine); a failed new-POST is logged and its score skipped.
    cur_cf = {c["name"]: c for c in old_cfs}
    kept = added = failed = 0
    for nm, defn in install.items():
        try:
            if nm in cur_cf:
                _put(base, key, f"customformat/{cur_cf[nm]['id']}", {**defn, "id": cur_cf[nm]["id"]})
            else:
                _post(base, key, "customformat", defn); added += 1
        except Exception:
            if nm in cur_cf:
                kept += 1                     # keep what's there
            else:
                failed += 1
    cf_id = {c["name"]: c["id"] for c in _get(base, key, "customformat")}
    lang_id = _lang_index(base, key)
    print(f"  CFs: {added} added, {kept} kept-as-is (update rejected), {failed} new-failed "
          f"(instance now has {len(cf_id)})")

    # 2. upsert profiles (PUT existing-by-name, POST new) — clone source items, full formatItems
    cur_qp = {p["name"]: p for p in old_qps}

    def _fmt(scores):
        return [{"format": cf_id[n], "name": n, "score": int(scores.get(n, 0))} for n in cf_id]

    created = updated = 0
    for p in bp:
        src = src_raw.get(_src_name(p["name"]))
        if not src:
            print(f"  ! no source items for {p['name']} — skipped"); continue
        payload = {
            "name": p["name"], "upgradeAllowed": p["upgradeAllowed"], "cutoff": src.get("cutoff"),
            "items": src.get("items"), "minFormatScore": p["minFormatScore"],
            "cutoffFormatScore": p["cutoffFormatScore"], "minUpgradeFormatScore": p["minUpgradeFormatScore"],
            "formatItems": _fmt(p["cf_scores"]),
        }
        lid = lang_id.get(p["language"], lang_id.get("Any"))
        if lid is not None:
            payload["language"] = {"id": lid, "name": p["language"]}
        try:
            if p["name"] in cur_qp:
                payload["id"] = cur_qp[p["name"]]["id"]
                _put(base, key, f"qualityprofile/{payload['id']}", payload); updated += 1
            else:
                _post(base, key, "qualityprofile", payload); created += 1
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")[:200]
            print(f"  ! profile {p['name']!r} failed: {body or e}")
    print(f"  profiles: {updated} updated, {created} created")

    # 3. reassign items off DROPPED profiles, then delete those profiles + dropped CFs
    new_qp = {p["name"]: p["id"] for p in _get(base, key, "qualityprofile")}
    old_id_name = {p["id"]: p["name"] for p in old_qps}
    rmap = _reassign_map(bp_names, list(cur_qp), "HD - 720p/1080p")
    ep = _ITEM_EP[svc]; idkey = "movieIds" if svc == "radarr" else "seriesIds"
    groups = defaultdict(list)
    for it in _get(base, key, ep):
        cur_name = old_id_name.get(it.get("qualityProfileId"))
        tgt = rmap.get(cur_name, cur_name)
        if tgt != cur_name and tgt in new_qp:
            groups[new_qp[tgt]].append(it["id"])
    moved = 0
    for pid, ids in groups.items():
        _put(base, key, f"{ep}/editor", {idkey: ids, "qualityProfileId": pid}); moved += len(ids)
    print(f"  reassigned {moved} {ep}(s) off dropped profiles")

    dp = dc = 0
    for name, prof in cur_qp.items():
        if name not in bp_names:
            try:
                _delete(base, key, f"qualityprofile/{prof['id']}"); dp += 1
            except Exception as e:
                print(f"  ! keep profile {name!r} ({e})")
    for name, c in cur_cf.items():
        if name not in install:
            try:
                _delete(base, key, f"customformat/{c['id']}"); dc += 1
            except Exception as e:
                print(f"  ! keep CF {name!r} ({e})")
    print(f"  deleted {dp} old profiles + {dc} old CFs")
    print(f"  DONE {svc}/{instance}: now {len(bp)} profiles, {len(install)} CFs. "
          f"(rollback: {snap.relative_to(_REPO_ROOT)})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Wipe + rebuild Radarr/Sonarr CFs + quality profiles.")
    ap.add_argument("--service", choices=["radarr", "sonarr"])
    ap.add_argument("--instance", default="standard", help="Instance name (default: standard).")
    ap.add_argument("--validate", action="store_true", help="Non-writing: prove payloads build vs the live schema.")
    ap.add_argument("--apply", action="store_true", help="EXECUTE the rebuild (requires --i-have-backups).")
    ap.add_argument("--i-have-backups", action="store_true", help="Required ack for --apply.")
    args = ap.parse_args()

    cfg = ConfigLoader(CONFIG_PATH).load()
    services = [args.service] if args.service else ["radarr", "sonarr"]

    if args.apply:
        if not args.i_have_backups:
            print("Refusing --apply without --i-have-backups (wipes/rebuilds CFs+QPs, reassigns items).")
            return 2
        ok = True
        for svc in services:
            ok = apply(cfg, svc, args.instance) and ok
        return 0 if ok else 1

    ok = True
    fn = validate if args.validate else dry_run
    for svc in services:
        ok = fn(cfg, svc, args.instance) and ok
    print(f"\n{'='*78}\n{'VALIDATE' if args.validate else 'DRY-RUN'} complete: {'PASS' if ok else 'see ⚠/FAIL above'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

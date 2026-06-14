"""Diagnostic: did the router_show file-moves actually happen on disk?

Sonarr's series.rootFolderPath was updated by the editor call, but the physical
file move is a separate background command. This samples series in each target
folder and checks whether their EPISODE FILE paths (ground truth) actually sit
under that folder or are still orphaned at an old root.
"""
from __future__ import annotations
import sys

# Same-dir siblings (scripts/support/tools/) — resolved via sys.path[0].
from sd_replace import SonarrClient, load_config
from router_show import resolve_instance

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOTS = [
    "/data/media/tv/anime",
    "/data/media/tv/kids",
    "/data/media/tv/reality",
    "/data/media/tv/documentaries",
    "/data/media/tv/series",
]


def norm(p):
    return (p or "").replace("\\", "/").rstrip("/").lower()


def root_of(path):
    n = norm(path)
    for r in ROOTS:
        if n == norm(r) or n.startswith(norm(r) + "/"):
            return r
    return "??(" + (path or "") + ")"


cfg = load_config()
name, url, key = resolve_instance(cfg, None)
client = SonarrClient(url, key)
print(f"Sonarr @ {url}\n")

series = client.get("series")
with_files = [s for s in series if (s.get("statistics") or {}).get("episodeFileCount", 0) > 0]
print(f"{len(series)} series, {len(with_files)} have files — full scan of those...\n")

mismatches = []
checked = 0
for s in with_files:
    efs = client.get("episodefile", params={"seriesId": s["id"]}) or []
    if not efs:
        continue
    checked += 1
    db_root = root_of(s.get("rootFolderPath") or s.get("path"))
    file_root = root_of(efs[0].get("path"))
    if norm(file_root) != norm(db_root):
        mismatches.append((s.get("title", "?"), db_root, file_root, efs[0].get("path")))
    if checked % 500 == 0:
        print(f"  …checked {checked} ({len(mismatches)} mismatches so far)")

print(f"\nChecked {checked} series-with-files. Orphaned (files NOT at Sonarr's path): {len(mismatches)}")
for title, db_root, file_root, fp in mismatches[:40]:
    print(f"  {title[:36]:<36} db={db_root}  files@ {file_root}")
if len(mismatches) > 40:
    print(f"  … and {len(mismatches)-40} more")
if not mismatches:
    print("\n✅ Every file is where Sonarr expects it. The moves completed — nothing to re-task.")
else:
    print(f"\n⚠️ {len(mismatches)} series have orphaned files — these need a real re-move.")

"""arr_trash_fetch.py — pull the canonical TRaSH-Guides JSON (custom formats + reference profiles).

Downloads the TRaSH-Guides/Guides repo tarball ONCE and extracts the community-approved JSON for both
services into ``scripts/support/profiles/trash/<service>/{cf,quality-profiles,quality-size}/``. This is
the authoritative source the rebuild merges from — re-run any time to refresh to the latest upstream.

Read-only against the internet; writes only into the local trash/ cache. Run:
    python -m scripts.support.tools.arr_trash_fetch
"""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
TRASH_ROOT = _REPO_ROOT / "scripts" / "support" / "profiles" / "trash"
TARBALL = "https://github.com/TRaSH-Guides/Guides/archive/refs/heads/master.tar.gz"

# (service, subdir) we keep out of docs/json/<service>/
_WANT = [("radarr", "cf"), ("radarr", "quality-profiles"), ("radarr", "quality-size"),
         ("sonarr", "cf"), ("sonarr", "quality-profiles"), ("sonarr", "quality-size")]


def main() -> int:
    print(f"Downloading TRaSH-Guides tarball …")
    r = requests.get(TARBALL, timeout=120)
    r.raise_for_status()
    counts: dict[str, int] = {}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not (m.isfile() and m.name.endswith(".json")):
                continue
            # strip the leading "Guides-master/" component
            parts = m.name.split("/", 1)
            rel = parts[1] if len(parts) == 2 else m.name
            if not rel.startswith("docs/json/"):
                continue
            tail = rel[len("docs/json/"):]                       # e.g. radarr/cf/2160p.json
            svc, _, rest = tail.partition("/")
            sub = rest.split("/", 1)[0]
            if (svc, sub) not in _WANT:
                continue
            dest = TRASH_ROOT / svc / rest
            dest.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            dest.write_bytes(f.read())
            counts[f"{svc}/{sub}"] = counts.get(f"{svc}/{sub}", 0) + 1
    for k in sorted(counts):
        print(f"  {k}: {counts[k]} files")
    print(f"-> {TRASH_ROOT.relative_to(_REPO_ROOT)}")
    return 0 if counts else 1


if __name__ == "__main__":
    sys.exit(main())

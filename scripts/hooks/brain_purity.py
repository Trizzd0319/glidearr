#!/usr/bin/env python3
"""
Brain-layer purity guard for Glidearr (ML migration invariant).

``machine_learning/`` is the pure decision/brain layer: it THINKS, the service
managers FETCH / CACHE / APPLY. To keep that boundary from eroding as new decision
cores land, every module under the migrated brain subpackages must NOT import:

  * an HTTP client            (requests / httpx / urllib3 / aiohttp)
  * the service layer         (scripts.managers.services.*)
  * any ``*_api`` module       (radarr_api / sonarr_api / trakt_api / tautulli_api)

Pure data in, pure data out — the service adapter does the I/O and passes scalars.

This is AST-based (not grep) so the many docstring mentions of "NO global_cache /
NO HTTP" don't false-positive. Two legacy FLAT top-level modules
(profile_selector.py, watchhistoryaggregator.py) predate the migration and are
Step-9 cleanup targets — they live at the package root, not in a subpackage, so the
subpackage scope below skips them until they're migrated.

Run:  python scripts/hooks/brain_purity.py     (exit 1 on any violation)
"""
import ast
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BRAIN = os.path.join(_REPO, "scripts", "managers", "machine_learning")

# The migrated brain — every decision core lives in one of these subpackages.
_GUARDED_SUBPACKAGES = (
    "contracts", "affinity", "features", "scoring", "likelihood", "sizing",
    "classification", "space", "lifecycle", "acquisition", "ledger",
    "routing", "quality_analytics", "eval", "next_watch", "playlists",
    "people_matrix",
)

_FORBIDDEN_TOP = {"requests", "httpx", "urllib3", "aiohttp"}


def _violations(path: str) -> list[str]:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception as e:  # a syntax error is its own (separate) problem
        return [f"{path}: could not parse ({e})"]
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (level > 0) — they stay inside the brain.
            mods = [node.module or ""] if node.level == 0 else []
        else:
            continue
        for m in mods:
            top = m.split(".")[0]
            if top in _FORBIDDEN_TOP:
                out.append(f"{path}:{node.lineno}: imports HTTP client '{m}'")
            elif m.startswith("scripts.managers.services"):
                out.append(f"{path}:{node.lineno}: imports the service layer '{m}'")
            elif m.split(".")[-1].endswith("_api"):
                out.append(f"{path}:{node.lineno}: imports an *_api module '{m}'")
    return out


def main() -> int:
    all_viol: list[str] = []
    for sub in _GUARDED_SUBPACKAGES:
        base = os.path.join(_BRAIN, sub)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py") or fn.startswith("test_"):
                    continue
                all_viol.extend(_violations(os.path.join(root, fn)))
    if all_viol:
        sys.stderr.write(
            "BRAIN PURITY VIOLATION — machine_learning/ must not import HTTP / the "
            "service layer / *_api:\n"
        )
        for v in all_viol:
            sys.stderr.write("  " + os.path.relpath(v.split(':', 1)[0], _REPO) +
                             ":" + v.split(':', 1)[1] + "\n")
        return 1
    print("brain purity OK — guarded subpackages import no HTTP / service / *_api.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

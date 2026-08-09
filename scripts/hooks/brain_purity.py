#!/usr/bin/env python3
"""
Brain-layer purity guard for Glidearr (ML migration invariant).

``machine_learning/`` is the pure decision/brain layer: it THINKS, the service
managers FETCH / CACHE / APPLY. To keep that boundary from eroding as new decision
cores land, every module under the brain must NOT import:

  * an HTTP client            (requests / httpx / urllib3 / aiohttp)
  * the service layer         (scripts.managers.services.*)
  * any ``*_api`` module       (radarr_api / sonarr_api / trakt_api / tautulli_api)
  * a MIGRATION SHIM under ``scripts.support.utilities.*`` (see below)

Pure data in, pure data out — the service adapter does the I/O and passes scalars.

WHY THE SHIM RULE EXISTS. ``machine_learning/space/dual_version.py`` imported
``scripts.support.utilities.size_model`` — the Step-5 re-export shim for
``machine_learning.sizing.size_model``. That is a LAYERING INVERSION: the brain
reaching *outward* through a services-era compatibility shim to reach its own
neighbour two directories away. Neither of the rules above caught it, because
``support.utilities`` is neither a service nor a brain package — a shim fits through
the gap between the two by construction. It would also have broken the module for no
reason at all when the shims are deleted at MIGRATION Step 10.

WHY THE SCOPE IS NOW EVERY SUBPACKAGE. This used to walk a hand-maintained
``_GUARDED_SUBPACKAGES`` allowlist, which meant a brain package was unguarded UNTIL
SOMEONE REMEMBERED TO ADD IT — and ``thresholds/`` (the whole calibration + axis-drift
machinery) never was. Absent from a list is not the same as not a brain package. The
default is now inverted: every subpackage is guarded unless explicitly excluded, so a
new decision core is covered the moment it exists rather than the moment someone
notices.

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

# EVERY subpackage of machine_learning/ is guarded — see the module docstring for why
# the old hand-maintained allowlist was replaced. Only these are skipped, and each needs
# a reason:
#   __pycache__  — not source
#   tests        — a test may legitimately import a service to build a fixture
_EXCLUDED_SUBPACKAGES = {"__pycache__", "tests"}

# Migration re-export shims under scripts/support/utilities/. Each is a thin re-export of
# a module that now lives IN the brain, kept only so pre-migration service callers keep
# working until MIGRATION Step 10 deletes them. A brain module importing one is reaching
# outward to reach itself; it should import the real module directly.
_FORBIDDEN_SUPPORT_SHIMS = {
    "scripts.support.utilities.space_targets",       # -> machine_learning.space.space_targets
    "scripts.support.utilities.size_model",           # -> machine_learning.sizing.size_model
    "scripts.support.utilities.watch_likelihood",     # -> machine_learning.likelihood.watch_likelihood
    "scripts.support.utilities.library_classifier",   # -> machine_learning.classification.library_classifier
}

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
            elif m in _FORBIDDEN_SUPPORT_SHIMS:
                out.append(
                    f"{path}:{node.lineno}: imports the MIGRATION SHIM '{m}' — a brain module "
                    f"reaching outward to reach itself. Import the real brain module directly "
                    f"(the shim is deleted at MIGRATION Step 10)")
            elif m.split(".")[-1].endswith("_api"):
                out.append(f"{path}:{node.lineno}: imports an *_api module '{m}'")
    return out


def main() -> int:
    all_viol: list[str] = []
    guarded: list[str] = []
    for sub in sorted(os.listdir(_BRAIN) if os.path.isdir(_BRAIN) else []):
        base = os.path.join(_BRAIN, sub)
        if not os.path.isdir(base) or sub in _EXCLUDED_SUBPACKAGES:
            continue
        guarded.append(sub)
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
            "service layer / *_api / a support migration shim:\n"
        )
        for v in all_viol:
            sys.stderr.write("  " + os.path.relpath(v.split(':', 1)[0], _REPO) +
                             ":" + v.split(':', 1)[1] + "\n")
        return 1
    print(f"brain purity OK — {len(guarded)} subpackage(s) checked, none import HTTP / "
          f"service / *_api / a migration shim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

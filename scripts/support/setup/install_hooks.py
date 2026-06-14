"""
install_hooks.py - activate the committed git hooks (pre-commit secret guard).

    python scripts/support/setup/install_hooks.py

Points git at the repo's committed hooks (scripts/hooks) via core.hooksPath, so
the pre-commit secret scanner runs on every commit in this clone. Run once per
clone. (Optional but recommended: install `gitleaks` for the full ruleset - the
hook uses it automatically when present.)
"""
import os
import subprocess
import sys
from pathlib import Path

# Console output: UTF-8 safe (never crash on a cp1252 console), emoji-free,
# colour-coded - red=stopped/error, green=good. Honours NO_COLOR and non-TTY.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
def _red(s):   return f"\033[31m{s}\033[0m" if _COLOR else s
def _green(s): return f"\033[32m{s}\033[0m" if _COLOR else s

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/*/<name>.py)
HOOKS_DIR = "scripts/hooks"


def main() -> int:
    try:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "config", "core.hooksPath", HOOKS_DIR],
            check=True,
        )
    except Exception as e:
        print(_red(f"ERROR: could not set core.hooksPath: {e}"))
        return 1
    print(_green(f"git hooks activated - core.hooksPath = {HOOKS_DIR}"))
    print("     Pre-commit secret scanning is now ON for this clone.")
    print("     (Install gitleaks for the full ruleset; the hook auto-detects it.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

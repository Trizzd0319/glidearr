"""
setup_secrets.py - interactive SecretStore setup / update.

    python scripts/support/setup/setup_secrets.py

Shows where each secret currently resolves from (env / keyring / plaintext /
missing), then lets you set or update each one with hidden input. Values are
stored in the OS keyring; config.json secret fields are left blank. Press Enter
to keep the current value. Use this for first-time setup, after rotating
credentials, or to add an optional secret you skipped earlier.
"""
import getpass
import os
import sys
from collections import Counter
from pathlib import Path

# Console output: UTF-8 safe (never crash on a cp1252 console), emoji-free,
# colour-coded - red=stopped/error, yellow=caution, green=good. Honours NO_COLOR
# and non-TTY (falls back to plain text).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
def _red(s):    return f"\033[31m{s}\033[0m" if _COLOR else s
def _yellow(s): return f"\033[33m{s}\033[0m" if _COLOR else s
def _green(s):  return f"\033[32m{s}\033[0m" if _COLOR else s

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/*/<name>.py)
sys.path.insert(0, str(REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader            # noqa: E402
from scripts.managers.factories.config.secret_store import env_name                  # noqa: E402
from scripts.managers.factories.config.secret_bootstrap import (                     # noqa: E402
    SecretBootstrap, SENTINEL_PATH,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.json"  # scripts/support/config/


def main() -> int:
    if not CONFIG.exists():
        print(_red(f"ERROR: config not found: {CONFIG}"))
        return 1

    loader = ConfigLoader(CONFIG)
    cfg = loader.load()
    store = loader._secret_store
    boot = SecretBootstrap(loader)

    audit = boot.audit(cfg)
    print(f"\nSecretStore backend: {store.backend_name()}")
    print("Current: " + " | ".join(f"{k}={v}" for k, v in Counter(audit.values()).items()))
    print("Enter a value to set/update, or press Enter to keep the current one.\n")

    tags = {"env": "[env]", "keyring": "[keyring]", "inline": "[PLAINTEXT]", "missing": "[MISSING]"}
    changed = 0
    for path, src in audit.items():
        try:
            val = getpass.getpass(f"  {path} {tags.get(src, '')}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ninterrupted.")
            break
        if val:
            if store.set(path, val):
                changed += 1
                print(_green("     stored in keyring"))
            else:
                print(_yellow(f"     WARNING: no keyring backend - set env var {env_name(path)} instead"))

    # Pull stored values into the live config, mark provisioned, and rewrite
    # config.json with secrets blanked (everything now lives in the keyring).
    try:
        loader._overlay_secrets(cfg)
        store.set(SENTINEL_PATH, "1")
        loader.save(cfg)
    except Exception as e:
        print(_yellow(f"WARNING: finalize step failed: {e}"))

    print(_green(f"\nUpdated {changed} secret(s). config.json secret fields are blank; "
                 f"values live in the OS keyring."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

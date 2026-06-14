"""
migrate_secrets.py - move plaintext secrets out of config.json into the OS keyring.

Run once, e.g. right after rotating credentials:

    python scripts/support/setup/migrate_secrets.py

It reads scripts/support/config/config.json, stores every secret value in the OS
keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service), and
rewrites config.json with those fields BLANK. Downstream code is unchanged - the
SecretStore overlay fills the values back in at load time.

On a host with no keyring backend (e.g. a headless server / container), set the
secrets as env vars instead (RECOMMENDARR_TRAKT_CLIENT_SECRET=..., etc.). This
script leaves any secret it cannot safely persist inline and prints the exact env
var name to set.
"""
import json
import os
import sys
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

from scripts.managers.factories.config.config_loader import ConfigLoader          # noqa: E402
from scripts.managers.factories.config.secret_store import (                       # noqa: E402
    SecretStore, iter_secret_paths, env_name,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.json"  # scripts/support/config/


def _inline_secrets(path: Path):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(p, v) for p, v in iter_secret_paths(data) if isinstance(v, str) and v]


def main() -> int:
    store = SecretStore()
    print(f"SecretStore backend: {store.backend_name()}")
    if not CONFIG.exists():
        print(_red(f"ERROR: config not found: {CONFIG}"))
        return 1

    before = _inline_secrets(CONFIG)
    if not before:
        print(_green("No plaintext secrets in config.json - already clean."))
        return 0

    print(f"Found {len(before)} plaintext secret(s) in config.json. Migrating...")
    loader = ConfigLoader(CONFIG)
    cfg = loader.load()      # overlay (no-op while store empty) + register for log scrubbing
    loader.save(cfg)         # persists secrets to the store + blanks them on disk (no-loss)
    try:                     # mark the SecretStore as provisioned
        from scripts.managers.factories.config.secret_bootstrap import SENTINEL_PATH
        store.set(SENTINEL_PATH, "1")
    except Exception:
        pass

    remaining = _inline_secrets(CONFIG) or []
    migrated = len(before) - len(remaining)
    print(_green(f"\nMigrated {migrated} secret(s) to the keyring; config.json fields blanked."))
    if remaining:
        print(_yellow(f"WARNING: {len(remaining)} secret(s) could NOT be persisted (no keyring backend)."))
        print("   Provide them via these environment variables instead:")
        for p, _v in remaining:
            print(f"     {env_name(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

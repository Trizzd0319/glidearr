import copy
import json
import os
import tempfile
from pathlib import Path

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.managers.factories.config.secret_store import (
    SecretStore, is_secret_key, env_name, iter_secret_paths,
)


class ConfigLoader:
    """
    Loads/saves config.json and keeps secrets OUT of the plaintext file.

    On load: secret leaves are overlaid from the SecretStore (env var → OS keyring)
    so the in-memory config carries real values while the file can stay blank.
    On save: secrets are persisted to the SecretStore and written to disk BLANK
    (kept inline only if there is nowhere safe to persist them, so nothing is lost).
    """

    def __init__(self, config_path: Path, logger=None):
        self.logger = logger or LoggerManager()
        self.config_path = config_path
        self.raw = {}
        self._secret_store = SecretStore(logger=self.logger)

    # ── Secret overlay (load) ────────────────────────────────────────────────
    def _overlay_secrets(self, cfg):
        store = self._secret_store
        plaintext = []

        def walk(obj, prefix=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    p = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        walk(v, p)
                    elif is_secret_key(k):
                        resolved = store.get(p)
                        if resolved:
                            obj[k] = resolved            # env/keyring value wins
                        elif isinstance(v, str) and v:
                            plaintext.append(p)          # legacy inline secret
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    if isinstance(v, (dict, list)):
                        walk(v, f"{prefix}.{i}")

        walk(cfg)
        if plaintext:
            shown = ", ".join(plaintext[:4]) + ("…" if len(plaintext) > 4 else "")
            self.logger.log_warning(
                f"⚠️ {len(plaintext)} secret(s) still stored in PLAINTEXT config.json "
                f"({shown}) — run `python scripts/support/setup/migrate_secrets.py` to move them into "
                f"the OS keyring (or supply them via RECOMMENDARR_* env vars)."
            )

    @staticmethod
    def _collect_secret_values(cfg):
        return [v for _p, v in iter_secret_paths(cfg) if isinstance(v, str) and v]

    def load(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.raw = json.load(f)
            try:
                self._overlay_secrets(self.raw)
                # Register resolved secret values so they are scrubbed from all logs.
                LoggerManager.register_secrets(self._collect_secret_values(self.raw))
            except Exception:
                pass
            self.logger.log_info("✅ Configuration loaded.")
        except FileNotFoundError:
            self.logger.log_error(f"❌ Config file not found: {self.config_path}")
        except json.JSONDecodeError:
            self.logger.log_error(f"❌ Failed to parse config file: {self.config_path}")
        return self.raw or {}

    # ── Secret strip (save) ──────────────────────────────────────────────────
    def _strip_secrets_to_store(self, cfg):
        """Deep copy of cfg with secrets persisted to the store and blanked on disk.
        A secret that can't be safely persisted (no keyring backend AND no env var)
        is left inline so it is never lost."""
        on_disk = copy.deepcopy(cfg)
        store = self._secret_store

        def walk(src, dst, prefix=""):
            if isinstance(src, dict):
                for k, v in src.items():
                    p = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        walk(v, dst.get(k), p)
                    elif is_secret_key(k) and isinstance(v, str) and v:
                        stored = store.set(p, v)
                        env_present = os.environ.get(env_name(p)) is not None
                        if stored or env_present:
                            dst[k] = ""                  # blank on disk
            elif isinstance(src, list):
                for i, v in enumerate(src):
                    if isinstance(v, (dict, list)) and isinstance(dst, list) and i < len(dst):
                        walk(v, dst[i], f"{prefix}.{i}")

        walk(cfg, on_disk)
        return on_disk

    def save(self, config: dict):
        try:
            path = self.config_path
            try:
                on_disk = self._strip_secrets_to_store(config)
            except Exception as e:
                self.logger.log_warning(f"[SecretStore] secret-strip failed, writing config as-is: {e}")
                on_disk = config
            # Atomic, owner-only (0600) write so any residual secrets aren't left
            # world-readable for co-tenants on a multi-user host.
            fd, tmp = tempfile.mkstemp(dir=str(Path(path).parent), prefix=".config_", suffix=".tmp")
            try:
                try:
                    os.fchmod(fd, 0o600)  # no-op/AttributeError on Windows
                except (AttributeError, OSError):
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(on_disk, f, indent=4)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            try:
                os.chmod(path, 0o600)  # defensive; no-op on Windows
            except OSError:
                pass
            self.logger.log_info("💾 Config saved.")
        except Exception as e:
            self.logger.log_error(f"❌ Failed to save config: {e}")

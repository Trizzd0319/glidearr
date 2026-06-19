"""
services/backup — pre-destructive service backups (native *arr Backup), validated loadable.
================================================================================
Before a REAL run (``dry_run=false``) makes any destructive change, this triggers each
Radarr/Sonarr instance's NATIVE ``Backup`` command (the *arr-blessed, restorable DB+config
zip), waits for it to finish, downloads the freshest one, and VALIDATES it is loadable —
a valid zip whose CRCs check out and which contains the service DB + ``config.xml``.

The result arms or DISARMS the run-scoped backup gate (``system/backup_gate`` in the shared
cache). On failure the run DEGRADES TO DRY-RUN: destructive primitives read the gate via
``support/utilities/backup_gate`` and make no real change (they log "would …" instead), so
nothing is ever deleted/re-grabbed without a validated rollback point sitting on disk.

Talks to the *arr REST API DIRECTLY (config ``base_url`` + ``api`` key) rather than through
the validated api stack, so it is dependency-light and can be exercised standalone. Triggered
once at the top of ``Main.run`` (real runs only) — see ``Main`` wiring.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timezone

import requests

GATE_KEY = "system/backup_gate"

# A loadable *arr backup zip carries the service DB and its config.
_DB_SUFFIXES = (".db",)
_CONFIG_SUFFIX = "config.xml"


class ServiceBackupManager:
    POLL_INTERVAL_S = 3.0
    BACKUP_TIMEOUT_S = 300.0          # native backups of a big DB can take a minute+
    MIN_BACKUP_BYTES = 64 * 1024      # below this a "backup" is empty/garbage, not a real DB dump
    DEFAULT_MAX_AGE_HOURS = 24.0      # reuse a backup younger than this instead of making a new one
    _DONE = ("completed", "failed", "aborted", "cancelled")

    def __init__(self, logger, config, global_cache=None, *, dry_run: bool = False):
        self.logger = logger
        self.config = config or {}
        self.global_cache = global_cache
        self.dry_run = bool(dry_run)

    # ── public entry ─────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        """Backups run unless explicitly disabled (``backup_before_destructive: false``)."""
        return self._cfg("backup_before_destructive", True) is not False

    def ensure_backups(self) -> dict:
        """Create + validate a native backup of every configured Radarr/Sonarr instance and
        ARM/DISARM the destructive-write gate. No-op on a dry run (nothing destructive to guard)
        or when disabled — the gate stays ARMED so dry runs and opted-out users are unaffected.
        Returns ``{service:instance -> {ok, ...}}``."""
        if self.dry_run:
            self._set_gate(True, reason="dry_run")
            return {}
        if not self.enabled():
            self._set_gate(True, reason="disabled")
            return {}

        results: dict = {}
        for service in ("radarr", "sonarr"):
            for inst in self._instances(service):
                results[f"{service}:{inst}"] = self._backup_one(service, inst)

        all_ok = bool(results) and all(r.get("ok") for r in results.values())
        self._set_gate(all_ok, reason="ok" if all_ok else "backup_failed", results=results)
        if all_ok:
            names = ", ".join(
                f"{k} ({v.get('size_mb', 0):.0f} MB{', reused' if v.get('reused') else ''})"
                for k, v in results.items())
            self.logger.log_success(
                f"[Backup] validated loadable pre-destructive backups: {names}."
            )
        else:
            bad = ", ".join(k for k, v in results.items() if not v.get("ok")) or "(none created)"
            self.logger.log_warning(
                f"[Backup] backup FAILED or not loadable for: {bad}. DEGRADING this run to "
                f"dry-run — NO destructive changes will be made (every delete/re-grab logs "
                f"'would …' instead). Fix the backup target and re-run for live changes."
            )
        return results

    # ── per-instance backup + validation ─────────────────────────────────────────
    def _backup_one(self, service: str, inst: str) -> dict:
        base, key = self._conn(service, inst)
        if not base or not key:
            return {"ok": False, "detail": "no base_url / api key in config"}
        try:
            existing = self._list_backups(base, key) or []
            newest = self._pick_newest(existing)
            # FRESHNESS: reuse a recent, valid backup instead of dumping a new ~300 MB one every
            # run — a library barely changes between short scheduled runs (e.g. every 3h), so this
            # caps backups at one per backup_max_age_hours window. Picks the newest backup of ANY
            # kind (our manual OR the *arr's own scheduled backup), so churn drops further.
            if newest and self._fresh_enough(newest):
                res = self._finalize(service, inst, base, key, newest, reused=True)
                if res.get("ok"):
                    return res
                # present but failed validation → fall through and create a fresh one

            before = {b.get("path") for b in existing}
            cmd = self._api_post(base, key, "command", {"name": "Backup"})
            cid = (cmd or {}).get("id")
            if not self._wait_command(base, key, cid):
                return {"ok": False, "detail": "Backup command did not complete"}
            backups = self._list_backups(base, key) or []
            fresh = self._pick_newest(backups, exclude_paths=before) or self._pick_newest(backups)
            if not fresh:
                return {"ok": False, "detail": "no backup file appeared"}
            return self._finalize(service, inst, base, key, fresh, reused=False)
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def _finalize(self, service: str, inst: str, base: str, key: str, newest: dict, *, reused: bool) -> dict:
        """Validate a listed backup and log it — shared by the REUSE and the just-CREATED paths.

        CREATION/size check: the *arr only lists a backup once it has finished writing it, and its
        reported size is the file size — a non-trivial size is a strong, restorable rollback point.
        DEEP loadability (valid zip, CRCs pass, DB + config.xml present) is opt-in AND only when the
        static /backup file is actually fetchable; many installs gate that route behind UI session
        auth the API key can't satisfy (download = login page), so a non-zip download is NOT a
        failure — we fall back to the size check rather than disarm a validly-created backup."""
        api_size = int(newest.get("size") or 0)
        size_mb = api_size / 1e6
        created_ok = api_size >= self.MIN_BACKUP_BYTES
        deep = None
        if created_ok and self._cfg("backup_deep_validate", False):
            content = self._download(base, newest.get("path"), key)
            if self._looks_like_zip(content):
                deep = self.validate_backup_zip(content, service)
                size_mb = len(content) / 1e6
        loadable = bool(deep) if deep is not None else created_ok
        how = "zip-verified" if deep else ("size-verified" if loadable else "invalid")
        if reused:
            detail = (f"reusing {newest.get('name')} from {self._age_hours(newest):.1f}h ago "
                      f"({size_mb:.1f} MB, {how}) — within the {self._max_age_hours():.0f}h freshness window")
        else:
            detail = (f"{newest.get('name')} ({size_mb:.1f} MB) — "
                      f"{'loadable' if loadable else 'NOT loadable'} ({how})")
        (self.logger.log_info if loadable else self.logger.log_warning)(f"[Backup] {service}/{inst}: {detail}.")
        return {"ok": bool(loadable), "name": newest.get("name"), "size_mb": round(size_mb, 1),
                "path": newest.get("path"), "validated": how, "reused": reused}

    # ── freshness window ─────────────────────────────────────────────────────────
    def _max_age_hours(self) -> float:
        try:
            return float(self._cfg("backup_max_age_hours", self.DEFAULT_MAX_AGE_HOURS))
        except (TypeError, ValueError):
            return self.DEFAULT_MAX_AGE_HOURS

    def _age_hours(self, backup: dict) -> float:
        """Hours since a listed backup's ``time`` (ISO-8601, possibly Z-suffixed); +inf if unknown."""
        t = str((backup or {}).get("time") or "")
        if not t:
            return float("inf")
        try:
            bt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if bt.tzinfo is None:
                bt = bt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - bt).total_seconds() / 3600.0
        except Exception:
            return float("inf")

    def _fresh_enough(self, backup: dict) -> bool:
        """True if ``backup`` is younger than the configured window AND non-trivial in size.
        ``backup_max_age_hours <= 0`` disables reuse (back to a fresh backup every run)."""
        max_age = self._max_age_hours()
        if max_age <= 0:
            return False
        if int((backup or {}).get("size") or 0) < self.MIN_BACKUP_BYTES:
            return False
        return self._age_hours(backup) <= max_age

    @staticmethod
    def _looks_like_zip(content: "bytes | None") -> bool:
        """Cheap discriminator: a real zip starts with the PK local-file-header magic. A login
        HTML page (the auth-gated /backup route) does not, so we never CRC-check that."""
        return bool(content) and content[:2] == b"PK"

    @classmethod
    def validate_backup_zip(cls, content: "bytes | None", service: str = "") -> bool:
        """A backup is LOADABLE when the bytes are a valid zip whose CRCs all check out and which
        contains the service DB (``*.db``) plus ``config.xml``. Pure — safe to unit test."""
        if not content:
            return False
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            if zf.testzip() is not None:           # first member that fails its CRC → corrupt
                return False
            names = [n.lower() for n in zf.namelist()]
            has_db = any(n.endswith(s) for n in names for s in _DB_SUFFIXES)
            has_cfg = any(n.endswith(_CONFIG_SUFFIX) for n in names)
            return has_db and has_cfg
        except Exception:
            return False

    # ── gate ─────────────────────────────────────────────────────────────────────
    def _set_gate(self, armed: bool, *, reason: str, results: "dict | None" = None) -> None:
        if not self.global_cache:
            return
        try:
            self.global_cache.set(GATE_KEY, {"armed": bool(armed), "reason": reason,
                                             "results": results or {}})
        except Exception:
            pass

    # ── HTTP (direct REST; dependency-light) ─────────────────────────────────────
    def _wait_command(self, base: str, key: str, cid) -> bool:
        if not cid:
            return False
        start = time.time()
        while time.time() - start < self.BACKUP_TIMEOUT_S:
            cmd = self._api_get(base, key, f"command/{cid}", fallback={}) or {}
            status = cmd.get("status")
            if status in self._DONE:
                return status == "completed"
            time.sleep(self.POLL_INTERVAL_S)
        return False

    def _list_backups(self, base: str, key: str) -> list:
        return self._api_get(base, key, "system/backup", fallback=[]) or []

    @staticmethod
    def _pick_newest(backups: list, exclude_paths: "set | None" = None) -> "dict | None":
        cand = [b for b in (backups or []) if isinstance(b, dict)
                and (not exclude_paths or b.get("path") not in exclude_paths)]
        if not cand:
            return None
        # 'time' is an ISO string; lexicographic max is chronological for ISO-8601.
        return max(cand, key=lambda b: str(b.get("time") or ""))

    def _api_get(self, base: str, key: str, path: str, fallback=None):
        r = requests.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, timeout=30)
        return r.json() if (r.ok and r.content) else fallback

    def _api_post(self, base: str, key: str, path: str, payload):
        r = requests.post(f"{base}/api/v3/{path}", headers={"X-Api-Key": key},
                          json=payload, timeout=30)
        return r.json() if (r.ok and r.content) else None

    def _download(self, base: str, path: str, key: str) -> bytes:
        # The backup list returns a server-relative path like /backup/manual/<name>.zip,
        # served off the app root (NOT under /api/v3).
        r = requests.get(f"{base.rstrip('/')}{path}", headers={"X-Api-Key": key}, timeout=120)
        return r.content if r.ok else b""

    # ── config helpers ───────────────────────────────────────────────────────────
    def _cfg(self, key, default=None):
        try:
            return self.config.get(key, default) if hasattr(self.config, "get") else default
        except Exception:
            return default

    def _instances(self, service: str) -> list:
        raw = self._cfg(f"{service}_instances", {}) or {}
        out = []
        for name, c in (raw.items() if isinstance(raw, dict) else []):
            if name == "default_instance" or not isinstance(c, dict):
                continue
            out.append(name)
        return out

    def _conn(self, service: str, inst: str):
        c = ((self._cfg(f"{service}_instances", {}) or {}).get(inst, {}) or {})
        base = c.get("base_url")
        if not base:
            url, port = c.get("url"), c.get("port")
            if url:
                base = url if str(url).startswith("http") else f"http://{url}:{port or 8989}"
        key = c.get("api") or c.get("apikey") or c.get("api_key")
        return (base.rstrip("/") if base else None), key

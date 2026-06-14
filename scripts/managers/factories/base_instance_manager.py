"""
BaseInstanceManager
===================
Shared base for Radarr, Sonarr, and any future *arr instance managers.

Centralises:
  - URL parsing           (_parse_host)
  - Error redaction       (_redact_error)
  - Timeout adapter       (_inject_timeout)
  - REST request routing  (_make_request — subclasses set self._apis)
  - Interactive correction scaffold (_handle_interactive_correction)
  - Recovery confirmation (_confirm_and_clear_failed_flag)
  - prepare() stub        (no-op; instance managers have no sub-components)
  - load_summary / all_components_loaded bookkeeping helpers

Subclasses MUST implement:
  _api_class()        → the arrapi class (e.g. RadarrAPI)
  _config_key()       → config dict key (e.g. "radarr_instances")
  _apis_attr()        → name of the dict attr holding validated APIs (e.g. "radarr_apis")
  _service_name()     → display string for logs (e.g. "Radarr")
"""
from __future__ import annotations

import random
import re
import threading
import time
from urllib.parse import urlsplit
from typing import Any

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


# Process-wide per-(service, instance) write locks. *arr apps use SQLite (single
# writer); this guarantees we never have two of OUR writes in flight against the
# same DB at once — most importantly the JIT background worker vs the main
# pipeline. Keyed by (service_name, resolved_instance) so different instances /
# services (separate DBs) don't serialise against each other.
_WRITE_LOCKS: dict = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock_for(key) -> threading.Lock:
    with _WRITE_LOCKS_GUARD:
        lk = _WRITE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _WRITE_LOCKS[key] = lk
        return lk


# ── Run-scoped collection snapshot cache ───────────────────────────────────────
# The heavy full-library collection GETs — "movie" (Radarr) and "series" (Sonarr) —
# are independently re-fetched 13+ times per run by separate repair/monitoring scans
# (profiler: ~268s / 77% of a dry_run wall on a ~20k-item library). Their payload is
# identical within a run UNLESS something writes, so memoize them at this single
# chokepoint: a cacheable GET returns the stored snapshot (network skipped); ANY
# write (POST/PUT/DELETE) to (service, instance) drops both its collection snapshots
# AND bumps a generation counter, so the next GET re-fetches live and no in-flight
# fetch can persist a stale snapshot. In dry_run zero writes fire, so the first GET
# of each collection populates and every later identical GET is a hit for the whole
# run — exactly the redundancy the profiler flagged.
#
# INVARIANTS (do not break):
#   * Whitelist is EXACT-MATCH. Never add a volatile/action endpoint (command, queue,
#     system/status, rootfolder, diskspace, wanted/missing, health) or any
#     parameterized form (movie/{id}, movie/editor, series?page=..., movie?tmdbId=).
#   * A write to "movie"/"series" MUST go through *this* _make_request to invalidate.
#     (Raw arrapi client._make_request bypasses it; today only sonarr/quality/selector
#     writes "qualityprofile" that way — never the cached collections.)
#   * Inner dicts are SHARED across callers + global_cache(.full); callers of bare
#     "movie"/"series" must treat results as read-only (shallow-copy {**m} before edit).
_COLLECTION_CACHE: dict = {}            # (service, instance, endpoint) -> (monotonic_ts, list)
_COLLECTION_CACHE_GEN: dict = {}        # (service, instance) -> int  (bumped on every write)
_COLLECTION_CACHE_GUARD = threading.Lock()
_CACHEABLE_GET_ENDPOINTS = frozenset({"movie", "series"})
_COLLECTION_CACHE_TTL_S = 900.0         # safety backstop only; correctness = write-invalidation


def _clear_collection_cache() -> None:
    """Release all snapshots + reset generations (memory hygiene at teardown)."""
    with _COLLECTION_CACHE_GUARD:
        _COLLECTION_CACHE.clear()
        _COLLECTION_CACHE_GEN.clear()


class BaseInstanceManager(BaseManager, ComponentManagerMixin):

    # ── Subclass interface ────────────────────────────────────────────────────

    def _api_class(self):
        raise NotImplementedError

    def _config_key(self) -> str:
        raise NotImplementedError

    def _apis_attr(self) -> str:
        raise NotImplementedError

    def _service_name(self) -> str:
        return self.__class__.__name__

    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_host(config: dict) -> tuple[str, str, int]:
        """
        Return (protocol, host, port) from an instance config dict.

        Accepts both:
          {"base_url": "http://192.168.1.110:8988", ...}
          {"url": "192.168.1.110", "port": 8988, "ssl": False, ...}
        """
        raw = config.get("base_url") or config.get("url") or ""
        default_port = int(config.get("port", 443))

        if not raw.startswith(("http://", "https://")):
            protocol = "https" if config.get("ssl", True) else "http"
            raw = f"{protocol}://{raw}"
        else:
            protocol = raw.split("://", 1)[0]

        parsed = urlsplit(raw)
        host   = parsed.hostname or ""
        port   = parsed.port or default_port
        return protocol, host, int(port)

    @staticmethod
    def _redact_error(message: str) -> str:
        """Strip API keys, tokens, and secrets from exception strings."""
        message = re.sub(r'[Xx]-[Aa]pi-[Kk]ey[:\s]+\S+',   "X-Api-Key: [REDACTED]",  message)
        message = re.sub(r'\b[0-9a-fA-F]{32}\b',             "[REDACTED_KEY]",          message)
        message = re.sub(
            r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b',
            "[REDACTED_UUID]", message,
        )
        message = re.sub(r'Bearer\s+\S+', "Bearer [REDACTED]", message)
        return message

    @staticmethod
    def _inject_timeout(api: Any, timeout: int = 30) -> None:
        """
        Wrap the arrapi session's HTTP adapters so every request gets a
        default timeout without modifying arrapi itself.
        """
        try:
            from requests.adapters import HTTPAdapter

            class _T(HTTPAdapter):
                def send(self, *a, **kw):
                    kw.setdefault("timeout", timeout)
                    return super().send(*a, **kw)

            session = api._raw.session
            session.mount("http://",  _T())
            session.mount("https://", _T())
        except Exception:
            pass  # non-fatal

    def _get_apis(self) -> dict:
        return getattr(self, self._apis_attr(), {})

    def _set_api(self, name: str, api: Any) -> None:
        apis = getattr(self, self._apis_attr(), None)
        if apis is None:
            setattr(self, self._apis_attr(), {})
            apis = getattr(self, self._apis_attr())
        apis[name] = api

    # ── Instance lifecycle ────────────────────────────────────────────────────

    def _process_instance(self, name: str, config: dict) -> str:
        """
        Validate one configured instance.  Returns "success" | "recovered" | "fail".
        """
        if config.get("failed"):
            self.logger.log_warning(
                f"[{self._service_name()}] '{name}' marked failed — update config to retry."
            )
            self.load_summary[name] = "❌"
            return "fail"

        protocol, host, port = self._parse_host(config)
        base_url = f"{protocol}://{host}:{port}"

        try:
            api     = self._api_class()(base_url, config.get("api"))
            version = getattr(api.system_status(), "version", "unknown")
            self._inject_timeout(api)
            self._set_api(name, api)
            self.load_summary[name] = "✅"
            if hasattr(self, "updater"):
                self.updater.apply_corrections({name: "success"})
            self.logger.log_debug(
                f"[{self._service_name()}Instance] {name} v{version} @ {base_url}"
            )
            return "success"

        except Exception as e:
            return self._handle_interactive_correction(
                name, config, self._redact_error(str(e)), protocol, port
            )

    def _confirm_and_clear_failed_flag(self, name: str, config: dict) -> None:
        """Re-validate after interactive correction and clear the failed flag if ok."""
        protocol, host, port = self._parse_host(config)
        base_url = f"{protocol}://{host}:{port}"
        try:
            api = self._api_class()(base_url, config.get("api"))
            api.system_status()
            config.pop("failed", None)
            if hasattr(self, "updater"):
                self.updater.apply_corrections({name: "success"})
            self.logger.log_debug(
                f"[{self._service_name()}Instance] '{name}' confirmed operational."
            )
        except Exception as e:
            self.logger.log_error(
                f"[{self._service_name()}Instance] Final confirm failed for '{name}': "
                f"{self._redact_error(str(e))}"
            )
            config["failed"] = True
            if hasattr(self, "updater"):
                self.updater.apply_corrections({name: "fail"})

    def _handle_interactive_correction(
        self,
        name: str,
        config: dict,
        error_msg: str,
        protocol: str,
        port: int,
    ) -> str:
        """
        Prompt the user to fix invalid credentials or a wrong host, then retry.
        Returns "recovered" | "fail".
        """
        import getpass
        updated = False

        if "401" in error_msg or "Unauthorized" in error_msg:
            self.logger.log_warning(f"[{self._service_name()}] API key invalid for '{name}'.")
            config["api"] = getpass.getpass(f"🔑 Enter new API key for {name}: ").strip()
            updated = True

        elif "Failed to Connect" in error_msg or "Connection" in error_msg:
            self.logger.log_warning(f"[{self._service_name()}] Connection failed for '{name}'.")
            host     = input(f"🌐 Enter new host (IP/URL) for {name}: ").strip()
            raw_port = input(f"🔧 Enter new port for {name}: ").strip()
            if not raw_port.isdigit():
                self.logger.log_error(
                    f"[{self._service_name()}] Invalid port for '{name}': {raw_port!r}"
                )
                config["failed"] = True
                self.load_summary[name] = "❌"
                return "fail"
            config["base_url"] = f"{protocol}://{host}:{raw_port}"
            updated = True

        if updated:
            try:
                retry = self._api_class()(config["base_url"], config.get("api"))
                retry.system_status()
                self._inject_timeout(retry)
                self._set_api(name, retry)
                self.load_summary[name] = "✅"
                if hasattr(self, "updater"):
                    self.updater.apply_corrections({name: "recovered"})
                return "recovered"
            except Exception as retry_e:
                self.logger.log_error(
                    f"[{self._service_name()}] Retry failed for '{name}': "
                    f"{self._redact_error(str(retry_e))}"
                )

        config["failed"] = True
        if hasattr(self, "updater"):
            self.updater.apply_corrections({name: "fail"})
        self.load_summary[name] = "❌"
        return "fail"

    # ── Run-scoped collection snapshot memo ───────────────────────────────────

    @staticmethod
    def _collection_cache_lookup(service, instance, endpoint):
        """
        Return (hit, gen0). On a hit, ``hit`` is a FRESH list() copy of the stored
        snapshot (list-level mutation by callers is safe; inner dicts stay shared and
        must be treated read-only). On a miss, ``hit`` is None and ``gen0`` is the
        current write-generation for (service, instance) — pass it back to
        _collection_cache_store so a write landing mid-fetch rejects the store.
        """
        key = (service, instance, endpoint)
        with _COLLECTION_CACHE_GUARD:
            gen0  = _COLLECTION_CACHE_GEN.get((service, instance), 0)
            entry = _COLLECTION_CACHE.get(key)
            if entry is not None:
                ts, snap = entry
                if (time.monotonic() - ts) <= _COLLECTION_CACHE_TTL_S:
                    return list(snap), gen0
                _COLLECTION_CACHE.pop(key, None)
        return None, gen0

    @staticmethod
    def _collection_cache_store(service, instance, endpoint, snapshot, gen0):
        """
        Store ``snapshot`` (by reference) for (service, instance, endpoint) — but ONLY
        if no write invalidated this instance since ``gen0`` was captured. Closes the
        store-after-invalidate race: a concurrent write (e.g. the live JIT worker)
        bumps the generation, so a racing fetch's pre-write snapshot is dropped rather
        than persisted stale.
        """
        with _COLLECTION_CACHE_GUARD:
            if _COLLECTION_CACHE_GEN.get((service, instance), 0) != gen0:
                return
            _COLLECTION_CACHE[(service, instance, endpoint)] = (time.monotonic(), snapshot)

    @staticmethod
    def _collection_cache_invalidate(service, instance):
        """Drop both collection snapshots for (service, instance) and bump its
        write-generation so any in-flight fetch's store is rejected."""
        with _COLLECTION_CACHE_GUARD:
            _COLLECTION_CACHE_GEN[(service, instance)] = (
                _COLLECTION_CACHE_GEN.get((service, instance), 0) + 1
            )
            for k in [k for k in _COLLECTION_CACHE
                      if k[0] == service and k[1] == instance]:
                _COLLECTION_CACHE.pop(k, None)

    # ── _make_request ─────────────────────────────────────────────────────────

    def _make_request(
        self,
        instance: str,
        endpoint: str,
        method: str = "GET",
        payload: Any = None,
        fallback: Any = None,
        retries: int = 1,
        **kwargs,
    ) -> Any:
        """
        Route a raw HTTP request to the given instance's REST API.
        Subclasses set self.<_apis_attr()> during validation.
        """
        apis     = self._get_apis()
        resolved = self._resolve_instance_name(instance)
        api      = apis.get(resolved)

        if not api:
            self.logger.log_warning(
                f"[{self._service_name()}] No validated API for '{instance}' "
                f"(resolved: '{resolved}') — returning fallback."
            )
            return fallback

        raw          = api._raw
        method_upper = (method or "GET").upper()
        last_exc     = None

        if method_upper not in ("GET", "POST", "PUT", "DELETE"):
            self.logger.log_warning(
                f"[{self._service_name()}] Unknown HTTP method '{method}' for /{endpoint}"
            )
            return fallback

        def _do_call():
            if method_upper == "GET":
                return raw._get(endpoint)
            if method_upper == "POST":
                return raw._post(endpoint, json=payload)
            if method_upper == "PUT":
                return raw._put(endpoint, json=payload)
            raw._delete(endpoint)  # DELETE
            return None

        is_write     = method_upper in ("POST", "PUT", "DELETE")
        write_lock   = _write_lock_for((self._service_name(), resolved)) if is_write else None
        busy_retries = 0

        # ── Run-scoped snapshot memo for heavy full-library collection GETs ──────
        # ("movie"/"series" only). A hit skips the network entirely; any write to this
        # (service, instance) invalidates both its snapshots. Every cache touch is
        # best-effort — a memo failure must never break the request.
        service          = self._service_name()
        is_cacheable_get = (method_upper == "GET" and endpoint in _CACHEABLE_GET_ENDPOINTS)
        cache_gen0       = 0
        if is_cacheable_get:
            try:
                hit, cache_gen0 = self._collection_cache_lookup(service, resolved, endpoint)
                if hit is not None:
                    return hit
            except Exception:
                is_cacheable_get = False
        if is_write:
            try:
                self._collection_cache_invalidate(service, resolved)
            except Exception:
                pass

        for _ in range(max(1, retries)):
            while True:
                try:
                    # Hold the per-instance write lock only for the call itself —
                    # never during the backoff sleep — so writers serialise but
                    # a long backoff doesn't block every other write.
                    if write_lock is not None:
                        write_lock.acquire()
                    try:
                        result = _do_call()
                    finally:
                        if write_lock is not None:
                            write_lock.release()

                    if method_upper == "DELETE":
                        return None
                    # Memoize a successful full-library snapshot (never the fallback)
                    # and hand the caller a list() copy so the cached list is never
                    # aliased (its inner dicts remain shared / read-only).
                    if is_cacheable_get and result is not None and isinstance(result, list):
                        try:
                            self._collection_cache_store(service, resolved, endpoint, result, cache_gen0)
                        except Exception:
                            pass
                        return list(result)
                    return result if result is not None else fallback
                except Exception as e:
                    last_exc = e
                    # SQLite "database is locked": the write never committed, so a
                    # backed-off retry is safe even for POST /command. Bounded.
                    if self._is_db_locked(e) and busy_retries < self._SQLITE_BUSY_MAX_RETRIES:
                        busy_retries += 1
                        delay = self._sqlite_backoff_delay(busy_retries)
                        self.logger.log_debug(
                            f"[{self._service_name()}] {method_upper} /{endpoint} on "
                            f"'{resolved}' — DB locked, retry {busy_retries}/"
                            f"{self._SQLITE_BUSY_MAX_RETRIES} in {delay:.1f}s"
                        )
                        time.sleep(delay)
                        continue  # same generic attempt, retry the call
                    break  # non-busy error, or DB-locked budget exhausted

        self.logger.log_warning(
            f"[{self._service_name()}] {method_upper} /{endpoint} on '{resolved}' "
            f"failed after {max(1, retries)} attempt(s)"
            + (f" + {busy_retries} DB-locked retr{'y' if busy_retries == 1 else 'ies'}"
               if busy_retries else "")
            + f": {self._redact_error(str(last_exc))}"
        )
        return fallback

    # ── SQLite-busy ("database is locked") retry policy ───────────────────────
    # *arr apps use SQLite (single writer). Under contention — usually the app
    # processing our EpisodeSearch commands (search→grab→import) — writes fail
    # fast with a 500 "database is locked". The write never committed, so a
    # backed-off retry is safe. ~8 tries with exponential backoff ≈ 30s ceiling.
    _SQLITE_BUSY_MAX_RETRIES = 8
    _SQLITE_BUSY_BASE_DELAY  = 0.25   # seconds
    _SQLITE_BUSY_MAX_DELAY   = 8.0    # per-attempt cap

    @staticmethod
    def _is_db_locked(exc) -> bool:
        msg = str(exc).lower()
        return (
            "database is locked" in msg
            or "sqlite_busy" in msg
            or "code = busy" in msg
        )

    def _sqlite_backoff_delay(self, attempt: int) -> float:
        # 1-based attempt → exponential with ±20% jitter, capped per attempt.
        base = min(self._SQLITE_BUSY_MAX_DELAY,
                   self._SQLITE_BUSY_BASE_DELAY * (2 ** (attempt - 1)))
        return base * (0.8 + random.random() * 0.4)

    # ── Free-space helpers (mount-deduped) ─────────────────────────────────────
    # Root folders that share a physical disk each report that disk's FULL free
    # space, so summing freeSpace per root folder double/triple-counts. These
    # helpers deduplicate by the underlying /diskspace mount (one entry per disk).

    @staticmethod
    def _norm_path(p: str) -> str:
        """Lower-case, forward-slash, strip trailing slash. Bare root → '/'."""
        if not p:
            return ""
        q = str(p).replace("\\", "/").rstrip("/").lower()
        return q or "/"  # keep a bare-root mount as "/" not ""

    @staticmethod
    def _path_under_mount(rp: str, mp: str) -> bool:
        """True if normalized root path rp equals or lives under mount mp, using a
        separator boundary so '/data' does NOT match '/database'."""
        if not mp:
            return False
        if mp == "/":                       # bare root mount: prefix of everything
            return rp.startswith("/")
        return rp == mp or rp.startswith(mp + "/")

    def disk_free_bytes(self, instance: str) -> float:
        """
        Free bytes available to this instance's root folders, deduped by physical
        mount (root folders sharing a disk are counted once). Returns
        float('inf') on error or when there are no root folders (legacy
        'assume sufficient' contract).
        """
        try:
            roots = self._make_request(instance, "rootfolder", fallback=[]) or []
            root_paths = [self._norm_path(r.get("path", "")) for r in roots
                          if isinstance(r, dict) and r.get("path")]
            if not root_paths:
                return float("inf")

            disks  = self._make_request(instance, "diskspace", fallback=[]) or []
            mounts = [d for d in disks if isinstance(d, dict) and d.get("path")]
            if mounts:
                chosen: dict[str, float] = {}     # norm mount path → freeSpace
                for rp in root_paths:
                    best_path, best_len, best_free = None, -1, 0
                    for d in mounts:
                        mp = self._norm_path(d.get("path", ""))
                        if self._path_under_mount(rp, mp) and len(mp) > best_len:
                            best_path, best_len = mp, len(mp)
                            best_free = d.get("freeSpace", 0) or 0
                    if best_path is not None:
                        chosen[best_path] = best_free
                    else:
                        # Root matched no mount — don't silently drop its disk;
                        # recover its own reported freeSpace (keyed by path).
                        own = next((r.get("freeSpace", 0) or 0 for r in roots
                                    if isinstance(r, dict)
                                    and self._norm_path(r.get("path", "")) == rp), 0)
                        chosen.setdefault(rp, own)
                if chosen:
                    return float(sum(chosen.values()))

            # Fallback (no usable /diskspace): dedup rootfolder freeSpace by VALUE
            # (same disk → identical free bytes). Can under-count if two distinct
            # disks coincidentally match — only used when /diskspace is missing.
            seen, total = set(), 0
            for r in roots:
                if not isinstance(r, dict):
                    continue
                fv = r.get("freeSpace", 0) or 0
                if fv not in seen:
                    seen.add(fv)
                    total += fv
            return float(total)
        except Exception as e:
            self.logger.log_warning(
                f"[{self._service_name()}] Could not read free space for "
                f"'{instance}': {e} — assuming sufficient"
            )
            return float("inf")

    def disk_free_gb(self, instance: str) -> float:
        """Mount-deduped free space in GiB (binary, 1024**3)."""
        free = self.disk_free_bytes(instance)
        return free if free == float("inf") else free / (1024 ** 3)

    def disk_total_bytes(self, instance: str) -> float:
        """Total bytes across the mounts hosting this instance's root folders,
        deduped by mount. Returns float('inf') on error / no root folders."""
        try:
            roots = self._make_request(instance, "rootfolder", fallback=[]) or []
            root_paths = [self._norm_path(r.get("path", "")) for r in roots
                          if isinstance(r, dict) and r.get("path")]
            if not root_paths:
                return float("inf")
            disks  = self._make_request(instance, "diskspace", fallback=[]) or []
            mounts = [d for d in disks if isinstance(d, dict) and d.get("path")]
            if mounts:
                chosen: dict[str, float] = {}
                for rp in root_paths:
                    best_path, best_len, best_total = None, -1, 0
                    for d in mounts:
                        mp = self._norm_path(d.get("path", ""))
                        if self._path_under_mount(rp, mp) and len(mp) > best_len:
                            best_path, best_len = mp, len(mp)
                            best_total = d.get("totalSpace", 0) or 0
                    if best_path is not None:
                        chosen[best_path] = best_total
                if chosen:
                    return float(sum(chosen.values()))
            seen, total = set(), 0
            for r in roots:
                if not isinstance(r, dict):
                    continue
                tv = r.get("totalSpace", 0) or 0
                if tv not in seen:
                    seen.add(tv)
                    total += tv
            return float(total)
        except Exception as e:
            self.logger.log_warning(
                f"[{self._service_name()}] Could not read total space for '{instance}': {e}"
            )
            return float("inf")

    def disk_total_gb(self, instance: str) -> float:
        """Mount-deduped total space in GiB (binary, 1024**3). inf on error."""
        total = self.disk_total_bytes(instance)
        return total if total == float("inf") else total / (1024 ** 3)

    def _resolve_instance_name(self, name: str | None) -> str:
        """
        Return a validated instance name.  Subclasses override resolve_instance()
        to add service-specific logic; this method is the internal fallback.
        """
        if hasattr(self, "resolve_instance"):
            return self.resolve_instance(name)
        apis = self._get_apis()
        if isinstance(name, str) and name in apis:
            return name
        return next(iter(apis), name or "default")

    # ── Finalization ──────────────────────────────────────────────────────────

    def _finalize(self, service_name: str, flag_key: str) -> None:
        """
        Called at the end of __init__ / run() to set registry flags and
        emit the standardized summary log line.
        """
        apis      = self._get_apis()
        all_ok    = bool(apis) and all(
            str(self.load_summary.get(n, "")).startswith("✅")
            for n in apis
        )
        self.all_components_loaded = all_ok
        self.registry.set_flag(flag_key, all_ok)
        # Emit one compact summary line (same format as component managers)
        parts  = "  ".join(
            f"{n}{str(self.load_summary.get(n, '')).startswith('✅') and '✅' or '❌'}"
            for n in apis
        )
        n_ok   = sum(1 for n in apis if str(self.load_summary.get(n, "")).startswith("✅"))
        status = "✅" if all_ok else "⚠️"
        self.logger.log_debug(
            f"[{self.__class__.__name__}] {status} {n_ok}/{len(apis)}: {parts}"
        )

    # ── prepare (no-op) ───────────────────────────────────────────────────────

    def prepare(self) -> None:
        """Instance managers have no sub-components to prepare."""
        pass

"""
enrich_daemon.py
================
Standalone background enrichment daemon.

Continuously fetches Trakt metadata for the movies (Radarr) and series (Sonarr)
in your libraries so the watchability scorer / relational builder has complete
data BEFORE a run needs it - and so the main run never has to make a live Trakt
call (it reads only what this daemon has already cached).

Usage
-----
    python scripts/support/daemons/enrich_daemon.py             # run forever (one cycle every ~5.1 min)
    python scripts/support/daemons/enrich_daemon.py --once      # single pass then exit
    python scripts/support/daemons/enrich_daemon.py --dry-run   # log what would be fetched, no writes
    python scripts/support/daemons/enrich_daemon.py --verbose   # debug logging
    python scripts/support/daemons/enrich_daemon.py --show-items   # log each title + buckets grabbed
    python scripts/support/daemons/enrich_daemon.py --status    # one-shot progress report, then exit (no fetching)

Per-item logging (which title got which data buckets) is ON automatically when
run attached to a terminal (interactive / docker -it), and OFF when the supervisor
runs it detached (so the logfile isn't flooded). Force it with --show-items or
suppress it with --quiet-items.

Normally you do NOT launch this by hand - main.py (re)spawns it via
EnrichDaemonSupervisor when ``daemons.enrich.enabled`` is set.

How it works
------------
Each cycle:
  1. Loads config through ConfigLoader so secrets (Trakt + *arr keys) are overlaid
     from the OS keyring / env - the live config.json is blank for secrets.
  2. Fetches the Radarr movie list and Sonarr series list.
  3. Enriches OWNED (in-library) items FIRST, then unowned ones (configurable).
  4. For each movie it fetches EVERY endpoint in the configured ``scope`` into its
     own per-type cache bucket; shows get summary (genres) + people + ratings + related.
  5. Spends at most SAFE_THROUGHPUT_CALLS endpoint-calls per cycle (each movie now
     costs len(scope) calls), then sleeps one rate window so it stays well under
     Trakt's 1000-calls / 5-min limit.
  6. A cursor file persists progress so restarts resume where they left off; a stop
     sentinel lets the supervisor shut it down cleanly within ~1.5s.

Each cycle also runs a few periodic, budget-paced SIDE tasks: Common Sense Media age
lookups (MDBList), and — at most once every ``franchise_catalog_ttl_days`` (default 14) —
a DETACHED regen of the TV-franchise catalog (Wikidata + Wikipedia), spawned as a background
subprocess so its slow, heavily rate-limited fetch never blocks a cycle.

Cache buckets (file = {tmdb_id}.json.gz, 7-day TTL) and all paths/constants are
single-sourced in managers/factories/daemons/daemon_paths.py.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# UTF-8 safe console (never crash / mojibake on cp1252), mirrors onboarding.py.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Make ``scripts.*`` importable when launched detached (mirror onboarding.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root (file: scripts/support/daemons/enrich_daemon.py)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import (                 # noqa: E402
    CACHE_TRAKT, CACHE_TTL_S, CONFIG_PATH, CURSOR_PATH, DEFAULT_SCOPE,
    MAIN_ACTIVE_MAX_AGE_S, MAIN_ACTIVE_POLL_S, MAIN_ACTIVE_SENTINEL, MOVIE_BUCKETS,
    PID_PATH, POLL_INTERVAL_S, SAFE_THROUGHPUT_CALLS, SHOW_BUCKETS, SHOW_SCOPE,
    SLEEP_SECONDS, STOP_SENTINEL,
)
from scripts.managers.services.mdblist import age_cache                        # noqa: E402
from scripts.managers.services.mdblist.client import show_ratings              # noqa: E402

_BASE_URL     = "https://api.trakt.tv"
_TOKEN_BUFFER = 86_400     # refresh 1 day before expiry

# Per-bucket Trakt endpoint templates ({id} = tmdbId for movies, tvdbId for shows).
MOVIE_ENDPOINTS = {
    "people":       "movies/{id}/people",
    "summary":      "movies/{id}?extended=full",
    "ratings":      "movies/{id}/ratings",
    "related":      "movies/{id}/related",
    "aliases":      "movies/{id}/aliases",
    "studios":      "movies/{id}/studios",
    "translations": "movies/{id}/translations",
    "lists":        "movies/{id}/lists",
}
SHOW_ENDPOINTS = {
    "summary": "shows/{id}?extended=full",   # genres + overview → TV genre affinity (incl. cross-medium)
    "people":  "shows/{id}/people",
    "ratings": "shows/{id}/ratings",
    "related": "shows/{id}/related",
}

# Per-turn budget when round-robining pools within a priority tier. Each
# movie/show pool gets at most this many endpoint-calls per turn before yielding
# to the next, so movie + show enrichment INTERLEAVE (the show pools feed the TV
# watchability scorer) instead of a large cold movie pool draining the whole
# cycle first. Warm buckets cost 0 calls, so a warm library still races through.
INTERLEAVE_SLICE_CALLS = 75

# Sentinel returned by TraktClient.get() when an endpoint DEFINITIVELY has no data
# (HTTP 404 or an empty 200 body) — as opposed to None, which means a TRANSIENT
# failure (429 over cap / timeout / 5xx) that should be retried later. The daemon
# negative-caches NO_DATA (writes an empty marker) so a permanently-empty endpoint
# is never re-fetched every wrap; None is left uncached so it retries.
NO_DATA = object()


# ── Logging ──────────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("enrich_daemon")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger

log = _setup_logger()


# ── Cursor ───────────────────────────────────────────────────────────────────────

def load_cursor() -> dict:
    if CURSOR_PATH.exists():
        try:
            with open(CURSOR_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cursor(cursor: dict):
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CURSOR_PATH, "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=2)


# ── Disk cache (atomic) ──────────────────────────────────────────────────────────

def _bucket_path(bucket_dir: Path, item_id: int) -> Path:
    return bucket_dir / f"{item_id}.json.gz"


def is_cached(bucket_dir: Path, item_id: int) -> bool:
    path = _bucket_path(bucket_dir, item_id)
    try:
        st = path.stat()
    except OSError:
        return False
    # A valid gz is never 0 bytes (even {} gzips to ~20). A 0-byte file is poison
    # left by a process killed mid-write or a file-sync (e.g. OneDrive) dehydration:
    # it reads back as a phantom-empty {} AND froze the title unenriched, because
    # is_cached() saw it as a fresh hit and never re-fetched it until the 7-day TTL
    # rolled. Treat it as uncached so this cycle re-fetches + atomically overwrites it.
    if st.st_size == 0:
        return False
    return (time.time() - st.st_mtime) <= CACHE_TTL_S


def write_cache(bucket_dir: Path, item_id: int, data) -> None:
    """Atomic gz write (temp + os.replace) so a hard kill never leaves a partial."""
    path = _bucket_path(bucket_dir, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trakt_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def purge_empty_caches(dry_run: bool = False) -> int:
    """Sweep 0-byte bucket files from every cache bucket so they re-fetch.

    A valid gz is never empty, so a 0-byte ``{id}.json.gz`` is always poison (a
    killed write or a file-sync dehydration). Left in place it (a) reads back as a
    phantom-empty entry and (b) was treated by ``is_cached()`` as a fresh hit and
    never re-fetched. ``is_cached()`` now skips them, which already self-heals over
    a few cycles; this one-shot boot sweep removes the poison up-front so the
    healing is immediate and on-disk counts reflect real coverage. Cheap: one glob
    per bucket. No-ops the deletes (log-only) in dry_run to honour the contract."""
    removed = 0
    for bdir in list(MOVIE_BUCKETS.values()) + list(SHOW_BUCKETS.values()):
        if not bdir.exists():
            continue
        for f in bdir.glob("*.json.gz"):
            try:
                if f.stat().st_size > 0:
                    continue
            except OSError:
                continue
            removed += 1
            if not dry_run:
                try:
                    f.unlink()
                except OSError:
                    pass
    if removed:
        verb = "would purge" if dry_run else "purged"
        log.info(f"Cache hygiene: {verb} {removed:,} empty (0-byte) bucket file(s) so they re-fetch.")
    return removed


def _stop_requested() -> bool:
    return STOP_SENTINEL.exists()


def _pid_alive(pid: int) -> bool:
    """Window-free liveness probe so the DETACHED daemon never flashes a console.

    The daemon has no console of its own, so spawning a child console app
    (tasklist) made Windows allocate a NEW console window every poll. Use the
    OpenProcess API directly (no subprocess) on Windows; os.kill(0) on POSIX.
    PID-reuse is bounded by the sentinel's MAIN_ACTIVE_MAX_AGE_S backstop.
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process EXISTS but is owned by another user (e.g. main.py launched
        # under a different account) — signal 0 is refused. Treat as alive, else
        # _main_active() would report main dead and the daemon would resume fetching
        # and compete for the shared Trakt rate-limit window.
        return True
    except OSError:
        return False


def _main_active() -> bool:
    """True while a main.py run holds the main-active sentinel AND is still alive.

    The daemon pauses fetching during a run so it doesn't compete for the shared
    Trakt rate-limit window. Robust to a crashed main: a missing file, a dead pid,
    or a timestamp older than MAIN_ACTIVE_MAX_AGE_S all clear the pause.
    """
    try:
        raw = MAIN_ACTIVE_SENTINEL.read_text()
    except (FileNotFoundError, OSError):
        return False
    try:
        data = json.loads(raw)
        pid = int(data.get("pid", 0))
        ts  = float(data.get("ts", 0))
    except Exception:
        return False
    if ts and (time.time() - ts) > MAIN_ACTIVE_MAX_AGE_S:
        return False
    if pid and not _pid_alive(pid):
        return False
    return True


# ── Trakt HTTP ───────────────────────────────────────────────────────────────────

class TraktClient:
    # Cap any single 429 Retry-After wait so a hostile/huge value can't stall the
    # whole cycle, and bound retries to one (mirrors the production TraktAPIManager).
    _MAX_429_WAIT = 30

    def __init__(self, cfg: dict, loader: ConfigLoader):
        trakt_cfg             = cfg.get("trakt", {})
        auth                  = trakt_cfg.get("authorization", {})
        self.client_id        = trakt_cfg.get("client_id", "")
        self.client_secret    = trakt_cfg.get("client_secret", "")
        self.access_token     = auth.get("access_token", "")
        self.refresh_token    = auth.get("refresh_token", "")
        self.token_expires_at = auth.get("created_at", 0) + auth.get("expires_in", 0)
        self._cfg             = cfg
        self._loader          = loader
        self._session         = requests.Session()
        self._request_times: list[float] = []
        # Tripped when Trakt rate-limits us beyond the retry cap. The cycle then
        # stops firing calls and lets the inter-cycle sleep clear the window,
        # instead of blasting hundreds of rejected requests that keep the account
        # suppressed. Reset each cycle (this client is rebuilt per run_cycle).
        self.rate_limited     = False
        self._sync_headers()

    def _sync_headers(self):
        headers = {
            "Content-Type":      "application/json",
            "trakt-api-version": "2",
            "trakt-api-key":     self.client_id,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        self._session.headers.update(headers)

    def _token_expiring(self) -> bool:
        return self.token_expires_at > 0 and time.time() > self.token_expires_at - _TOKEN_BUFFER

    def _refresh(self) -> bool:
        if not all([self.refresh_token, self.client_id, self.client_secret]):
            log.warning("Cannot refresh Trakt token - missing credentials")
            return False
        try:
            resp = requests.post(
                f"{_BASE_URL}/oauth/token",
                json={
                    "refresh_token": self.refresh_token,
                    "client_id":     self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type":    "refresh_token",
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            new_auth = resp.json()
            self.access_token     = new_auth["access_token"]
            self.refresh_token    = new_auth.get("refresh_token", self.refresh_token)
            self.token_expires_at = new_auth.get("created_at", 0) + new_auth.get("expires_in", 0)
            # Persist via ConfigLoader so the refreshed tokens go to the keyring
            # (NOT plaintext config.json).
            self._cfg.setdefault("trakt", {})["authorization"] = new_auth
            self._loader.save(self._cfg)
            self._sync_headers()
            log.info("Trakt token refreshed and saved (keyring).")
            return True
        except Exception as e:
            log.warning(f"Token refresh failed: {e}")
            return False

    def _throttle(self):
        """Stay within 1 000 calls / 5-minute window (5% buffer)."""
        now = time.time()
        self._request_times = [t for t in self._request_times if now - t < 300]
        if len(self._request_times) >= 950:
            wait = 300 - (now - self._request_times[0]) + 1
            if wait > 0:
                log.info(f"Rate limit - sleeping {wait:.0f}s")
                time.sleep(wait)
        self._request_times.append(time.time())

    def get(self, endpoint: str, _retry: bool = True):
        if not self.client_id:
            return None
        if self._token_expiring():
            log.info("Token expiring - refreshing...")
            self._refresh()
        self._throttle()
        url = f"{_BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.get(url, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                # Bound the recursion to ONE retry and cap the wait. The 429 branch
                # previously ignored `_retry`, so a server returning repeated 429s
                # could recurse + sleep unbounded and a Retry-After:300 stalled the
                # whole cycle. Over the cap (or already retried) → give up this call.
                if not _retry or wait > self._MAX_429_WAIT:
                    log.warning(f"429 rate-limited - Retry-After {wait}s over cap "
                                f"({self._MAX_429_WAIT}s) or retry exhausted; backing off this cycle")
                    self.rate_limited = True
                    return None
                log.warning(f"429 rate-limited - sleeping {wait}s")
                time.sleep(wait)
                return self.get(endpoint, _retry=False)
            if resp.status_code == 401 and _retry:
                log.warning("401 - refreshing and retrying once")
                if self._refresh():
                    return self.get(endpoint, _retry=False)
                return None
            if resp.status_code == 404:
                return NO_DATA               # definitive: nothing at this endpoint
            resp.raise_for_status()
            return resp.json() if resp.content else NO_DATA
        except Exception as e:
            log.debug(f"GET /{endpoint} error: {e}")
            return None                      # transient: retry on a later wrap


# ── Normalise Trakt people response ─────────────────────────────────────────────

_DEPT_MAP = {
    "directing": "Directing", "writing": "Writing", "production": "Production",
    "sound": "Sound", "camera": "Camera", "editing": "Editing",
    "crew": "Crew", "costume & make-up": "Costume & Make-Up",
    "visual effects": "Visual Effects", "art": "Art", "lighting": "Lighting",
}


def normalise_people(raw: dict) -> dict:
    cast = []
    for i, member in enumerate(raw.get("cast") or []):
        person = member.get("person") or {}
        ids    = person.get("ids") or {}
        chars  = member.get("characters") or []
        cast.append({
            "name":      person.get("name", ""),
            "id":        ids.get("tmdb"),
            "character": chars[0] if chars else "",
            "order":     i,
        })
    crew = []
    for dept_key, members in (raw.get("crew") or {}).items():
        dept = _DEPT_MAP.get(dept_key.lower(), dept_key.title())
        for member in (members or []):
            person = member.get("person") or {}
            ids    = person.get("ids") or {}
            for job in (member.get("jobs") or []):
                crew.append({
                    "name":       person.get("name", ""),
                    "id":         ids.get("tmdb"),
                    "job":        job,
                    "department": dept,
                })
    return {"cast": cast, "crew": crew}


# ── Media fetchers ───────────────────────────────────────────────────────────────

# (connect, read) timeout. A big Sonarr /series (with statistics) can take a while
# to serialise, so the read budget is generous; retries cover transient slowness.
_ARR_TIMEOUT = (10, 180)
_ARR_RETRIES = 3


def _arr_get(url: str, api: str, what: str) -> list[dict]:
    """GET an *arr list endpoint with a generous timeout + backoff retries.
    A single slow response no longer drops the whole pool for the cycle."""
    last_err = None
    for attempt in range(1, _ARR_RETRIES + 1):
        if _stop_requested():
            return []
        try:
            resp = requests.get(url, headers={"X-Api-Key": api}, timeout=_ARR_TIMEOUT)
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            last_err = e
            if attempt < _ARR_RETRIES:
                wait = 3 * attempt
                log.warning(f"{what} fetch attempt {attempt}/{_ARR_RETRIES} failed: {e} - retrying in {wait}s")
                time.sleep(wait)
    log.warning(f"{what} fetch failed after {_ARR_RETRIES} attempts: {last_err}")
    return []


def get_radarr_movies(cfg: dict) -> list[dict]:
    inst = cfg.get("radarr_instances", {})
    name = (inst.get("default_instance") or {}).get("name", "standard")
    base = (inst.get(name) or {}).get("base_url", "")
    api  = (inst.get(name) or {}).get("api", "")
    if not base or not api:
        log.warning("Radarr not configured")
        return []
    return _arr_get(f"{base}/api/v3/movie", api, "Radarr movie")


def get_sonarr_series(cfg: dict) -> list[dict]:
    inst = cfg.get("sonarr_instances", {})
    name = str((inst.get("default_instance") or {}).get("name", "sonarr"))
    base = (inst.get(name) or {}).get("base_url", "")
    api  = (inst.get(name) or {}).get("api", "")
    if not base or not api:
        log.warning("Sonarr not configured")
        return []
    return _arr_get(f"{base}/api/v3/series", api, "Sonarr series")


# The library (hasFile / episodeFileCount) barely changes between 5-min cycles, so
# cache the full *arr lists and reuse them — this keeps the daemon from pulling 20k
# movies + 2k series off the *arr servers every cycle (which can time out / collide
# with a main.py run). On a failed/empty fetch, fall back to the last good cache.
LIBRARY_TTL_S = 1800   # 30 minutes


def _cached_library(name: str, fetch_fn, cfg: dict) -> list[dict]:
    path = CACHE_TRAKT / f"daemon_{name}.json"
    age  = (time.time() - path.stat().st_mtime) if path.exists() else None

    if age is not None and age <= LIBRARY_TTL_S:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            log.info(f"  reusing cached {name}: {len(data):,} items (age {int(age)}s, refetch in {int(LIBRARY_TTL_S - age)}s)")
            return data
        except Exception:
            pass  # corrupt cache — fall through to a live fetch

    data = fetch_fn(cfg)
    if data:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lib_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception as e:
            log.debug(f"could not cache {name}: {e}")
        return data

    # Live fetch failed/empty — use the last good cache rather than zeroing the pool.
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                stale = json.load(f)
            log.warning(f"  {name} live fetch failed - using stale cache: {len(stale):,} items (age {int(age or 0)}s)")
            return stale
        except Exception:
            pass
    return []


# ── Enrichment ───────────────────────────────────────────────────────────────────

def enrich_pool(
    trakt: TraktClient,
    item_ids: list[int],
    kind: str,                 # "movie" or "show"
    scope: list[str],
    buckets: dict,
    endpoints: dict,
    cursor_key: str,
    cursor: dict,
    budget: int,               # max endpoint-calls this pool may spend
    dry_run: bool,
    label_map: dict | None = None,
    show_items: bool = False,
) -> tuple[int, int, int, bool]:
    """Walk one pool from its cursor, fetching every bucket in *scope* per item
    until *budget* endpoint-calls are spent (cached buckets cost 0). The cursor
    only advances past FULLY-completed items, so a budget/stop cut mid-item
    resumes there next cycle (its already-cached buckets then skip).

    When *show_items* is set, logs one line per item naming the title and which
    data buckets were grabbed (interactive visibility).

    Returns (calls_used, items_completed, buckets_skipped_cached, stop_requested).
    """
    label_map = label_map or {}
    sorted_ids = sorted(set(item_ids))
    if not sorted_ids:
        return 0, 0, 0, False

    last_id = (cursor.get(cursor_key) or {}).get("last_tmdb_id", -1)
    start   = bisect.bisect_right(sorted_ids, last_id)
    if start >= len(sorted_ids):
        start = 0
        last_id = -1
        log.info(f"  [{kind}:{cursor_key}] cursor cycled - restarting from beginning")

    calls = skipped = completed = 0
    last_complete = last_id
    stop = False
    i = start
    n = len(sorted_ids)

    while i < n and calls < budget:
        if _stop_requested():
            stop = True
            break
        item_id = sorted_ids[i]
        item_complete = True
        grabbed: list[str] = []        # buckets fetched (or, in dry_run, would-fetch) for this item
        for bucket in scope:
            bdir = buckets.get(bucket)
            ep   = endpoints.get(bucket)
            if bdir is None or ep is None:
                continue
            if is_cached(bdir, item_id):
                skipped += 1
                continue
            if calls >= budget or _stop_requested():
                item_complete = False
                stop = stop or _stop_requested()
                break
            if dry_run:
                calls += 1            # simulate the call without writing
                grabbed.append(bucket)
                continue
            raw = trakt.get(ep.format(id=item_id))
            calls += 1
            if trakt.rate_limited:
                # Trakt is rate-limiting us: stop spending calls so the inter-cycle
                # sleep can recover the window. Resume at this item next cycle. NOT
                # the same as `stop` (the daemon-shutdown sentinel) — run_cycle ends
                # the cycle on trakt.rate_limited and the daemon sleeps as normal.
                item_complete = False
                break
            if raw is None:
                continue              # transient failure - left uncached, retried on next wrap
            if raw is NO_DATA:
                # Trakt has nothing here (404 / empty). Negative-cache an empty
                # marker so future wraps SKIP this bucket instead of re-fetching it
                # forever. Without this, "done" owned pools kept spending the whole
                # 650-call budget re-attempting the ~2,800 never-cacheable buckets
                # every cycle, starving the unowned pools. Consumers already treat an
                # empty/falsy cache entry as a clean miss, so {} is safe to store.
                write_cache(bdir, item_id, {})
                grabbed.append(f"{bucket}:none")
                continue
            data = normalise_people(raw) if bucket == "people" else raw
            write_cache(bdir, item_id, data)
            if bucket == "people":
                grabbed.append(f"people({len(data.get('cast', []))}c/{len(data.get('crew', []))}r)")
            else:
                grabbed.append(bucket)
        if show_items and grabbed:
            verb  = "would grab" if dry_run else "grabbed"
            label = label_map.get(item_id) or f"{kind} {item_id}"
            log.info(f"  {verb}: {label} [{kind} {item_id}] -> {', '.join(grabbed)}")
        if item_complete:
            last_complete = item_id
            completed += 1
            i += 1
        else:
            break                      # resume at this item next cycle

    cursor[cursor_key] = {
        "last_tmdb_id": last_complete,
        "position":     i,
        "total":        n,
        "updated_at":   datetime.now(tz=timezone.utc).isoformat(),
    }
    return calls, completed, skipped, stop


# ── Watched-set resolution (Tautulli) ──────────────────────────────────────────
# The shared global_cache the main run writes lives under support/cache/ (the
# sibling of this daemon's trakt cache), so we can read the household's watch
# state straight off disk — no extra Tautulli calls.
_TAUTULLI_CACHE = CACHE_TRAKT.parent / "tautulli"
_YEAR_SUFFIX    = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")   # strip a trailing "(2018)"


def _norm_title(t: str | None) -> str:
    """Lowercase, trim, drop a trailing release-year suffix, collapse whitespace —
    so Tautulli's 'Bluey (2018)' matches Sonarr's 'Bluey'."""
    s = _YEAR_SUFFIX.sub("", (t or "").lower().strip())
    return " ".join(s.split())


def watched_movie_tmdb_ids() -> set[int]:
    """tmdbIds of movies the household has watched — the union of every rating
    group's Tautulli completion map (any appearance counts as watched)."""
    base = _TAUTULLI_CACHE / "group"
    ids: set[int] = set()
    if not base.exists():
        return ids
    for comp in base.glob("*/tmdb_completions.json"):
        try:
            with open(comp, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            continue
        for k in data:
            try:
                ids.add(int(k))
            except (TypeError, ValueError):
                pass
    return ids


def watched_show_tvdb_ids(series: list[dict]) -> set[int]:
    """tvdbIds of shows the household has watched, resolved by matching the Tautulli
    history's per-episode ``grandparent_title`` against the Sonarr series titles.
    Best-effort: a title that doesn't match just falls back to the owned/unowned
    tier (no tvdbId in the history rows, so this is the cheapest reliable bridge)."""
    hist = _TAUTULLI_CACHE / "history" / "all.json"
    if not hist.exists():
        return set()
    try:
        with open(hist, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return set()
    rows = history if isinstance(history, list) else (history.get("data") or [])
    watched_titles = {
        _norm_title(r.get("grandparent_title"))
        for r in rows
        if isinstance(r, dict) and r.get("media_type") == "episode" and r.get("grandparent_title")
    }
    watched_titles.discard("")
    if not watched_titles:
        return set()
    title_to_tvdb = {
        _norm_title(s.get("title")): int(s["tvdbId"])
        for s in series if s.get("tvdbId") and s.get("title")
    }
    return {title_to_tvdb[t] for t in watched_titles if t in title_to_tvdb}


# ── Common Sense Media age enrichment (MDBList) ──────────────────────────────────
COMMONSENSE_PER_CYCLE = 100        # default MDBList lookups per daemon cycle


def run_commonsense(cfg: dict, movies: list[dict], dry_run: bool) -> None:
    """Incrementally fill the Common Sense age cache from MDBList — a bounded slice per
    cycle (OWNED movies first), skipping ids already cached and guarding the daily MDBList
    budget so it never blows the quota in one go. Opt-out via daemons.enrich.commonsense=false;
    no-ops when MDBList isn't keyed. The cache (services/mdblist/age_cache) is what the movie
    classifier reads as its primary kids signal."""
    enrich_cfg = (cfg.get("daemons", {}) or {}).get("enrich", {}) or {}
    if not enrich_cfg.get("commonsense", True):
        return
    apikey = (cfg.get("mdblist", {}) or {}).get("apikey") or ""
    if not apikey:
        return                                       # MDBList not configured → skip silently

    cache = age_cache.load()
    owned   = [int(m["tmdbId"]) for m in movies if m.get("hasFile") and m.get("tmdbId")]
    unowned = [int(m["tmdbId"]) for m in movies if not m.get("hasFile") and m.get("tmdbId")]
    todo = [t for t in (owned + unowned) if str(t) not in cache]   # owned-first, uncached
    if not todo:
        return                                       # whole library covered

    used, limit = age_cache.budget(apikey)
    headroom = max(0, limit - age_cache.BUDGET_FLOOR - used)
    per_cycle = int(enrich_cfg.get("commonsense_per_cycle", COMMONSENSE_PER_CYCLE))
    max_calls = min(per_cycle, headroom)
    if max_calls <= 0:
        log.info(f"  [commonsense] MDBList daily budget low ({used:,}/{limit:,}) - skipping this cycle.")
        return
    if dry_run:
        log.info(f"  [commonsense] would fetch {min(max_calls, len(todo)):,} CSM ages (dry-run); "
                 f"{len(todo):,} uncached.")
        return

    looked, covered = age_cache.fetch_into(apikey, todo, cache, max_calls=max_calls,
                                           stop=_stop_requested)
    if looked:
        age_cache.save(cache)
    log.info(f"  [commonsense] +{looked} ages ({covered} with CSM) | cache={len(cache):,} | "
             f"{len(todo) - looked:,} movies left | MDBList ~{used + looked:,}/{limit:,}")


def run_commonsense_tv(cfg: dict, series: list[dict], dry_run: bool) -> None:
    """TV counterpart of ``run_commonsense`` — fills the SEPARATE TV age cache
    (``age_ratings_tv.json``, keyed by show-space tmdbId) from MDBList ``/tmdb/show``,
    OWNED series first and paced against the same daily budget. The per-profile playlist
    cert-gate reads this as the fallback when a series carries no Sonarr certification
    (~41% of the library). Same opt-out (``daemons.enrich.commonsense``); no-ops when
    MDBList isn't keyed. Shares the budget with the movie pass (which runs first), so it
    only spends whatever headroom is left above the reserve floor."""
    enrich_cfg = (cfg.get("daemons", {}) or {}).get("enrich", {}) or {}
    if not enrich_cfg.get("commonsense", True):
        return
    apikey = (cfg.get("mdblist", {}) or {}).get("apikey") or ""
    if not apikey:
        return                                       # MDBList not configured → skip silently

    cache = age_cache.load(age_cache.TV_AGE_CACHE_PATH)
    def _owned(s):
        return ((s.get("statistics", {}) or {}).get("episodeFileCount", 0) or 0) > 0
    owned   = [int(s["tmdbId"]) for s in series if s.get("tmdbId") and _owned(s)]
    unowned = [int(s["tmdbId"]) for s in series if s.get("tmdbId") and not _owned(s)]
    todo = [t for t in (owned + unowned) if str(t) not in cache]   # owned-first, uncached
    if not todo:
        return                                       # whole library covered

    used, limit = age_cache.budget(apikey)
    headroom = max(0, limit - age_cache.BUDGET_FLOOR - used)
    per_cycle = int(enrich_cfg.get("commonsense_per_cycle", COMMONSENSE_PER_CYCLE))
    max_calls = min(per_cycle, headroom)
    if max_calls <= 0:
        log.info(f"  [commonsense-tv] MDBList daily budget low ({used:,}/{limit:,}) - skipping this cycle.")
        return
    if dry_run:
        log.info(f"  [commonsense-tv] would fetch {min(max_calls, len(todo)):,} CSM ages (dry-run); "
                 f"{len(todo):,} uncached.")
        return

    looked, covered = age_cache.fetch_into(apikey, todo, cache, max_calls=max_calls,
                                           stop=_stop_requested, lookup=show_ratings)
    if looked:
        age_cache.save(cache, age_cache.TV_AGE_CACHE_PATH)
    log.info(f"  [commonsense-tv] +{looked} ages ({covered} with CSM) | cache={len(cache):,} | "
             f"{len(todo) - looked:,} shows left | MDBList ~{used + looked:,}/{limit:,}")


# ── TV-franchise catalog (Wikidata + Wikipedia) — periodic, detached ─────────────
FRANCHISE_TTL_DAYS    = 14          # regenerate the TV-franchise catalog at most this often
FRANCHISE_RETRY_HOURS = 6           # after a (possibly failed) attempt, don't respawn the regen this soon


def _franchise_state_path() -> Path:
    return CACHE_TRAKT.parent / "franchise_catalog_state.json"


def run_franchise_catalog(cfg: dict, dry_run: bool) -> None:
    """Periodically regenerate the TV-franchise catalog (Wikidata P2512/P179 + Wikipedia categories +
    infobox related/spin-off links) by spawning ``generate_tv_franchises`` as a DETACHED subprocess —
    so the slow, heavily rate-limited Wikidata/Wikipedia fetch (it backs off 55-120s on every 429)
    grinds in the background and NEVER blocks the enrichment cycle. A catalog fresher than
    ``franchise_catalog_ttl_days`` is left alone; at most ONE regen runs at a time (pid-guarded); a
    recent attempt isn't immediately respawned. The generated catalog is the tier-2 (auto/unvetted)
    layer the acquisition already deprioritises, so refreshing it unattended is safe — promote good
    families into the committed floor by hand. Opt-out: ``daemons.enrich.franchise_catalog=false``."""
    enrich_cfg = (cfg.get("daemons", {}) or {}).get("enrich", {}) or {}
    if not enrich_cfg.get("franchise_catalog", True):
        return
    try:
        import scripts.support.tools.generate_tv_franchises as gen_tool
        catalog = Path(gen_tool._catalog_path())
        gen_src = Path(gen_tool.__file__)
    except Exception as e:
        log.debug(f"[franchise] generator unavailable: {e}")
        return
    ttl_days = float(enrich_cfg.get("franchise_catalog_ttl_days", FRANCHISE_TTL_DAYS))
    now = time.time()
    # Fresh = within TTL AND not older than the generator CODE — so adding an edge / fixing the
    # generator triggers a rebuild on the next cycle, not only after the TTL lapses.
    fresh = (catalog.exists()
             and (now - catalog.stat().st_mtime) < ttl_days * 86_400
             and catalog.stat().st_mtime >= gen_src.stat().st_mtime)
    if fresh:
        return
    spath = _franchise_state_path()
    state: dict = {}
    if spath.exists():
        try:
            state = json.loads(spath.read_text())
        except Exception:
            pass
    gen_pid = state.get("gen_pid")
    if gen_pid and _pid_alive(int(gen_pid)):
        log.info(f"  [franchise] regen already running (pid {gen_pid}); skipping.")
        return
    last = float(state.get("last_attempt_ts", 0) or 0)
    if last and (now - last) < FRANCHISE_RETRY_HOURS * 3600:
        return                                                  # recently attempted (maybe still rate-limited)
    if dry_run:
        log.info(f"  [franchise] catalog stale (>{ttl_days:.0f}d) — would spawn a detached regen (dry-run).")
        return
    kwargs: dict = {"cwd": str(_REPO_ROOT), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008   # DETACHED_PROCESS (no console)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([sys.executable, "-m", "scripts.support.tools.generate_tv_franchises"], **kwargs)
    except Exception as e:
        log.warning(f"[franchise] could not spawn regen: {e}")
        return
    state["gen_pid"] = proc.pid
    state["last_attempt_ts"] = now
    try:
        spath.parent.mkdir(parents=True, exist_ok=True)
        spath.write_text(json.dumps(state, indent=2))
    except OSError:
        pass
    log.info(f"  [franchise] catalog stale (>{ttl_days:.0f}d) — spawned detached regen (pid {proc.pid}); "
             f"it grinds through Wikidata/Wikipedia rate limits in the background, no cycle impact.")


def run_cycle(cfg: dict, loader: ConfigLoader, cursor: dict, dry_run: bool,
              show_items: bool = False) -> tuple[dict, bool]:
    enrich_cfg  = (cfg.get("daemons", {}) or {}).get("enrich", {}) or {}
    scope       = [s for s in (enrich_cfg.get("scope") or DEFAULT_SCOPE) if s in MOVIE_BUCKETS]
    if not scope:
        scope = list(DEFAULT_SCOPE)
    owned_first   = bool(enrich_cfg.get("owned_first", True))
    watched_first = bool(enrich_cfg.get("watched_first", True))

    trakt = TraktClient(cfg, loader)
    if not trakt.client_id:
        log.warning("Trakt is not configured (no client_id) - nothing to enrich this cycle.")
        return {}, False

    # ── Radarr movies ───────────────────────────────────────────────────────────
    movies = _cached_library("radarr_movies", get_radarr_movies, cfg)
    owned_movie_ids   = [int(m["tmdbId"]) for m in movies if m.get("hasFile") and m.get("tmdbId")]
    unowned_movie_ids = [int(m["tmdbId"]) for m in movies if not m.get("hasFile") and m.get("tmdbId")]

    # ── Sonarr series ───────────────────────────────────────────────────────────
    series = _cached_library("sonarr_series", get_sonarr_series, cfg)
    owned_show_ids   = [int(s["tvdbId"]) for s in series
                        if s.get("tvdbId") and (s.get("statistics", {}) or {}).get("episodeFileCount", 0) > 0]
    unowned_show_ids = [int(s["tvdbId"]) for s in series
                        if s.get("tvdbId") and s.get("monitored")
                        and (s.get("statistics", {}) or {}).get("episodeFileCount", 0) == 0]

    # tmdbId/tvdbId -> human label, so per-item logging can name the title.
    movie_labels = {
        int(m["tmdbId"]): (f"{m.get('title', '?')} ({m['year']})" if m.get("year") else m.get("title", "?"))
        for m in movies if m.get("tmdbId")
    }
    show_labels = {int(s["tvdbId"]): s.get("title", "?") for s in series if s.get("tvdbId")}

    # ── Watched tier: Tautulli-watched items get enriched FIRST ──────────────────
    # Only library items are enrichable, so intersect the watched sets with the ids
    # we actually have, then REMOVE them from the owned/unowned pools so each id
    # lives in exactly one pool. A watched-but-unowned title still rides this tier
    # (you watched it → enrich it before the cold backlog).
    watched_movie_ids: list[int] = []
    watched_show_ids:  list[int] = []
    if watched_first:
        # Movies must be in Radarr to be enrichable. Shows resolved from the Sonarr
        # title map are already all in Sonarr, so take every watched one — including
        # unmonitored/unowned titles the household has watched (still relevant for
        # affinity), which the owned/unowned pools would otherwise never reach.
        lib_movie_ids = set(owned_movie_ids) | set(unowned_movie_ids)
        wm = watched_movie_tmdb_ids() & lib_movie_ids
        ws = watched_show_tvdb_ids(series)
        watched_movie_ids = sorted(wm)
        watched_show_ids  = sorted(ws)
        owned_movie_ids   = [i for i in owned_movie_ids   if i not in wm]
        unowned_movie_ids = [i for i in unowned_movie_ids if i not in wm]
        owned_show_ids    = [i for i in owned_show_ids    if i not in ws]
        unowned_show_ids  = [i for i in unowned_show_ids  if i not in ws]

    log.info(f"Library: watched movies={len(watched_movie_ids):,} shows={len(watched_show_ids):,} | "
             f"owned movies={len(owned_movie_ids):,} shows={len(owned_show_ids):,} | "
             f"unowned movies={len(unowned_movie_ids):,} shows={len(unowned_show_ids):,} | "
             f"scope={scope} watched_first={watched_first} owned_first={owned_first}")

    watched_pools = [
        ("movies_watched", watched_movie_ids, "movie", scope, MOVIE_BUCKETS, MOVIE_ENDPOINTS),
        ("shows_watched",  watched_show_ids,  "show",  SHOW_SCOPE, SHOW_BUCKETS, SHOW_ENDPOINTS),
    ]
    owned_pools = [
        ("movies_owned", owned_movie_ids, "movie", scope, MOVIE_BUCKETS, MOVIE_ENDPOINTS),
        ("shows_owned",  owned_show_ids,  "show",  SHOW_SCOPE, SHOW_BUCKETS, SHOW_ENDPOINTS),
    ]
    unowned_pools = [
        ("movies_unowned", unowned_movie_ids, "movie", scope, MOVIE_BUCKETS, MOVIE_ENDPOINTS),
        ("shows_unowned",  unowned_show_ids,  "show",  SHOW_SCOPE, SHOW_BUCKETS, SHOW_ENDPOINTS),
    ]
    # Priority tiers, fully worked top-down: watched (if enabled) → owned → unowned
    # (owned_first preserved). WITHIN a tier the movie and show pools round-robin so
    # both make progress every cycle.
    tiers = [owned_pools, unowned_pools] if owned_first else [unowned_pools, owned_pools]
    if watched_first:
        tiers = [watched_pools] + tiers
    pool_sizes = {p[0]: len(set(p[1])) for p in (watched_pools + owned_pools + unowned_pools)}

    stats: dict = {}
    spent = 0
    stop = False
    # Round-robin within each tier: hand each pool an INTERLEAVE_SLICE_CALLS slice
    # per turn and alternate movie<->show. enrich_pool resumes from each pool's
    # cursor, so successive slices walk the list incrementally. A pool that spends
    # LESS than its slice has reached the end of its id list with no fresh buckets
    # left this cycle, so it retires from the rotation; the loop ends when the
    # budget is spent or every pool in the tier has retired.
    for tier in tiers:
        if stop or trakt.rate_limited:
            break
        active = list(tier)
        while active and spent < SAFE_THROUGHPUT_CALLS and not stop and not trakt.rate_limited:
            retired: list[str] = []
            for cursor_key, ids, kind, pscope, buckets, endpoints in active:
                remaining = SAFE_THROUGHPUT_CALLS - spent
                if remaining <= 0:
                    break
                slice_budget = min(INTERLEAVE_SLICE_CALLS, remaining)
                labels = movie_labels if kind == "movie" else show_labels
                calls, completed, cached, pstop = enrich_pool(
                    trakt, ids, kind, pscope, buckets, endpoints, cursor_key, cursor,
                    slice_budget, dry_run, label_map=labels, show_items=show_items,
                )
                spent += calls
                agg = stats.setdefault(cursor_key, {"calls": 0, "completed": 0, "cached": 0})
                agg["calls"]     += calls
                agg["completed"] += completed
                agg["cached"]    += cached
                if pstop:
                    stop = True
                    break
                if trakt.rate_limited:
                    break          # end the cycle early; the inter-cycle sleep recovers the window
                if calls < slice_budget:
                    retired.append(cursor_key)
            active = [p for p in active if p[0] not in retired]

    for cursor_key, agg in stats.items():
        pos = (cursor.get(cursor_key) or {}).get("position", 0)
        log.info(f"  [{cursor_key}] calls={agg['calls']} completed={agg['completed']} "
                 f"cached_skip={agg['cached']} (pos {pos}/{pool_sizes.get(cursor_key, 0)})")
    for cursor_key, size in pool_sizes.items():
        if cursor_key not in stats and size > 0:
            log.info(f"  [{cursor_key}] deferred - budget spent on higher-priority pools")
    if trakt.rate_limited:
        log.warning(f"Trakt rate-limited - ended cycle early after {spent} call(s); backing off "
                    f"~{SLEEP_SECONDS / 60:.1f} min so the rate window recovers before the next cycle.")
    log.info(f"Cycle spent {spent}/{SAFE_THROUGHPUT_CALLS} endpoint-calls.")

    # ── Common Sense Media ages (MDBList) — incremental, budget-paced, owned-first ──
    # Movies first (richer cache, established), then TV into its own cache; both feed the
    # per-profile playlist cert-gate as the fallback for titles with no Sonarr/Radarr cert.
    try:
        run_commonsense(cfg, movies, dry_run)
    except Exception as e:
        log.warning(f"[commonsense] step failed: {e}")
    try:
        run_commonsense_tv(cfg, series, dry_run)
    except Exception as e:
        log.warning(f"[commonsense-tv] step failed: {e}")
    # ── TV-franchise catalog regen (Wikidata + Wikipedia) — periodic, spawned detached ──
    try:
        run_franchise_catalog(cfg, dry_run)
    except Exception as e:
        log.warning(f"[franchise] step failed: {e}")
    return stats, stop


# ── Status report (read-only) ──────────────────────────────────────────────────

# Friendly pool labels + the order they're worked (owned tier before unowned).
_POOL_LABELS = {
    "movies_watched": "movies (watched)",
    "shows_watched":  "shows (watched)",
    "movies_owned":   "movies (owned)",
    "shows_owned":    "shows (owned)",
    "movies_unowned": "movies (unowned)",
    "shows_unowned":  "shows (unowned)",
}
_POOL_ORDER = ["movies_watched", "shows_watched",
               "movies_owned", "shows_owned", "movies_unowned", "shows_unowned"]


def _fmt_age(iso: str) -> str:
    """Human 'Nm ago' from an ISO-8601 timestamp; '' if unparseable."""
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = (datetime.now(tz=timezone.utc) - then).total_seconds()
    except Exception:
        return ""
    if secs < 0:
        return "just now"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172_800:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.1f}d ago"


def _franchise_catalog_report() -> None:
    """Print what the Wikidata+Wikipedia TV-franchise catalog has gathered — the wiki-sourced
    counterpart to the per-bucket cache report. Shows the generated catalog's family/show counts,
    the per-edge corroboration breakdown (which of spin-off P2512 / series P179 / wiki-category /
    infobox built each family) and how many generated families that cross-validation auto-promotes to
    the curated tier, the hand-curated floor, and the background regen state. Pure read; graceful when
    the catalog predates source tracking (sources fill in on the next regen) or doesn't exist yet."""
    try:
        import scripts.support.tools.generate_tv_franchises as gen_tool
        gen_path = Path(gen_tool._catalog_path())
    except Exception:
        return
    floor_path = gen_path.parent / "tv_franchises.json"

    def _load(p: Path) -> dict:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict) and "shows" in v}
        except Exception:
            return {}

    def _shows(fams: dict) -> int:
        return sum(len(v.get("shows", [])) for v in fams.values())

    def _age_of(p: Path) -> str:
        try:
            return _fmt_age(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat())
        except Exception:
            return ""

    gen   = _load(gen_path)
    floor = _load(floor_path)

    print()
    print("  TV-franchise catalog (Wikidata + Wikipedia):")
    if not gen and not floor:
        print("    (none yet - the background regen has not produced a catalog)")
        return

    if gen:
        built = _age_of(gen_path)
        suffix = f"   (built {built})" if built else ""
        print(f"    {'generated':<20}{len(gen):>4,} families  {_shows(gen):>5,} shows{suffix}")
        # corroboration: per-edge family counts + how many clear the >=2-source promotion bar
        src_labels = [("p2512", "spin-off(P2512)"), ("p179", "series(P179)"),
                      ("wiki-cat", "wiki-category"), ("infobox", "infobox")]
        per_src = {s: 0 for s, _ in src_labels}
        promoted = 0
        for v in gen.values():
            srcs = v.get("sources") or []
            for s in srcs:
                if s in per_src:
                    per_src[s] += 1
            if len(srcs) >= 2:
                promoted += 1
        if any(per_src.values()):
            parts = "   ".join(f"{lbl} {per_src[s]:,}" for s, lbl in src_labels if per_src[s])
            print(f"    {'corroboration':<20}{parts}")
            print(f"    {'promoted (>=2 src)':<20}{promoted:,} families auto-raised to the curated tier")
        else:
            print(f"    {'corroboration':<20}pending - rebuilds with edge sources on the next regen")
    if floor:
        print(f"    {'floor (curated)':<20}{len(floor):>4,} families  {_shows(floor):>5,} shows")

    # ── background regen state (last attempt + whether a regen is grinding right now) ──
    try:
        spath = _franchise_state_path()
        if spath.exists():
            st = json.loads(spath.read_text())
            last, gen_pid = st.get("last_attempt_ts"), st.get("gen_pid")
            when = _fmt_age(datetime.fromtimestamp(float(last), tz=timezone.utc).isoformat()) if last else ""
            running = bool(gen_pid and _pid_alive(int(gen_pid)))
            run_s = f"RUNNING (pid {gen_pid})" if running else "finished/idle"
            print(f"    {'regen':<20}" + (f"last attempt {when}; {run_s}" if when else run_s))
        else:
            print(f"    {'regen':<20}idle (no regen attempted yet)")
    except Exception:
        pass


def print_status() -> None:
    """Print a one-shot progress report from the cursor + on-disk cache, then return.

    Pure read: no Trakt / *arr calls and no daemon loop, so it's safe to run at any
    time — including while the supervisor-spawned daemon is mid-cycle. Mirrors the
    per-pool position/total the daemon persists plus the actual file counts per
    cache bucket (the two can differ: an item is 'complete' even when an endpoint
    returned no data, so a bucket legitimately holds fewer files than the pool size),
    and finally what the Wikidata+Wikipedia TV-franchise catalog has gathered.
    """
    # ── Liveness ──────────────────────────────────────────────────────────────
    pid = None
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
        except (ValueError, OSError):
            pid = None
    running = bool(pid and _pid_alive(pid))
    if running:
        state = f"RUNNING (pid {pid})" + (" - paused (main.py run active)" if _main_active() else "")
    elif pid:
        state = f"STOPPED (stale pid file: {pid})"
    else:
        state = "STOPPED (no pid file)"

    print("=" * 64)
    print("  Trakt Enrichment Daemon - status")
    print("=" * 64)
    print(f"  Daemon : {state}")

    # ── Per-pool progress (from the cursor) ───────────────────────────────────
    cursor = load_cursor()
    if not cursor:
        print("  Cursor : none yet (daemon has not completed a cycle)")
    else:
        newest = max((p.get("updated_at", "") for p in cursor.values()
                      if isinstance(p, dict)), default="")
        if newest:
            age = _fmt_age(newest)
            print(f"  Cursor : last written {newest}" + (f" ({age})" if age else ""))
        print()
        print(f"  {'Pool':<18}{'Position':>16}{'%':>8}   State")
        print(f"  {'-' * 18}{'-' * 16}{'-' * 8}   {'-' * 11}")
        for key in _POOL_ORDER:
            p = cursor.get(key)
            if not isinstance(p, dict):
                continue
            pos   = int(p.get("position", 0))
            total = int(p.get("total", 0))
            pct   = (pos / total * 100) if total else 0.0
            st    = "done" if (total and pos >= total) else ("pending" if pos == 0 else "in progress")
            label = _POOL_LABELS.get(key, key)
            print(f"  {label:<18}{f'{pos:,}/{total:,}':>16}{pct:>7.1f}%   {st}")

    # ── On-disk cache coverage (actual files per bucket) ──────────────────────
    print()
    print("  Cache buckets (files on disk):")
    for bdir in list(MOVIE_BUCKETS.values()) + list(SHOW_BUCKETS.values()):
        if bdir.exists():
            count = sum(1 for _ in bdir.glob("*.json.gz"))
            print(f"    {bdir.name:<18}{count:>8,}")
        else:
            print(f"    {bdir.name:<18}{'(none yet)':>10}")

    # ── Wiki-sourced TV-franchise catalog (Wikidata + Wikipedia) ──────────────
    _franchise_catalog_report()
    print("=" * 64)


# ── Per-item check (read-only) ───────────────────────────────────────────────────

def _load_daemon_library(name: str) -> list[dict]:
    """Read the daemon's cached *arr list (the title → id source). Empty on miss."""
    path = CACHE_TRAKT / f"daemon_{name}.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _bucket_report(item_id: int, buckets: dict, scope: list[str]) -> None:
    """Print per-bucket cache state (fresh / stale / not worked yet) for one item."""
    now = time.time()
    for bucket in scope:
        bdir = buckets.get(bucket)
        if bdir is None:
            continue
        path = bdir / f"{item_id}.json.gz"
        if not path.exists():
            print(f"      {bucket:<9} not worked yet")
            continue
        mtime = path.stat().st_mtime
        flag  = "fresh" if (now - mtime) <= CACHE_TTL_S else "STALE - will refetch next pass"
        when  = _fmt_age(datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat())
        extra = ""
        if bucket == "people":
            # Distinguish real credits from the {} no-data negative marker.
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and (data.get("cast") or data.get("crew")):
                    extra = f"  ({len(data.get('cast', []))} cast / {len(data.get('crew', []))} crew)"
                else:
                    extra = "  (no-data marker)"
            except Exception:
                pass
        print(f"      {bucket:<9} {flag}, written {when}{extra}")


def check_item(query: str) -> None:
    """Look up movies/shows by partial title in the daemon's cached *arr lists and
    report whether each Trakt bucket has been enriched (and is still fresh).

    Pure read — no network, no daemon loop — safe to run while the daemon is mid-cycle.
    'not worked yet' for every bucket means the item hasn't been enriched (e.g. an
    unowned + unmonitored show is never queued); a {} no-data marker means Trakt had
    nothing for that endpoint (404) and it won't be re-fetched until the TTL expires.
    """
    q      = query.strip().lower()
    movies = _load_daemon_library("radarr_movies")
    series = _load_daemon_library("sonarr_series")
    if not movies and not series:
        print("No cached *arr library yet - the daemon hasn't completed a cycle "
              "(run the app once, or use --status).")
        return

    show_hits  = [s for s in series if s.get("tvdbId") and q in (s.get("title") or "").lower()]
    movie_hits = [m for m in movies if m.get("tmdbId") and q in (m.get("title") or "").lower()]
    if not show_hits and not movie_hits:
        print(f'No movie or show in your library matches "{query}".')
        return

    print("=" * 64)
    print(f'  Enrichment check - "{query}"  ({len(show_hits)} show(s), {len(movie_hits)} movie(s))')
    print("=" * 64)

    for s in sorted(show_hits, key=lambda x: (x.get("title") or "").lower())[:25]:
        year  = s.get("year")
        tvdb  = int(s["tvdbId"])
        owned = (s.get("statistics", {}) or {}).get("episodeFileCount", 0) > 0
        print(f"\n  SHOW   {s.get('title', '?')}{f' ({year})' if year else ''}"
              f"  [tvdbId {tvdb}]  {'owned' if owned else 'unowned'}")
        _bucket_report(tvdb, SHOW_BUCKETS, SHOW_SCOPE)

    for m in sorted(movie_hits, key=lambda x: (x.get("title") or "").lower())[:25]:
        year  = m.get("year")
        tmdb  = int(m["tmdbId"])
        owned = bool(m.get("hasFile"))
        print(f"\n  MOVIE  {m.get('title', '?')}{f' ({year})' if year else ''}"
              f"  [tmdbId {tmdb}]  {'owned' if owned else 'unowned'}")
        _bucket_report(tmdb, MOVIE_BUCKETS, DEFAULT_SCOPE)

    if len(show_hits) > 25 or len(movie_hits) > 25:
        print("\n  (showing first 25 of each - narrow the query for fewer matches)")
    print("=" * 64)


# ── Main loop ────────────────────────────────────────────────────────────────────

def _interruptible_sleep(total_s: float) -> bool:
    """Sleep in small increments; return True if a stop was requested."""
    waited = 0.0
    while waited < total_s:
        if _stop_requested():
            return True
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
    return False


def main():
    parser = argparse.ArgumentParser(description="Trakt enrichment daemon")
    parser.add_argument("--once",    action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log without writing")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--show-items", action="store_true",
                        help="Log each title + the data buckets grabbed (auto-on in an interactive terminal)")
    parser.add_argument("--quiet-items", action="store_true",
                        help="Suppress per-item lines even in an interactive terminal")
    parser.add_argument("--force", action="store_true",
                        help="Start even if another instance appears to be running")
    parser.add_argument("--status", action="store_true",
                        help="Print a one-shot progress report (cursor + cache) and exit; no fetching")
    parser.add_argument("--check", metavar="TITLE", default=None,
                        help="Check whether a movie/show (partial title match) has been enriched, then exit")
    args = parser.parse_args()

    # Read-only reports: print and exit BEFORE the pid guard / daemon loop, so they
    # work while the supervisor-spawned daemon is running and never spawn a second
    # fetcher.
    if args.status:
        print_status()
        return
    if args.check:
        check_item(args.check)
        return

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Single-instance guard: refuse to start if another live daemon owns the pid
    # file, so two manual runs (or a manual run colliding with the supervisor-spawned
    # one) can't both hammer the *arr servers. --force overrides.
    if PID_PATH.exists() and not args.force:
        try:
            other = int(PID_PATH.read_text().strip())
        except (ValueError, OSError):
            other = None
        # Use the LOCAL window-free probe (ctypes OpenProcess), not the supervisor's
        # subprocess/tasklist version — the latter allocates a console window on the
        # detached daemon every startup (the documented window-flash regression).
        try:
            alive = bool(other and other != os.getpid() and _pid_alive(other))
        except Exception:
            alive = False
        if alive:
            log.error(f"Another enrich_daemon is already running (pid {other}). "
                      f"Stop it first (or pass --force to override).")
            sys.exit(1)

    # Show per-item detail when attached to a terminal (interactive / -it), or when
    # explicitly asked; stay quiet when detached (supervisor logs to a file) so the
    # logfile isn't flooded. --quiet-items forces it off.
    show_items = (not args.quiet_items) and (
        args.show_items or args.verbose or bool(getattr(sys.stdout, "isatty", lambda: False)())
    )

    # PID + sentinel lifecycle.
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        PID_PATH.write_text(str(os.getpid()))
    except OSError as e:
        log.warning(f"Could not write pid file: {e}")
    try:
        STOP_SENTINEL.unlink(missing_ok=True)   # clear any stale stop from a prior run
    except OSError:
        pass

    log.info("=" * 60)
    log.info("  Trakt Enrichment Daemon")
    log.info(f"  budget={SAFE_THROUGHPUT_CALLS} calls/cycle  sleep={SLEEP_SECONDS}s  "
             f"dry_run={args.dry_run}  pid={os.getpid()}")
    log.info("=" * 60)

    # One-shot cache hygiene: clear 0-byte poison before the first cycle so frozen
    # titles re-fetch immediately instead of waiting out the 7-day TTL.
    purge_empty_caches(dry_run=args.dry_run)

    cycle = 0
    try:
        while True:
            cycle += 1
            # Yield to an active main.py run: pause fetching so we don't compete
            # for the shared Trakt rate-limit window (the collision that caused
            # 429 storms during runs). Poll until the run finishes, the sentinel
            # goes stale, or a stop is requested.
            if _main_active():
                log.info("main.py run active - pausing enrichment until it finishes...")
                paused = 0.0
                while _main_active() and not _stop_requested():
                    _interruptible_sleep(MAIN_ACTIVE_POLL_S)
                    paused += MAIN_ACTIVE_POLL_S
                if _stop_requested():
                    log.info("Stop requested while paused - exiting.")
                    break
                log.info(f"main.py run finished (paused ~{paused:.0f}s) - resuming enrichment.")

            loader = ConfigLoader(CONFIG_PATH)       # overlays secrets from keyring/env
            cfg    = loader.load()
            cursor = load_cursor()

            log.info(f"\n-- Cycle {cycle}  {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} --")
            try:
                _stats, stop = run_cycle(cfg, loader, cursor, dry_run=args.dry_run, show_items=show_items)
                if not args.dry_run:
                    save_cursor(cursor)
                if stop:
                    log.info("Stop requested - exiting.")
                    break
            except KeyboardInterrupt:
                log.info("Interrupted - exiting.")
                break
            except Exception as e:
                log.error(f"Cycle {cycle} failed: {e}", exc_info=True)

            if args.once:
                log.info("--once flag set - exiting.")
                break

            log.info(f"Sleeping ~{SLEEP_SECONDS / 60:.1f} min until next cycle...")
            if _interruptible_sleep(SLEEP_SECONDS):
                log.info("Stop requested during sleep - exiting.")
                break
    except KeyboardInterrupt:
        log.info("Interrupted - exiting.")
    finally:
        # Diagnostic: a clean exit (stop sentinel / --once / interrupt) logs this.
        # If the daemon vanishes WITHOUT this line, it was hard-killed externally
        # (e.g. an IDE job object reaping the process tree) — see the supervisor's
        # CREATE_BREAKAWAY_FROM_JOB spawn.
        log.info(f"Daemon shutting down cleanly (after cycle {cycle}, pid {os.getpid()}).")
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()

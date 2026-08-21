"""
qBittorrent WebUI client — torrent-presence lookup for the seed gate (GLD-RST-06)
================================================================================
The ONLY question this client exists to answer:

    for these infohashes, does the client still hold the data?

That boolean is what decides whether a step-down is net positive (see
machine_learning/space/seed_gate). Everything else qBittorrent exposes is out of
scope on purpose — a client that can only read torrent state cannot be talked into
removing, pausing, or re-prioritising anything, which matters because it is being
called from a pass whose next action is a delete.

THE THREE-WAY ANSWER. ``states_for`` returns ``(records, reachable)`` rather than a
plain dict, because "the client says it does not have this hash" and "the client did
not answer" are opposite conclusions and collapsing them is the P-C failure this
codebase keeps finding (absent conflated with empty). Reachable-and-missing means
Sonarr already removed the torrent and the step-down should PROCEED; unreachable
means we know nothing and the gate must DEFER.

ONE REQUEST FOR THE WHOLE BATCH. qBittorrent's ``torrents/info`` takes a
pipe-delimited ``hashes`` list, so a pass over N candidate files costs one HTTP call,
not N. Sonarr's grab history is the expensive half of the gate; this half is free.
"""
from __future__ import annotations

import threading

try:
    import requests
except ImportError:                                     # pragma: no cover
    requests = None


class QbittorrentClient:
    """Read-only qBittorrent WebUI v2 client. Never raises; degrades to unreachable."""

    LOGIN_PATH = "/api/v2/auth/login"
    INFO_PATH = "/api/v2/torrents/info"
    TIMEOUT_S = 8

    def __init__(self, config=None, logger=None):
        self.config = config
        self.logger = logger
        self._session = None
        self._lock = threading.Lock()
        self._warned = False

    # ── configuration ────────────────────────────────────────────────────────
    def _settings(self) -> dict:
        try:
            clients = self.config.get("download_clients") if self.config else None
            qb = (clients or {}).get("qbittorrent") if isinstance(clients, dict) else None
            return qb if isinstance(qb, dict) else {}
        except (AttributeError, TypeError):
            return {}

    @property
    def enabled(self) -> bool:
        s = self._settings()
        return bool(s.get("enabled")) and bool(self._base_url())

    def _base_url(self) -> str:
        s = self._settings()
        base = str(s.get("base_url") or "").strip().rstrip("/")
        if base:
            return base
        url, port = str(s.get("url") or "").strip(), str(s.get("port") or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return f"{url.rstrip('/')}:{port}" if port else url.rstrip("/")

    # ── session ──────────────────────────────────────────────────────────────
    def _login(self):
        """An authenticated session, or None. Cached; re-established on expiry.

        qBittorrent answers a bad credential with HTTP 200 and the body ``Fails.``
        rather than a 4xx, so the body is checked explicitly — a status-only check
        would hand back a session that 403s on every later call and read as the
        client being down.
        """
        if requests is None:
            self._warn("python 'requests' is not installed — seed gate cannot reach qBittorrent.")
            return None
        base = self._base_url()
        if not base:
            return None
        s = self._settings()
        try:
            sess = requests.Session()
            resp = sess.post(
                f"{base}{self.LOGIN_PATH}",
                data={"username": s.get("username") or "", "password": s.get("password") or ""},
                timeout=self.TIMEOUT_S,
                headers={"Referer": base},          # qBittorrent enforces this
            )
            if resp.status_code != 200 or "fail" in (resp.text or "").strip().lower():
                self._warn(f"qBittorrent login rejected at {base} — seed gate will assume pinned.")
                return None
            return sess
        except Exception as e:
            self._warn(f"qBittorrent unreachable at {base} ({e}) — seed gate will assume pinned.")
            return None

    def _warn(self, msg):
        """Warn ONCE per process. This is called from inside a per-file loop, so an
        unthrottled warning would print a line per candidate and bury the run."""
        if self._warned or not self.logger:
            return
        self._warned = True
        try:
            self.logger.log_warning(f"⚠️ {msg}")
        except Exception:
            pass

    # ── the one query ────────────────────────────────────────────────────────
    def states_for(self, hashes) -> tuple:
        """``(records, reachable)`` for *hashes*.

        records   {lowercase infohash: torrent dict} — ONLY hashes the client holds.
        reachable True when the client answered at all. False means "no information",
                  which the gate must treat as pinned, NOT as released.
        """
        wanted = sorted({str(h).strip().lower() for h in (hashes or []) if h})
        if not wanted:
            return {}, True                       # nothing asked; trivially answered
        if not self.enabled:
            return {}, False                      # not configured ⇒ no information
        with self._lock:
            if self._session is None:
                self._session = self._login()
            sess = self._session
        if sess is None:
            return {}, False
        base = self._base_url()
        try:
            resp = sess.get(f"{base}{self.INFO_PATH}",
                            params={"hashes": "|".join(wanted)},
                            timeout=self.TIMEOUT_S)
            if resp.status_code == 403:
                # Session expired mid-pass. Re-login ONCE; a second 403 is a real
                # auth problem, not a stale cookie, and retrying it would loop.
                with self._lock:
                    self._session = self._login()
                    sess = self._session
                if sess is None:
                    return {}, False
                resp = sess.get(f"{base}{self.INFO_PATH}",
                                params={"hashes": "|".join(wanted)},
                                timeout=self.TIMEOUT_S)
            if resp.status_code != 200:
                self._warn(f"qBittorrent returned HTTP {resp.status_code} — seed gate will assume pinned.")
                return {}, False
            rows = resp.json()
        except Exception as e:
            self._warn(f"qBittorrent query failed ({e}) — seed gate will assume pinned.")
            return {}, False
        if not isinstance(rows, list):
            return {}, False
        out = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            h = str(r.get("hash") or r.get("infohash_v1") or "").strip().lower()
            if h:
                out[h] = r
        return out, True

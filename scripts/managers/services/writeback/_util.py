"""
_util.py — shared helpers for write-back (ID extraction, time, history fetch).
================================================================================
The Tautulli watch-history projection cached for the rest of the app is
PII-minimised and drops season/episode indices + the watched timestamp. Write-back
therefore reads raw history straight from the Tautulli API (``get_history``) and
resolves external IDs via the ``tautulli/metadata/index`` GUIDs.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def extract_id(md: dict, kind: str):
    """Pull a tvdb/tmdb (int) or imdb (str) id from a metadata-index entry."""
    if not isinstance(md, dict):
        return None
    if kind == "tmdb" and md.get("tmdb_id"):
        return md["tmdb_id"]
    candidates = list(md.get("guids") or [])
    if md.get("guid"):
        candidates.append(md["guid"])
    prefix = f"{kind}://"
    for g in candidates:
        raw = g.get("id", "") if isinstance(g, dict) else str(g)
        if raw.startswith(prefix):
            val = raw[len(prefix):]
            if kind == "imdb":
                return val or None
            return int(val) if str(val).isdigit() else None
    return None


def iso_utc(unix_ts) -> str | None:
    """Unix seconds → Trakt-style ISO-8601 UTC (e.g. 2026-01-02T03:04:05.000Z)."""
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (TypeError, ValueError, OSError):
        return None


def norm_title(title) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_history(tau_api, logger, *, length: int = 1000, max_pages: int = 20) -> list:
    """Paginate Tautulli get_history into a flat list of raw entries (all fields)."""
    out: list = []
    if tau_api is None or not hasattr(tau_api, "get_history"):
        return out
    start = 0
    for _ in range(max_pages):
        resp = tau_api.get_history(length=length, start=start)
        data = (((resp or {}).get("response") or {}).get("data") or {}) if isinstance(resp, dict) else {}
        rows = data.get("data") or []
        if not rows:
            break
        out.extend(rows)
        total = data.get("recordsFiltered") or data.get("recordsTotal") or 0
        start += length
        if start >= int(total or 0):
            break
    return out

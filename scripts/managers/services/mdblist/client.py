"""
mdblist/client.py — MDBList API auth + account-tier probe.
================================================================================
The only live call this slice makes: ``validate_key()`` GETs ``/user`` and reads the
username, the patron/supporter TIER, and the API request budget (daily limit + used).

Tolerant by design: MDBList's payload field names are read with several aliases (and a
rate-limit-header fallback) so a small API tweak doesn't break validation, and it NEVER
raises — every failure path returns ``{"ok": False, ...}`` so callers can skip gracefully.
The ``/user`` endpoint + base URL are module constants so they're trivial to adjust once a
real key confirms the exact response shape.
"""
from __future__ import annotations

BASE_URL = "https://api.mdblist.com"
USER_PATH = "/user"
MOVIE_PATH = "/tmdb/movie"   # GET /tmdb/movie/{tmdbId} → ratings incl. Common Sense age
SHOW_PATH = "/tmdb/show"     # GET /tmdb/show/{tmdbId}  → same shape for TV series

# List endpoints — return a list's items in its RANK (== defined/saga) order. Like the paths
# above, these are module constants so they're trivial to adjust once a live key confirms the
# exact route + response shape; the parser is alias-tolerant and the call NEVER raises, so a
# route/shape drift soft-degrades to ``{"ok": False, "items": []}`` (caller falls back to dates).
IMDB_LIST_PATH = "/lists/imdb/{ident}/items"     # mdblist proxy for an IMDb list (ls…)
LIST_BY_ID_PATH = "/lists/{ident}/items"          # numeric mdblist list id
LIST_BY_SLUG_PATH = "/lists/{user}/{slug}/items"  # user list, e.g. k0meta/external/15110


def _ratings(apikey: str, media_path: str, media_id, *, base_url: str, timeout: float) -> dict:
    """Shared MDBList media lookup → Common Sense / cert fields (never raises). The only
    difference between a movie and a show lookup is ``media_path`` (/tmdb/movie vs /tmdb/show)."""
    if not apikey or not media_id:
        return {"ok": False, "age_rating": None, "commonsense": None,
                "certification": None, "status": None, "error": "missing apikey/id"}
    url = f"{base_url.rstrip('/')}{media_path}/{media_id}"
    try:
        status, _headers, body = _http_get(url, {"apikey": apikey}, timeout)
    except Exception as e:                        # noqa: BLE001 — never raise to callers
        return {"ok": False, "age_rating": None, "commonsense": None,
                "certification": None, "status": None, "error": str(e)[:80]}
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "age_rating": None, "commonsense": None,
                "certification": None, "status": status, "error": f"HTTP {status}"}
    return {"ok": True, "age_rating": _int_or_none(body.get("age_rating")),
            "commonsense": body.get("commonsense"),
            "certification": (body.get("certification") or None),
            "status": status, "error": None}


def movie_ratings(apikey: str, tmdb_id, *, base_url: str = BASE_URL, timeout: float = 15.0) -> dict:
    """Look up one MOVIE by tmdbId and return its Common Sense / cert fields.

    Returns (never raises):
        {"ok": bool, "age_rating": int|None, "commonsense": bool|None,
         "certification": str|None, "status": int|None, "error": str|None}
    ``age_rating`` is the Common Sense Media recommended age (the kids signal); it is
    None when CSM has not rated the title OR the lookup failed (``ok``/``error`` tell
    them apart, so the caller can cache a real 'no-CSM' miss vs retry a transient one).
    """
    return _ratings(apikey, MOVIE_PATH, tmdb_id, base_url=base_url, timeout=timeout)


def show_ratings(apikey: str, tmdb_id, *, base_url: str = BASE_URL, timeout: float = 15.0) -> dict:
    """Look up one TV SHOW by its (show-space) tmdbId — same return shape as ``movie_ratings``.
    The TV age cache must be kept SEPARATE from the movie cache: show and movie tmdbIds share
    the same integer space, so a show id 82728 and a movie id 82728 are different titles."""
    return _ratings(apikey, SHOW_PATH, tmdb_id, base_url=base_url, timeout=timeout)


def list_items(apikey: str, ref: dict, *, base_url: str = BASE_URL,
               timeout: float = 20.0, limit: int = 2000) -> dict:
    """Fetch a universe list's items IN LIST ORDER (rank.asc = the list's defined saga order).

    ``ref`` selects the list: ``{"imdb": "ls539646485"}`` | ``{"mdblist": "k0meta/external/15110"}``
    | ``{"id": 15110}``. Returns (never raises):
        {"ok": bool, "items": [{"tmdb": int|None, "tvdb": int|None, "media": "movie"|"show"|None}],
         "error": str|None}
    Items keep the list's order. Alias-tolerant parse; any non-200/parse failure → ok=False, [] so
    the caller degrades to release/air-date ordering."""
    if not apikey or not isinstance(ref, dict) or not ref:
        return {"ok": False, "items": [], "error": "missing/invalid apikey/ref"}
    if ref.get("imdb"):
        path = IMDB_LIST_PATH.format(ident=ref["imdb"])
    elif ref.get("id") is not None:
        path = LIST_BY_ID_PATH.format(ident=ref["id"])
    elif ref.get("mdblist"):
        parts = str(ref["mdblist"]).strip("/").split("/", 1)
        path = LIST_BY_SLUG_PATH.format(user=parts[0], slug=parts[1] if len(parts) > 1 else "")
    else:
        return {"ok": False, "items": [], "error": "unrecognized ref"}
    # The fetch AND the body parse are both inside the guard — a shape-drift in the body (e.g.
    # ``{"movies": 5}``) must soft-degrade to ok=False, not raise, so _universe_source keeps LAST-GOOD.
    try:
        status, _headers, body = _http_get(f"{base_url.rstrip('/')}{path}",
                                           {"apikey": apikey, "limit": limit}, timeout)
        if status != 200:
            return {"ok": False, "items": [], "error": f"HTTP {status}"}
        items = [it for raw in _list_rows(body) if (it := _parse_list_item(raw)) is not None]
    except Exception as e:                        # noqa: BLE001 — never raise to callers
        return {"ok": False, "items": [], "error": str(e)[:80]}
    return {"ok": True, "items": items, "error": None}


def _list_rows(body) -> list:
    """mdblist returns either a bare item list OR ``{"movies": [...], "shows": [...]}`` — flatten
    movies-then-shows, preserving each section's order. A non-list ``movies``/``shows`` (shape
    drift) coerces to [] rather than blowing up ``list()``; anything else → []."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        mv, sh = body.get("movies"), body.get("shows")
        return (mv if isinstance(mv, list) else []) + (sh if isinstance(sh, list) else [])
    return []


def _parse_list_item(r) -> "dict | None":
    """One list row → ``{"tmdb", "tvdb", "media"}`` (alias-tolerant), or None if it carries no usable
    id. mdblist rows nest the cross-ids under ``ids`` (``{"tmdb","tvdb","imdb",…}``) and put the TMDB
    id in the top-level ``id`` field — NOT ``tmdb_id`` — so prefer ``ids`` then fall back to ``id``
    (a prior version read only ``tmdb_id`` → tmdb was always None → every MOVIE was misfiled as a
    show). ``media`` comes from ``mediatype``; it's only inferred from the ids when absent."""
    if not isinstance(r, dict):
        return None
    ids = r.get("ids") if isinstance(r.get("ids"), dict) else {}
    tmdb = _int_or_none(_first(ids, "tmdb")) if ids else None
    if tmdb is None:
        tmdb = _int_or_none(_first(r, "tmdb_id", "tmdbid", "tmdb", "id"))
    tvdb = _int_or_none(_first(ids, "tvdb")) if ids else None
    if tvdb is None:
        tvdb = _int_or_none(_first(r, "tvdb_id", "tvdbid", "tvdb"))
    if tmdb is None and tvdb is None:
        return None
    media = _first(r, "mediatype", "media_type", "type")
    media = str(media).strip().lower() if media else None
    if media in ("tv", "series"):
        media = "show"
    if media not in ("movie", "show") or (tmdb is None and tvdb is not None):
        # absent/odd media, OR a row with no usable tmdb but a tvdb → place by the id we actually
        # have (a tvdb-only row can only be a show; else split_list_media would silently drop it).
        media = "show" if (tvdb is not None and tmdb is None) else "movie"
    return {"tmdb": tmdb, "tvdb": tvdb, "media": media}


def validate_key(apikey: str, *, base_url: str = BASE_URL, timeout: float = 10.0) -> dict:
    """Validate an MDBList API key and read the account tier + request budget.

    Returns a dict (never raises):
        {"ok": bool, "username": str, "tier": "supporter"|"free"|"unknown",
         "limit": int|None, "used": int|None, "error": str|None, "raw": dict|None}
    ``tier`` is the patron status; ``limit``/``used`` are the daily API request budget.
    """
    if not apikey:
        return _result(False, error="no apikey")
    url = f"{base_url.rstrip('/')}{USER_PATH}"
    try:
        status, headers, body = _http_get(url, {"apikey": apikey}, timeout)
    except Exception as e:                       # noqa: BLE001 — never raise to callers
        return _result(False, error=str(e)[:80])

    if status in (401, 403):
        return _result(False, error=f"invalid key (HTTP {status})")
    if status != 200:
        return _result(False, error=f"HTTP {status}")
    if not isinstance(body, dict):
        return _result(False, error="non-JSON response")
    return _parse_user(body, headers or {})


# ── internals ────────────────────────────────────────────────────────────────────
def _http_get(url: str, params: dict, timeout: float):
    """Thin GET → (status_code, headers, json|None). Isolated so tests can stub it."""
    import requests
    r = requests.get(url, params=params, timeout=timeout)
    try:
        body = r.json()
    except Exception:                            # noqa: BLE001 — non-JSON bodies are handled by the caller
        body = None
    return r.status_code, dict(r.headers), body


def _parse_user(data: dict, headers: dict) -> dict:
    """Extract username / tier / limit / used from the /user payload (alias-tolerant),
    falling back to rate-limit response headers for the budget when the body omits it."""
    username = str(_first(data, "username", "user", "name") or "")

    # Tier: any truthy patron/supporter signal -> 'supporter'; explicit falsey -> 'free';
    # no signal at all -> 'unknown' (key is valid, tier just not reported).
    patron = _first(data, "patron_status", "patron", "patron_active", "is_supporter", "supporter")
    if patron is None:
        tier = "unknown"
    elif isinstance(patron, str):
        tier = "supporter" if patron.strip().lower() in {
            "active_patron", "active", "patron", "supporter", "true", "yes", "1",
        } else "free"
    else:
        tier = "supporter" if bool(patron) else "free"

    limit = _int_or_none(_first(data, "api_requests", "api_limit", "daily_limit", "limit", "requests"))
    used = _int_or_none(_first(data, "api_requests_count", "requests_used", "used", "api_used", "count"))
    # Header fallback (e.g. X-RateLimit-Limit / -Remaining) when the body has no budget.
    if limit is None:
        limit = _int_or_none(headers.get("X-RateLimit-Limit") or headers.get("x-ratelimit-limit"))
    if used is None and limit is not None:
        remaining = _int_or_none(headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining"))
        if remaining is not None:
            used = max(0, limit - remaining)

    return _result(True, username=username, tier=tier, limit=limit, used=used, raw=data)


def _result(ok: bool, *, username: str = "", tier: str = "unknown",
            limit=None, used=None, error: "str | None" = None, raw=None) -> dict:
    return {"ok": ok, "username": username, "tier": tier,
            "limit": limit, "used": used, "error": error, "raw": raw}


def _first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

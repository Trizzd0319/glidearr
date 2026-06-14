"""
validators.py — live connectivity checks used during onboarding.
================================================================================
Warn-and-continue: every check returns a uniform dict and never raises, so a
flaky service can never trap the user mid-setup.

    {"ok": bool, "version": str|None, "error": str|None, "root_folders": [str]}

Sonarr/Radarr reuse the exact pattern from auth_validator.py
(``arrapi.<X>API(base_url, api).system_status()``) and additionally pull
``rootfolder`` so the wizard can offer those paths as root-folder choices.
"""
from __future__ import annotations

import re

import requests

_TIMEOUT = 10


def _redact(msg: str) -> str:
    """Strip obvious API keys/tokens from an error string before display."""
    msg = re.sub(r"[Xx]-[Aa]pi-[Kk]ey[:\s]+\S+", "X-Api-Key: [REDACTED]", msg)
    msg = re.sub(r"\b[0-9a-fA-F]{32}\b", "[REDACTED_KEY]", msg)
    msg = re.sub(r"(?i)(apikey=)\S+", r"\1[REDACTED]", msg)
    # A Plex Home PIN rides as a ``?pin=`` query param on the /switch endpoint, so a
    # requests ConnectionError/Timeout (which embeds the full URL) would otherwise
    # leak it into a warning. Scrub it like any other credential.
    msg = re.sub(r"(?i)(pin=)\S+", r"\1[REDACTED]", msg)
    return msg[:140]


def _result(ok, version=None, error=None, root_folders=None):
    return {"ok": ok, "version": version, "error": error, "root_folders": root_folders or []}


# plex.tv v2 endpoints expect a device contract (DESIGN_plex_service.md "required
# contract"): token + product + version + a STABLE client-identifier. Mirror
# PlexAPI._headers exactly so a probe/verify here is the same request the runtime
# makes — otherwise the wizard could disagree with the live mint.
_PLEX_PRODUCT = "Glidearr"
_PLEX_VERSION = "1.0"


def _plex_v2_headers(token: str, client_identifier: str = "") -> dict:
    headers = {
        "X-Plex-Token": token,
        "X-Plex-Product": _PLEX_PRODUCT,
        "X-Plex-Version": _PLEX_VERSION,
        "Accept": "application/json",
    }
    if client_identifier:
        headers["X-Plex-Client-Identifier"] = client_identifier
    return headers


def arr_status(base_url: str, api_key: str, kind: str = "sonarr") -> dict:
    """Validate a Sonarr/Radarr instance and pull its root folders."""
    if not base_url or not api_key:
        return _result(False, error="missing base_url or api key")
    try:
        if kind == "radarr":
            from arrapi import RadarrAPI as _API
        else:
            from arrapi import SonarrAPI as _API
        api = _API(base_url, api_key)
        version = getattr(api.system_status(), "version", "unknown")
        roots = []
        try:
            raw = api._raw._get("rootfolder") or []
            roots = [r.get("path", "") for r in raw if isinstance(r, dict) and r.get("path")]
        except Exception:
            pass
        return _result(True, version=version, root_folders=roots)
    except Exception as e:
        return _result(False, error=_redact(str(e)))


def _base(url: str, port, default_scheme: str = "http") -> str:
    url = (url or "").strip().rstrip("/")
    port = str(port or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return f"{default_scheme}://{url}:{port}" if port else f"{default_scheme}://{url}"


def tautulli_ping(url: str, port, api_key: str, base_url: str = "") -> dict:
    """Validate Tautulli via the get_server_info command."""
    base = base_url or _base(url, port)
    if not base or not api_key:
        return _result(False, error="missing url or api key")
    try:
        resp = requests.get(
            f"{base}/api/v2",
            params={"apikey": api_key, "cmd": "get_server_info"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            body = resp.json().get("response", {})
            if body.get("result") == "success":
                ver = (body.get("data") or {}).get("pms_version") or "ok"
                return _result(True, version=ver)
            return _result(False, error=str(body.get("message") or "unsuccessful response")[:140])
        return _result(False, error=f"HTTP {resp.status_code}")
    except Exception as e:
        return _result(False, error=_redact(str(e)))


def plex_ping(url: str, port, token: str) -> dict:
    """Validate a Plex server via /identity with the X-Plex-Token."""
    base = _base(url, port)
    if not base or not token:
        return _result(False, error="missing url or token")
    try:
        resp = requests.get(
            f"{base}/identity",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            ver = ""
            try:
                ver = (resp.json().get("MediaContainer", {}) or {}).get("version", "")
            except Exception:
                pass
            return _result(True, version=ver or "ok")
        return _result(False, error=f"HTTP {resp.status_code}")
    except Exception as e:
        return _result(False, error=_redact(str(e)))


def plex_account_scope(token: str, client_identifier: str = "") -> dict:
    """Probe whether the Plex token is ACCOUNT-OWNER-scoped via plex.tv/api/v2/user.

    The captured token has only ever been used server-side (the stress-test), so its
    scope is unverified (DESIGN Q1). A server/managed-scoped token 401s here, meaning
    every per-user Plex capability degrades to owner-only — surfacing that at config
    time (not silently at run) is the whole point. ``version`` carries the resolved
    account username on success."""
    if not token:
        return _result(False, error="missing token")
    headers = _plex_v2_headers(token, client_identifier)
    try:
        resp = requests.get("https://plex.tv/api/v2/user", headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            name = ""
            try:
                body = resp.json()
                name = body.get("username") or body.get("title") or "ok"
            except Exception:
                name = "ok"
            return _result(True, version=name)
        if resp.status_code in (401, 403):
            return _result(False, error="token not account-owner-scoped (per-user features disabled)")
        return _result(False, error=f"HTTP {resp.status_code}")
    except Exception as e:
        return _result(False, error=_redact(str(e)))


def _parse_plex_home_users(data) -> list:
    """Schema-tolerant parse of the plex.tv home/users payload — mirrors
    PlexUsersManager._parse_home_users so the wizard and the runtime agree on which
    profiles are PIN-protected. Accepts ``{"users":[...]}``, ``{"Users":[...]}``, a
    ``MediaContainer.User`` list, or a bare list; drops untitled rows."""
    if isinstance(data, dict):
        raw = data.get("users") or data.get("Users") or (data.get("MediaContainer", {}) or {}).get("User") or []
    elif isinstance(data, list):
        raw = data
    else:
        raw = []
    out = []
    for u in raw:
        if not isinstance(u, dict):
            continue
        title = (u.get("title") or u.get("username") or u.get("friendlyName") or "").strip()
        if not title:
            continue
        out.append({
            # uuid is the key the /switch endpoint mints a token on — keep it so the
            # wizard can verify a PIN. Empty when the payload omits it (verify is then
            # skipped rather than guessed).
            "uuid": str(u.get("uuid") or u.get("id") or ""),
            "title": title,
            "is_admin": bool(u.get("admin") or u.get("isAdmin")),
            "is_managed": bool(u.get("restricted") or u.get("guest") or u.get("restrictedProfile")),
            "protected": bool(u.get("protected") or u.get("hasPassword")),
            # Plex parental-controls age tier (little_kid/older_kid/teen) when present —
            # seeds the onboarding age-rating default. Canonical key only (a bool
            # ``restrictedProfile`` above is the managed flag, not the tier string).
            "restriction_profile": (u.get("restrictionProfile") or u.get("restriction_profile") or None),
        })
    return out


def plex_home_users(token: str, client_identifier: str = "") -> dict:
    """List Plex Home profiles via plex.tv/api/v2/home/users (account-owner token).

    Returns ``{"ok": bool, "users": [...], "error": str|None}`` where each user is
    ``{"title", "is_admin", "is_managed", "protected"}`` (``protected`` == PIN-gated).
    Never raises — a failed fetch yields ok=False + an empty list so onboarding can
    degrade to free-text title entry. Only meaningful with an account-owner token
    (a server/managed-scoped token 401s, same as plex_account_scope)."""
    if not token:
        return {"ok": False, "users": [], "error": "missing token"}
    headers = _plex_v2_headers(token, client_identifier)
    try:
        resp = requests.get("https://plex.tv/api/v2/home/users", headers=headers, timeout=_TIMEOUT)
    except Exception as e:
        return {"ok": False, "users": [], "error": _redact(str(e))}
    if resp.status_code in (401, 403):
        return {"ok": False, "users": [], "error": "token not account-owner-scoped"}
    if resp.status_code != 200:
        return {"ok": False, "users": [], "error": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "users": [], "error": "non-JSON response"}
    return {"ok": True, "users": _parse_plex_home_users(data), "error": None}


def _extract_switch_token(body) -> str | None:
    """Pull the minted token from a /switch response — mirrors
    PlexUsersManager._extract_token EXACTLY so the wizard's success verdict matches
    what the runtime will actually mint. ``authenticationToken`` is the documented
    field (python-plexapi reads it); ``authToken`` is the v2-JSON variant; a nested
    ``user`` envelope is also tolerated. Checking only one key would falsely report a
    correct PIN as 'unverified' whenever the live shape differs."""
    if not isinstance(body, dict):
        return None
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    return (body.get("authToken") or body.get("authenticationToken")
            or body.get("authentication_token") or body.get("token")
            or user.get("authToken") or user.get("authenticationToken"))


def plex_switch_user(token: str, client_identifier: str, user_uuid: str, pin: str) -> dict:
    """Verify a Home-profile PIN by minting its per-user token — POST
    ``plex.tv/api/v2/home/users/{uuid}/switch?pin=``. This is the EXACT call the
    runtime makes (mirrors PlexAPI.switch_home_user — same method, URL, pin param,
    device headers — and PlexUsersManager._extract_token for the success test), so a
    PIN that validates here is guaranteed to mint at run-time; a wrong one is caught
    now instead of silently skipping that profile every run.

    Returns ``{"ok": bool, "rejected": bool, "error": str|None}``:
      * ok=True                    — 2xx + a token minted → PIN correct & usable.
      * rejected=True              — the PIN was refused (HTTP 401/403/400/422). A wrong
                                     PIN never 2xx's, so this is the actionable "wrong PIN"
                                     case to warn + re-prompt on.
      * ok=False, rejected=False   — inconclusive: a transient error (5xx / 404 / non-JSON
                                     / network) OR a 2xx that carried no recognizable token.
                                     Saved unverified, never punished.
    The PIN is a credential: it rides as a POST query param and is NEVER placed into
    an error string (``_redact`` also scrubs ``pin=`` from any embedded URL)."""
    if not token or not user_uuid or not pin:
        return {"ok": False, "rejected": False, "error": "missing token/uuid/pin"}
    headers = _plex_v2_headers(token, client_identifier)
    try:
        resp = requests.post(
            f"https://plex.tv/api/v2/home/users/{user_uuid}/switch",
            params={"pin": pin}, headers=headers, timeout=_TIMEOUT)
    except Exception as e:
        return {"ok": False, "rejected": False, "error": _redact(str(e))}
    # 4xx the /switch endpoint returns for a bad PIN: 401/403 (auth) and the 400/422
    # plex.tv has historically used for an invalid PIN. The device headers are now sent,
    # so a 400 here is a PIN problem, not a malformed request.
    if resp.status_code in (401, 403, 400, 422):
        return {"ok": False, "rejected": True, "error": "PIN rejected"}
    # A successful switch is any 2xx — Plex returns 201 Created (observed live), not just
    # 200; the runtime's raise_for_status passes the whole 2xx range identically.
    if not 200 <= resp.status_code < 300:
        return {"ok": False, "rejected": False, "error": f"HTTP {resp.status_code}"}
    try:
        body = resp.json()
    except Exception:
        return {"ok": False, "rejected": False, "error": "non-JSON response"}
    if _extract_switch_token(body):
        return {"ok": True, "rejected": False, "error": None}
    # 2xx means Plex ACCEPTED the PIN (a wrong PIN 4xx's) but no token was found under any
    # known key — can't confirm a usable mint, so report inconclusive rather than wrongly
    # telling the user their correct PIN was rejected.
    return {"ok": False, "rejected": False, "error": "accepted but no token in response"}

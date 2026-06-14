"""
oauth.py — shared, dependency-light OAuth helpers for Trakt AND MyAnimeList.
================================================================================
One implementation of each provider's OAuth, used by BOTH the onboarding wizard
and the runtime managers (DRY). The Trakt device-code flow + refresh originally
lived inside ``TraktInstanceManager``; they were extracted here, and the MAL
authorization-code + PKCE flow was added alongside so both providers share one
home. These functions are pure HTTP — they take credentials and return the raw
authorization dict; persisting it into config is the caller's job.

Two flows live here, one per provider:
  • Trakt — OAuth2 device-code flow (``device_flow`` / ``refresh_token`` /
    ``fetch_username``). Needs no TTY: it surfaces the verification URL + user
    code via the ``notice`` callback (and the logger), then polls. In a
    container/unraid that ``notice`` lands in the logs, the operator authorizes
    in a browser, and the poll completes — headless Trakt setup as intended.
  • MAL — OAuth2 authorization-code flow with PKCE (``mal_new_verifier`` /
    ``mal_authorize_url`` / ``mal_extract_code`` / ``mal_exchange_code`` /
    ``mal_refresh_token`` / ``mal_fetch_username``). MAL has no device flow, so
    the user authorizes in a browser and pastes the returned ``code`` back; the
    onboarding MAL step owns that paste interaction.

Consumers: onboarding ``steps/trakt.py`` + ``steps/mal.py`` (setup), and the
runtime ``trakt/instances`` + ``mal/api`` + ``mal/instances`` managers (refresh).
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

_TRAKT_BASE = "https://api.trakt.tv"
_OAUTH_TIMEOUT = 30   # seconds for token exchange / refresh
_TOKEN_TIMEOUT = 10   # seconds for the /users/me ping
DEFAULT_EXPIRES_IN = 7_776_000  # 90 days — Trakt's default token lifetime


def _log(logger, level, msg):
    fn = getattr(logger, level, None) if logger is not None else None
    if callable(fn):
        try:
            fn(msg)
        except Exception:
            pass


def auth_headers(access_token: str, client_id: str) -> dict:
    return {
        "Content-Type":      "application/json",
        "trakt-api-version": "2",
        "trakt-api-key":     client_id,
        "Authorization":     f"Bearer {access_token}",
    }


def fetch_username(access_token: str, client_id: str, *, logger=None, timeout: int = _TOKEN_TIMEOUT) -> str:
    """Single ``/users/me`` call → username, or '' on any failure."""
    if not access_token or not client_id:
        return ""
    try:
        resp = requests.get(
            f"{_TRAKT_BASE}/users/me",
            headers=auth_headers(access_token, client_id),
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json().get("username", "") or ""
    except Exception as e:
        _log(logger, "log_debug", f"[trakt-oauth] username fetch failed: {e}")
    return ""


def refresh_token(client_id: str, client_secret: str, refresh_tok: str, *, logger=None) -> dict | None:
    """Exchange a refresh token for a fresh authorization dict, or None on failure."""
    if not all([client_id, client_secret, refresh_tok]):
        return None
    try:
        resp = requests.post(
            f"{_TRAKT_BASE}/oauth/token",
            json={
                "refresh_token": refresh_tok,
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
                "grant_type":    "refresh_token",
            },
            headers={"Content-Type": "application/json"},
            timeout=_OAUTH_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _log(logger, "log_warning", f"[trakt-oauth] token refresh failed: {e}")
        return None


def device_flow(client_id: str, client_secret: str, *, logger=None, notice=None) -> dict | None:
    """Run the OAuth device-code flow. Returns an authorization dict (with
    ``created_at`` + ``expires_in`` populated) on success, else None.

    ``notice(msg)`` is called with the user-facing "visit URL / enter code"
    instruction so the caller controls how it is surfaced (terminal print and/or
    log line). Falls back to the logger if ``notice`` is None.
    """
    if not client_id or not client_secret:
        _log(logger, "log_warning", "[trakt-oauth] device flow needs client_id + client_secret.")
        return None

    def _say(msg):
        if callable(notice):
            notice(msg)
        else:
            _log(logger, "log_info", msg)

    try:
        resp = requests.post(f"{_TRAKT_BASE}/oauth/device/code", json={"client_id": client_id})
        if resp.status_code != 200:
            _log(logger, "log_error", f"[trakt-oauth] device code request failed: {resp.status_code}")
            return None

        data             = resp.json()
        device_code      = data["device_code"]
        user_code        = data["user_code"]
        verification_url = data["verification_url"]
        interval         = data.get("interval", 5)
        expires_in       = data.get("expires_in", 600)

        _say(f"Authorize Trakt: visit {verification_url} and enter code: {user_code}")
        _log(logger, "log_info", "[trakt-oauth] waiting for device authorization…")

        start = time.time()
        while time.time() - start < expires_in:
            poll = requests.post(
                f"{_TRAKT_BASE}/oauth/device/token",
                json={"code": device_code, "client_id": client_id, "client_secret": client_secret},
            )
            if poll.status_code == 200:
                token = poll.json()
                token["created_at"] = int(time.time())
                token.setdefault("expires_in", DEFAULT_EXPIRES_IN)
                _log(logger, "log_debug", "[trakt-oauth] device flow authorized — token acquired")
                return token
            if poll.status_code in (400, 428):       # pending — keep polling
                time.sleep(interval)
            elif poll.status_code == 429:            # rate limited — back off
                _log(logger, "log_warning", "[trakt-oauth] rate limited — sleeping longer")
                time.sleep(interval * 2)
            elif poll.status_code in (404, 409, 410):  # invalid / used / expired
                _log(logger, "log_error", f"[trakt-oauth] device flow error: {poll.status_code}")
                return None
            else:
                _log(logger, "log_error", f"[trakt-oauth] unexpected poll status: {poll.status_code}")
                return None

        _log(logger, "log_error", "[trakt-oauth] device flow timed out.")
        return None
    except Exception as e:
        _log(logger, "log_error", f"[trakt-oauth] exception in device flow: {e}")
        return None


# ── MyAnimeList (MAL) OAuth2 — authorization-code flow with PKCE (plain) ───────
# MAL has NO device flow: the user authorizes in a browser and pastes the returned
# ``code`` back. PKCE uses code_challenge_method=plain, so the challenge IS the
# verifier. These helpers are pure HTTP / URL building — the onboarding MAL step
# owns the browser/paste interaction (mirroring how the Trakt step drives device_flow).
_MAL_AUTH_BASE = "https://myanimelist.net/v1/oauth2"
_MAL_API_BASE  = "https://api.myanimelist.net/v2"
_OOB_URI       = "urn:ietf:wg:oauth:2.0:oob"  # MAL doesn't support OOB — treat as "use app default"
MAL_DEFAULT_EXPIRES_IN = 2_678_400            # 31 days — MAL access-token lifetime


def mal_new_verifier() -> str:
    """A PKCE code_verifier (also the challenge, since MAL uses method=plain).
    URL-safe, 43–128 chars from the unreserved set — valid per RFC 7636."""
    return secrets.token_urlsafe(96)[:128]


def mal_authorize_url(client_id: str, code_verifier: str, redirect_uri: str = "", state: str = "") -> str:
    """Build the MAL authorize URL the user opens in a browser.

    Matches MAL's documented example (and Kometa's tool): omit
    ``code_challenge_method`` (MAL defaults to plain, the only supported method),
    so ``code_challenge == code_verifier``. ``redirect_uri`` is sent whenever it is
    a real URL and MUST exactly match the app's registered App Redirect URL. The
    legacy OOB urn is NOT supported by MAL, so it is never sent.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": code_verifier,   # plain → challenge == verifier
    }
    if state:
        params["state"] = state
    if redirect_uri and redirect_uri != _OOB_URI:
        params["redirect_uri"] = redirect_uri
    return f"{_MAL_AUTH_BASE}/authorize?{urlencode(params)}"


def mal_extract_code(pasted: str) -> str:
    """Pull ``code`` from a pasted redirect URL, or return the raw code."""
    pasted = (pasted or "").strip()
    if not pasted or "code=" not in pasted:
        return pasted
    try:
        qs = parse_qs(urlparse(pasted).query)
        if qs.get("code"):
            return qs["code"][0]
    except Exception:
        pass
    return pasted.split("code=", 1)[1].split("&", 1)[0]


def _mal_token_request(data: dict, *, logger=None) -> dict | None:
    try:
        resp = requests.post(
            f"{_MAL_AUTH_BASE}/token",
            data=data,  # MAL expects application/x-www-form-urlencoded
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_OAUTH_TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json()
        token["created_at"] = int(time.time())
        token.setdefault("expires_in", MAL_DEFAULT_EXPIRES_IN)
        return token
    except Exception as e:
        _log(logger, "log_warning", f"[mal-oauth] token request failed: {e}")
        return None


def mal_exchange_code(client_id, client_secret, code, code_verifier, redirect_uri="", *, logger=None) -> dict | None:
    """Exchange an authorization code (+ PKCE verifier) for tokens."""
    if not client_id or not code or not code_verifier:
        return None
    data = {
        "client_id": client_id,
        "client_secret": client_secret or "",
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
    }
    if redirect_uri and redirect_uri != _OOB_URI:
        data["redirect_uri"] = redirect_uri
    return _mal_token_request(data, logger=logger)


def mal_refresh_token(client_id, client_secret, refresh_tok, *, logger=None) -> dict | None:
    """Exchange a MAL refresh token for a fresh authorization dict."""
    if not all([client_id, client_secret, refresh_tok]):
        return None
    return _mal_token_request({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
    }, logger=logger)


def mal_fetch_username(access_token: str, *, logger=None, timeout: int = _TOKEN_TIMEOUT) -> str:
    """Resolve the MAL username (``name``) via /users/@me, or '' on failure."""
    if not access_token:
        return ""
    try:
        resp = requests.get(
            f"{_MAL_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json().get("name", "") or ""
    except Exception as e:
        _log(logger, "log_debug", f"[mal-oauth] username fetch failed: {e}")
    return ""

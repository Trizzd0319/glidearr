"""
steps/mal.py — MyAnimeList credentials + OAuth2 (PKCE) token generation.
================================================================================
MAL uses the OAuth2 authorization-code flow with PKCE (``code_challenge_method=
plain``) — there is no device/poll flow like Trakt. So:

  * Interactive: print the authorize URL, the user approves in a browser and
    pastes back the redirected URL (or bare ``code``), then we exchange it
    (with the PKCE verifier) for tokens via the shared ``oauth`` helpers.
  * Headless: a browser paste-back isn't possible, so we refresh from a
    pre-seeded ``mal.authorization.refresh_token`` if present; otherwise the
    credentials are stored and a runtime token is expected via env.

Stores ``mal.authorization`` (tokens → keyring) and ``mal.username`` alongside
the client credentials.
"""
from __future__ import annotations

import secrets

from scripts.managers.factories.onboarding import oauth
from scripts.managers.factories.onboarding.steps.base import (
    Step, StepResult, should_configure, token_expired,
)


class MalStep(Step):
    name = "mal"
    title = "MyAnimeList"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("MyAnimeList")
        cur = cfg.get("mal", {}) or {}
        if not should_configure(prompter, "mal", "MyAnimeList",
                                default_on=bool(cur.get("client_id")),
                                probe_path="mal.client_id"):
            return [StepResult("mal", ok=None, detail="skipped", skipped=True)]

        prompter.notice("Create a MAL API app at https://myanimelist.net/apps/api/clients (App Type: web).")
        cid = prompter.secret("mal.client_id", "MAL client_id",
                              default=cur.get("client_id", ""), required=True)
        csec = prompter.secret("mal.client_secret", "MAL client_secret (blank for public apps)",
                               default=cur.get("client_secret", ""), required=False)
        # Never offer the OOB urn as a default — MAL doesn't support it.
        cur_redirect = cur.get("redirect_uri")
        if cur_redirect in (None, "", "urn:ietf:wg:oauth:2.0:oob"):
            cur_redirect = "http://localhost/oauth"
        redirect = prompter.text("mal.redirect_uri",
                                 "MAL App Redirect URL — register this EXACT value in your MAL app",
                                 default=cur_redirect, required=False)

        auth = dict(cur.get("authorization", {}) or {})
        mal = {
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect,
            "authorization": auth,
            "username": cur.get("username", ""),
        }
        cfg["mal"] = mal

        if not cid:
            prompter.warn("   MAL client_id missing — skipping OAuth.")
            return [StepResult("mal", ok=None, detail="credentials missing", skipped=True)]

        token = self._obtain_token(prompter, cid, csec, redirect, auth)
        if token:
            mal["authorization"] = token
            uname = oauth.mal_fetch_username(token.get("access_token", ""), logger=self.logger)
            if uname:
                mal["username"] = uname

        if mal["authorization"].get("access_token"):
            prompter.success(f"   MAL authorized as {mal.get('username') or '?'}")
            return [StepResult("mal", ok=True, detail=mal.get("username") or "authorized")]
        prompter.notice("   MAL credentials saved (not authorized).")
        return [StepResult("mal", ok=None, detail="saved (not authorized)")]

    def _obtain_token(self, prompter, cid, csec, redirect, auth):
        """Keep a valid token, else refresh, else (interactive) run the PKCE flow."""
        have_valid = bool(auth.get("access_token")) and not token_expired(auth)
        if have_valid:
            if not prompter.is_interactive or prompter.confirm(
                    "mal.keep_token", "Existing MAL token looks valid — keep it?", default=True):
                return auth

        if auth.get("refresh_token"):
            prompter.notice("Refreshing existing MAL token…")
            refreshed = oauth.mal_refresh_token(cid, csec, auth["refresh_token"], logger=self.logger)
            if refreshed:
                return refreshed

        if prompter.is_interactive:
            # Escape hatch: let the user paste a token generated elsewhere (e.g. the
            # hosted Kometa MAL OAuth tool) instead of doing the in-app browser flow.
            pasted = prompter.text(
                "mal.paste_refresh_token",
                "Paste an existing MAL refresh token (e.g. from the Kometa MAL OAuth tool) "
                "— or leave blank to authorize in your browser",
                default="", required=False)
            if pasted:
                refreshed = oauth.mal_refresh_token(cid, csec, pasted.strip(), logger=self.logger)
                if refreshed:
                    return refreshed
                prompter.warn("   Could not exchange that refresh token — falling back to browser authorization.")
            return self._authorize_interactive(prompter, cid, csec, redirect)
        return auth if have_valid else None

    def _authorize_interactive(self, prompter, cid, csec, redirect):
        verifier = oauth.mal_new_verifier()
        state = secrets.token_urlsafe(16)
        url = oauth.mal_authorize_url(cid, verifier, redirect_uri=redirect, state=state)
        prompter.notice("Authorize MyAnimeList in your browser:")
        prompter.notice(f"  1) In your MAL app settings, the 'App Redirect URL' must be EXACTLY: {redirect}")
        prompter.notice(f"  2) Open this URL and click Allow:\n     {url}")
        prompter.notice(f"  3) Your browser will redirect to '{redirect}?code=...' and likely show a "
                        "'can't reach this page' error — that's expected.")
        prompter.notice("  4) Copy the FULL URL from the address bar and paste it below.")
        pasted = prompter.text("mal.auth_code", "Paste the redirected URL (or just the code)", required=True)
        code = oauth.mal_extract_code(pasted)
        if not code:
            prompter.warn("   No authorization code detected in what you pasted.")
            return None
        token = oauth.mal_exchange_code(cid, csec, code, verifier, redirect_uri=redirect, logger=self.logger)
        if not token:
            prompter.warn("   MAL token exchange failed (check client_id/secret and that the "
                          "redirect URL matches exactly).")
        return token

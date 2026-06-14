"""
steps/trakt.py — Trakt credentials + OAuth token generation.
================================================================================
Collects client_id/client_secret, then generates the OAuth token via the shared
``oauth`` helper: refresh an existing token if a refresh_token is present,
otherwise run the device-code flow (works on a TTY and headless — the device code
is surfaced through the prompter, which routes to logs in a container). Finally
resolves the Trakt username from /users/me.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding import oauth
from scripts.managers.factories.onboarding.steps.base import Step, StepResult, token_expired


class TraktStep(Step):
    name = "trakt"
    title = "Trakt"

    def run(self, prompter, cfg, ctx):
        prompter.section("Trakt")
        trakt = cfg.setdefault("trakt", {})
        auth = trakt.setdefault("authorization", {})

        prompter.notice("Create a Trakt API app at https://trakt.tv/oauth/applications "
                        "(redirect uri: urn:ietf:wg:oauth:2.0:oob)")
        cid = prompter.secret("trakt.client_id", "Trakt client_id",
                              default=trakt.get("client_id", ""), required=True)
        csec = prompter.secret("trakt.client_secret", "Trakt client_secret",
                               default=trakt.get("client_secret", ""), required=True)
        trakt["client_id"] = cid
        trakt["client_secret"] = csec

        if not cid or not csec:
            prompter.warn("   Trakt client credentials missing — skipping OAuth.")
            return [StepResult("trakt", ok=None, detail="credentials missing", skipped=True)]

        # Decide whether to (re)authorize.
        have_valid = bool(auth.get("access_token")) and not token_expired(auth)
        do_oauth = True
        if have_valid:
            do_oauth = prompter.is_interactive and not prompter.confirm(
                "trakt.keep_token", "Existing Trakt token looks valid — keep it?", default=True)

        if do_oauth:
            new_auth = None
            if auth.get("refresh_token"):
                prompter.notice("Refreshing existing Trakt token…")
                new_auth = oauth.refresh_token(cid, csec, auth["refresh_token"], logger=self.logger)
            if not new_auth:
                new_auth = oauth.device_flow(cid, csec, logger=self.logger, notice=prompter.notice)
            if new_auth:
                trakt["authorization"] = new_auth
                auth = new_auth

        token = (trakt.get("authorization") or {}).get("access_token", "")
        if token:
            uname = oauth.fetch_username(token, cid, logger=self.logger)
            if uname:
                trakt["username"] = uname

        ok = bool((trakt.get("authorization") or {}).get("access_token"))
        if ok:
            detail = trakt.get("username") or "authorized"
            prompter.success(f"   Trakt authorized as {trakt.get('username') or '?'}")
        else:
            detail = "credentials saved, not authorized"
            prompter.warn("   Trakt not authorized — credentials saved; re-run to authorize.")
        return [StepResult("trakt", ok=ok, detail=detail)]

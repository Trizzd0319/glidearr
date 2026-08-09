"""
steps/trakt.py — Trakt credentials + OAuth token generation + write-back choices.
================================================================================
Collects client_id/client_secret, then generates the OAuth token via the shared
``oauth`` helper: refresh an existing token if a refresh_token is present,
otherwise run the device-code flow (works on a TTY and headless — the device code
is surfaced through the prompter, which routes to logs in a container). Resolves
the Trakt username from /users/me.

Finally (only once authorized) asks what to push BACK — see :meth:`TraktStep._writeback`.
Trakt's COLLECTION (owned) and HISTORY (watched) are separate lists and the operator
is asked about them separately, because "mirror my library" and "record what we
watched" are different intentions with very different blast radii.
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
            self._writeback(prompter, cfg)
        else:
            detail = "credentials saved, not authorized"
            prompter.warn("   Trakt not authorized — credentials saved; re-run to authorize.")
        return [StepResult("trakt", ok=ok, detail=detail)]

    # ── write-back (push local state OUT to Trakt) ───────────────────────────
    @staticmethod
    def _writeback(prompter, cfg) -> None:
        """Ask what, if anything, glidearr should push BACK to Trakt.

        Trakt keeps two independent facts about a title and this step is the only place
        the difference is explained to the operator:

          COLLECTION  "I own this"     -> sync/collection
          HISTORY     "I watched this" -> sync/history

        They are not the same list, and the honest default for both is OFF: this writes to
        the operator's real Trakt account, and a first collection push can add thousands of
        titles at once. Everything asked here is still gated by ``dry_run`` at run time, so
        an armed config previews ("would add N") before it ever writes.
        """
        tw = cfg.setdefault("trakt_writeback", {})
        prompter.notice("Trakt write-back — push YOUR library/history OUT to your Trakt account.")
        prompter.notice("   Trakt tracks two separate things: your COLLECTION (what you own) "
                        "and your HISTORY (what you have watched).")

        if not prompter.confirm("trakt_writeback.enabled",
                                "Push anything from this library back to Trakt?",
                                default=bool(tw.get("enabled", False))):
            tw["enabled"] = False
            prompter.notice("   Trakt write-back off — glidearr will only READ from Trakt.")
            return
        tw["enabled"] = True

        # ── COLLECTION (owned) ──────────────────────────────────────────
        prompter.notice("   COLLECTION marks titles as OWNED on Trakt. Your whole library is "
                        "usually thousands of items on a first push.")
        if prompter.confirm("trakt_writeback.collection",
                            "   Mark titles as OWNED in your Trakt collection?",
                            default=bool(tw.get("collection", True))):
            tw["collection"] = True
            prompter.notice("      Whole library = everything Sonarr/Radarr holds. "
                            "Watched-only = just the titles this household has actually watched.")
            tw["collection_watched_only"] = prompter.confirm(
                "trakt_writeback.collection_watched_only",
                "      Only mark WATCHED titles as owned? (No = mark the whole library)",
                default=bool(tw.get("collection_watched_only", False)))
        else:
            tw["collection"] = False
            tw["collection_watched_only"] = False

        # ── HISTORY (watched) ──────────────────────────────────────────
        prompter.notice("   HISTORY pushes episode/film PLAYS from Tautulli, so Trakt reflects "
                        "what the household actually watched (independent of ownership).")
        tw["history"] = prompter.confirm(
            "trakt_writeback.history", "   Push watched history to Trakt?",
            default=bool(tw.get("history", True)))

        # MAL is asked in its OWN step (steps/mal.py), not here. AccountsStep orders its
        # members [TraktStep, MalStep, ...], so on a first run MAL has no client_id yet
        # when this step runs -- a MAL question gated on that would never fire. And
        # `--service mal` must be able to reach it.

        _bits = []
        if tw.get("collection"):
            _bits.append("collection (watched only)" if tw.get("collection_watched_only")
                         else "collection (whole library)")
        if tw.get("history"):
            _bits.append("watched history")
        prompter.success("   Trakt write-back: " + (", ".join(_bits) if _bits else "nothing selected"))
        prompter.notice("   Nothing is written while dry_run is true — the run logs "
                        "'would add N' first so you can check the number.")

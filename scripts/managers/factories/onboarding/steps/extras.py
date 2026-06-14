"""
steps/extras.py — TVDB credentials (optional, store-only).
================================================================================
TVDB is collected but NOT live-validated here: it needs a separate login exchange
to mint a token, which the runtime handles. Onboarding just stores the
credentials (secrets → keyring). MAL has its own step (steps/mal.py) since it
performs a real OAuth2/PKCE flow.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult, should_configure


class TvdbStep(Step):
    name = "tvdb"
    title = "TVDB"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("TVDB")
        cur = cfg.get("tvdb", {}) or {}
        if not should_configure(prompter, "tvdb", "TVDB",
                                default_on=bool(cur.get("api")),
                                probe_path="tvdb.api"):
            return [StepResult("tvdb", ok=None, detail="skipped", skipped=True)]

        api = prompter.secret("tvdb.api", "TVDB API key", default=cur.get("api", ""), required=True)
        pin = prompter.secret("tvdb.pin", "TVDB subscriber PIN (optional)", default=cur.get("pin", ""), required=False)
        cfg["tvdb"] = {"api": api, "pin": pin, "token": cur.get("token", "")}
        prompter.notice("   TVDB credentials saved (validated at runtime).")
        return [StepResult("tvdb", ok=None, detail="saved (not live-checked)")]

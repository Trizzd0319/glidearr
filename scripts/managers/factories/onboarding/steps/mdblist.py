"""
steps/mdblist.py — MDBList API key (optional). Live-validates + shows the account tier.
================================================================================
MDBList (mdblist.com) aggregates ratings + hosts lists. The whole integration is
OPT-IN: skip here and nothing in the runtime touches MDBList. When a key IS given we
validate it against ``/user`` and surface the account TIER + request budget, so the
operator sees their rate ceiling at setup time. ``apikey`` -> keyring (secret).
"""
from __future__ import annotations

from scripts.managers.factories.onboarding.steps.base import Step, StepResult, should_configure


class MdblistStep(Step):
    name = "mdblist"
    title = "MDBList"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("MDBList")
        cur = cfg.get("mdblist", {}) or {}
        if not should_configure(prompter, "mdblist", "MDBList (aggregated ratings + lists)",
                                default_on=bool(cur.get("apikey")),
                                probe_path="mdblist.apikey"):
            return [StepResult("mdblist", ok=None, detail="skipped", skipped=True)]

        api = prompter.secret("mdblist.apikey", "MDBList API key (mdblist.com/preferences)",
                              default=cur.get("apikey", ""), required=True)
        cfg["mdblist"] = {"apikey": api}

        # Live-validate + surface the account tier / request budget now (interactive only).
        if api and prompter.is_interactive:
            try:
                from scripts.managers.services.mdblist.client import validate_key
                r = validate_key(api)
            except Exception as e:                       # noqa: BLE001
                prompter.warn(f"   MDBList validation skipped: {e}")
                return [StepResult("mdblist", ok=None, detail="saved (not live-checked)")]
            if r.get("ok"):
                budget = ""
                if r.get("limit") is not None:
                    used = r.get("used")
                    budget = f", {used if used is not None else '?'}/{r['limit']}/day"
                prompter.success(f"   MDBList OK: {r.get('username') or '?'} (tier={r.get('tier')}{budget})")
                return [StepResult("mdblist", ok=True, detail=f"tier={r.get('tier')}")]
            prompter.warn(f"   MDBList key not validated: {r.get('error')} — saved, fix later")
            return [StepResult("mdblist", ok=False, detail=r.get("error") or "validation failed")]

        prompter.notice("   MDBList key saved (validated at runtime).")
        return [StepResult("mdblist", ok=None, detail="saved")]

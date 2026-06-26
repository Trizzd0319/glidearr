"""
steps/daemons.py — background daemons (optional).
================================================================================
Asks whether to run the standalone Trakt enrichment daemon. When enabled, main.py
(re)spawns ``scripts/support/daemons/enrich_daemon.py`` on each launch and the run itself becomes
cache-only (no live Trakt calls → it can never hang on a 429). The daemon
pre-fetches every Trakt endpoint in ``scope`` for in-library (owned) movies first.

Also configures the pilot-search daemon (``daemons.pilot_search.*``, ON by default), which
drains LARGE Sonarr pilot interactive-search batches (> threshold stubs) out of the run process
so a massive search spree can never hang the run; smaller batches stay in-process.

``daemons.*`` are NOT secrets, so they persist to config.json plaintext.
"""
from __future__ import annotations

from scripts.managers.factories.daemons.daemon_paths import DEFAULT_SCOPE
from scripts.managers.factories.onboarding.steps.base import (
    Step, StepResult, csv_field, should_configure,
)


class DaemonsStep(Step):
    name = "daemons"
    title = "Background enrichment daemon"
    optional = True

    def run(self, prompter, cfg, ctx):
        prompter.section("Background enrichment daemon")
        block = cfg.setdefault("daemons", {}).setdefault(
            "enrich", {"enabled": False, "scope": [], "owned_first": True}
        )

        if not should_configure(
            prompter, "daemons.enrich", "the background Trakt enrichment daemon",
            default_on=bool(block.get("enabled")),
            probe_path="daemons.enrich.enabled",
        ):
            return [StepResult("daemons", ok=None, detail="skipped", skipped=True)]

        prompter.notice(
            "   The daemon pre-fetches Trakt metadata in the background so main runs "
            "stay fast and never stall on Trakt's rate limit. Recommended for large libraries."
        )
        block["enabled"] = prompter.confirm(
            "daemons.enrich.enabled",
            "Enable the background enrichment daemon (main runs become cache-only)?",
            default=bool(block.get("enabled")),
        )
        block["owned_first"] = prompter.confirm(
            "daemons.enrich.owned_first",
            "Enrich in-library (owned) movies before unowned ones?",
            default=bool(block.get("owned_first", True)),
        )
        block["scope"] = csv_field(
            prompter, "daemons.enrich.scope",
            "Trakt data buckets to fetch per movie (comma-separated)",
            block.get("scope") or [],
            suggestions=list(DEFAULT_SCOPE) + ["lists"],
            fallback=list(DEFAULT_SCOPE),
        )

        # ── Pilot-search daemon (ON by default — keeps big search sprees off the run) ──
        pilot = cfg.setdefault("daemons", {}).setdefault(
            "pilot_search", {"enabled": True, "threshold": 10}
        )
        prompter.notice(
            "   The pilot-search daemon drains LARGE Sonarr pilot interactive-search batches "
            "(more than the threshold below) into a background process, so a run that finds "
            "thousands of missing pilots never hangs while every indexer is searched. Smaller "
            "batches still run inside the run. Recommended ON."
        )
        pilot["enabled"] = prompter.confirm(
            "daemons.pilot_search.enabled",
            "Offload large pilot-search batches to the background daemon?",
            default=bool(pilot.get("enabled", True)),
        )
        if pilot["enabled"]:
            pilot["threshold"] = prompter.integer(
                "daemons.pilot_search.threshold",
                "Spill to the daemon when a batch exceeds this many stub pilots",
                default=int(pilot.get("threshold", 10) or 10),
            )

        detail = "enabled" if block["enabled"] else "configured (disabled)"
        detail += f"; pilot_search {'on' if pilot['enabled'] else 'off'}"
        return [StepResult("daemons", ok=bool(block["enabled"]), detail=detail)]

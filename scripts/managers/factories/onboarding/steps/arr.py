"""
steps/arr.py — Sonarr & Radarr session collection.
================================================================================
Handles the user-defined number of sessions ("# of sessions for sonarr/radarr").
Interactive: asks how many sessions, then loops collecting host/port/api per
session. Headless: ``RECOMMENDARR_<SERVICE>_INSTANCE_NAMES`` drives the loop and
each field comes from ``RECOMMENDARR_<SERVICE>_INSTANCES_<NAME>_<FIELD>``.

Writes the LIVE shape:
    {"default_instance": {"name": <name>}, "<name>": {url, port, api, base_url}}

Each session is validated via arrapi ``system_status`` (warn-and-continue) and its
root folders are stashed in ``ctx`` for the Library step to offer as choices.
"""
from __future__ import annotations

from scripts.managers.factories.onboarding import env_map, schema, validators
from scripts.managers.factories.onboarding.steps.base import Step, StepResult, host_field


class ArrStep(Step):
    service = "sonarr"     # overridden by subclasses
    kind = "sonarr"        # arrapi client kind
    categorized = False    # overridden per-subclass: Radarr=True (categorized), Sonarr=False (single-instance)
    categorize_labels = ("720p", "1080p", "4K")   # tier role labels (Radarr only); subclasses may add "anime"

    def run(self, prompter, cfg, ctx):
        service = self.service
        prompter.section(f"{service.capitalize()} sessions")
        instances = cfg.setdefault(f"{service}_instances", {"default_instance": {"name": ""}})
        existing_names = [k for k, v in instances.items()
                          if k != "default_instance" and isinstance(v, dict)]

        names = self._session_names(prompter, service, existing_names)

        results = []
        for nm in names:
            cur = instances.get(nm) if isinstance(instances.get(nm), dict) else {}
            res, block = self._collect_one(prompter, service, nm, cur, ctx)
            instances[nm] = block
            if res["ok"]:
                ctx.setdefault("root_folders", []).extend(res.get("root_folders", []))
                prompter.success(f"   {service}[{nm}] OK (v{res['version']})")
                results.append(StepResult(f"{service}:{nm}", ok=True, detail=f"v{res['version']}"))
            else:
                prompter.warn(f"   {service}[{nm}] not reachable: {res['error']} — saved, fix later")
                results.append(StepResult(f"{service}:{nm}", ok=False, detail=res["error"] or "unreachable"))

        if names:
            instances["default_instance"] = {"name": self._default_name(prompter, service, names, instances)}
            if self.categorized:
                self._collect_categorized(prompter, cfg, names)
        else:
            results.append(StepResult(service, ok=None, detail="no sessions configured", skipped=True))
        return results

    # ── helpers ───────────────────────────────────────────────────────────────
    def _session_names(self, prompter, service, existing_names):
        if not prompter.is_interactive:
            return env_map.instance_names(service) or existing_names
        default_count = len(existing_names) or 1
        count = max(0, prompter.integer(
            f"{service}.session_count",
            f"How many {service.capitalize()} sessions/instances?",
            default=default_count, required=True))
        names = []
        for i in range(count):
            dn = existing_names[i] if i < len(existing_names) else ""
            nm = prompter.text(
                f"{service}.session_name.{i}",
                f"  Label for {service.capitalize()} session #{i + 1} (e.g. 720, 1080, 4k)",
                default=dn, required=True)
            names.append(nm)
        return names

    def _collect_one(self, prompter, service, nm, cur, ctx):
        host = host_field(prompter, ctx, f"{service}_instances.{nm}.url",
                          f"  [{nm}] host or IP (or full URL)", default=cur.get("url", ""))
        port = prompter.text(f"{service}_instances.{nm}.port",
                             f"  [{nm}] port",
                             default=str(cur.get("port", "")), required=False)
        api = prompter.secret(f"{service}_instances.{nm}.api",
                              f"  [{nm}] API key",
                              default=cur.get("api", ""), required=True)
        block = schema.instance_block(host, port, api)
        res = validators.arr_status(block["base_url"], api, kind=self.kind)

        # One interactive retry on a failed check (mirrors the runtime's
        # _handle_interactive_correction UX: re-enter host/port/api, retry once).
        if not res["ok"] and prompter.is_interactive and (host or api):
            prompter.warn(f"   {service}[{nm}] check failed: {res['error']}")
            if prompter.confirm(f"{service}.{nm}.retry", "   Re-enter details and retry?", default=True):
                host = host_field(prompter, ctx, f"{service}_instances.{nm}.url",
                                  f"  [{nm}] host or IP (or full URL)", default=block.get("url", ""))
                port = prompter.text(f"{service}_instances.{nm}.port",
                                     f"  [{nm}] port",
                                     default=str(block.get("port", "")), required=False)
                api = prompter.secret(f"{service}_instances.{nm}.api",
                                      f"  [{nm}] API key",
                                      default=block.get("api", ""), required=True)
                block = schema.instance_block(host, port, api)
                res = validators.arr_status(block["base_url"], api, kind=self.kind)
        return res, block

    def _default_name(self, prompter, service, names, instances):
        cur_default = ""
        di = instances.get("default_instance")
        if isinstance(di, dict):
            cur_default = str(di.get("name") or "")
        if prompter.is_interactive and len(names) > 1:
            return prompter.choice(f"{service}_instances.default_instance.name",
                                   f"Which {service.capitalize()} session is the default?",
                                   options=names, default=(cur_default if cur_default in names else names[0]))
        env_default = env_map.get_env(f"{service}_instances.default_instance.name")
        chosen = env_default or cur_default or names[0]
        return chosen if chosen in names else names[0]

    def _collect_categorized(self, prompter, cfg, names):
        """Map tier labels (self.categorize_labels) → instance names, into
        ``<service>_instances_categorized`` — consumed by acquisition's
        ``gateway.categorized_instance`` to route new content to the right session.
        Radarr-only now: writes radarr_instances_categorized (resolution tiers + optional anime).
        Sonarr is single-instance (SonarrStep.categorized=False) so it never reaches this."""
        svc = self.service
        key = f"{svc}_instances_categorized"
        cat = cfg.setdefault(key, {})
        labels = self.categorize_labels
        if not prompter.is_interactive:
            for label in labels:
                v = env_map.get_env(f"{key}.{label}")
                if v:
                    cat[label] = v
            return
        prompter.notice(f"   Tier categories tell the app which {svc.capitalize()} session holds each tier")
        prompter.notice(f"   ({' / '.join(labels)}), so new content is routed — and tier-aware upgrades /")
        prompter.notice("   repair run — against the right instance. Skip any tier you don't split out.")
        if not prompter.confirm(f"{svc}.categorize",
                                f"   Map tiers ({'/'.join(labels)}) to your {svc.capitalize()} sessions?",
                                default=bool(cat)):
            return
        SKIP = "— skip —"
        for label in labels:
            guess = next((n for n in names if str(n).lower() in str(label).lower()), None)
            default = cat.get(label) or guess or (SKIP if label == "anime" else names[0])
            chosen = prompter.choice(f"{key}.{label}",
                                     f"   Which session holds {label} content?",
                                     options=list(names) + [SKIP], default=default)
            if chosen and chosen != SKIP:
                cat[label] = chosen
            else:
                cat.pop(label, None)


class SonarrStep(ArrStep):
    name = "sonarr"
    title = "Sonarr"
    service = "sonarr"
    kind = "sonarr"
    # Single-instance: Sonarr no longer splits by resolution tier, so onboarding does NOT
    # collect sonarr_instances_categorized. (Radarr keeps categorized=True for standard/ultra.)
    categorized = False


class RadarrStep(ArrStep):
    name = "radarr"
    title = "Radarr"
    service = "radarr"
    kind = "radarr"
    # Capture each Radarr instance's ROLE so acquisition routes movies by tier (mirrors Sonarr).
    # Writes radarr_instances_categorized; consumed by gateway.categorized_instance. Resolution
    # tiers + an optional dedicated anime instance (see radarr/README.md multi-instance routing).
    categorized = True
    categorize_labels = ("720p", "1080p", "4K", "anime")

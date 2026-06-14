"""
adder.py — build the *arr add payload and (optionally) POST it.
================================================================================
Builds a Sonarr v4 / Radarr add payload from the resolved lookup object and adds
it **monitored, search OFF** by default (per the configured policy). Writes are
gated: nothing is POSTed when ``dry_run`` is set — the candidate is reported as
"would-add" instead.
"""
from __future__ import annotations


class Adder:
    def __init__(self, gateways: dict, logger, *, dry_run: bool,
                 monitored: bool = True, search: bool = False):
        self.gw = gateways
        self.logger = logger
        self.dry_run = dry_run
        self.monitored = monitored
        self.search = search

    def build_payload(self, enriched: dict, *, search: "bool | None" = None) -> dict:
        eff_search = self.search if search is None else bool(search)
        obj = dict(enriched.get("lookup") or {})
        obj["qualityProfileId"] = enriched["quality_profile"]["id"]
        obj["rootFolderPath"] = enriched["root_folder"]
        obj["monitored"] = self.monitored
        obj.setdefault("tags", obj.get("tags", []) or [])

        if enriched.get("type") == "show":
            obj["seasonFolder"] = True
            obj["seriesType"] = "anime" if enriched.get("is_anime") else obj.get("seriesType", "standard")
            obj["addOptions"] = {
                "monitor": "all" if self.monitored else "none",
                "searchForMissingEpisodes": eff_search,
                "searchForCutoffUnmetEpisodes": False,
            }
            obj.pop("languageProfileId", None)  # removed in Sonarr v4
        else:
            obj.setdefault("minimumAvailability", "released")
            obj["addOptions"] = {"searchForMovie": eff_search}
        return obj

    def add(self, enriched: dict, *, search: "bool | None" = None) -> dict:
        """Add the title. ``search`` overrides the instance default (used to force
        search OFF when deferring under space pressure); None = use ``self.search``."""
        eff_search = self.search if search is None else bool(search)
        is_show = enriched.get("type") == "show"
        gw = self.gw.get("sonarr" if is_show else "radarr")
        payload = self.build_payload(enriched, search=eff_search)
        title = enriched.get("title") or enriched.get("ext_id")
        inst = enriched.get("instance")

        if self.dry_run:
            self.logger.log_info(
                f"[acquire] dry_run — would add '{title}' to {inst} "
                f"(profile={enriched['quality_profile'].get('name')}, "
                f"monitored={self.monitored}, search={eff_search})"
            )
            return {"action": "would-add", "ok": True}

        try:
            result = gw.add(inst, payload)
            ok = bool(result)
            self.logger.log_success(f"[acquire] added '{title}' to {inst}") if ok else \
                self.logger.log_warning(f"[acquire] add returned empty for '{title}' on {inst}")
            return {"action": "added" if ok else "add-failed", "ok": ok, "result": result}
        except Exception as e:
            self.logger.log_warning(f"[acquire] add failed for '{title}': {e}")
            return {"action": "add-failed", "ok": False, "error": str(e)[:120]}

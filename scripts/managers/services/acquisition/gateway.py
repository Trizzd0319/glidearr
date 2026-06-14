"""
gateway.py — thin, cached access to a Sonarr/Radarr instance for acquisition.
================================================================================
Wraps a service's ``instance_manager`` (which exposes ``_make_request`` /
``resolve_instance`` from BaseInstanceManager) with the handful of reads the
acquisition pipeline needs — library id-sets (for dedup), quality profiles, root
folders, quality definitions (for size estimates) and metadata lookup.

Everything is memoised per instance for the run. The full-library GET ("series"/
"movie") rides the BaseInstanceManager run-scoped snapshot cache, so dedup is one
fetch per instance regardless of how many candidates we test.
"""
from __future__ import annotations

from urllib.parse import quote


class ArrGateway:
    def __init__(self, service: str, instance_manager, config, logger):
        self.service = service            # "sonarr" | "radarr"
        self.im = instance_manager
        self.config = config
        self.logger = logger
        self._lib_ids: dict = {}
        self._lib_items: dict = {}
        self._qp: dict = {}
        self._rf: dict = {}
        self._qd: dict = {}

    @property
    def available(self) -> bool:
        return self.im is not None and hasattr(self.im, "_make_request")

    def _req(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if not self.available:
            return fallback
        return self.im._make_request(inst, endpoint, method=method, payload=payload, fallback=fallback)

    def resolve(self, inst):
        if self.im and hasattr(self.im, "resolve_instance"):
            try:
                return self.im.resolve_instance(inst)
            except Exception:
                pass
        return inst

    # ── instance selection ────────────────────────────────────────────────────
    def default_instance(self) -> str:
        insts = self.config.get(f"{self.service}_instances", {}) or {}
        di = insts.get("default_instance")
        name = di.get("name") if isinstance(di, dict) else di
        if name and str(name) in insts:
            return str(name)
        for k, v in insts.items():
            if k != "default_instance" and isinstance(v, dict):
                return k
        return "default"

    def categorized_instance(self, label: str = "1080p") -> str:
        cat = self.config.get(f"{self.service}_instances_categorized", {}) or {}
        chosen = cat.get(label)
        insts = self.config.get(f"{self.service}_instances", {}) or {}
        if chosen and str(chosen) in insts:
            return str(chosen)
        return self.default_instance()

    # ── cached reads ──────────────────────────────────────────────────────────
    def _library(self, inst):
        endpoint = "series" if self.service == "sonarr" else "movie"
        return self._req(inst, endpoint, fallback=[]) or []

    def library_ids(self, inst, id_field: str) -> set:
        inst = self.resolve(inst)
        cache_key = (inst, id_field)
        if cache_key not in self._lib_ids:
            items = self._library(inst)
            self._lib_ids[cache_key] = {
                str(i.get(id_field)) for i in items
                if isinstance(i, dict) and i.get(id_field)
            }
        return self._lib_ids[cache_key]

    def in_library(self, inst, id_field: str, value) -> bool:
        return value is not None and str(value) in self.library_ids(inst, id_field)

    def library_items(self, inst) -> list:
        inst = self.resolve(inst)
        if inst not in self._lib_items:
            self._lib_items[inst] = self._library(inst)
        return self._lib_items[inst]

    def quality_profiles(self, inst) -> list:
        inst = self.resolve(inst)
        if inst not in self._qp:
            self._qp[inst] = self._req(inst, "qualityprofile", fallback=[]) or []
        return self._qp[inst]

    def root_folders(self, inst) -> list:
        inst = self.resolve(inst)
        if inst not in self._rf:
            self._rf[inst] = self._req(inst, "rootfolder", fallback=[]) or []
        return self._rf[inst]

    def quality_definitions(self, inst) -> list:
        inst = self.resolve(inst)
        if inst not in self._qd:
            self._qd[inst] = self._req(inst, "qualitydefinition", fallback=[]) or []
        return self._qd[inst]

    def lookup(self, inst, term: str) -> list:
        base = "series/lookup" if self.service == "sonarr" else "movie/lookup"
        return self._req(inst, f"{base}?term={quote(term)}", fallback=[]) or []

    # ── write ─────────────────────────────────────────────────────────────────
    def add(self, inst, payload):
        endpoint = "series" if self.service == "sonarr" else "movie"
        return self._req(self.resolve(inst), endpoint, method="POST", payload=payload)

    def put(self, inst, endpoint, payload):
        return self._req(self.resolve(inst), endpoint, method="PUT", payload=payload)

    def command(self, inst, payload):
        return self._req(self.resolve(inst), "command", method="POST", payload=payload)

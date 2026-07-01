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
        self._tags: dict = {}

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

    def tags(self, inst) -> list:
        """All tag definitions on the instance — ``[{id, label}, …]`` (cached per run)."""
        inst = self.resolve(inst)
        if inst not in self._tags:
            self._tags[inst] = self._req(inst, "tag", fallback=[]) or []
        return self._tags[inst]

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

    def get(self, inst, endpoint, fallback=None):
        """Raw GET of an arbitrary endpoint (e.g. ``manualimport?folder=…``) — mirrors
        :meth:`put`/:meth:`command`. Not cached; the caller owns any repetition."""
        return self._req(self.resolve(inst), endpoint, fallback=fallback)

    def delete(self, inst, endpoint):
        """DELETE an endpoint on the instance (e.g. ``moviefile/{id}`` to remove a FILE while keeping
        the Radarr record). Mirrors :meth:`put`/:meth:`command`; the caller owns gating."""
        return self._req(self.resolve(inst), endpoint, method="DELETE")

    def ensure_tag(self, inst, label):
        """Return the id of the tag with ``label`` on the instance, CREATING it if missing (POST
        /tag). Tag ids are per-instance, so this is how a label is carried across instances. Returns
        None if the create fails. The caller owns dry-run gating (a write)."""
        inst = self.resolve(inst)
        existing = next((t for t in self.tags(inst)
                         if str(t.get("label", "")).lower() == str(label).lower()), None)
        if existing is not None:
            return existing.get("id")
        created = self._req(inst, "tag", method="POST", payload={"label": label})
        tid = created.get("id") if isinstance(created, dict) else None
        if tid is not None:
            self._tags.setdefault(inst, []).append({"id": tid, "label": label})
        return tid

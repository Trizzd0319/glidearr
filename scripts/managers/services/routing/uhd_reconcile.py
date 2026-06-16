"""
uhd_reconcile.py — steady-state dual-version reconcile (mirror standard 2160p → 4K instance).
================================================================================
The add-time path (resolver.plan_uhd_companion) keeps a NEW movie as a <=1080 baseline on the
standard instance PLUS a 2160p copy on the 4K instance when 4k_policy=='both'. This sweep is the
catch-up for OWNED movies that the add-time path never saw — the operator's case:

    "If we add a movie to standard radarr, and it gets upgraded to 4k, add it to the 4k
     instance and go through the profile selection process."

For each standard-instance movie whose CURRENT FILE is 2160p (it was upgraded past the HD tier,
or added manually) and that has NO copy on the 4K instance yet, it mirrors the title onto the 4K
instance — its top (2160p) profile, ``movieRootFolders['4k']`` root, monitored, search ON — so the
premium copy lives where it belongs. The 1080p baseline already exists on the standard instance,
so this is make-before-break by construction (the durable copy is never absent).

Gated like the rest of the re-organizer, so it is inert until the operator opts in:
  • routing.configured        — the routing onboarding step has run.
  • routing.movies.4k_policy  — must be "both"; "highest_only" (default) → skip.
  • a DISTINCT 4K instance     — the categorized "4K"/"4k" label resolves to a session other than
                                 the default; otherwise there is nowhere to mirror to.
  • routing.reorg_mode        — off (skip) / log_only (LOG mirror candidates, add NOTHING —
                                 the default) / same_instance (actuate the mirror adds). A
                                 dry_run never POSTs regardless. Mirroring is an ADD (it never
                                 moves or deletes a file), so unlike a folder move it does not
                                 require relocation_consent — the same_instance switch is enough.

Score-based mirroring (a high-watchability owned movie that only has a 1080p file and should gain
a 4K copy) is a deliberate follow-up: it needs the per-title watchability score threaded in from
the Radarr Parquet cache. This sweep covers the explicit file-upgrade case only.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space.routing_targets import reorg_mode
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.support.utilities.size_model import profile_max_quality

# Alias-aware: the role map writes "4K" while the folder bucket is "4k" (and operators may use
# uhd/2160) — accept them all so a casing/naming split never silently disables the mirror.
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
_UHD_RES = 2160


class UhdReconcileManager:
    def __init__(self, config=None, logger=None, *, radarr=None, dry_run=False, **kwargs):
        self.config = config or {}
        self.logger = logger
        self.dry_run = bool(dry_run)
        self._im = self._extract_im(radarr)
        self._routing = self.config.get("routing", {}) or {}
        self._mrf = self.config.get("movieRootFolders", {}) or {}

    @staticmethod
    def _extract_im(mgr):
        if mgr is None:
            return None
        return getattr(mgr, "instance_manager", None) or getattr(mgr, "radarr_api", None)

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    # ── entry ─────────────────────────────────────────────────────────────────
    def run(self):
        if not self._routing.get("configured"):
            return                                          # never-onboarded → nothing
        if (self._routing.get("movies", {}) or {}).get("4k_policy") != "both":
            return                                          # single-copy policy → nothing to mirror
        mode = reorg_mode(self.config)
        if mode == "off":
            return
        im = self._im
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        gw = ArrGateway("radarr", im, self.config, self.logger)
        fourk = self._uhd_instance(gw)
        if not fourk:
            return                                          # no distinct 4K instance → nowhere to mirror
        prof_id, prof_name = self._top_profile(gw, fourk)
        root = self._mrf.get("4k") or self._first_root(gw, fourk)
        if prof_id is None or not root:
            self._log("log_warning", f"[UHD] 4K instance '{fourk}' has no quality profile or "
                                     f"root folder configured — skipping mirror.")
            return
        apply = (mode == "same_instance") and not self.dry_run
        for name in list((im._get_apis() or {}).keys()):
            if name == fourk:
                continue                                    # don't scan the 4K instance itself
            self._reconcile_instance(gw, name, fourk, prof_id, prof_name, root, apply)

    # ── per standard instance ──────────────────────────────────────────────────
    def _reconcile_instance(self, gw, std_inst, fourk, prof_id, prof_name, root, apply):
        try:
            movies = gw.library_items(std_inst) or []
        except Exception as e:
            self._log("log_warning", f"[UHD] movie fetch failed for '{std_inst}': {e}")
            return
        mirrored = 0
        for mv in movies:
            tmdb = mv.get("tmdbId")
            if tmdb is None or self._res(mv) < _UHD_RES:
                continue                                    # no 2160p file → not an upgrade case
            if gw.in_library(fourk, "tmdbId", tmdb):
                continue                                    # a 4K copy already exists
            self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}' is 2160p with no 4K copy "
                                  f"→ mirror to '{fourk}' ({'adding' if apply else 'log only'})")
            if not apply:
                continue
            try:
                gw.add(fourk, self._add_payload(mv, prof_id, root))
                mirrored += 1
                self._log("log_success", f"[UHD] mirrored '{mv.get('title')}' → {fourk} "
                                         f"({prof_name}, search ON)")
            except Exception as e:
                self._log("log_warning", f"[UHD] mirror add failed for '{mv.get('title')}': {e}")
        if mirrored:
            self._log("log_info", f"[UHD] {std_inst}: mirrored {mirrored} title(s) to {fourk}.")

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _uhd_instance(self, gw):
        """The DISTINCT 4K/UHD Radarr instance, or None (categorized label resolves to default)."""
        default_inst = gw.default_instance()
        for label in _UHD_LABELS:
            inst = gw.categorized_instance(label)
            if inst and inst != default_inst:
                return inst
        return None

    @staticmethod
    def _res(movie) -> int:
        """The movie's current file resolution (``movieFile.quality.quality.resolution``), or 0
        when it has no file / an unparseable quality."""
        mf = movie.get("movieFile") or {}
        q = ((mf.get("quality") or {}).get("quality") or {})
        r = q.get("resolution")
        try:
            return int(r) if r is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _top_profile(gw, inst):
        """(id, name) of the highest-resolution profile on the instance — the 4K library's 2160p
        tier by construction. (None, None) when the instance reports no profiles."""
        profiles = gw.quality_profiles(inst) or []
        if not profiles:
            return None, None
        top = max(profiles, key=lambda p: (profile_max_quality(p)[0] or 0))
        return top.get("id"), top.get("name")

    @staticmethod
    def _first_root(gw, inst) -> str:
        for f in (gw.root_folders(inst) or []):
            if isinstance(f, dict) and f.get("path"):
                return f["path"]
        return ""

    @staticmethod
    def _add_payload(movie, prof_id, root) -> dict:
        """Clone the owned standard-instance movie object into a Radarr add payload for the 4K
        instance — same tmdbId/title/images/year, but the 4K instance's profile + root, monitored,
        search ON. The standard instance's own id / movieFile are stripped (they don't apply on the
        4K session)."""
        payload = dict(movie)
        # Strip the standard-instance identity AND its absolute path. Radarr treats an explicit
        # ``path`` as authoritative over ``rootFolderPath``, so leaving the owned movie's
        # ``/standard/Title (Year)`` path would create the 4K copy in the STANDARD folder — drop
        # it (and ``folderName``) so ``rootFolderPath`` (the 4k root) wins.
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = prof_id
        payload["rootFolderPath"] = root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": True}
        return payload

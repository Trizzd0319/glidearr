"""
uhd_reconcile.py — steady-state dual-version reconcile (move standard 2160p → the 4K instance).
================================================================================
The add-time path (resolver.plan_uhd_companion) keeps a NEW movie as a <=1080 baseline on the
standard instance PLUS a 2160p copy on the 4K instance when 4k_policy=='both'. This sweep is the
catch-up for OWNED movies the add-time path never saw — the operator's case:

    "If we add a movie to standard radarr, and it gets upgraded to 4k, add it to the 4k
     instance ... If one instance doesn't have access to the other's folders, re-grab/downgrade."

For each standard-instance movie whose CURRENT FILE is 2160p, it drives a cross-instance MOVE
(``CrossInstanceMove``): the 4K instance imports+moves the existing 2160p file (no re-download,
shared storage), and once that's confirmed the standard record is retuned to its ≤1080 baseline
and re-grabbed — the ``both`` end-state (1080p on standard + 2160p on the 4K instance). The move is
make-before-break (the source is only changed after the destination has the file) and idempotent
across runs. State is read from BOTH instance libraries here; the mover does the gated API steps.

Gated, so it is inert until the operator opts in:
  • routing.configured              — the routing onboarding step has run.
  • routing.movies.4k_policy=='both'— "highest_only" (default) → skip.
  • a DISTINCT 4K instance           — the categorized "4K"/"4k" label resolves to another session.
  • routing.reorg_mode               — off (skip) / log_only (LOG the plan, move NOTHING — default)
                                       / same_instance (actuate).
  • relocation_consent               — the MOVE physically relocates files + re-searches, so (like a
                                       folder move) it requires explicit consent. Without it, or
                                       under dry_run, the mover only logs the plan.

The re-grab fallback for deployments where the two instances do NOT share storage (so the 4K
instance can't import from the standard folder) is a deliberate follow-up.

EXPERIMENTAL — the move uses Radarr's async import/rescan commands; the exact timing/convergence
wants validation against a live shared-storage pair before the consent gate is flipped on. The
source Radarr RECORD is never deleted (it is retuned in place, so its id/history survive) and no
physical file is ever deleted. Known, non-data-loss limitation: on a deployment whose instances do
NOT actually share storage, the destination import never completes and the move re-issues a scan
each run (harmless churn — no copy is ever lost).
"""
from __future__ import annotations

from scripts.managers.machine_learning.space import dual_version
from scripts.managers.machine_learning.space.routing_targets import reorg_mode, relocation_consented
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.radarr.storage.cross_instance_move import CrossInstanceMove
from scripts.support.utilities.size_model import profile_max_quality

# Alias-aware: the role map writes "4K" while the folder bucket is "4k" (and operators may use
# uhd/2160) — accept them all so a casing/naming split never silently disables the move.
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
            return                                          # single-copy policy → nothing to move
        mode = reorg_mode(self.config)
        if mode == "off":
            return
        im = self._im
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        gw = ArrGateway("radarr", im, self.config, self.logger)
        fourk = self._uhd_instance(gw)
        if not fourk:
            return                                          # no distinct 4K instance → nowhere to move
        dest_pid, _ = self._top_profile(gw, fourk)
        dest_root = self._mrf.get("4k") or self._first_root(gw, fourk)
        if dest_pid is None or not dest_root:
            self._log("log_warning", f"[UHD] 4K instance '{fourk}' has no quality profile or root "
                                     f"folder configured — skipping cross-instance move.")
            return

        # Actuating the MOVE physically relocates files + re-searches, so it needs explicit
        # relocation consent AND the same_instance mode AND a live run. Otherwise the mover runs
        # dry (logs the plan, writes nothing) — log_only / no-consent are safe by default.
        actuate = (mode == "same_instance") and relocation_consented(self.config) and not self.dry_run
        mover = CrossInstanceMove(gw, self.logger, dry_run=not actuate)

        ultra = gw.library_items(fourk) or []
        present = {m.get("tmdbId") for m in ultra if isinstance(m, dict) and m.get("tmdbId") is not None}
        hasfile = {m.get("tmdbId") for m in ultra
                   if isinstance(m, dict) and m.get("tmdbId") is not None and m.get("hasFile")}

        for name in list((im._get_apis() or {}).keys()):
            if name == fourk:
                continue                                    # don't scan the 4K instance itself
            self._reconcile_instance(gw, mover, name, fourk, dest_root, dest_pid, present, hasfile)

    # ── per standard instance ──────────────────────────────────────────────────
    def _reconcile_instance(self, gw, mover, std_inst, fourk, dest_root, dest_pid, present, hasfile):
        try:
            movies = gw.library_items(std_inst) or []
        except Exception as e:
            self._log("log_warning", f"[UHD] movie fetch failed for '{std_inst}': {e}")
            return
        hd = dual_version.pick_hd_profile(gw.quality_profiles(std_inst) or [], None)
        hd_pid = hd.get("id") if hd else None
        if hd_pid is None:
            self._log("log_warning", f"[UHD] {std_inst}: no ≤1080 quality profile — cannot keep a "
                                     f"1080p baseline; skipping cross-instance move.")
            return
        acted = 0
        for mv in movies:
            tmdb = mv.get("tmdbId")
            if tmdb is None:
                continue
            dest_hasfile = tmdb in hasfile
            res = self._res(mv)
            # FINALIZE: the 2160p has landed on the 4K instance and the source isn't yet the 1080p
            # baseline — but NOT a title already AT the baseline profile or already holding a healthy
            # ≤1080 file (a steady dual title is left untouched). MOVE-IN/PENDING: a 2160p file not
            # yet (with a file) on the 4K side.
            already_baseline = (mv.get("qualityProfileId") == hd_pid) or (mv.get("hasFile") and 0 < res <= 1080)
            is_finalize = dest_hasfile and not already_baseline
            is_movein = (not dest_hasfile) and res >= _UHD_RES
            if not (is_finalize or is_movein):
                continue
            res = mover.relocate(mv, from_inst=std_inst, to_inst=fourk, dest_root=dest_root,
                                 dest_profile_id=dest_pid, hd_profile_id=hd_pid,
                                 dest_present=tmdb in present, dest_hasfile=dest_hasfile)
            st = res.get("status")
            if st not in ("skip", "noop"):
                acted += 1
                self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}' → {fourk} [{st}]")
        if acted:
            self._log("log_info", f"[UHD] {std_inst}: {acted} title(s) in cross-instance move to {fourk}.")

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

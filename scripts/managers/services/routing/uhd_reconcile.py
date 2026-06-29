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

It also DEDUPS: when a title exists on BOTH a standard-tier instance and the 4K instance (a redundant
second physical file — e.g. a 2160p copy left on standard while the 4K instance also has one), it
keeps the better copy and reclaims the worse copy's FILE (the loser's Radarr record is preserved).
The dedup planner (``cross_instance_dedup.plan_dedup``) treats the intended dual-version split
(≤1080p on standard + 2160p on the 4K instance) as the desired end state, NOT a duplicate, so the
move and the dedup never fight; a same-path duplicate (two records, one physical file) is flagged for
the operator and NEVER auto-acted.

Gated, so it is inert until the operator opts in:
  • routing.configured              — the routing onboarding step has run.
  • routing.movies.4k_policy=='both'— "highest_only" (default) → skip.
  • a DISTINCT 4K instance           — the categorized "4K"/"4k" label resolves to another session.
  • routing.reorg_mode               — off (skip) / log_only (LOG the plan, move NOTHING — default)
                                       / cross_instance (actuate the cross-instance move + dedup).
                                       (same_instance still drives same-instance folder moves + the
                                       proactive 4K acquire; cross_instance is the file-relocation
                                       mode, un-conflated from folder moves.)
  • cross_instance_move_consent      — the MOVE physically relocates a file + re-searches, so it
                                       needs explicit consent (separate from the folder-move consent).
  • cross_instance_dedup_consent     — dedup DELETES the worse copy's file, so it carries its own
                                       consent on top, AND honours the backup gate. Without it (or
                                       under dry_run / a disarmed backup gate) the plan is logged only.
  • shared storage (pre-flight)      — the move only actuates when a probe confirms both instances
                                       back onto one mount; otherwise it degrades to log-only rather
                                       than churning a Move scan that can never complete.

Every destructive step honours ``effective_dry_run`` (the backup gate), so a real run whose backup
pre-flight failed writes nothing. The re-grab fallback for genuinely non-shared deployments (re-grab
a fresh 4K on the destination, then downgrade the source) is a deliberate follow-up.

EXPERIMENTAL — the move uses Radarr's async import/rescan commands; the exact timing/convergence
wants validation against a live shared-storage pair before the consent gate is flipped on. The
source Radarr RECORD is never deleted (it is retuned in place, so its id/history survive); the only
file ever deleted is a dedup LOSER's, and only after the keeper's file is confirmed present.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space import dual_version
from scripts.managers.machine_learning.space.cross_instance_dedup import plan_dedup
from scripts.managers.machine_learning.space.routing_targets import (
    cross_instance_dedup_enabled,
    cross_instance_move_enabled,
    evict_uhd_first,
    proactive_4k_enabled,
    relocation_enabled,
    reorg_mode,
    uhd_remote_play_ok,
)
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.radarr.storage.cross_instance_dedup_apply import CrossInstanceDedup
from scripts.managers.services.radarr.storage.cross_instance_move import CrossInstanceMove
from scripts.managers.services.radarr.storage.shared_storage import shared_storage_confirmed
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.size_model import profile_max_quality
from scripts.support.utilities.space_targets import space_targets
from scripts.support.utilities.watch_likelihood import watch_likelihood

# Alias-aware: the role map writes "4K" while the folder bucket is "4k" (and operators may use
# uhd/2160) — accept them all so a casing/naming split never silently disables the move.
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
_UHD_RES = 2160


class UhdReconcileManager:
    def __init__(self, config=None, logger=None, *, radarr=None, dry_run=False, registry=None, **kwargs):
        self.config = config or {}
        self.logger = logger
        self.dry_run = bool(dry_run)
        self._im = self._extract_im(radarr)
        self._registry = registry
        self.global_cache = kwargs.get("global_cache")
        self._routing = self.config.get("routing", {}) or {}
        self._mrf = self.config.get("movieRootFolders", {}) or {}

    @staticmethod
    def _extract_im(mgr):
        if mgr is None:
            return None
        return getattr(mgr, "instance_manager", None) or getattr(mgr, "radarr_api", None)

    def _remote_play_ok(self) -> bool:
        """Stage-C remote-play gate for the PROACTIVE 4K acquire — the SAME authority the add-time
        resolver uses (``routing_targets.uhd_remote_play_ok``), reading the same cached fingerprint
        matrix + platform weights, so reconcile and add-time never diverge on what counts as
        remote-playable. Returns True when the transcode gate is OFF (proactive acquire unchanged)."""
        gc = self.global_cache
        records = gc.get("tautulli/transcode_fingerprint") if gc else None
        weights = gc.get("tautulli/platforms") if gc else None
        return uhd_remote_play_ok(self.config, records, weights)

    def _likelihood_map(self, instance) -> dict:
        """``{tmdbId: watch_likelihood (0-100)}`` for owned movies on ``instance``, read from the
        Radarr movie_files Parquet cache (the SAME persisted watchability the quality/space managers
        use, so the dual-version 4K decision agrees with the upgrade brain). Empty when the
        registry/cache is unavailable — the move logic never depends on it."""
        sp = None
        if self._registry is not None:
            try:
                sp = self._registry.get("manager", "RadarrSpacePressureManager")
            except Exception:
                sp = None
        if sp is None or not hasattr(sp, "load_movie_files"):
            return {}
        try:
            df = sp.load_movie_files(instance)
        except Exception:
            return {}
        if df is None or getattr(df, "empty", True) or "tmdb_id" not in getattr(df, "columns", []):
            return {}
        out: dict = {}
        for _, row in df.iterrows():
            try:
                tmdb = int(row.get("tmdb_id"))
            except (TypeError, ValueError):
                continue
            try:
                out[tmdb] = float(watch_likelihood(row, config=self.config))
            except Exception:
                continue
        return out

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    # ── entry ─────────────────────────────────────────────────────────────────
    def run(self):
        if not self._routing.get("configured"):
            return                                          # never-onboarded → nothing
        if (self._routing.get("movies", {}) or {}).get("4k_policy") != "both":
            return                                          # single-copy policy → nothing to do
        mode = reorg_mode(self.config)
        evict = evict_uhd_first(self.config)
        if mode == "off" and not evict:
            return                                          # neither path enabled → nothing
        im = self._im
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        gw = ArrGateway("radarr", im, self.config, self.logger)
        fourk = self._uhd_instance(gw)
        if not fourk:
            return                                          # no distinct 4K instance → nowhere to route
        self._plan_rows = []                                # consolidated move/dedup preview grid
        self._plan_file_started = False
        # MOVE / ACQUIRE owned standard-tier titles toward the 4K instance (reorg-gated).
        if mode != "off":
            self._run_move_acquire(gw, fourk, mode)
        # DEDUP redundant cross-instance copies (cross_instance mode only). The plan is LOGGED even
        # when the dedup consent is off; a file is reclaimed only when the gate is armed AND the
        # backup gate is up — and never for a same-path duplicate.
        if mode == "cross_instance":
            self._run_dedup(gw, fourk)
        # DOWNGRADE low-watchability 4K-ONLY titles to a 1080p baseline on standard under pressure
        # (evict-gated), so the coordinator can then reclaim their 4K without losing the title.
        if evict:
            self._downgrade_orphan_4k(gw, fourk)
        self._flush_plan_grid()

    def _run_move_acquire(self, gw, fourk, mode):
        dest_pid, dest_name = self._top_profile(gw, fourk)
        dest_root = self._mrf.get("4k") or self._first_root(gw, fourk)
        if dest_pid is None or not dest_root:
            self._log("log_warning", f"[UHD] 4K instance '{fourk}' has no quality profile or root "
                                     f"folder configured — skipping cross-instance move.")
            return
        # The 4K instance must offer a genuine 2160p profile: every title routed here is added at
        # this top profile, so if it is NOT a 2160p profile the instance is misconfigured for 4K and
        # we must NOT land sub-4K copies on it. (This is the glidearr side of "only the 2160p QP is
        # selectable on the 4K instance"; also restrict the 4K instance's profiles in Radarr.)
        _dest_prof = next((p for p in (gw.quality_profiles(fourk) or []) if p.get("id") == dest_pid), None)
        try:
            _dest_res = profile_max_quality(_dest_prof)[0] if _dest_prof else None
        except Exception:
            _dest_res = None
        if not _dest_res or int(_dest_res) < _UHD_RES:
            self._log("log_warning", f"[UHD] 4K instance '{fourk}' top profile '{dest_name}' is "
                                     f"{_dest_res or '?'}p, not 2160p — skipping (configure a 2160p "
                                     f"quality profile on the 4K instance).")
            return
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        # Two ways the cross-instance FILE MOVE may actuate:
        #   • cross_instance mode + move consent — the HARDENED path: additionally requires the backup
        #     gate up AND a per-source shared-storage pre-flight (degrade-to-log when unconfirmed).
        #   • same_instance mode + relocation consent — the LEGACY dual-version reconcile, unchanged
        #     (no probe), so existing installs keep behaving exactly as before.
        # The two are mutually exclusive (reorg_mode is single-valued).
        move_cross_armed = cross_instance_move_enabled(self.config)
        move_legacy_armed = relocation_enabled(self.config)
        # Proactive 4K ACQUIRE (a NEW copy on the 4K instance; the source is untouched, no shared
        # storage needed) actuates under EITHER move gate — same_instance (legacy) or cross_instance
        # — so it stays coupled to the standard upgrade cap (proactive_4k_enabled) in both modes.
        acquire_actuate = (move_legacy_armed or move_cross_armed) and not eff_dry
        acq_mover = CrossInstanceMove(gw, self.logger, dry_run=not acquire_actuate)

        ultra = gw.library_items(fourk) or []
        present = {m.get("tmdbId") for m in ultra if isinstance(m, dict) and m.get("tmdbId") is not None}
        hasfile = {m.get("tmdbId") for m in ultra
                   if isinstance(m, dict) and m.get("tmdbId") is not None and m.get("hasFile")}
        # Per-tmdb 4K-side file path → lets the move SKIP same-path duplicates (two records, one
        # physical file): retuning/searching/Move-scanning such a title could destroy the shared file
        # the 4K record depends on (Invariant 4). Detected + flagged, never actuated. Also per-tmdb
        # 4K-side file RESOLUTION → so FINALIZE never downgrades a standard file to 1080p unless the
        # 4K instance genuinely holds an equal-or-higher-res copy (else the high-res standard file
        # would be the only one, and retune+search could destroy it).
        uhd_paths, uhd_res = {}, {}
        for m in ultra:
            if not isinstance(m, dict) or m.get("tmdbId") is None:
                continue
            p = str(((m.get("movieFile") or {}).get("path")) or "").replace("\\", "/").strip().rstrip("/")
            if p:
                uhd_paths[m.get("tmdbId")] = p
            uhd_res[m.get("tmdbId")] = self._res(m)

        # Stage-C remote-play gate (household-global) — computed once for the whole sweep.
        crp = self._remote_play_ok()

        for name in self._source_instances(gw, fourk):
            move_actuate = False
            if not eff_dry:
                if move_cross_armed:
                    ok, why = shared_storage_confirmed(gw, name, fourk)
                    move_actuate = ok
                    if not ok:
                        self._log("log_warning", f"[UHD] {name} -> {fourk}: cross-instance move held "
                                                 f"to log-only ({why}).")
                elif move_legacy_armed:
                    move_actuate = True
            move_mover = CrossInstanceMove(gw, self.logger, dry_run=not move_actuate)
            self._reconcile_instance(gw, move_mover, acq_mover, name, fourk, dest_root, dest_pid,
                                     present, hasfile, uhd_paths, uhd_res, crp)

    def _source_instances(self, gw, fourk) -> list:
        """The HD/standard-tier instances to scan as MOVE SOURCES — where the 1080p baseline lives.
        ONLY the categorized 720p/1080p instances plus the default; NEVER the 4K instance itself
        and NEVER an uncategorized or other-4K-tier instance. This is the guard that stops the sweep
        from dragging a *real* 4K library (an instance that legitimately holds 2160p) into the move
        just because it isn't the configured 4K target."""
        names: list = []
        for label in ("1080p", "720p"):
            inst = gw.categorized_instance(label)
            if inst and inst != fourk and inst not in names:
                names.append(inst)
        default = gw.default_instance()
        if default and default != fourk and default not in names:
            names.append(default)
        return names

    # ── dedup redundant cross-instance copies ─────────────────────────────────────
    def _run_dedup(self, gw, fourk):
        """Reclaim the worse of two copies when a title exists on BOTH a standard-tier instance and
        the 4K instance. Plans are computed from the libraries already fetched and LOGGED regardless;
        a file is deleted only when the dedup gate is armed AND the backup gate is up — and never for
        a same-path duplicate (two records, one physical file)."""
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        actuate = cross_instance_dedup_enabled(self.config) and not eff_dry
        dedup = CrossInstanceDedup(gw, self.logger, dry_run=not actuate)
        try:
            uhd_movies = gw.library_items(fourk) or []
        except Exception as e:
            self._log("log_warning", f"[Dedup] 4K library fetch failed for '{fourk}': {e}")
            return
        reclaimed = flagged = 0
        for src in self._source_instances(gw, fourk):
            try:
                std_movies = gw.library_items(src) or []
            except Exception as e:
                self._log("log_warning", f"[Dedup] library fetch failed for '{src}': {e}")
                continue
            for p in plan_dedup(src, std_movies, fourk, uhd_movies):
                try:
                    st = dedup.apply(p).get("status")
                except Exception as e:                      # one bad title never aborts the sweep
                    self._log("log_warning", f"[Dedup] apply failed for '{p.get('title')}' "
                                             f"(tmdb {p.get('tmdb')}): {e}; skipping.")
                    continue
                loser = p.get("loser_inst", src)
                keeper = p.get("keeper_inst", fourk)
                self._record_plan(p.get("title"), p.get("tmdb"), loser, keeper, "dedup", st,
                                  reason=p.get("reason", ""))
                if st in ("deduped", "would-dedup"):
                    reclaimed += 1
                elif st == "flag-same-path":
                    flagged += 1
        if reclaimed or flagged:
            verb = "reclaimed" if actuate else "would reclaim"
            self._log("log_info", f"[Dedup] {verb} {reclaimed} redundant cross-instance copy(ies); "
                                  f"{flagged} same-path duplicate(s) flagged for the operator.")

    # ── consolidated plan preview (grid + per-title file sink) ─────────────────────
    def _record_plan(self, title, tmdb, frm, to, op, status, reason=""):
        """Append one row to the consolidated reconcile preview and mirror it to the dedicated
        ``relocation`` log file (so the per-title plan doesn't flood the main run log)."""
        rows = getattr(self, "_plan_rows", None)
        if rows is None:
            self._plan_rows = rows = []
        rows.append([str(title or "?")[:38], str(tmdb if tmdb is not None else "?"),
                     str(frm or "?"), str(to or "?"), op, status])
        line = f"{op:7} | {status:16} | {frm} -> {to} | {title} (tmdb {tmdb}){(' | ' + reason) if reason else ''}"
        self._log_to_file_safe(line)

    def _log_to_file_safe(self, line):
        started = getattr(self, "_plan_file_started", False)
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("relocation", line, reset=not started)
        self._plan_file_started = True

    def _flush_plan_grid(self):
        rows = getattr(self, "_plan_rows", None)
        if not rows:
            return
        if self.logger and hasattr(self.logger, "log_grid"):
            self.logger.log_grid(["Title", "TMDB", "From", "To", "Op", "Status"], rows,
                                 title="Cross-instance reconcile plan")

    # ── downgrade low-watchability 4K-only titles under pressure ──────────────────
    def _downgrade_orphan_4k(self, gw, fourk):
        """Under space pressure, for each LOW-watchability 4K-ONLY title on the 4K instance (no copy
        on any standard-tier instance), grab a ≤1080 baseline on the default standard instance —
        monitored, search ON. MAKE-BEFORE-BREAK: the 4K stays; the coordinator reclaims it on a later
        run once the baseline file lands (the title is then baseline-backed). Idempotent — skipped
        once the title has ANY standard record (pending or filed), so the add never repeats."""
        std_inst = gw.default_instance()
        if not std_inst or std_inst == fourk or not self._under_pressure(gw, std_inst):
            return                                          # no standard tier, or no pressure → skip
        hd = dual_version.pick_hd_profile(gw.quality_profiles(std_inst) or [], None)
        hd_pid = hd.get("id") if hd else None
        std_root = self._mrf.get("standard") or self._first_root(gw, std_inst)
        if hd_pid is None or not std_root:
            self._log("log_warning", f"[UHD] standard instance '{std_inst}' has no ≤1080 profile / root "
                                     f"— cannot downgrade 4K-only titles.")
            return
        # Any tmdbId with a record on a standard-tier instance already has (or is grabbing) a baseline.
        present = set()
        for name in self._source_instances(gw, fourk):
            for m in (gw.library_items(name) or []):
                if m.get("tmdbId") is not None:
                    present.add(m.get("tmdbId"))
        threshold = int((self._routing.get("movies", {}) or {}).get("4k_dual_min_score")
                        or dual_version.DEFAULT_UHD_SCORE)
        likelihoods = self._likelihood_map(fourk)
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        downgraded = 0
        for mv in (gw.library_items(fourk) or []):
            tmdb = mv.get("tmdbId")
            if tmdb is None or tmdb in present:
                continue                                    # has a baseline (or one pending) → skip
            lk = likelihoods.get(tmdb)
            if lk is None or lk >= threshold:
                continue                                    # warrants 4K (or unscored) → keep in 4K
            if eff_dry:
                self._log("log_info", f"[UHD] would downgrade 4K-only '{mv.get('title')}' (watch {lk:.0f})"
                                      f" → 1080p baseline on {std_inst}; 4K reclaimed once it lands.")
                downgraded += 1
            elif self._grab_baseline(gw, std_inst, mv, hd_pid, std_root):
                downgraded += 1
                self._log("log_info", f"[UHD] downgrading 4K-only '{mv.get('title')}' (watch {lk:.0f}) →"
                                      f" 1080p baseline searching on {std_inst}; 4K reclaimed once it lands.")
        if downgraded:
            self._log("log_info", f"[UHD] {downgraded} low-watchability 4K-only title(s) downgrading to a "
                                  f"1080p baseline (make-before-break; 4K reclaimed by the coordinator next run).")

    def _under_pressure(self, gw, inst) -> bool:
        """True when the shared mount is in the pressure band (free < U). FAIL-OPEN: an unreadable
        disk returns False, so no speculative downgrade happens when free space is unknown."""
        im = getattr(gw, "im", None)
        if im is None:
            return False
        try:
            free = float(im.disk_free_gb(inst))
        except Exception:
            return False
        try:
            total = im.disk_total_gb(inst)
        except Exception:
            total = None
        try:
            _, U = space_targets(self.config, total_gb=total)
        except Exception:
            return False
        return free < U

    @staticmethod
    def _grab_baseline(gw, std_inst, src_movie, hd_pid, std_root) -> bool:
        """Add a fresh ≤1080 baseline of a 4K-only title to the standard instance (monitored, search
        ON). Clones the 4K movie object; strips its identity/path so the standard root wins. The 4K
        copy is untouched (make-before-break). Returns ok."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = hd_pid
        payload["rootFolderPath"] = std_root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": True}
        try:
            return bool(gw.add(std_inst, payload))
        except Exception:
            return False

    # ── per standard instance ──────────────────────────────────────────────────
    def _reconcile_instance(self, gw, move_mover, acq_mover, std_inst, fourk, dest_root, dest_pid,
                            present, hasfile, uhd_paths=None, uhd_res=None, can_remote_play=True):
        uhd_paths = uhd_paths or {}
        uhd_res = uhd_res or {}
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
        likelihoods = self._likelihood_map(std_inst)     # {tmdbId: watch_likelihood}; {} if unavailable
        proactive = bool((self._routing.get("movies", {}) or {}).get("proactive_4k"))
        threshold = int((self._routing.get("movies", {}) or {}).get("4k_dual_min_score")
                        or dual_version.DEFAULT_UHD_SCORE)
        space_ok_4k = self._space_allows(gw, fourk) if proactive else False
        acted = 0
        for mv in movies:
            tmdb = mv.get("tmdbId")
            if tmdb is None:
                continue
            dest_hasfile = tmdb in hasfile
            dest_present = tmdb in present
            res = self._res(mv)
            lk = likelihoods.get(tmdb)
            watch = f" (watch {lk:.0f})" if lk is not None else ""

            # SAME-PATH guard (Invariant 4): if the source and 4K records point at ONE physical file,
            # never move/finalize it — retuning/searching/Move-scanning could destroy the shared file.
            src_path = str(((mv.get("movieFile") or {}).get("path")) or "").replace("\\", "/").strip().rstrip("/")
            if src_path and src_path == uhd_paths.get(tmdb):
                self._log("log_warning", f"[UHD] {std_inst}: '{mv.get('title')}' shares ONE physical "
                                         f"file with {fourk} (same path) — skipping move/finalize; "
                                         f"operator must resolve on disk.")
                self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "move", "flag-same-path")
                continue

            # PROACTIVE ACQUIRE: an owned movie that WARRANTS 4K (watch-likelihood ≥ threshold) but
            # has no 4K anywhere (no 2160p file on the source AND not on the 4K instance) → acquire a
            # fresh 4K copy on the 4K instance; the source keeps its ≤1080 baseline. Gated on the
            # proactive_4k flag + 4K-instance space; actuation (add vs log) rides the mover's dry_run.
            if proactive and not dest_present and res < _UHD_RES and dual_version.wants_uhd(
                    keep_tagged=False, score=lk, space_allows=space_ok_4k,
                    uhd_threshold=threshold, can_remote_play=can_remote_play):
                ast = acq_mover.acquire(mv, to_inst=fourk, dest_root=dest_root,
                                        dest_profile_id=dest_pid, from_inst=std_inst).get("status")
                if ast not in ("skip", "noop"):
                    acted += 1
                    self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} [{ast}]")
                    self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "acquire", ast)
                continue

            # FINALIZE: the 2160p has landed on the 4K instance and the source isn't yet the 1080p
            # baseline — but NOT a title already AT the baseline profile or already holding a healthy
            # ≤1080 file (a steady dual title is left untouched). MOVE-IN/PENDING: a 2160p file not
            # yet (with a file) on the 4K side.
            already_baseline = (mv.get("qualityProfileId") == hd_pid) or (mv.get("hasFile") and 0 < res <= 1080)
            is_finalize = dest_hasfile and not already_baseline
            is_movein = (not dest_hasfile) and res >= _UHD_RES
            # SAFETY: never retune the source down to 1080p unless the 4K instance genuinely holds an
            # equal-or-higher-res copy. If the 4K side is LOWER res than the source's current file,
            # the source is the better copy — finalize would search+replace it down to 1080p and the
            # high-res original would be lost. Hold it for dedup/move instead.
            if is_finalize and res > 0 and uhd_res.get(tmdb, 0) < res:
                self._log("log_warning", f"[UHD] {std_inst}: '{mv.get('title')}' held — {fourk} copy "
                                         f"({uhd_res.get(tmdb, 0)}p) is lower-res than standard ({res}p); "
                                         f"not downgrading the better copy.")
                self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "move", "held-uhd-lower-res")
                continue
            if not (is_finalize or is_movein):
                continue
            st = move_mover.relocate(mv, from_inst=std_inst, to_inst=fourk, dest_root=dest_root,
                                     dest_profile_id=dest_pid, hd_profile_id=hd_pid,
                                     dest_present=dest_present, dest_hasfile=dest_hasfile).get("status")
            if st not in ("skip", "noop"):
                acted += 1
                self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} [{st}]")
                self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "move", st)
        if acted:
            self._log("log_info", f"[UHD] {std_inst}: {acted} title(s) routed to {fourk} (move/acquire).")

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _uhd_instance(self, gw):
        """The DISTINCT 4K/UHD Radarr instance, or None (categorized label resolves to default)."""
        default_inst = gw.default_instance()
        for label in _UHD_LABELS:
            inst = gw.categorized_instance(label)
            if inst and inst != default_inst:
                return inst
        return None

    def _space_allows(self, gw, inst) -> bool:
        """True when ``inst`` is comfortably above its pressure band (free >= U) — room for a
        speculative 4K acquire. FAIL-CLOSED: an unreadable disk returns False, so proactive 4K is
        never grabbed when free space is unknown (unlike the always-allowed move of an existing file)."""
        im = getattr(gw, "im", None)
        if im is None:
            return False
        try:
            free = float(im.disk_free_gb(inst))
        except Exception:
            return False
        try:
            total = im.disk_total_gb(inst)
        except Exception:
            total = None
        try:
            _, U = space_targets(self.config, total_gb=total)
        except Exception:
            return False
        return free >= U

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

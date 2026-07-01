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

from datetime import datetime, timezone

from scripts.managers.machine_learning.classification.keep_policy import resolve_keep_policy
from scripts.managers.machine_learning.lifecycle.stale_prune_policy import clock_age
from scripts.managers.machine_learning.space import dual_version
from scripts.managers.machine_learning.space.cross_instance_dedup import plan_dedup
from scripts.managers.machine_learning.space.routing_targets import (
    cross_instance_dedup_enabled,
    cross_instance_move_enabled,
    demote_4k_on_watchability_enabled,
    evict_uhd_first,
    proactive_4k_enabled,
    relocation_enabled,
    reorg_mode,
    shared_storage_mode,
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


def _tmdb_present(ledger_key, present_tmdbs) -> bool:
    """Is a watch-demote ledger key (a str tmdbId) still in the current 4K library (a set of int
    tmdbIds)? Used to prune ledger entries whose 4K record vanished entirely."""
    try:
        return int(ledger_key) in present_tmdbs
    except (TypeError, ValueError):
        return False


def _norm_path(p) -> str:
    """Slash-tolerant path normalisation for same-physical-file comparison across instances."""
    return str(p or "").replace("\\", "/").strip().rstrip("/")


class UhdReconcileManager:
    # global_cache ledger of titles whose 4K BONUS was demoted on WATCHABILITY (fileless,
    # unmonitored 4K record kept). Keyed by 4K instance; entries drop when the title's score
    # recovers (re-acquired), regains a 4K file by other means, or leaves the 4K library.
    _WATCH_DEMOTED_KEY = "radarr/{inst}/watch_demoted_4k"
    # Per-title "continuously below the demote floor since" clock (ISO), for the optional dwell that
    # absorbs a transient large affinity swing. Keyed by 4K instance; reset the instant a title
    # recovers to/above the demote floor.
    _WATCH_DEMOTE_CLOCK_KEY = "radarr/{inst}/watch_demote_clock"
    # Per-title "relocate ManualImport issued at" ledger (ISO), keyed by 4K instance. The relocate copy
    # is an ASYNC Radarr command; while a marker is within this grace window the reconcile must NOT re-
    # issue the import (a sweep firing before the copy latches hasFile would otherwise re-command it every
    # run). Once the 4K record gains its own 2160p the phase-1 branch stops firing and the marker is
    # dropped; a marker aged past the grace re-issues (rate-limited), so a stuck import can't loop per-run.
    _RELOCATE_PENDING_KEY = "radarr/{inst}/relocate_pending"
    _RELOCATE_GRACE_DAYS = 0.5

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
        demote = demote_4k_on_watchability_enabled(self.config)
        if mode == "off" and not evict and not demote:
            return                                          # no path enabled → nothing
        im = self._im
        if im is None or not hasattr(im, "_get_apis") or not hasattr(im, "_make_request"):
            return
        gw = ArrGateway("radarr", im, self.config, self.logger)
        fourk = self._uhd_instance(gw)
        if not fourk:
            return                                          # no distinct 4K instance → nowhere to route
        self._plan_rows = []                                # consolidated move/dedup preview grid
        self._plan_file_started = False
        self._baselines_grabbed = set()                     # tmdbs given a standard baseline THIS run —
        #                                                     shared by the pressure (_downgrade_orphan_4k)
        #                                                     and watchability (_demote_overqualified_4k)
        #                                                     legs so they never double-grab one title (the
        #                                                     gateway library cache wouldn't reflect the add).
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
        # DEMOTE dual-version 4K BONUS copies whose WATCHABILITY fell below the UHD threshold — even
        # when space is fine (the pressure path stays with evict_uhd_first / the coordinator). The
        # 1080p baseline must survive on standard (never the last copy); the 4K record is kept so the
        # companion is re-acquired if the score recovers.
        if demote:
            self._demote_overqualified_4k(gw, fourk)
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
        # DOWNLOAD-BASED dual-version (no cross-instance file move): the 4K instance ACQUIRES its own
        # 2160p (download) and the standard record is retuned to its ≤1080 baseline (Radarr grabs a
        # 1080p and replaces the 2160p on import). Actuates under the same gate the file-move used to —
        # cross_instance + move consent, OR the legacy same_instance relocation consent; both modes now
        # do the same download-based reconcile, and no shared mount / storage probe is needed. eff_dry
        # already folds in the pre-destructive backup gate (the retune drops the source 2160p once a
        # 1080p imports).
        dual_armed = cross_instance_move_enabled(self.config) or relocation_enabled(self.config)
        actuator = CrossInstanceMove(gw, self.logger, dry_run=not (dual_armed and not eff_dry))

        ultra = gw.library_items(fourk) or []
        present = {m.get("tmdbId") for m in ultra if isinstance(m, dict) and m.get("tmdbId") is not None}
        hasfile = {m.get("tmdbId") for m in ultra
                   if isinstance(m, dict) and m.get("tmdbId") is not None and m.get("hasFile")}
        # Per-tmdb 4K-side file path → SKIP same-path duplicates (two records, one physical file):
        # retuning/searching such a title could destroy the shared file the 4K record depends on
        # (Invariant 4). Detected + flagged, never actuated. Also per-tmdb 4K-side RESOLUTION → so the
        # standard retune never downgrades a 2160p to 1080p when the 4K side holds only a LOWER-res copy
        # and no fresh 4K is being acquired (else the better copy would be the only one and get lost).
        # uhd_ids: tmdb → the 4K-instance movie id, so we can DRIVE an existing-but-fileless 4K record
        # (profile + monitor + search) instead of relying on its own RSS — fixes a stale/un-monitored
        # 4K shell that would otherwise never grab its own 2160p.
        uhd_paths, uhd_res, uhd_ids = {}, {}, {}
        for m in ultra:
            if not isinstance(m, dict) or m.get("tmdbId") is None:
                continue
            p = str(((m.get("movieFile") or {}).get("path")) or "").replace("\\", "/").strip().rstrip("/")
            if p:
                uhd_paths[m.get("tmdbId")] = p
            uhd_res[m.get("tmdbId")] = self._res(m)
            if m.get("id") is not None:
                uhd_ids[m.get("tmdbId")] = m.get("id")

        # Stage-C remote-play gate (household-global) — computed once for the whole sweep.
        crp = self._remote_play_ok()
        # SHARED-STORAGE ROUTING: relocate the standard 2160p into the 4K instance (hardlink, no re-
        # download) when both instances back onto one filesystem; otherwise download. Mode read once;
        # the confirm is per source↔4K pair (they could differ), and the actuator adds a per-title
        # "can the 4K instance actually see the file" check on top.
        shared_mode = shared_storage_mode(self.config)
        self._relocated = set()          # tmdbs relocated this sweep — a title on >1 source instance
        relo_key = self._RELOCATE_PENDING_KEY.format(inst=fourk)   # must not be re-imported into the 4K record
        _rp = self.global_cache.get(relo_key) if self.global_cache else None
        self._relo_prev = _rp if isinstance(_rp, dict) else {}     # {tmdb: issued-ISO} from prior sweeps
        self._relo_new = {}              # markers still pending after THIS sweep (persisted below)
        self._relo_now = datetime.now(timezone.utc)
        for name in self._source_instances(gw, fourk):
            shared_ok = self._shared_storage_ok(gw, shared_mode, name, fourk)
            self._reconcile_instance(gw, actuator, name, fourk, dest_root, dest_pid,
                                     present, hasfile, uhd_paths, uhd_res, crp, uhd_ids, shared_ok)
        # Persist the pending-relocate ledger (non-destructive tracking of async imports in flight; only
        # a real 'relocating' issue is marked, so a dry-run adds nothing). A title that gained its 2160p
        # is simply not carried forward, so its marker clears.
        if self.global_cache is not None:
            try:
                self.global_cache.set(relo_key, self._relo_new)
            except Exception:
                pass

    def _shared_storage_ok(self, gw, mode, src, dst) -> bool:
        """Does ``src`` (standard) share one backing filesystem with ``dst`` (4K) — so the 4K side can
        hardlink-RELOCATE the existing 2160p instead of re-downloading it? ``mode`` is
        ``routing.movies.shared_storage``: ``true``/``false`` force it; ``auto`` probes
        (``shared_storage_confirmed``: common mount ancestor + equal backing capacity — a further
        per-title 'can it see the file' check runs in the actuator). Logged once per source instance;
        fail-closed to download on any probe error."""
        if mode == "false":
            return False
        if mode == "true":
            self._log("log_info", f"[UHD] shared-storage {src}→{dst}: forced ON (config) → relocate")
            return True
        try:
            ok, reason = shared_storage_confirmed(gw, src, dst)
        except Exception as e:                                # pragma: no cover - defensive
            self._log("log_warning", f"[UHD] shared-storage probe {src}→{dst} failed ({e}) → download")
            return False
        self._log("log_info", f"[UHD] shared-storage {src}→{dst}: "
                              f"{'YES → relocate' if ok else 'no → download'} ({reason})")
        return bool(ok)

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
        grabbed = self.__dict__.setdefault("_baselines_grabbed", set())   # shared with the demote leg
        downgraded = 0
        for mv in (gw.library_items(fourk) or []):
            tmdb = mv.get("tmdbId")
            if tmdb is None or tmdb in present or tmdb in grabbed:
                continue                                    # has a baseline (pending/filed/just-grabbed) → skip
            lk = likelihoods.get(tmdb)
            if lk is None or lk >= threshold:
                continue                                    # warrants 4K (or unscored) → keep in 4K
            if eff_dry:
                self._log("log_info", f"[UHD] would downgrade 4K-only '{mv.get('title')}' (watch {lk:.0f})"
                                      f" → 1080p baseline on {std_inst}; 4K reclaimed once it lands.")
                downgraded += 1
            elif self._grab_baseline(gw, std_inst, mv, hd_pid, std_root):
                downgraded += 1
                grabbed.add(tmdb)                           # block the demote leg from re-grabbing it
                self._log("log_info", f"[UHD] downgrading 4K-only '{mv.get('title')}' (watch {lk:.0f}) →"
                                      f" 1080p baseline searching on {std_inst}; 4K reclaimed once it lands.")
        if downgraded:
            self._log("log_info", f"[UHD] {downgraded} low-watchability 4K-only title(s) downgrading to a "
                                  f"1080p baseline (make-before-break; 4K reclaimed by the coordinator next run).")

    # ── watchability-driven demote of dual-version 4K bonus copies ────────────────
    def _keep_pinned(self, movie, tag_label_map) -> bool:
        """True when the title carries a keep/universe pin (keep_forever/keep_movie/keep_universe/
        universe) → spared from the watchability demote regardless of score (a saga member keeps its
        4K). The recency-decayed borrowed credit for UNTAGGED saga members is already folded into the
        watch-likelihood, so between this pin and that credit the saga bonus is fully honoured."""
        try:
            return resolve_keep_policy(movie, tag_label_map) is not None
        except Exception:
            return False

    def _demote_overqualified_4k(self, gw, fourk):
        """Demote 4K copies whose saga-aware WATCHABILITY fell below the UHD threshold — the
        pressure-INDEPENDENT companion to the coordinator's space-driven evict. A HYSTERESIS BAND
        (promote at the UHD threshold, demote only below ``threshold - 4k_demote_gap``) plus an
        optional per-title dwell clock (``4k_demote_dwell_days``) stop a title from FLAPPING (delete +
        re-acquire its 4K file every run) as the household watches SIBLING-ADJACENT films
        (genre/cast/crew/studio) and nudges its affinity-driven score across the line. For a 2160p
        title below the demote floor (for ≥ the dwell), not keep/universe-pinned:
          • a SURVIVING 1080p baseline FILE on a standard-tier instance exists → delete the 4K FILE +
            unmonitor (record kept, fileless, ledgered). NEVER the last copy.
          • NO baseline yet (a 4K-ONLY title) → make-before-break: grab a ≤1080 baseline on standard
            NOW (4K untouched); next run, once it imports, the title is a survivor and the branch above
            evicts the 4K. No separate drain ledger — the survivor guard IS the make-before-break.
        RECOVER: a ledgered, fileless, unmonitored 4K shell whose score climbs back to/above the UHD
        threshold (the upper band edge) is re-monitored + searched. The asymmetry is deliberate —
        demote fires below the FLOOR but recover only at/above the THRESHOLD, so a demoted title must
        cross the whole band to re-acquire its 4K and can never demote-then-recover on a small swing
        (a shell that recovers only INTO the band stays demoted; this is the anti-thrash, not a bug).
        Space pressure stays the coordinator's job (``evict_uhd_first`` / FORK-D); the two compose."""
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        mv_cfg = self._routing.get("movies", {}) or {}

        def _int_cfg(key, default):
            try:
                return int(mv_cfg.get(key) or default)   # falsy (None/"" /0-as-unset) → default
            except (TypeError, ValueError):
                return default                            # non-numeric string → default, never raise
        threshold = _int_cfg("4k_dual_min_score", dual_version.DEFAULT_UHD_SCORE)
        gap = max(0, _int_cfg("4k_demote_gap", 10))
        dwell_days = max(0, _int_cfg("4k_demote_dwell_days", 0))
        demote_floor = max(0, threshold - gap)             # band [demote_floor, threshold) is sticky
        try:
            ultra = gw.library_items(fourk) or []
        except Exception as e:
            self._log("log_warning", f"[UHD] 4K library fetch failed for '{fourk}': {e}")
            return
        # Standard-tier state: every tmdb with ANY record (so a 4K-only rehome never re-grabs a pending
        # baseline) and the subset with an actual FILE (the survivors — the make-before-break guard).
        # survivor_path keeps the baseline file's path so we never delete a 4K file that is the SAME
        # physical file as the "survivor" (shared storage / symlink — mirrors the move path's guard).
        std_records: set = set()
        survivors: set = set()
        survivor_path: dict = {}
        for name in self._source_instances(gw, fourk):
            for m in (gw.library_items(name) or []):
                if m.get("tmdbId") is None:
                    continue
                std_records.add(m.get("tmdbId"))
                if m.get("hasFile"):
                    survivors.add(m.get("tmdbId"))
                    p = _norm_path(((m.get("movieFile") or {}).get("path")) or "")
                    if p:
                        survivor_path[m.get("tmdbId")] = p
        likelihoods = self._likelihood_map(fourk)
        tag_label_map = {t.get("id"): t.get("label") for t in (gw.tags(fourk) or [])
                         if t.get("id") is not None}
        # ≤1080 baseline target on the standard instance (for the 4K-only rehome leg).
        std_inst = gw.default_instance()
        hd = dual_version.pick_hd_profile(gw.quality_profiles(std_inst) or [], None) if std_inst else None
        hd_pid = hd.get("id") if hd else None
        std_root = self._mrf.get("standard") or (self._first_root(gw, std_inst) if std_inst else "")

        key = self._WATCH_DEMOTED_KEY.format(inst=fourk)
        ledger = self.global_cache.get(key) if self.global_cache else None
        ledger = dict(ledger) if isinstance(ledger, dict) else {}
        clock_key = self._WATCH_DEMOTE_CLOCK_KEY.format(inst=fourk)
        clock = self.global_cache.get(clock_key) if self.global_cache else None
        clock = clock if isinstance(clock, dict) else {}
        new_clock: dict = {}
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        present_tmdbs: set = set()
        demoted = rehomed = recovered = 0

        for mv in ultra:
            tmdb = mv.get("tmdbId")
            if tmdb is None:
                continue
            present_tmdbs.add(tmdb)
            lk = likelihoods.get(tmdb)

            # ── RECOVER: a demoted shell (fileless, unmonitored, ledgered) whose watchability
            # climbed back to/above the UHD threshold (the upper band edge) → re-monitor + search.
            if not mv.get("hasFile"):
                if (str(tmdb) in ledger and not mv.get("monitored")
                        and lk is not None and lk >= threshold and mv.get("id") is not None):
                    if eff_dry:
                        self._log("log_info", f"[UHD] would re-acquire demoted 4K '{mv.get('title')}' "
                                              f"(watch {lk:.0f}) on {fourk}.")
                    else:
                        gw.put(fourk, "movie/editor", {"movieIds": [mv.get("id")], "monitored": True})
                        gw.command(fourk, {"name": "MoviesSearch", "movieIds": [mv.get("id")]})
                        ledger.pop(str(tmdb), None)
                        self._log("log_info", f"[UHD] re-acquiring demoted 4K '{mv.get('title')}' "
                                              f"(watch {lk:.0f}) on {fourk}: re-monitored + searched.")
                    self._record_plan(mv.get("title"), tmdb, "standard", fourk, "recover-4k",
                                      "would-recover" if eff_dry else "recovered",
                                      reason=f"watch {lk:.0f} >= {threshold}")
                    recovered += 1
                continue

            # A 4K record that HAS a file is no longer a demoted shell — drop any stale ledger entry.
            ledger.pop(str(tmdb), None)

            # ── DEMOTE candidacy: 2160p, scored, not keep/universe-pinned, BELOW the demote floor.
            if self._res(mv) <= 1080 or lk is None:
                continue                                   # not a 2160p copy, or unscored → keep
            if self._keep_pinned(mv, tag_label_map):
                continue                                   # keep/universe pin → keep 4K (clock resets)
            if lk >= demote_floor:
                continue                                   # at/above the demote floor → sticky, keep 4K
            # Below the demote floor → advance the dwell clock; only act once it's been below for the
            # configured dwell (0 = act now; the band alone is the anti-flap).
            since_iso, age_days = clock_age(clock.get(str(tmdb)) or now_iso, now)
            if age_days < dwell_days:
                new_clock[str(tmdb)] = since_iso           # keep aging, don't act yet
                continue

            grabbed = self.__dict__.setdefault("_baselines_grabbed", set())   # baselines added THIS run

            if tmdb in survivors:
                # SAME-PHYSICAL-FILE guard (shared storage): never delete a 4K file that IS the
                # "surviving" baseline file — that would orphan the standard record (mirrors the
                # move path's same-path guard). Hold it for the operator; keep aging the clock.
                cur_4k_path = _norm_path(((mv.get("movieFile") or {}).get("path")) or "")
                if cur_4k_path and cur_4k_path == survivor_path.get(tmdb):
                    self._log("log_warning", f"[UHD] '{mv.get('title')}' on {fourk} shares ONE physical file "
                                             f"with its standard baseline (same path) — skipping demote.")
                    self._record_plan(mv.get("title"), tmdb, fourk, "standard", "demote", "flag-same-path")
                    new_clock[str(tmdb)] = since_iso
                    continue
                # Baseline survives → safe to evict the 4K file now (make-before-break satisfied).
                mid = mv.get("id")
                fid = (mv.get("movieFile") or {}).get("id")
                if mid is None or fid is None:
                    continue
                if eff_dry:
                    self._log("log_info", f"[UHD] would demote 4K bonus '{mv.get('title')}' (watch {lk:.0f}) "
                                          f"on {fourk}: delete 4K file + unmonitor (1080p baseline survives).")
                    new_clock[str(tmdb)] = since_iso       # not actually demoted → keep aging the clock
                else:
                    # Unmonitor BEFORE deleting the file so Radarr won't instantly re-grab the 2160p.
                    # Isolated so a single failed delete neither aborts the sweep nor half-applies: on
                    # error we keep the clock and retry next run rather than ledger a non-demotion.
                    try:
                        gw.put(fourk, "movie/editor", {"movieIds": [mid], "monitored": False})
                        gw.delete(fourk, f"moviefile/{fid}")
                    except Exception as e:
                        self._log("log_warning", f"[UHD] demote failed for '{mv.get('title')}' on {fourk}: {e}")
                        new_clock[str(tmdb)] = since_iso
                        continue
                    ledger[str(tmdb)] = now_iso            # file gone → ledger tracks the shell for recovery
                    self._log("log_info", f"[UHD] demoted 4K bonus '{mv.get('title')}' (watch {lk:.0f}) on "
                                          f"{fourk}: 4K file deleted + unmonitored; 1080p baseline survives.")
                self._record_plan(mv.get("title"), tmdb, fourk, "standard", "demote",
                                  "would-demote" if eff_dry else "demoted",
                                  reason=f"watch {lk:.0f} < {demote_floor}")
                demoted += 1
            elif tmdb not in std_records and tmdb not in grabbed:
                # 4K-ONLY title below the floor → make-before-break: grab a ≤1080 baseline on standard
                # NOW (4K untouched). Keep the clock; once the baseline imports it becomes a survivor
                # and the branch above evicts the 4K next run. The grabbed-this-run set stops a
                # double-grab when the pressure-driven _downgrade_orphan_4k already added it (the
                # gateway library cache wouldn't yet reflect that add).
                new_clock[str(tmdb)] = since_iso
                if hd_pid is None or not std_root:
                    self._log("log_warning", f"[UHD] cannot rehome 4K-only '{mv.get('title')}' — standard "
                                             f"'{std_inst}' has no ≤1080 profile / root folder.")
                    continue
                if eff_dry:
                    self._log("log_info", f"[UHD] would rehome 4K-only '{mv.get('title')}' (watch {lk:.0f}) → "
                                          f"1080p baseline on {std_inst}; 4K reclaimed once it lands.")
                    rehomed += 1
                    self._record_plan(mv.get("title"), tmdb, fourk, std_inst, "rehome-4k", "would-rehome",
                                      reason=f"watch {lk:.0f} < {demote_floor}")
                elif self._grab_baseline(gw, std_inst, mv, hd_pid, std_root):
                    rehomed += 1
                    grabbed.add(tmdb)
                    self._log("log_info", f"[UHD] rehoming 4K-only '{mv.get('title')}' (watch {lk:.0f}) → 1080p "
                                          f"baseline searching on {std_inst}; 4K reclaimed once it lands.")
                    self._record_plan(mv.get("title"), tmdb, fourk, std_inst, "rehome-4k", "rehomed",
                                      reason=f"watch {lk:.0f} < {demote_floor}")
                else:
                    self._log("log_warning", f"[UHD] rehome baseline grab failed for '{mv.get('title')}' "
                                             f"on {std_inst}; 4K left intact, will retry.")
            else:
                # Baseline RECORD exists (or just grabbed this run) but no FILE yet (a rehome in
                # flight) → keep clocking, wait for the import before evicting the 4K.
                new_clock[str(tmdb)] = since_iso

        # Prune ledger entries whose 4K record vanished from the library entirely.
        for t in [t for t in ledger if not _tmdb_present(t, present_tmdbs)]:
            ledger.pop(t, None)
        if self.global_cache is not None:
            # The dwell clock is non-destructive aging state → persist it even under eff_dry so a
            # title's below-floor time survives a preview / backup-degraded run (mirrors the
            # stale-prune clock). The demote LEDGER tracks REAL deletions, so it is only written on a
            # live run.
            writes = [(clock_key, new_clock)] if eff_dry else [(key, ledger), (clock_key, new_clock)]
            for _k, _v in writes:
                try:
                    self.global_cache.set(_k, _v)
                except Exception:
                    pass
        if demoted or rehomed or recovered:
            verb = "would " if eff_dry else ""
            self._log("log_info", f"[UHD] watchability 4K routing on {fourk}: {verb}demote {demoted}, "
                                  f"{verb}rehome {rehomed}, {verb}re-acquire {recovered} "
                                  f"(band {demote_floor}-{threshold}, dwell {dwell_days}d; baseline always survives).")

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
    def _reconcile_instance(self, gw, actuator, std_inst, fourk, dest_root, dest_pid,
                            present, hasfile, uhd_paths=None, uhd_res=None, can_remote_play=True,
                            uhd_ids=None, shared_ok=False):
        uhd_paths = uhd_paths or {}
        uhd_res = uhd_res or {}
        uhd_ids = uhd_ids or {}
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
                ast = actuator.acquire(mv, to_inst=fourk, dest_root=dest_root,
                                       dest_profile_id=dest_pid, from_inst=std_inst).get("status")
                if ast not in ("skip", "noop"):
                    acted += 1
                    self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} [{ast}]")
                    self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "acquire", ast)
                continue

            # DOWNLOAD-BASED dual-version for a 2160p that lives on the standard instance (or whose
            # 2160p is already on the 4K instance but the standard record isn't the baseline yet): the
            # 4K instance ACQUIRES its own 2160p if it has no file (download, search ON, ASAP), and the
            # standard record is retuned to its ≤1080 baseline — Radarr grabs a 1080p and REPLACES the
            # 2160p ON IMPORT (make-before-break: the source keeps its 2160p until the 1080p lands, so
            # it is never file-less). No cross-instance file move.
            already_baseline = (mv.get("qualityProfileId") == hd_pid) or (mv.get("hasFile") and 0 < res <= 1080)
            if already_baseline or not (res >= _UHD_RES or dest_hasfile):
                continue                                       # already a 1080p baseline, or nothing to do
            # MAKE-BEFORE-BREAK ACROSS INSTANCES: standard's existing 2160p is the only in-hand 4K copy,
            # so it is downgraded to 1080p ONLY once the 4K instance genuinely holds its OWN 2160p file —
            # never before. `uhd_has_2160` is that gate; until it's true the standard 2160p is held.
            uhd_has_2160 = dest_hasfile and uhd_res.get(tmdb, 0) >= _UHD_RES
            # PHASE 1 — the 4K instance does NOT yet hold its own 2160p: DRIVE it to acquire one (4K
            # profile + monitored + search) and FREEZE standard's 2160p (un-monitor) until that copy
            # lands. Driving the 4K side even when a record already exists fixes a stale / un-monitored 4K
            # shell that its own RSS would otherwise never grab.
            if not uhd_has_2160:
                # SHARED STORAGE → RELOCATE: import standard's EXISTING 2160p into the 4K instance
                # (copy → hardlink, no re-download; source untouched). Only when the source↔4K pair
                # shares a filesystem AND the actuator's per-title probe confirms the 4K instance can
                # see the file — otherwise it returns a non-relocating status and we DOWNLOAD instead.
                relocated = tmdb in self._relocated        # already relocated from another source instance
                # A prior sweep's relocate copy is async — while its marker is within the grace window the
                # import is still in flight, so HOLD (don't re-issue); an aged marker re-issues.
                pend = self._relo_prev.get(str(tmdb)) if not relocated else None
                if pend:
                    _, _age = clock_age(pend, self._relo_now)
                    if _age is not None and _age < self._RELOCATE_GRACE_DAYS:
                        relocated = True
                        self._relo_new[str(tmdb)] = pend   # keep aging until the 4K record latches hasFile
                        self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} "
                                              f"relocate pending (import in flight)")
                if shared_ok and not relocated:
                    rst = actuator.relocate(mv, to_inst=fourk, dest_root=dest_root,
                                            dest_profile_id=dest_pid, from_inst=std_inst,
                                            dest_id=uhd_ids.get(tmdb)).get("status")
                    if rst in ("relocating", "would-relocate"):
                        relocated = True
                        self._relocated.add(tmdb)
                        if rst == "relocating":            # a real async import was issued → track it
                            self._relo_new[str(tmdb)] = self._relo_now.isoformat()
                        acted += 1
                        self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} "
                                              f"relocate-4K (hardlink) [{rst}]")
                        self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "relocate-4k", rst)
                if not relocated:                              # download (no shared storage / not visible)
                    if not dest_present:                       # no 4K record yet → add it (search ON)
                        ast = actuator.acquire(mv, to_inst=fourk, dest_root=dest_root,
                                               dest_profile_id=dest_pid, from_inst=std_inst).get("status")
                    else:                                      # 4K record exists but no 2160p → drive it
                        ast = actuator.ensure_acquiring(uhd_ids.get(tmdb), inst=fourk,
                                                        profile_id=dest_pid).get("status")
                    if ast not in ("skip", "noop"):
                        acted += 1
                        self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → {fourk} acquire-4K [{ast}]")
                        self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "acquire-4k", ast)
                # Freeze standard's 2160p (un-monitor) so Radarr won't touch it while the 4K is acquired;
                # phase 2 re-monitors it at the 1080p baseline once the 4K copy is confirmed. The 2160p
                # file STAYS — so the title is never file-less and the only 4K copy is never lost.
                if mv.get("monitored") and mv.get("id") is not None:
                    fst = actuator.unmonitor(mv.get("id"), inst=std_inst).get("status")
                    verb = "would freeze" if fst == "would-freeze" else "froze"
                    self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}' {verb} standard 2160p "
                                          f"(un-monitored) until {fourk} confirms its 4K copy.")
                    self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "freeze-std", fst)
                continue
            # PHASE 2 — the 4K instance confirmed its own 2160p → reset standard to the ≤1080 baseline
            # (profile → monitor → search; Radarr replaces the 2160p on import).
            st = actuator.retune_baseline(mv, inst=std_inst, hd_profile_id=hd_pid).get("status")
            if st not in ("skip", "noop"):
                acted += 1
                self._log("log_info", f"[UHD] {std_inst}: '{mv.get('title')}'{watch} → 1080p baseline [{st}]")
                self._record_plan(mv.get("title"), tmdb, std_inst, fourk, "retune-1080", st)
        if acted:
            self._log("log_info", f"[UHD] {std_inst}: {acted} title(s) reconciled to dual-version on {fourk}.")

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

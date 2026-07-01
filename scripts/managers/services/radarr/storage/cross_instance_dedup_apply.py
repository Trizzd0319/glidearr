"""
cross_instance_dedup_apply.py — actuate a cross-instance dedup plan (file-only reclaim, gated).
================================================================================
Applies the plans from :mod:`...machine_learning.space.cross_instance_dedup` over an ``ArrGateway``.
The destructive half of the cross-instance reconcile, so it is paranoid by construction:

  • SAME-PATH plans (``flag_only``) are LOGGED and SKIPPED — two records pointing at one physical
    file cannot be split over the API, so deleting either side's moviefile would destroy the file the
    other still depends on. NEVER actioned.
  • Before any delete it RE-CONFIRMS the KEEPER instance still reports ``hasFile`` for the title in
    the current library snapshot (Invariant 1). No keeper-confirmed → no delete.
  • It then UN-MONITORS the loser record (so Radarr can't immediately re-grab and recreate the
    duplicate) and deletes only the loser's FILE (``DELETE moviefile/{id}``) — the loser's Radarr
    RECORD is preserved, so its id / grab history survive (the 1080p baseline, if wanted, is
    re-established by the cross-instance MOVE finalize on a later sweep).
  • Every write branches on the dry-run passed in by the caller, which threads ``effective_dry_run``
    (the backup gate) — a real run whose backup pre-flight failed reclaims nothing.

PURE orchestration over the gateway — no config; the caller owns the consent gates.
"""
from __future__ import annotations


class CrossInstanceDedup:
    def __init__(self, gw, logger=None, *, dry_run: bool = True):
        self.gw = gw
        self.logger = logger
        self.dry_run = bool(dry_run)
        self._hasfile_cache: dict = {}                       # inst -> {tmdbId: bool}

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    def _keeper_has_file(self, inst, tmdb, movie_id=None) -> bool:
        """Confirm the keeper still has its file. Prefer a FRESH, uncached per-title fetch
        (``GET movie/{id}``) right before the destructive delete, so an external mid-run change to the
        keeper is caught; fall back to the run-cached library scan when a fresh read isn't available.
        FAIL-SAFE: returns True only on a POSITIVE confirmation (a fresh read that errors falls back;
        a missing record / unreadable library → False, so no delete)."""
        im = getattr(self.gw, "im", None)
        if movie_id is not None and im is not None and hasattr(im, "_make_request"):
            resolved = self.gw.resolve(inst) if hasattr(self.gw, "resolve") else inst
            try:
                rec = im._make_request(resolved, f"movie/{movie_id}", fallback=None)
            except Exception:
                rec = None
            if isinstance(rec, dict):                        # fresh read succeeded — authoritative
                return bool(rec.get("hasFile"))
        if inst not in self._hasfile_cache:
            try:
                items = self.gw.library_items(inst) or []
            except Exception:
                items = []
            self._hasfile_cache[inst] = {
                m.get("tmdbId"): bool(m.get("hasFile"))
                for m in items if isinstance(m, dict) and m.get("tmdbId") is not None
            }
        return bool(self._hasfile_cache[inst].get(tmdb))

    def _loser_file_stale(self, inst, movie_id, file_id) -> bool:
        """POSITIVELY detect that the planned loser movieFile is no longer the loser's current file — a
        fresh ``GET movie/{id}`` whose movieFile id DIFFERS from ``file_id`` (re-imported under a new id
        by a concurrent retune/rescan) or that has no file at all (already reclaimed). The plan's
        ``loser_file_id`` is a run-start snapshot, so deleting a re-id'd file 404s and reclaims nothing
        (while still claiming success). Returns True ONLY on a positive fresh read that contradicts the
        plan; when it can't confirm (no id / no im / read error / id still matches) it returns False so
        the delete proceeds — never skip a valid reclaim on an unconfirmed read."""
        if movie_id is None:
            return False
        im = getattr(self.gw, "im", None)
        if im is None or not hasattr(im, "_make_request"):
            return False
        resolved = self.gw.resolve(inst) if hasattr(self.gw, "resolve") else inst
        try:
            rec = im._make_request(resolved, f"movie/{movie_id}", fallback=None)
        except Exception:
            return False
        if not isinstance(rec, dict):
            return False                                     # couldn't read → don't skip (proceed)
        return (rec.get("movieFile") or {}).get("id") != file_id   # different / missing → stale

    def apply(self, plan: dict) -> dict:
        """Advance one dedup plan. Returns ``{status, title, tmdb}``; status is flag-same-path /
        keeper-unconfirmed / would-dedup / deduped / skip."""
        tmdb = plan.get("tmdb")
        title = plan.get("title")

        if plan.get("is_same_path") or plan.get("action") == "flag_only":
            self._log("log_warning", f"[Dedup] '{title}' (tmdb {tmdb}): two records share ONE physical "
                                     f"file ({plan.get('path')}); cannot split over the API — flagged for "
                                     f"the operator, NOT reclaimed.")
            return {"status": "flag-same-path", "title": title, "tmdb": tmdb}

        if plan.get("action") != "reclaim_loser_file":
            return {"status": "skip", "title": title, "tmdb": tmdb}

        keeper_inst = plan.get("keeper_inst")
        loser_inst = plan.get("loser_inst")
        loser_file_id = plan.get("loser_file_id")
        loser_movie_id = plan.get("loser_movie_id")
        if loser_file_id is None or not keeper_inst or not loser_inst:
            return {"status": "skip", "reason": "incomplete plan", "title": title, "tmdb": tmdb}

        # INVARIANT 1: never delete a copy unless the keeper still has its file (fresh re-confirm).
        if not self._keeper_has_file(keeper_inst, tmdb, plan.get("keeper_movie_id")):
            self._log("log_warning", f"[Dedup] '{title}': keeper on {keeper_inst} no longer reports a "
                                     f"file — skipping reclaim (never leave the title copy-less).")
            return {"status": "keeper-unconfirmed", "title": title, "tmdb": tmdb}

        if self.dry_run:
            self._log("log_info", f"[Dedup] would reclaim '{title}': {plan.get('reason')} "
                                  f"(delete moviefile/{loser_file_id} on {loser_inst}).")
            return {"status": "would-dedup", "title": title, "tmdb": tmdb}

        # The planned loser_file_id is from a run-start snapshot; a concurrent retune/rescan (the
        # dual-version baseline retune runs the same sweep) can re-import the loser's file under a NEW id
        # or replace it with the 1080p, so deleting the stale id 404s + reclaims nothing (and must not
        # claim success). Fresh-confirm the SAME file is still current; if it has moved on, the reclaim is
        # already handled (or re-planned next run) — skip cleanly, no destructive call. Only guarded when
        # the loser record id is known.
        if self._loser_file_stale(loser_inst, loser_movie_id, loser_file_id):
            self._log("log_info", f"[Dedup] '{title}': loser's copy on {loser_inst} is already gone or "
                                  f"re-imported (moviefile/{loser_file_id} stale) — nothing to reclaim.")
            return {"status": "already-reclaimed", "title": title, "tmdb": tmdb}

        # Stop the loser re-grabbing and recreating the duplicate, then delete only its FILE. A
        # transient Radarr error on the delete fails THIS title only (self-heals next run) — never
        # aborts the rest of the sweep.
        if loser_movie_id is not None:
            try:
                self.gw.put(loser_inst, "movie/editor",
                            {"movieIds": [loser_movie_id], "monitored": False})
            except Exception as e:
                self._log("log_warning", f"[Dedup] '{title}': could not un-monitor loser on "
                                         f"{loser_inst} ({e}); proceeding with file reclaim.")
        try:
            self.gw.delete(loser_inst, f"moviefile/{loser_file_id}")
        except Exception as e:
            self._log("log_warning", f"[Dedup] '{title}': file reclaim failed on {loser_inst} "
                                     f"({e}); will retry next run.")
            return {"status": "dedup-failed", "title": title, "tmdb": tmdb}
        self._log("log_success", f"[Dedup] '{title}': {plan.get('reason')} — reclaimed "
                                 f"moviefile/{loser_file_id} on {loser_inst} (keeper on "
                                 f"{keeper_inst} confirmed; loser record kept).")
        return {"status": "deduped", "title": title, "tmdb": tmdb}

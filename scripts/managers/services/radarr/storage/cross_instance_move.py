"""
cross_instance_move.py — move a movie's 2160p FILE between two Radarr instances (pure orchestration).
================================================================================
Relocates the physical file from a source Radarr instance (e.g. ``standard``) to a destination
instance (e.g. ``ultra``) that SHARES the same storage tree, using only Radarr's own HTTP APIs —
glidearr never touches the filesystem, so this works whether glidearr runs alongside the *arr
stack or remotely (it only needs HTTP reach to both instances). The destination performs the
physical move via ``DownloadedMoviesScan importMode=Move`` against the source folder.

Two steps, idempotent across runs and make-before-break. NOTHING is ever deleted — not a physical
file, and not even the Radarr movie RECORD: the source movie is retuned IN PLACE, so its Radarr id,
grab/import history and added-date are all preserved (glidearr's own caches are tmdb-keyed, so they
are unaffected either way). The in-flight marker is the source's quality profile (a 2160p-capable
profile ≠ the baseline) plus its file state:

  • MOVE-IN (``dest_hasfile`` False, source has a 2160p file): un-monitor the source (so it can't
    auto-re-grab the 2160p while its file is being moved out from under it), add the title to the
    destination monitored + SEARCH OFF (we want the existing file, not a re-download), then trigger
    ``DownloadedMoviesScan importMode=Move`` on the destination pointed at the SOURCE folder → the
    destination moves+imports the 2160p into its own 4K root. The source FILE is untouched until the
    destination takes it.

  • FINALIZE (``dest_hasfile`` True AND the source is not yet at the baseline profile / a healthy
    ≤1080 file): the 2160p is safely on the destination, so retune the EXISTING source record to its
    ≤1080 baseline profile + re-monitor (``movie/editor`` — no delete), ``RescanMovie`` it so Radarr
    notices the 2160p file was moved away, then ``MoviesSearch`` → the source re-grabs the small
    1080p baseline (the ``4k_policy=='both'`` end-state: 1080p on the source + 2160p on the
    destination). Because the search runs after the rescan clears the stale file — and the source is
    left monitored at the 1080p profile — Radarr's own missing-file monitoring backstops the grab if
    the immediate search races the async rescan. Fires once (the source then sits at the baseline
    profile, so it is no longer selected).

If the destination import never completes (e.g. the instances don't really share storage, so the
destination can't see the source folder), ``dest_hasfile`` stays False, the source keeps its 2160p
file, and nothing is lost; the caller can fall back to a re-grab. Every write is gated by ``dry_run``.

PURE orchestration over an ``ArrGateway`` — no config, no registry; the caller supplies the
instances, the destination root + 2160p profile, the source ≤1080 baseline profile, and the
destination presence/has-file state it already fetched.
"""
from __future__ import annotations


class CrossInstanceMove:
    def __init__(self, gw, logger=None, *, dry_run: bool = True):
        self.gw = gw                       # ArrGateway("radarr", im, config, logger)
        self.logger = logger
        self.dry_run = bool(dry_run)

    def _log(self, level, msg):
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    @staticmethod
    def _file_res(movie) -> int:
        """Current file resolution (``movieFile.quality.quality.resolution``), 0 when no file."""
        q = (((movie.get("movieFile") or {}).get("quality") or {}).get("quality") or {})
        try:
            r = q.get("resolution")
            return int(r) if r is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _src_folder(src_movie) -> str:
        """The folder holding the source movie's file (parent of ``movieFile.path``), slash-tolerant.
        Falls back to the movie's own ``path`` (already a folder) when there is no file path."""
        mf = src_movie.get("movieFile") or {}
        file_path = mf.get("path")
        if file_path:
            p = str(file_path).replace("\\", "/").rstrip("/")
            return p.rsplit("/", 1)[0] if "/" in p else p
        return str(src_movie.get("path") or "").replace("\\", "/").rstrip("/")

    def relocate(self, src_movie: dict, *, from_inst: str, to_inst: str, dest_root: str,
                 dest_profile_id, hd_profile_id, dest_present: bool, dest_hasfile: bool) -> dict:
        """Advance the move for one source movie by one step. ``dest_present`` / ``dest_hasfile``
        are the destination state the caller already fetched. Returns ``{status, title, tmdb}``;
        status is would-* under dry_run, else moved-in / pending-import / finalized / add-failed /
        readd-failed / skip / noop."""
        tmdb = src_movie.get("tmdbId")
        src_id = src_movie.get("id")
        title = src_movie.get("title")
        if tmdb is None or src_id is None:
            return {"status": "skip", "reason": "missing id/tmdbId", "title": title, "tmdb": tmdb}
        monitored = bool(src_movie.get("monitored", True))

        # ── FINALIZE: 2160p on the destination → retune the EXISTING source record in place ─────
        if dest_hasfile:
            # SAFETY: never touch a title already AT the baseline profile, or already holding a
            # healthy ≤1080 file — that's a steady dual-version title (or one we already finalized),
            # not one we are moving. So a spurious finalize can't fire on it.
            if hd_profile_id is None:
                return {"status": "noop", "title": title, "tmdb": tmdb}
            if src_movie.get("qualityProfileId") == hd_profile_id or \
                    (src_movie.get("hasFile") and 0 < self._file_res(src_movie) <= 1080):
                return {"status": "noop", "title": title, "tmdb": tmdb}
            if self.dry_run:
                self._log("log_info", f"[Relocate] would finalize '{title}': retune {from_inst} to "
                                      f"1080p baseline + rescan + search (2160p already on {to_inst}).")
                return {"status": "would-finalize", "title": title, "tmdb": tmdb}
            # Retune the EXISTING record (no delete → Radarr id/history preserved). RescanMovie so
            # Radarr clears the moved-away 2160p, then search; left monitored at 1080p so Radarr's
            # missing-file monitoring backstops the grab if the search races the async rescan.
            self.gw.put(from_inst, "movie/editor",
                        {"movieIds": [src_id], "qualityProfileId": hd_profile_id, "monitored": True})
            self.gw.command(from_inst, {"name": "RescanMovie", "movieIds": [src_id]})
            self.gw.command(from_inst, {"name": "MoviesSearch", "movieIds": [src_id]})
            self._log("log_success", f"[Relocate] '{title}': 2160p on {to_inst}; {from_inst} retuned "
                                     f"in place to the 1080p baseline + rescan + search.")
            return {"status": "finalized", "title": title, "tmdb": tmdb}

        # ── MOVE-IN / PENDING: get the 2160p onto the destination (source file untouched) ───────
        folder = self._src_folder(src_movie)
        if not folder:
            return {"status": "skip", "reason": "no source file path", "title": title, "tmdb": tmdb}
        if self.dry_run:
            self._log("log_info", f"[Relocate] would move '{title}': {to_inst} imports 2160p from "
                                  f"{folder} → {dest_root} (Move); {from_inst} un-monitored meanwhile.")
            return {"status": "would-move-in", "title": title, "tmdb": tmdb}

        # Race guard + in-flight marker: stop the source auto-re-grabbing the 2160p once its file
        # is moved away (only on the first pass — already un-monitored on later passes).
        if monitored:
            self.gw.put(from_inst, "movie/editor", {"movieIds": [src_id], "monitored": False})
        if not dest_present:
            if not self._add_to_dest(src_movie, to_inst, dest_root, dest_profile_id):
                return {"status": "add-failed", "title": title, "tmdb": tmdb}
        self.gw.command(to_inst, {"name": "DownloadedMoviesScan", "path": folder, "importMode": "Move"})
        self._log("log_info", f"[Relocate] '{title}': {to_inst} importing 2160p from {folder} (Move); "
                              f"{from_inst} retuned to 1080p once the import confirms.")
        return {"status": "moved-in" if monitored else "pending-import", "title": title, "tmdb": tmdb}

    def acquire(self, src_movie: dict, *, to_inst: str, dest_root: str, dest_profile_id) -> dict:
        """Acquire a NEW 4K copy on the destination instance (monitored, SEARCH ON) for an owned
        movie that WARRANTS 4K but has none — the proactive dual-version fill. The SOURCE instance
        is untouched (it keeps its existing ≤1080 baseline); this only adds the 4K side. The caller
        owns eligibility (proactive_4k gate + watchability + space); this just emits the add."""
        tmdb = src_movie.get("tmdbId")
        title = src_movie.get("title")
        if tmdb is None:
            return {"status": "skip", "title": title, "tmdb": tmdb}
        if self.dry_run:
            self._log("log_info", f"[Relocate] would acquire a 4K copy of '{title}' on {to_inst} "
                                  f"(search ON; {src_movie.get('rootFolderPath') or 'source'} stays ≤1080).")
            return {"status": "would-acquire", "title": title, "tmdb": tmdb}
        ok = self._add_to_dest(src_movie, to_inst, dest_root, dest_profile_id, search=True)
        if ok:
            self._log("log_success", f"[Relocate] acquiring a 4K copy of '{title}' on {to_inst} (search ON).")
        return {"status": "acquired" if ok else "acquire-failed", "title": title, "tmdb": tmdb}

    def _add_to_dest(self, src_movie, to_inst, dest_root, dest_profile_id, *, search: bool = False) -> bool:
        """Add the title to the destination, monitored. ``search`` False (default) = take the moved
        file, not a fresh download (the MOVE path); True = acquire a fresh copy (the proactive 4K
        path). Clones the source object; strips the source identity + its absolute path so the
        destination's ``rootFolderPath`` is authoritative. Returns ok."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = dest_profile_id
        payload["rootFolderPath"] = dest_root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": bool(search)}
        try:
            return bool(self.gw.add(to_inst, payload))
        except Exception as e:
            self._log("log_warning", f"[Relocate] add to {to_inst} failed for "
                                     f"'{src_movie.get('title')}': {e}")
            return False

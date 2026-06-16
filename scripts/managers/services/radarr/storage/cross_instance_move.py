"""
cross_instance_move.py — move a movie's 2160p FILE between two Radarr instances (pure orchestration).
================================================================================
Relocates the physical file from a source Radarr instance (e.g. ``standard``) to a destination
instance (e.g. ``ultra``) that SHARES the same storage tree, using only Radarr's own HTTP APIs —
glidearr never touches the filesystem, so this works whether glidearr runs alongside the *arr
stack or remotely (it only needs HTTP reach to both instances). The destination performs the
physical move via ``DownloadedMoviesScan importMode=Move`` against the source folder.

Two steps, idempotent across runs and make-before-break — the source is never left without a copy
of the title, and a PHYSICAL file is never deleted (the only DELETE is a movie RECORD, always with
``deleteFiles=false``). The source's ``monitored`` flag is the unambiguous in-flight marker:

  • MOVE-IN (``dest_hasfile`` False, source still ``monitored``): un-monitor the source (so it can't
    auto-re-grab the 2160p while its file is being moved out from under it — this is also the
    in-flight marker), add the title to the destination monitored + SEARCH OFF (we want the
    existing file, not a re-download), then trigger ``DownloadedMoviesScan importMode=Move`` on the
    destination pointed at the SOURCE folder → the destination moves+imports the 2160p into its own
    4K root. The source FILE is untouched until the destination takes it; nothing is deleted.

  • FINALIZE (``dest_hasfile`` True AND source un-monitored — our marker): the 2160p is safely on
    the destination, so DELETE the (now-stale) source movie RECORD with ``deleteFiles=false`` (never
    touches a physical file) and re-add the source fresh at its ≤1080 baseline profile, monitored,
    SEARCH ON → it grabs the small 1080p baseline (the ``4k_policy=='both'`` end-state: 1080p on the
    source + 2160p on the destination). A fresh add can't be confused by the stale 2160p record, so
    the 1080p actually grabs. Re-add only proceeds when both a baseline profile and the source root
    are known, so the source is never deleted without a clean re-add.

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

        # ── FINALIZE: 2160p on the destination AND the source is our in-flight marker ───────────
        if dest_hasfile and not monitored:
            # SAFETY: never touch a title that already holds a healthy ≤1080 baseline file — that's
            # a steady dual-version title (or an already-finalized one) an operator merely
            # un-monitored, NOT one we are moving. ``monitored`` alone is operator-writable, so this
            # guard stops a spurious DELETE+re-add of a title that was never in a move.
            if src_movie.get("hasFile") and 0 < self._file_res(src_movie) <= 1080:
                return {"status": "noop", "title": title, "tmdb": tmdb}
            root = src_movie.get("rootFolderPath")
            if hd_profile_id is None or not root:
                self._log("log_warning", f"[Relocate] '{title}': 2160p on {to_inst}, but {from_inst} "
                                         f"has no ≤1080 profile / known root — leaving source as-is.")
                return {"status": "noop", "title": title, "tmdb": tmdb}
            if self.dry_run:
                self._log("log_info", f"[Relocate] would finalize '{title}': re-add {from_inst} as "
                                      f"1080p baseline + search (2160p already on {to_inst}).")
                return {"status": "would-finalize", "title": title, "tmdb": tmdb}
            # DELETE the stale source record (deleteFiles=false → no physical file touched), then
            # re-add fresh so a 1080p search isn't suppressed by the moved-away 2160p record.
            self.gw.delete(from_inst, f"movie/{src_id}?deleteFiles=false")
            if not self._readd_baseline(src_movie, from_inst, hd_profile_id, root):
                return {"status": "readd-failed", "title": title, "tmdb": tmdb}
            self._log("log_success", f"[Relocate] '{title}': 2160p on {to_inst}; {from_inst} re-added "
                                     f"as 1080p baseline + searching.")
            return {"status": "finalized", "title": title, "tmdb": tmdb}
        if dest_hasfile:
            return {"status": "noop", "title": title, "tmdb": tmdb}     # monitored + on dest → steady

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
                              f"{from_inst} re-added as 1080p once the import confirms.")
        return {"status": "moved-in" if monitored else "pending-import", "title": title, "tmdb": tmdb}

    def _readd_baseline(self, src_movie, from_inst, hd_profile_id, root) -> bool:
        """Re-add the title to the SOURCE instance fresh at its ≤1080 baseline profile, monitored,
        SEARCH ON — into its existing root folder (kids/standard preserved). A fresh add has no
        stale file record, so the 1080p search actually grabs. Returns ok."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = hd_profile_id
        payload["rootFolderPath"] = root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": True}
        try:
            return bool(self.gw.add(from_inst, payload))
        except Exception as e:
            self._log("log_warning", f"[Relocate] baseline re-add to {from_inst} failed for "
                                     f"'{src_movie.get('title')}': {e}")
            return False

    def _add_to_dest(self, src_movie, to_inst, dest_root, dest_profile_id) -> bool:
        """Add the title to the destination, monitored, SEARCH OFF (we want the moved file, not a
        fresh download). Clones the source object; strips the source identity + its absolute path so
        the destination's ``rootFolderPath`` is authoritative. Returns ok."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = dest_profile_id
        payload["rootFolderPath"] = dest_root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": False}
        try:
            return bool(self.gw.add(to_inst, payload))
        except Exception as e:
            self._log("log_warning", f"[Relocate] add to {to_inst} failed for "
                                     f"'{src_movie.get('title')}': {e}")
            return False

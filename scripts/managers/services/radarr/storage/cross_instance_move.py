"""
cross_instance_move.py — dual-version actuator across two Radarr instances (relocate OR download).
================================================================================
Drives the ``4k_policy=='both'`` end-state (2160p on the 4K instance + a ≤1080p baseline on the
standard instance). The 4K side gets its 2160p one of two ways, chosen by the caller from a
shared-storage probe:

  • ``relocate`` — SHARED STORAGE: import the standard instance's EXISTING 2160p file into the 4K
    instance with ``importMode=copy`` (Radarr hardlinks it when ``copyUsingHardlinks`` is on and both
    ends are one filesystem; a safe local copy on a cross-disk array union). NO re-download. Uses copy,
    NOT move, so the SOURCE file is untouched — make-before-break — until the standard record later
    retunes to 1080p and drops its 2160p (a hardlink survives that, the data persists on the 4K side).
    A per-title ``manualimport`` probe first confirms the 4K instance can actually SEE the source
    folder; if it can't (→ not really shared), ``relocate`` returns ``not-visible`` and the caller
    falls back to ``acquire``. (The earlier ``importMode=Move`` relocation was removed because a Move
    dropped the source immediately and jammed under load; this copy/hardlink variant is safe + only
    imports once the 4K instance proves it can read the file.)

  • ``acquire`` — NO shared storage (or the probe failed): the 4K side downloads its OWN 2160p (clone
    the record onto the 4K instance, monitored, SEARCH ON). The source instance is untouched. Works on
    any household: it only needs the 4K instance to reach indexers + a download client. ``ensure_
    acquiring`` drives an existing-but-fileless 4K record the same way (profile + monitor + search).

  • ``retune_baseline`` — the standard side is retuned DOWN to its ≤1080p baseline profile + re-
    monitor + RescanMovie + MoviesSearch. No file move, no delete: ``movie/editor`` retunes the
    EXISTING record in place (Radarr id / grab history / added-date preserved); Radarr then grabs a
    1080p and REPLACES the 2160p ON IMPORT. The 2160p stays on disk until the 1080p lands, so the title
    is never file-less.

Every write is gated by ``dry_run``. PURE orchestration over an ``ArrGateway`` — no config, no
registry; the caller supplies the instances, the 4K root + 2160p profile, and the standard ≤1080p
baseline profile.
"""
from __future__ import annotations

from urllib.parse import quote

_UHD_RES = 2160


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

    def retune_baseline(self, movie: dict, *, inst: str, hd_profile_id) -> dict:
        """Retune an owned 2160p record DOWN to its ≤1080 baseline profile + re-monitor, RescanMovie,
        then MoviesSearch — the standard half of download-based dual-version. NO file move and NO
        delete: ``movie/editor`` retunes the EXISTING record in place (Radarr id / grab history /
        added-date preserved), then Radarr grabs a 1080p and REPLACES the 2160p ON IMPORT. MAKE-BEFORE-
        BREAK: the 2160p stays on disk until the 1080p lands, so the title is never file-less; with a
        ≤1080 profile that disallows 2160p Radarr treats the 2160p as unwanted and searches, and its
        missing-file monitoring backstops the grab if the search races the rescan. Idempotent — a record
        already at the baseline profile, or already holding a ≤1080 file, is a no-op. dry_run-aware."""
        tmdb = movie.get("tmdbId")
        mid = movie.get("id")
        title = movie.get("title")
        if mid is None or hd_profile_id is None:
            return {"status": "noop", "title": title, "tmdb": tmdb}
        if movie.get("qualityProfileId") == hd_profile_id or \
                (movie.get("hasFile") and 0 < self._file_res(movie) <= 1080):
            return {"status": "noop", "title": title, "tmdb": tmdb}     # already the 1080p baseline
        if self.dry_run:
            self._log("log_info", f"[Relocate] would retune '{title}' on {inst} → 1080p baseline + "
                                  f"rescan + search (drops the 2160p once the 1080p imports).")
            return {"status": "would-retune", "title": title, "tmdb": tmdb}
        self.gw.put(inst, "movie/editor",
                    {"movieIds": [mid], "qualityProfileId": hd_profile_id, "monitored": True})
        self.gw.command(inst, {"name": "RescanMovie", "movieIds": [mid]})
        self.gw.command(inst, {"name": "MoviesSearch", "movieIds": [mid]})
        self._log("log_success", f"[Relocate] '{title}': {inst} retuned to the 1080p baseline + rescan "
                                 f"+ search (2160p replaced on import).")
        return {"status": "retuned", "title": title, "tmdb": tmdb}

    def acquire(self, src_movie: dict, *, to_inst: str, dest_root: str, dest_profile_id,
                from_inst=None) -> dict:
        """Acquire a NEW 4K copy on the destination instance (monitored, SEARCH ON) for an owned
        movie that WARRANTS 4K but has none — the proactive dual-version fill. The SOURCE instance
        is untouched (it keeps its existing ≤1080 baseline); this only adds the 4K side, carrying the
        source's tags across by label. The caller owns eligibility (proactive_4k gate + watchability
        + space); this just emits the add."""
        tmdb = src_movie.get("tmdbId")
        title = src_movie.get("title")
        if tmdb is None:
            return {"status": "skip", "title": title, "tmdb": tmdb}
        if self.dry_run:
            tags = self._src_tag_labels(src_movie, from_inst)
            tagtxt = f"; tags -> {tags}" if tags else ""
            self._log("log_info", f"[Relocate] would acquire a 4K copy of '{title}' on {to_inst} "
                                  f"(search ON; {src_movie.get('rootFolderPath') or 'source'} stays ≤1080{tagtxt}).")
            return {"status": "would-acquire", "title": title, "tmdb": tmdb}
        ok = self._add_to_dest(src_movie, to_inst, dest_root, dest_profile_id, search=True,
                               from_inst=from_inst)
        if ok:
            self._log("log_success", f"[Relocate] acquiring a 4K copy of '{title}' on {to_inst} (search ON).")
        return {"status": "acquired" if ok else "acquire-failed", "title": title, "tmdb": tmdb}

    def ensure_acquiring(self, movie_id, *, inst, profile_id) -> dict:
        """Drive an EXISTING 4K-instance record that has no 2160p yet to acquire one: set it to the 4K
        quality profile + monitored, then MoviesSearch. For a record that already exists but is stale /
        un-monitored (e.g. left over from earlier churn) so its own RSS would never grab. No add, no
        delete — just movie/editor + search. dry_run-aware."""
        if movie_id is None or profile_id is None:
            return {"status": "noop"}
        if self.dry_run:
            self._log("log_info", f"[Relocate] would set 4K record {movie_id} on {inst} → 4K profile "
                                  f"+ monitored + search.")
            return {"status": "would-acquire"}
        self.gw.put(inst, "movie/editor",
                    {"movieIds": [movie_id], "qualityProfileId": profile_id, "monitored": True})
        self.gw.command(inst, {"name": "MoviesSearch", "movieIds": [movie_id]})
        self._log("log_success", f"[Relocate] 4K record {movie_id} on {inst}: set 4K profile + "
                                 f"monitored + searched.")
        return {"status": "acquiring"}

    def relocate(self, src_movie: dict, *, to_inst: str, dest_root: str, dest_profile_id,
                 from_inst=None, dest_id=None) -> dict:
        """SHARED-STORAGE hardlink-relocate: import the source instance's EXISTING 2160p file into the
        4K instance with ``importMode=copy`` (Radarr hardlinks it when copyUsingHardlinks is on and both
        ends are one filesystem; a safe local copy on a cross-disk union). NO re-download; copy (not
        move) leaves the SOURCE file untouched → make-before-break. Ensures a monitored 4K record to
        import into (adds one, SEARCH OFF, if ``dest_id`` is None). A ``manualimport`` probe first
        confirms the 4K instance can SEE the source folder — if it can't, returns ``not-visible`` so the
        caller downloads instead. Requires the 4K instance's root folder to NOT contain the source tree
        (Radarr refuses to import from inside its own root). dry_run-aware."""
        tmdb = src_movie.get("tmdbId")
        title = src_movie.get("title")
        folder = str(src_movie.get("path") or "").replace("\\", "/").strip().rstrip("/")
        if not folder:
            sf = str(((src_movie.get("movieFile") or {}).get("path")) or "").replace("\\", "/").strip()
            folder = sf.rsplit("/", 1)[0] if "/" in sf else ""
        if tmdb is None or not folder:
            return {"status": "skip", "title": title, "tmdb": tmdb}
        if self.dry_run:
            self._log("log_info", f"[Relocate] would hardlink-relocate '{title}' ({folder}) → {to_inst} "
                                  f"(import copy → hardlink on one fs; source 2160p untouched).")
            return {"status": "would-relocate", "title": title, "tmdb": tmdb}
        # 1. PROBE FIRST (read-only): can the 4K instance actually SEE an importable file in the source
        #    folder? This is the per-title shared-storage confirm. It runs BEFORE any write so that a
        #    not-visible result adds NOTHING — the caller then downloads and adds the record itself
        #    (adding here first would duplicate the 4K record).
        cand = self._manual_import_candidate(to_inst, folder)
        if cand is None:
            self._log("log_info", f"[Relocate] {to_inst} sees no importable file in '{folder}' — not "
                                  f"shared storage for '{title}'; caller will download instead.")
            return {"status": "not-visible", "title": title, "tmdb": tmdb}
        # 2. a 4K record to import into — monitored, 4K profile, NO search (we import the existing file).
        mid = dest_id
        if mid is None:
            payload = self._build_dest_payload(src_movie, to_inst, dest_root, dest_profile_id,
                                               search=False, from_inst=from_inst)
            try:
                created = self.gw.add(to_inst, payload)
            except Exception as e:
                self._log("log_warning", f"[Relocate] add to {to_inst} failed for '{title}': {e}")
                return {"status": "failed", "title": title, "tmdb": tmdb}
            mid = created.get("id") if isinstance(created, dict) else None
            if mid is None:
                return {"status": "failed", "title": title, "tmdb": tmdb}
        else:
            self.gw.put(to_inst, "movie/editor",
                        {"movieIds": [mid], "qualityProfileId": dest_profile_id, "monitored": True})
        # 3. import COPY (→ hardlink on one fs) into the 4K record; the SOURCE file is left in place.
        cand["movieId"] = mid
        self.gw.command(to_inst, {"name": "ManualImport", "importMode": "copy", "files": [cand]})
        self._log("log_success", f"[Relocate] '{title}': importing existing 2160p into {to_inst} "
                                 f"(copy → hardlink; no re-download; source stays until it retunes).")
        return {"status": "relocating", "title": title, "tmdb": tmdb}

    def _manual_import_candidate(self, inst, folder):
        """GET ``manualimport`` for ``folder`` on ``inst`` and return the largest GENUINE-2160p,
        non-rejected importable video file as a ManualImport ``files[]`` entry (no ``movieId`` yet —
        the caller injects it after ensuring the record) — or None when the instance sees no eligible
        2160p file there (it does not share the source storage, the folder is empty/unreadable, or it
        holds only sub-4K / Radarr-rejected files). The None-path is the caller's cue to DOWNLOAD.
        The ``resolution >= 2160`` filter keeps the caller's ``uhd_has_2160`` gate honest — relocating a
        sub-4K file would never satisfy it, so the reconcile would re-import forever and never converge;
        skipping ``rejections`` avoids force-importing a sample/unparsed companion."""
        resp = self.gw.get(inst, f"manualimport?folder={quote(folder)}&filterExistingFiles=false",
                           fallback=None)
        if not isinstance(resp, list) or not resp:
            return None
        best = None
        for it in resp:
            if not isinstance(it, dict) or not it.get("path") or not it.get("quality"):
                continue
            if it.get("rejections"):                       # Radarr already refused it (sample/unparsed/…)
                continue
            if self._file_res({"movieFile": it}) < _UHD_RES:   # only relocate a real 2160p
                continue
            if best is None or (it.get("size") or 0) > (best.get("size") or 0):
                best = it
        if best is None:
            return None
        return {
            "path": best.get("path"),
            "quality": best.get("quality"),
            "languages": best.get("languages") or [],
            "releaseGroup": best.get("releaseGroup") or "",
        }

    def unmonitor(self, movie_id, *, inst) -> dict:
        """Freeze a record by un-monitoring it (keeps its file; stops Radarr touching/upgrading it).
        Used to hold the standard 2160p while the 4K instance acquires its own copy. dry_run-aware."""
        if movie_id is None:
            return {"status": "noop"}
        if self.dry_run:
            self._log("log_info", f"[Relocate] would unmonitor record {movie_id} on {inst} (freeze).")
            return {"status": "would-freeze"}
        self.gw.put(inst, "movie/editor", {"movieIds": [movie_id], "monitored": False})
        return {"status": "frozen"}

    def _src_tag_labels(self, src_movie, from_inst) -> list:
        """The LABELS of the source movie's tags. Radarr tag ids are per-instance, so we carry the
        labels (not the raw ids) across instances. Empty when there are no tags or no source."""
        ids = src_movie.get("tags") or []
        if not ids or not from_inst:
            return []
        by_id = {t.get("id"): t.get("label") for t in (self.gw.tags(from_inst) or [])
                 if t.get("id") is not None}
        return [by_id[i] for i in ids if by_id.get(i)]

    def _dest_tag_ids(self, labels, to_inst) -> list:
        """Resolve labels to the DESTINATION instance's tag ids, creating any that are missing, so
        the moved/acquired copy carries the SAME tags (keep/universe/franchise) as the source."""
        out = []
        for label in labels:
            tid = self.gw.ensure_tag(to_inst, label)
            if tid is not None and tid not in out:
                out.append(tid)
        return out

    def _build_dest_payload(self, src_movie, to_inst, dest_root, dest_profile_id, *,
                            search: bool = False, from_inst=None) -> dict:
        """Clone the source movie into a destination ADD payload: strip the source identity + its
        absolute path so the destination ``rootFolderPath`` is authoritative, force the destination
        quality profile, set monitored + ``searchForMovie=search`` (acquire → True downloads;
        relocate → False imports the existing file), and carry TAGS across by LABEL (per-instance ids
        would be wrong; missing labels are created on the destination)."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = dest_profile_id
        payload["rootFolderPath"] = dest_root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": bool(search)}
        payload["tags"] = self._dest_tag_ids(self._src_tag_labels(src_movie, from_inst), to_inst)
        return payload

    def _add_to_dest(self, src_movie, to_inst, dest_root, dest_profile_id, *, search: bool = False,
                     from_inst=None) -> bool:
        """Add the title to the destination (see :meth:`_build_dest_payload`). Returns ok."""
        try:
            return bool(self.gw.add(to_inst, self._build_dest_payload(
                src_movie, to_inst, dest_root, dest_profile_id, search=search, from_inst=from_inst)))
        except Exception as e:
            self._log("log_warning", f"[Relocate] add to {to_inst} failed for "
                                     f"'{src_movie.get('title')}': {e}")
            return False

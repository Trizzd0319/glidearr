"""
cross_instance_move.py — DOWNLOAD-BASED dual-version actuator across two Radarr instances.
================================================================================
Drives the ``4k_policy=='both'`` end-state (2160p on the 4K instance + a ≤1080p baseline on the
standard instance) using ONLY each instance's own indexers + download client — NO cross-instance file
move. The earlier ``importMode=Move`` relocation was removed: it required a shared mount + reliable
filesystem-move semantics that can't be guaranteed across households (unRAID ``/mnt/user`` FUSE moves
were not durable — the moved file vanished on the next rescan), so it isn't a portable mechanism.

Two independent, make-before-break actuations the caller composes per title:

  • ``acquire`` — the 4K side gets its OWN 2160p (clone the record onto the 4K instance, monitored,
    SEARCH ON) so it downloads ASAP. The source instance is untouched. Works on any household: it only
    needs the 4K instance to reach indexers + a download client.

  • ``retune_baseline`` — the standard side is retuned DOWN to its ≤1080p baseline profile + re-
    monitor + RescanMovie + MoviesSearch. No file move, no delete: ``movie/editor`` retunes the
    EXISTING record in place (Radarr id / grab history / added-date preserved); Radarr then grabs a
    1080p and REPLACES the 2160p ON IMPORT. The 2160p stays on disk until the 1080p lands, so the title
    is never file-less.

Both are idempotent (a record already at the 4K instance / already at the 1080p baseline is a no-op)
and every write is gated by ``dry_run``. PURE orchestration over an ``ArrGateway`` — no config, no
registry; the caller supplies the instances, the 4K root + 2160p profile, and the standard ≤1080p
baseline profile.
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

    def _add_to_dest(self, src_movie, to_inst, dest_root, dest_profile_id, *, search: bool = False,
                     from_inst=None) -> bool:
        """Add the title to the destination, monitored, with ``searchForMovie=search`` (acquire always
        passes True → the 4K instance downloads its own copy). Clones the source object; strips the
        source identity + its absolute path so the destination's ``rootFolderPath`` is authoritative;
        carries TAGS across by label (per-instance ids would be wrong) and forces the destination
        quality profile. Returns ok."""
        payload = dict(src_movie)
        for k in ("id", "movieFile", "movieFileId", "path", "folderName"):
            payload.pop(k, None)
        payload["qualityProfileId"] = dest_profile_id
        payload["rootFolderPath"] = dest_root
        payload["monitored"] = True
        payload.setdefault("minimumAvailability", "released")
        payload["addOptions"] = {"searchForMovie": bool(search)}
        # Tags by LABEL, not the source's per-instance ids (else the destination gets mismatched or
        # nonexistent tags). Missing labels are created on the destination.
        payload["tags"] = self._dest_tag_ids(self._src_tag_labels(src_movie, from_inst), to_inst)
        try:
            return bool(self.gw.add(to_inst, payload))
        except Exception as e:
            self._log("log_warning", f"[Relocate] add to {to_inst} failed for "
                                     f"'{src_movie.get('title')}': {e}")
            return False

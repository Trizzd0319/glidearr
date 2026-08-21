"""
SonarrSpacePressureManager — Stage-1 TV downgrade under space pressure
=====================================================================
The Sonarr twin of ``RadarrSpacePressureManager.run_downgrades`` (Phase 3 of the
cross-service space plan). When free space is in the pressure band (free < U),
downgrade the lowest-watchability SERIES to HD-720p and REALIZE the reclaim, freeing
space BEFORE anything is deleted. Non-destructive and reversible (you keep the
show, just at a lower quality).

*arr never downgrades an existing file (a file above the new profile's cutoff →
"cutoff met" → every release rejected), so the old live path — PUT the series
profile + SeriesSearch and hope — reclaimed NOTHING (TV step-down space was
phantom). The live branch now mirrors the shipped movie/universe realize at
EPISODE-FILE granularity: after the profile PUT, each owned file above the target
resolution gets ONE interactive ``release?episodeId=`` search (the shared
``_pick_stepdown_release`` ladder picker, imported from the Radarr manager, with an
episode-sized fake floor); a pick exists → DELETE ``episodefile/{fid}`` then POST
the guid grab; grab error → post-delete blind ``EpisodeSearch`` fallback (chunked —
effective once the file is gone, since the cutoff-met blocker died with it); no
pick → file KEPT (a title is never traded for an empty indexer result), profile
stays lowered, counted ``no_release``, re-probes next run. An inline cap
(``tv_downgrade_realize_cap``, default 15) bounds the slow interactive searches per
pass; files over the cap re-qualify next run while still oversized (the planner
re-picks their series from file resolutions, not the profile).

Differences from the Radarr movie template:
  * SERIES-level — episode_files.parquet is per-episode, but watchability_score is
    per-series (broadcast onto every row by refresh_scores). We group by series_id,
    score once per series, change the SERIES qualityProfileId (PUT series/{id}),
    and stamp the plan on every episode row of that series. The REALIZE step then
    walks the series' owned episode files individually.
  * Reads the already-broadcast ``watchability_score`` column (Phase 2) — it does
    NOT recompute scores.
  * Adds a recently-AIRED guard (no Radarr analog): never downgrade a series with an
    episode aired within RECENT_AIR_DAYS.
  * U-target loop — downgrades just enough (lowest score first) to project free ≥ U,
    rather than downgrading the whole low-value catalog at once (avoids a re-grab
    storm on a large TV library). Falls back to "all candidates" if U is unreachable.

Gating lives in the orchestration wrapper (run_space_pressure_downgrades): it only
calls run_downgrades when free < U and ``tv_downgrade_enabled`` is set. dry_run only
changes whether the PUT/search actually fire — the plan is always stamped + persisted
so it is previewable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.ledger.decision_ledger import stamp
from scripts.managers.machine_learning.space.downgrade_planner import plan_series_downgrades
from scripts.managers.machine_learning.lifecycle.restore_policy import (
    episode_key,
    merge_ledger_entry,
    release_record,
)
from scripts.managers.machine_learning.space.reclaim_ledger import (
    planned_reclaim_gb,
    record_planned_reclaim,
)
from scripts.managers.machine_learning.space.seed_gate import (
    obligation_shortfall,
    seed_config,
    should_defer_stepdown,
    unbounded_seeding,
)
from scripts.managers.machine_learning.thresholds.registry import get_threshold
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import (
    downgrade_regrab_cap, exhaustive_downgrade, space_targets,
)

# The step-down release picker is SHARED with the movie/universe realize paths —
# imported, not copied (same ladder semantics: rungs >= 720 strictly below the current
# resolution, 720 -> 1080 climb, median size in a rung, undersized fakes rejected).
_pick_stepdown_release = RadarrSpacePressureManager._pick_stepdown_release


class SonarrSpacePressureManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    HD_720P_PROFILE_NAME = "HD-720p"
    PRESSURE_FALLBACK_GB = 0.0   # NO last-resort floor (config free_space_limit, else 25% of total, else none)
    RECENT_WATCH_DAYS    = 7      # don't downgrade a series watched within this window
    RECENT_AIR_DAYS      = 30     # don't downgrade a series with a very recently aired ep
    # 17, not 20: re-anchored with the whole delete family when Group D v2 replaced a
    # near-constant +12 bonus with a transcode-risk penalty and translated the score axis
    # (file-owning series median 21 -> 8). See machine_learning/thresholds/registry.py.
    DEFAULT_SCORE_CEILING = 17    # tv_space_pressure_score_ceiling default (0-100 scale)
    # GLD-INV-01 — row origins the SIZE math must ignore.
    #
    # A pilot row is a codec/quality FINGERPRINT: one representative file per series from
    # _pick_representative_file, with episode_number=None. It was never an inventory of the
    # series, but this pass read its size_bytes/resolution as though it were — so Marvel's
    # Daredevil (39 episodes of 4K HDR on disk) presented as one 0.4 GB 480p file, and
    # library-wide 12,075 pilot rows carried 5,160 GB of single-file sizes standing in for
    # whole series.
    #
    # THIS IS ALSO A CORRECTNESS PRECONDITION FOR _ingest_inventory_tv, not just a tidy-up:
    # once that pass writes real per-episode rows, the pilot row for the same series still
    # exists (it is the score/keep anchor other passes read), so counting BOTH would
    # double-count the representative file on top of the real inventory. Excluding by
    # origin is what makes the two safe together.
    #
    # Precedent: cold_scan rows are already excluded from a consumer for the same
    # "this row is not what it looks like" reason (episode_files ~4337).
    NON_INVENTORY_ROWS = ("pilot",)
    DEFAULT_RUNTIME_MIN  = 45.0   # fallback per-episode runtime when unknown
    # DELETE-scoped keep policies (what the Sonarr tags actually mean — see
    # cache/episode_files._resolve_keep_policy_map: "No episode from this series will ever
    # be marked for deletion"). Documentation only on this path; the delete guard lives in
    # _apply_grace_period and matches these labels inline.
    #   NOTE 'keep_universe' / 'keep_forever' are RADARR policies. _resolve_keep_policy_map
    #   only ever emits 'keep_series' | 'keep_season' | None for a TV row, so those two
    #   entries could never match here — dead branches, kept out of the live set below.
    KEEP_TAGS = frozenset({"keep_series", "keep_season"})
    # DOWNGRADE-scoped keep policies — DELIBERATELY EMPTY. GLD-TVQ-01.
    #
    # A keep tag answers "may this episode be DELETED?", not "may its quality change?".
    # Feeding the delete-scoped set into plan_series_downgrades made a keep-tagged series
    # permanently immune to the step-down ladder as well, which is the opposite of the
    # operator's intent: a pinned series should hold its EPISODES (playlist order, saga
    # completeness) while still shrinking toward the floor under space pressure. On a live
    # run that silently removed 78 series from the downgrade pool.
    #
    # The movie twin does gate on keep tags, but for an unrelated reason: keep_universe /
    # bare universe titles have their quality owned by the universe manager's credit-gated
    # ladder (see space.downgrade_planner.plan_movie_downgrades). There is NO equivalent
    # second owner on the TV side, so a skip here is a permanent exemption, not a handoff.
    #
    # The planner keeps its ``keep_tags`` parameter (the movie path and the unit tests both
    # exercise it) — this is a POLICY choice at the service boundary, not a capability removal.
    DOWNGRADE_KEEP_TAGS = frozenset()
    DEFAULT_REALIZE_CAP  = 15     # tv_downgrade_realize_cap default — episode files searched+replaced
                                  # inline per pass (interactive searches are slow); rest defers
    SEARCH_CHUNK         = 100    # episodeIds per blind EpisodeSearch fallback command (Sonarr accepts a list)
    STEPDOWN_MIN_RELEASE_BYTES = 50 * 1024 * 1024   # episode fake/undersized floor for the shared picker
                                  # (movies use 300 MiB; a legit 720p episode can be far smaller)
    # ---- GLD-RST-02: the PRE-DOWNGRADE release archive -----------------------
    # A step-down is the ONLY path in this system that destroys a file without
    # recording what it was. The delete pass writes sonarr/{inst}/deleted_episodes via
    # restore_policy.release_record; this pass wrote nothing, so a downgrade was strictly
    # one-way: once the 1080p file is gone, nothing knows it was a 2.18 GB x265 NTb file,
    # and nothing can identify it in a recycle bin or an indexer search afterwards.
    #
    # DELIBERATELY A SEPARATE KEY from deleted_episodes. That ledger is an INPUT to
    # restore_recovered_episode_deletions, which re-monitors and re-grabs on score
    # RECOVERY. Filing step-downs there would make the restore pass fight the space pass:
    # every series shrunk under pressure would be queued to grow back the moment its score
    # ticked up -- the upgrade pass's job, and not a decision space pressure asked for.
    # This key is an ARCHIVE ("here is what we gave up"), never a queue: NOTHING reads it
    # to take an action. Same entry SHAPE as deleted_episodes, so ledger_releases /
    # merge_ledger_entry / match_release read it unchanged.
    STEPDOWN_RELEASES_KEY = "sonarr/{inst}/stepdown_releases"
    # GLD-RST-06 — when each (episode, resolution) was FIRST held back by the seed
    # gate, so `max_defer_days` can expire a deferral that is never going to clear.
    # Separate from stepdown_cooldown: that ledger backs off after a FAILED search
    # and its retry cadence is tuned for indexer flakiness, whereas this records a
    # deliberate not-yet on a healthy candidate. Sharing one key would make an
    # expiring seed deferral look like a failing search and vice versa.
    SEED_DEFER_KEY = "sonarr/{inst}/seed_defer"
    # Ceiling on gate examinations per pass. A deferred file does NOT consume the
    # realize budget (it never gets searched, so it costs no indexer call and should
    # not burn a slot), but each examination IS one Sonarr history call — so without
    # a bound, a library where most files are pinned would walk the entire candidate
    # set every run hitting the API. Expressed as a multiple of the realize budget so
    # it scales with whatever the operator set rather than being a second magic number.
    SEED_GATE_EXAMINE_MULT = 4
    # Query params that carry an indexer SECRET. A grab URL is worth archiving (it is the
    # release/push descriptor) but it embeds the indexer api key, and this ledger is a
    # plaintext JSON cache that safe_cache_clear deliberately PRESERVES. The value is
    # redacted on the way in and the key re-injected at push time from indexer config --
    # so a leaked cache leaks a URL, not a credential.
    _SECRET_QS_KEYS = ("apikey", "api_key", "apikey", "passkey", "rss_key", "token", "r", "i")

    # ---- GLD-RST-02 helpers -------------------------------------------------
    @staticmethod
    def _redact_grab_url(url, secret_keys) -> "str | None":
        """*url* with every secret-bearing query param blanked to ``<redacted>``.

        Returns None for anything unparseable rather than storing a half-scrubbed
        string: a URL we cannot confidently redact is one we must not persist.
        """
        if not url:
            return None
        try:
            from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
            p = urlparse(str(url))
            if not p.scheme or not p.netloc:
                return None
            qs = [(k, "<redacted>" if k.lower() in secret_keys else v)
                  for k, v in parse_qsl(p.query, keep_blank_values=True)]
            return urlunparse(p._replace(query=urlencode(qs)))
        except Exception:
            return None

    def _push_descriptor(self, instance, eid) -> dict:
        """The ``POST /release/push`` descriptor for the file about to be destroyed.

        Sonarr can re-inject a known release WITHOUT an indexer search via
        ``release/push`` (the autodl/autobrr path), which needs title + downloadUrl +
        protocol + publishDate. Those live only in Sonarr's grab HISTORY, and history
        is finite -- so it is captured HERE, at the moment of destruction, rather than
        hoped for later.

        Best-effort by construction: a missing descriptor degrades the future restore
        to an indexer search, which is what happens today. It must never cost a
        step-down, so every failure path returns {} and the caller proceeds.
        """
        try:
            raw = self.sonarr_api._make_request(
                instance, f"history?episodeId={int(eid)}&eventType=1&pageSize=50",
                fallback=None)
        except Exception:
            return {}
        # Sonarr returns a bare list on some routes and a paged {records:[...]} envelope
        # on others; tolerate both rather than betting on one.
        rows = raw.get("records") if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or not rows:
            return {}
        newest = max(rows, key=lambda r: str(r.get("date") or "") if isinstance(r, dict) else "")
        if not isinstance(newest, dict):
            return {}
        data = newest.get("data") if isinstance(newest.get("data"), dict) else {}
        secrets = {k.lower() for k in self._SECRET_QS_KEYS}
        out = {
            "title":        newest.get("sourceTitle"),
            "protocol":     data.get("protocol") or newest.get("protocol"),
            "publish_date": data.get("publishedDate"),
            "indexer":      data.get("indexer"),
            "info_url":     self._redact_grab_url(data.get("nzbInfoUrl"), secrets),
            "download_url": self._redact_grab_url(data.get("downloadUrl"), secrets),
            # Torrents only, and the ONE durable identifier in the whole record: an
            # infohash is content-addressed, so it stays valid for as long as a swarm
            # exists. A usenet grab has no equivalent -- see the note on download_url.
            "info_hash":    data.get("torrentInfoHash"),
        }
        return {k: v for k, v in out.items() if v}

    def _archive_stepdown_release(self, instance, sid, sn, en, row, eid, from_res, to_res) -> dict:
        """File what this episode WAS, immediately before the step-down destroys it.

        Returns the push descriptor (``{}`` when unavailable) so the caller can read
        ``info_hash`` off it — that is how a torrent-sourced file is identified for the
        GLD-RST-05 pinned-reclaim split, using the SAME history call the archive already
        makes rather than a second one.

        Never raises and never blocks the step-down: the archive is a courtesy to a
        future restore, and losing it must not cost the reclaim that is the point of
        the pass. dry_run writes nothing -- a disarmed run must not mutate a ledger.
        """
        if getattr(self, "dry_run", False):
            return {}
        push = {}
        try:
            push = self._push_descriptor(instance, eid) or {}
            ekey = episode_key(sn, en)
            rec = release_record(row)
            if not ekey or not rec:
                return push
            rec = dict(rec)
            if push:
                rec["push"] = push
            rec["from_resolution"] = from_res
            rec["to_resolution"] = to_res
            rec["archived_at"] = datetime.now(timezone.utc).isoformat()
            key = self.STEPDOWN_RELEASES_KEY.format(inst=instance)
            ledger = self.global_cache.get(key) or {}
            sid_key = str(int(sid))
            ledger[sid_key] = merge_ledger_entry(
                ledger.get(sid_key), {"episodes": [[int(sn), int(en)]], "releases": {ekey: rec}})
            self.global_cache.set(key, ledger)
        except Exception as e:
            self.logger.log_debug(f"  \U0001f4dd step-down archive skipped ({e}) — reclaim unaffected.")
        return push

    # ---- GLD-RST-06: seed gate plumbing -------------------------------------
    def _qbit(self):
        """Lazily-built read-only qBittorrent client, or None when not configured.

        Import is local so a deployment without the download_clients block (or
        without `requests`) never pays for the module at all.
        """
        if getattr(self, "_qbit_client", "__unset__") == "__unset__":
            try:
                from scripts.managers.services.qbittorrent.client import QbittorrentClient
                c = QbittorrentClient(config=self.config, logger=self.logger)
                self._qbit_client = c if c.enabled else None
            except Exception:
                self._qbit_client = None
        return self._qbit_client

    def _torrent_state(self, info_hash):
        """The gate's three-way answer for one infohash.

        ``None``      client answered and does not hold it -> proceed
        ``"unknown"`` client could not be asked            -> defer (assume pinned)
        ``dict``      client holds it                      -> defer

        Results are memoised for the pass: one candidate series can put many episodes
        of the same season pack through here, and a season pack is ONE torrent.
        """
        if not info_hash:
            return None
        cache = getattr(self, "_qbit_seen", None)
        if cache is None:
            cache = self._qbit_seen = {}
        key = str(info_hash).strip().lower()
        if key in cache:
            return cache[key]
        client = self._qbit()
        if client is None:
            cache[key] = "unknown"
            return "unknown"
        records, reachable = client.states_for([key])
        cache[key] = records.get(key) if reachable else "unknown"
        return cache[key]

    def _seed_defer_mark(self, instance, eid, res, action="read"):
        """The seed-deferral window ledger. *action* is ``read`` | ``stamp`` | ``clear``.

        Read is deliberately separated from stamp. The gate needs the EXISTING mark to
        evaluate `max_defer_days` before it can decide, but a file that then proceeds
        must not have been written at all -- an unconditional stamp-then-clear wrote
        this JSON ledger twice for every candidate the gate waved through, including
        every usenet file that can never defer.
        """
        key = self.SEED_DEFER_KEY.format(inst=instance)
        try:
            ledger = self.global_cache.get(key) or {}
            ekey = f"{int(eid)}:{int(res) if res else 0}"
            if action == "read":
                return ledger.get(ekey)
            if getattr(self, "dry_run", False):
                return ledger.get(ekey)       # a disarmed run reads but never writes
            if action == "clear":
                if ekey in ledger:
                    ledger.pop(ekey, None)
                    self.global_cache.set(key, ledger)
                return None
            if ekey not in ledger:            # stamp: first deferral only
                ledger[ekey] = datetime.now(timezone.utc).isoformat()
                self.global_cache.set(key, ledger)
            return ledger.get(ekey)
        except Exception:
            return None                       # never let bookkeeping block the pass

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)

        # Resolve dry_run robustly — this manager PUTs to Sonarr, so never default
        # to False silently (the dry_run-propagation footgun). Walk kwargs → parent
        # → SonarrManager → Main; raise if unresolvable.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        for _root_name in ("SonarrManager", "Main"):
            if _dry_run is not None:
                break
            if self.registry:
                try:
                    _root = self.registry.get("manager", _root_name)
                    _dry_run = getattr(_root, "dry_run", None) if _root else None
                except Exception:
                    _dry_run = None
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"SonarrManager, or Main. Refusing to initialize without an explicit "
                f"value to prevent accidental live profile changes."
            )
        self.dry_run = bool(_dry_run)

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    def prepare(self):
        pass

    def run(self):
        # No-op for the SonarrSeriesManager component-iteration; the downgrade pass
        # is driven by the orchestration (run_space_pressure_downgrades) AFTER
        # refresh_scores so it operates on fresh watchability scores.
        return {}

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def get_free_space_gb(self, instance: str) -> float:
        """Free space (GiB) across this instance's disks, mount-deduped."""
        if self.sonarr_api is None:
            return float("inf")
        return self.sonarr_api.disk_free_gb(instance)

    def _space_targets(self, instance: str | None = None) -> tuple[float, float]:
        """(T, U) from the shared helper. When ``free_space_limit`` is unset the floor
        defaults to 25% of the total drive (mount-deduped via ``disk_total_gb``);
        PRESSURE_FALLBACK_GB is the last resort only when the total is also unreadable."""
        total_gb = None
        if instance is not None and self.sonarr_api is not None:
            try:
                total_gb = self.sonarr_api.disk_total_gb(instance)
            except Exception:
                total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, total_gb)
        return space_targets(self.config, fallback_gb=self.PRESSURE_FALLBACK_GB, total_gb=total_gb)

    def _score_ceiling(self) -> float:
        try:
            ceiling = float((self.config or {}).get("tv_space_pressure_score_ceiling", self.DEFAULT_SCORE_CEILING))
        except (TypeError, ValueError):
            ceiling = float(self.DEFAULT_SCORE_CEILING)
        return get_threshold("tv_delete_ceiling", self.config, ceiling, logger=getattr(self, "logger", None))

    def _realize_cap(self) -> int:
        """Inline per-pass budget of episode files REALIZED (interactive-searched and, when
        a smaller release exists, deleted + re-grabbed). Interactive searches are slow
        (seconds each), so the pass bounds them; files over the cap are counted
        ``deferred`` and re-qualify next run while still oversized (the planner re-picks
        their series from file resolutions). 0 = profile flips only, nothing realized."""
        try:
            v = int((self.config or {}).get("tv_downgrade_realize_cap", self.DEFAULT_REALIZE_CAP))
        except (TypeError, ValueError):
            return int(self.DEFAULT_REALIZE_CAP)
        return max(0, v)

    def _get_episode_files_manager(self):
        try:
            return self.registry.get("manager", "SonarrCacheEpisodeFilesManager")
        except Exception:
            return None

    @timeit("_fetch_hd720p_profile")
    def _fetch_hd720p_profile(self, instance: str) -> dict | None:
        """Fetch the HD-720p quality profile from Sonarr by exact name (Sonarr's
        endpoint is camelCase ``qualityProfile``; Radarr's is lowercase)."""
        if self.sonarr_api is None:
            return None
        profiles = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        for p in profiles:
            if (p.get("name") or "").strip().lower() == self.HD_720P_PROFILE_NAME.lower():
                return p
        self.logger.log_warning(
            f"⚠️ [SpacePressure-TV] quality profile '{self.HD_720P_PROFILE_NAME}' not found in "
            f"'{instance}'. Available: {[p.get('name') for p in profiles]}"
        )
        return None

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        """Max allowed resolution of a Sonarr quality profile (0 if none)."""
        from scripts.managers.machine_learning.sizing.size_model import profile_max_quality
        res, _ = profile_max_quality(profile) if profile else (-1, None)
        return res if isinstance(res, (int, float)) and res > 0 else 0

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """All Sonarr quality profiles sorted ascending by max allowed resolution — the
        ladder the step-down downgrade walks one resolution tier at a time."""
        if self.sonarr_api is None:
            return []
        raw = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        return sorted(raw, key=self._profile_max_resolution)

    @staticmethod
    def _ensure_plan_cols(df) -> None:
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

    def _stamp_plan(self, df, idx, action: str, reason: str, reclaim_gb) -> None:
        # Delegates the ledger write to the brain (ledger.decision_ledger.stamp).
        stamp(df, idx, action, reason, reclaim_gb)

    # NOTE: the _max_ts helper moved to the brain (space.downgrade_planner) in ML Step 7c.

    # ── Realize helpers (episode-file granularity) ────────────────────────────────

    @staticmethod
    def _iter_stepdown_file_rows(df, cand, target_res: int) -> list:
        """The candidate series' owned episode files ABOVE the step-down target
        resolution — ``(idx, fid, res, size_bytes, season, episode)`` tuples, largest
        file first (the inline cap spends its budget on the biggest reclaim), one tuple
        per physical file (a multi-episode file yields only its first backing row)."""
        out, seen = [], set()
        for idx in (cand.get("indices") or []):
            if idx not in df.index:
                continue
            # GLD-INV-01 — a pilot row is a FINGERPRINT, not a file this pass may act on.
            # It has episode_number=None, so a step-down could not target it anyway, but
            # its size_bytes would still be counted toward the series' reclaim on top of
            # the real per-episode rows _ingest_inventory_tv writes. Exclude by origin.
            if "is_pilot" in df.columns:
                try:
                    if bool(df.at[idx, "is_pilot"]):
                        continue
                except (TypeError, ValueError):
                    pass
            fid = df.at[idx, "episode_file_id"] if "episode_file_id" in df.columns else None
            if fid is None or pd.isna(fid):
                continue
            fid = int(fid)
            if fid in seen:
                continue
            try:
                res = int(df.at[idx, "resolution"]) if "resolution" in df.columns else 0
            except (TypeError, ValueError):
                continue
            if res <= int(target_res):
                continue
            size = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            size = float(size) if size is not None and pd.notna(size) else 0.0
            if size <= 0:
                continue
            sn = df.at[idx, "season_number"] if "season_number" in df.columns else None
            en = df.at[idx, "episode_number"] if "episode_number" in df.columns else None
            seen.add(fid)
            out.append((idx, fid, res, size, sn, en))
        out.sort(key=lambda t: -t[3])
        return out

    def _series_alt_titles(self, instance: str, sid, display_title: str) -> tuple:
        """GLD-ACQ-27b — alternate title candidates for the identity gate, so releases
        named in the ORIGINAL language pass for series Sonarr displays in English
        ('Kimetsu no Yaiba' releases for 'Demon Slayer: Kimetsu no Yaiba').

        Two sources, cheap first: (1) SEGMENTS of the display title split on ':' and
        parentheticals — the combined-title convention means the romaji is usually
        already sitting inside the Sonarr title; segments need ≥2 tokens or ≥8 chars
        so a stray one-word fragment can't loosen the gate. (2) Sonarr's own
        ``series/{id}.alternateTitles`` (scene + original-language names), fetched
        once per candidate series per pass and failure-tolerant — the gate stays
        functional on the segments alone if the call fails. Capped, deduped."""
        out, seen = [], set()

        def _add(t):
            t = (t or "").strip()
            if not t:
                return
            toks = [x for x in __import__("re").split(r"[^A-Za-z0-9]+", t) if x]
            if len(toks) < 2 and len(t) < 8:
                return
            key = t.lower()
            if key not in seen:
                seen.add(key)
                out.append(t)

        import re as _re
        for seg in _re.split(r"[:\(\)\[\]–—]+", str(display_title or "")):
            _add(seg)
        try:
            series = self.sonarr_api._make_request(
                instance, f"series/{int(sid)}", fallback=None) or {}
            for at in (series.get("alternateTitles") or [])[:20]:
                _add((at or {}).get("title"))
        except Exception:
            pass
        # The display title itself is the matcher's primary; segments/aliases ride as
        # alternates. Drop the primary from the alt list if it slipped in.
        prim = str(display_title or "").strip().lower()
        return tuple(t for t in out if t.lower() != prim)[:12]

    def _series_absolute_map(self, instance: str, sid) -> "dict | None":
        """GLD-ACQ-27c — {(season, episode): absoluteEpisodeNumber} for ANIME-typed
        series, so the identity gate can validate bare-number releases against the
        EXACT absolute number Sonarr assigns ('Kimetsu no Yaiba - 33' IS S02E07)
        instead of the season-1-only heuristic. Returns None for non-anime series
        (bare numbers on standard shows stay under the strict rule) and on any
        failure — the gate then falls back to the prior conservative behaviour.
        One ``series/{id}`` + one ``episode?seriesId=`` call per candidate series
        per pass; this path realizes a handful of files per run, so the cost is
        noise. Sonarr itself already SEARCHES with absolute forms for anime-typed
        series — this is the matching half of that same convention."""
        try:
            series = self.sonarr_api._make_request(
                instance, f"series/{int(sid)}", fallback=None) or {}
            if str(series.get("seriesType") or "").lower() != "anime":
                return None
            eps = self.sonarr_api._make_request(
                instance, f"episode?seriesId={int(sid)}", fallback=None) or []
            out = {}
            for e in eps:
                try:
                    sn, en = int(e.get("seasonNumber")), int(e.get("episodeNumber"))
                    ab = e.get("absoluteEpisodeNumber")
                    if sn > 0 and ab is not None:
                        out[(sn, en)] = int(ab)
                except (TypeError, ValueError):
                    continue
            return out or None
        except Exception:
            return None

    @staticmethod
    def _release_ok_for_episode(release: dict, series_title: str,
                                season, episode, alt_titles: tuple = (),
                                absolute: "int | None" = None) -> bool:
        """GLD-ACQ-27 — does this release NAME the series and CONTAIN the episode we
        intend to replace? The Sonarr twin of GLD-RAD-30's wrong-movie gate.

        Born 2026-08-07 from a live run: the step-down deleted Space Brothers S01E06
        and grabbed 'Property.Brothers.S11E06…', S01E05 → 'Super.Giant.Robot.Brothers.
        S01E05…', and S01E20 → 'Space.Brothers.E81…' (absolute-numbering mismatch).
        ``release?episodeId=`` returns RAW fuzzy indexer results — including other
        shows that merely share a word — and the shared ladder picker ranks size/
        resolution without ever asking WHICH show or WHICH episode the filename
        claims; POSTing the pick with a forced ``episodeId`` then imports the
        stranger INTO the slot. All gates are conservative: no identity evidence ⇒
        no grab ⇒ the file is KEPT (a kept file beats a wrong grab on the
        destructive path; the blind-fallback EpisodeSearch lane, where Sonarr does
        its own matching, remains for shapes we reject).

          1. TITLE — the shared GLD-RAD-30 token-subsequence matcher must find the
             series title — or any alternate title (original-language / romaji /
             scene, via ``_series_alt_titles``) — in the release name.
          2. EPISODE — an SxxEyy / SxxEyyEzz / NxM token must cover (season,
             episode); a bare ``Enn`` or bare-number token (anime absolute style)
             passes when it equals the series' TRUE ``absoluteEpisodeNumber`` for
             this episode (``_series_absolute_map``, anime-typed series only —
             'Kimetsu no Yaiba - 33' IS S02E07) or, absent that data, only when
             season == 1 AND it equals the episode number (kills E81-for-S01E20);
             resolution-like numbers are ignored; season packs and episode-less
             names are rejected.
          3. LANGUAGE — the shared ``_release_language_ok`` gate.
        """
        title = str(release.get("title") or "")
        if not title or not series_title:
            return False
        try:
            want_s, want_e = int(season), int(episode)
        except (TypeError, ValueError):
            return False
        if not RadarrSpacePressureManager._release_matches_movie(
                title, str(series_title), alt_titles=alt_titles):
            # The movie matcher's sequel-number boundary (built to kill 'Scorpion King 4'
            # wrong-film grabs) also kills 'Space Brothers - 20' — where the trailing
            # number IS the episode. Retry with the target episode's bare token masked
            # once; every other title mismatch still fails all variants.
            import re as _re0
            ok_title = False
            _mask_pats = {f"{want_e:d}", f"{want_e:02d}", f"{want_e:03d}"}
            if absolute is not None:
                _mask_pats |= {f"{int(absolute):d}", f"{int(absolute):02d}",
                               f"{int(absolute):03d}", f"{int(absolute):04d}"}
            for pat in sorted(_mask_pats, key=len, reverse=True):
                masked = _re0.sub(rf"(?<![0-9]){pat}(?![0-9])", " ", title, count=1)
                if masked != title and RadarrSpacePressureManager._release_matches_movie(
                        masked, str(series_title), alt_titles=alt_titles):
                    ok_title = True
                    break
            if not ok_title:
                return False
        if not RadarrSpacePressureManager._release_language_ok(release):
            return False
        import re as _re
        toks = [t for t in _re.split(r"[^a-z0-9]+", title.lower()) if t]
        saw_episode_evidence = False
        for t in toks:
            m = _re.fullmatch(r"s(\d{1,2})e(\d{1,3})(?:e(\d{1,3}))?", t)
            if m:
                saw_episode_evidence = True
                s, e1 = int(m.group(1)), int(m.group(2))
                e2 = int(m.group(3)) if m.group(3) else e1
                if s == want_s and e1 <= want_e <= e2:
                    return True
                continue
            m = _re.fullmatch(r"(\d{1,2})x(\d{1,3})", t)
            if m:
                saw_episode_evidence = True
                if int(m.group(1)) == want_s and int(m.group(2)) == want_e:
                    return True
                continue
            m = _re.fullmatch(r"e(\d{1,4})", t)
            if m:
                saw_episode_evidence = True
                n = int(m.group(1))
                if (want_s == 1 and n == want_e) or (absolute is not None
                                                     and n == int(absolute)):
                    return True
                continue
        if not saw_episode_evidence and (want_s == 1 or absolute is not None):
            # Anime bare-number style ('Space Brothers - 20', 'Kimetsu no Yaiba - 33'):
            # only when nothing SxxEyy-shaped appeared anywhere; the number must equal
            # the season-1 episode OR the series' true absolute number; resolution
            # tokens excluded.
            for t in toks:
                if not (t.isdigit() and 1 <= len(t) <= 4):
                    continue
                if t in ("480", "576", "720", "1080", "2160"):
                    continue
                n = int(t)
                if (want_s == 1 and n == want_e) or (absolute is not None
                                                     and n == int(absolute)):
                    return True
        return False

    def _realize_stepdown_files(self, instance: str, df, ef, cand, target_res: int,
                                fid_rowcount: dict, *, budget: int,
                                fallback_eids: list, stats: dict,
                                exhaustive: bool = False,
                                free_base_gb: "float | None" = None,
                                target_u_gb: "float | None" = None,
                                ledger: dict | None = None) -> int:
        """Realize ONE candidate series' step-down at episode-file granularity
        (verify → delete → guid-grab; the series profile was already PUT down).

        ``ledger`` is the shared step-down cooldown ledger, mutated in place; the caller
        loads and persists it once per run. None -> a throwaway dict, so the cooldown is
        simply inert rather than crashing.

        Per file above ``target_res``: resolve the Sonarr episode id (the episode-files
        manager's cached ``_get_episode_id``), run ONE interactive ``release?episodeId=``
        search, and pick with the shared ladder picker. A pick → DELETE the episode file
        then POST the guid grab; a grab error/soft-reject → the file is already gone, so
        the episode id joins the blind ``EpisodeSearch`` fallback pool (effective
        post-delete: the cutoff-met blocker died with the file). No pick → file KEPT,
        counted ``no_release`` (profile stays lowered; re-probes next run). Multi-episode
        files are never realized (a single-episode replacement would orphan the
        siblings). ``budget`` is the shared inline cap; files over it count ``deferred``.
        Mutates ``stats`` and ``fallback_eids``; returns the remaining budget.

        ``exhaustive`` + ``free_base_gb`` + ``target_u_gb`` (all default off/None → byte-identical):
        stop realizing as soon as free space NET of the re-grabs queued this run
        (``free_base_gb + realized_reclaim_gb − inflight_regrab_gb``) reaches ``target_u_gb``.
        Without that subtraction the pass would keep shrinking against the phantom headroom the
        just-deleted files created, since the smaller replacements have not imported yet."""
        sid = cand["sid"]
        # GLD-ACQ-27b: original-language / scene aliases for the identity gate — once
        # per candidate series, failure-tolerant. 27c: the anime absolute-number map
        # ({(season, ep): absolute}) so bare-number releases validate EXACTLY.
        _alt_titles = self._series_alt_titles(instance, sid, cand.get("title"))
        _abs_map = self._series_absolute_map(instance, sid)
        from scripts.support.utilities.stepdown_cooldown import (
            clear as _clear, cooldown_left as _cooldown_left, entry_key as _ekey,
            stamp_failure as _stamp_failure, wait_days as _wait_days,
        )
        _ledger = ledger if ledger is not None else {}
        # GLD-RST-06 gate state, per candidate series (the pass calls this method once
        # per series). `_gate_cap` bounds Sonarr history calls; the two _warned_ flags
        # keep advisory findings to one line each instead of one per pinned file.
        _seed_cfg = seed_config(self.config)
        _gate_examined = 0
        _gate_cap = max(1, int(budget) * self.SEED_GATE_EXAMINE_MULT)
        _warned_unbounded = False
        _warned_shortfall = False
        for idx, fid, res, size, sn, en in self._iter_stepdown_file_rows(df, cand, target_res):
            if exhaustive and free_base_gb is not None and target_u_gb is not None:
                # CONFIRMED reclaim only (GLD-RST-05): pinned bytes are still on disk, so
                # letting them raise `_net` would stop the pass short of the target while
                # believing it arrived — and every replacement grabbed on the way is real
                # new consumption. Pessimistic here is the safe direction, matching
                # bin_forecast's rule that pending reclaim may only make us LESS aggressive.
                _confirmed = stats["realized_reclaim_gb"] - stats.get("pinned_reclaim_gb", 0.0)
                _net = (float(free_base_gb) + _confirmed
                        - stats.get("inflight_regrab_gb", 0.0))
                if _net >= float(target_u_gb):
                    stats["stopped_at_target"] = stats.get("stopped_at_target", 0) + 1
                    continue
            label = (f"'{cand['title']}' S{int(sn):02d}E{int(en):02d}"
                     if (sn is not None and pd.notna(sn) and en is not None and pd.notna(en))
                     else f"'{cand['title']}' fid={fid}")
            if fid_rowcount.get(fid, 1) > 1:
                stats["skipped_multi_ep"] += 1
                self.logger.log_info(
                    f"  ⏸️ {label}: file backs {fid_rowcount[fid]} episodes — kept (a "
                    f"single-episode replacement would orphan the siblings).")
                continue
            if budget <= 0:
                stats["deferred"] += 1
                continue
            if sn is None or en is None or pd.isna(sn) or pd.isna(en):
                stats["failed"] += 1
                continue
            try:
                eid = ef._get_episode_id(instance, int(sid), int(sn), int(en))
            except Exception:
                eid = None
            if not eid:
                self.logger.log_warning(
                    f"  ⚠️ {label}: could not resolve the Sonarr episode id — file kept.")
                stats["failed"] += 1
                continue
            budget -= 1
            # ---- GLD-RST-06: the seed gate, BEFORE the interactive search --------
            # Placed here deliberately. A pinned file's step-down is net negative --
            # it unlinks a hardlink that frees nothing and then downloads a
            # replacement -- so the cheapest correct move is to find out before
            # spending an indexer call on it. The budget slot is refunded below for
            # the same reason: a deferral performs no search, so charging it a slot
            # would let pinned files starve the pass of the actionable ones.
            _push = self._push_descriptor(instance, eid)
            _seen = self._seed_defer_mark(instance, eid, res, action="read")
            _defer, _why = should_defer_stepdown(
                _push, self._torrent_state((_push or {}).get("info_hash")),
                self.config, first_deferred_at=_seen)
            if _defer:
                self._seed_defer_mark(instance, eid, res, action="stamp")
                budget += 1                       # refund: nothing was searched
                _gate_examined += 1
                stats["seed_deferred"] = stats.get("seed_deferred", 0) + 1
                stats["seed_deferred_gb"] = stats.get("seed_deferred_gb", 0.0) + size / (1024 ** 3)
                self.logger.log_debug(
                    f"  \U0001f6d1 {label}: step-down deferred ({_why}) — unlinking a "
                    f"seeded hardlink frees nothing and the replacement costs real space.")
                _tor = self._torrent_state((_push or {}).get("info_hash"))
                if isinstance(_tor, dict):
                    if unbounded_seeding(_tor, _seed_cfg) and not _warned_unbounded:
                        _warned_unbounded = True
                        self.logger.log_warning(
                            "⚠️ At least one pinned torrent has NO ratio or seed-time limit — "
                            "it will seed until you intervene, so every episode hardlinked to "
                            "it is permanently outside the reclaim pool.")
                    _short = obligation_shortfall(_tor, _seed_cfg)
                    if _short and not _warned_shortfall:
                        _warned_shortfall = True
                        self.logger.log_info(
                            f"  \u2139\ufe0f Seed floor not yet met ({_short}) — advisory only; "
                            f"Sonarr's indexer seedCriteria decide when the torrent is removed.")
                if _gate_examined >= _gate_cap:
                    self.logger.log_info(
                        f"  \U0001f6d1 seed-gate examination cap ({_gate_cap}) reached — "
                        f"remaining pinned candidates re-checked next run.")
                    break
                continue
            if _seen:
                # Only clear a mark that actually exists -- a proceed on a file that was
                # never deferred has nothing to clean up and must not touch the ledger.
                self._seed_defer_mark(instance, eid, res, action="clear")
            # Keyed on (episode id, CURRENT resolution): a file replaced at a different
            # tier lands on a fresh key and is retryable immediately.
            _ckey = _ekey(eid, res)
            # COOLDOWN, checked BEFORE the interactive search so a backed-off episode
            # costs no indexer call. Same shared ledger the Radarr passes use, keyed by
            # EPISODE id rather than episode_file_id: a step-down deletes the file, so a
            # file-keyed entry would be orphaned on the grab-failure path.
            _cd = _cooldown_left(_ledger, _ckey, self.config)
            if _cd > 0:
                stats["cooldown_skipped"] = stats.get("cooldown_skipped", 0) + 1
                self.logger.log_debug(
                    f"  ⏭️ {label}: step-down on cooldown, {_cd:.0f}d left — skipped.")
                continue
            releases = self.sonarr_api._make_request(
                instance, f"release?episodeId={int(eid)}", fallback=None) or []
            # GLD-ACQ-27 identity gate — BEFORE the picker ever ranks anything: only
            # releases that name THIS series and cover THIS episode survive. The raw
            # endpoint returns fuzzy strangers ('Property.Brothers.S11E06' for Space
            # Brothers S01E06) and the picker is identity-blind by construction.
            _n_raw = len(releases)
            _abs_n = (_abs_map or {}).get((int(sn), int(en)))
            releases = [r for r in releases
                        if self._release_ok_for_episode(r, cand.get("title"),
                                                        int(sn), int(en),
                                                        alt_titles=_alt_titles,
                                                        absolute=_abs_n)]
            if _n_raw > len(releases):
                stats["identity_rejected"] = (stats.get("identity_rejected", 0)
                                              + _n_raw - len(releases))
            pick = _pick_stepdown_release(releases, current_res=res,
                                          min_size_bytes=self.STEPDOWN_MIN_RELEASE_BYTES,
                                          allow_below_floor=exhaustive)
            if not pick:
                stats["no_release"] += 1
                _n = _stamp_failure(_ledger, _ckey)
                _wait = _wait_days(_ledger, _ckey, self.config)
                self.logger.log_info(
                    f"  ⏸️ {label}: no smaller release available — file kept at {res}p "
                    f"(profile now {cand['target_name']}; attempt {_n}, "
                    f"re-probes in {_wait:.0f}d).")
                continue
            # GLD-RST-02 — file what this episode IS, while the file still exists. The
            # DELETE below is the point of no return: after it, nothing in the system
            # knows what was here. Archive BEFORE, so a delete that succeeds can never
            # outrun its own record.
            _push_arch = _push
            self._archive_stepdown_release(
                instance, sid, sn, en, df.loc[idx], eid,
                from_res=res, to_res=int(target_res) if target_res else None)
            try:
                self.sonarr_api._make_request(instance, f"episodefile/{fid}", method="DELETE")
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(f"  ⚠️ {label}: episode-file delete failed — file kept: {e}")
                continue
            # GLD-RST-05 — the file is UNLINKED from here on, which is not the same as
            # freed. An infohash in the grab record means qbit holds the other hardlink,
            # so the bytes stay on disk until the torrent is removed. Book those
            # SEPARATELY: `realized_reclaim_gb` stays the honest "what we unlinked"
            # figure for reporting, and only the confirmed remainder is allowed to move
            # the exhaustive loop's stop condition below.
            stats["realized"] += 1
            _gb = size / (1024 ** 3)
            stats["realized_reclaim_gb"] += _gb
            if (_push_arch or {}).get("info_hash"):
                stats["pinned_reclaim_gb"] += _gb
                stats["pinned_files"] = stats.get("pinned_files", 0) + 1
                self.logger.log_info(
                    f"  \U0001f517 {label}: {_gb:.2f}GB unlinked but PINNED by a seeding "
                    f"torrent — not counted as free until qbit releases it.")
            # …but the replacement is IN FLIGHT: book its projected size against the
            # free-space figure so the pass can't chase the temporary spike.
            stats["inflight_regrab_gb"] = (stats.get("inflight_regrab_gb", 0.0)
                                           + float(pick.get("size") or 0) / (1024 ** 3))
            if pick.get("stepped_below_floor"):
                stats["below_floor_picks"] = stats.get("below_floor_picks", 0) + 1
                self.logger.log_info(
                    f"  ⤵️ {label}: stepped BELOW 720 — no >=720 release exists for this title.")
            grabbed = False
            try:
                # SEND THE episodeId — the Sonarr equivalent of Radarr's movieId. Without
                # it Sonarr re-parses the release title to identify the episode, which fails
                # on fansub and foreign-language names. The search above was
                # release?episodeId=N, so the id is already known.
                _res = self.sonarr_api._make_request(
                    instance, "release", method="POST", fallback=None,
                    payload={"guid": pick.get("guid"), "indexerId": pick.get("indexerId"),
                             "episodeId": int(eid)})
                # _make_request returns None on a soft rejection (release no longer
                # grabbable / indexer down) WITHOUT raising — the file is already gone,
                # so a soft-reject must fall back too (mirrors legacy_regrab's check).
                grabbed = _res is not None
            except Exception:
                grabbed = False
            if not grabbed:
                fallback_eids.append(int(eid))
                _stamp_failure(_ledger, _ckey)
                stats["grab_fallback"] += 1
                continue
            _clear(_ledger, _ckey)        # grabbed → steppable again
            _pick_gb = float(pick.get("size") or 0) / (1024 ** 3)
            self.logger.log_info(
                f"  📉 {label}: file deleted ({size / (1024 ** 3):.2f}GB @{res}p), grabbed "
                f"'{pick.get('title')}' ({_pick_gb:.2f}GB) — step-down realized.")
        return budget

    # ── Stage 1: downgrade to HD-720p ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_tv_downgrades")
    def run_downgrades(self, instance: str, free_space_gb: float) -> dict:
        """Downgrade the lowest-watchability series to HD-720p until projected free
        space reaches U. Series-level; stamps the plan on every episode row.
        ``free_space_gb`` is the current free space (the orchestration already
        verified free < U before calling)."""
        stats = {
            "candidates":        0,
            "downgraded":        0,
            "est_reclaim_gb":    0.0,
            "skipped_protected": 0,
            "skipped_high_score": 0,
            "skipped_recent":    0,
            "skipped_already":   0,
            "failed":            0,
            # ── realize accounting (live only; dry_run leaves these at 0) ──
            "realized":            0,     # episode files actually deleted + replaced
            "realized_reclaim_gb": 0.0,   # GB unlinked (sum of deleted file sizes) — NOT all freed
            # ---- GLD-RST-05: hardlink-pinned reclaim (qBittorrent) -------------
            # Under the TRaSH layout the download dir and the media root share a
            # filesystem and *arr HARDLINKS on import, so a seeding torrent's data and
            # the library file are two links to ONE inode. DELETE episodefile/{fid}
            # unlinks the library path; the seed keeps the inode alive; the delete frees
            # ZERO bytes. movie_files._mirrored_tmdb_ids already states the rule for the
            # cross-instance case -- "unlinking one of two hardlinks frees nothing" --
            # this is that rule with qbit as the second link holder.
            #
            # WHY IT MATTERS MORE THAN IT LOOKS: realized_reclaim_gb feeds the exhaustive
            # stop condition. Counting pinned bytes as freed makes the pass believe it is
            # gaining space, so it keeps going AND grabs replacements -- real new bytes
            # against a reclaim that never landed. Net space goes DOWN while the ledger
            # reports it going up: the 2026-08-08 phantom-headroom failure with a
            # different cause. Only CONFIRMED bytes may drive the loop.
            "pinned_reclaim_gb":   0.0,   # unlinked but still held by a seeding torrent
            "pinned_files":        0,     # how many of `realized` were torrent-sourced
            # GLD-RST-06 — held back BEFORE any action, because acting would have cost
            # space rather than saved it. Distinct from `deferred` (over the inline cap)
            # and `no_release` (nothing smaller existed): those two describe a file we
            # would still like to shrink, this one describes a file we deliberately will
            # not touch until qbit releases its link.
            "seed_deferred":       0,
            "seed_deferred_gb":    0.0,
            "no_release":          0,     # no smaller release existed → file KEPT
            "identity_rejected":   0,     # releases dropped by the GLD-ACQ-27 series/episode gate
            "grab_fallback":       0,     # guid grab failed → blind EpisodeSearch (file already gone)
            "deferred":            0,     # over the inline cap → next run
            "skipped_multi_ep":    0,     # file backs several episodes → never single-grabbed
            # ── exhaustive-mode accounting (0 on the legacy path) ──
            "inflight_regrab_gb":  0.0,   # projected size of the replacements queued THIS run
            "stopped_at_target":   0,     # items left untouched once free (net of in-flight) hit U
            "below_floor_picks":   0,     # stepped BELOW 720 — no >=720 release exists
        }

        ef = self._get_episode_files_manager()
        if ef is None:
            self.logger.log_warning("[SpacePressure-TV] episode_files manager unavailable — skipping downgrades")
            return stats
        df = ef.load(instance)
        if df.empty or "series_id" not in df.columns:
            return stats
        if "watchability_score" not in df.columns:
            self.logger.log_warning("[SpacePressure-TV] no watchability_score column — run refresh_scores first")
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning("[SpacePressure-TV] Could not fetch quality profiles — skipping downgrades")
            return stats
        # Series floor at the HD-720p resolution: they step DOWN toward it (4K → 1080p →
        # 720p) but never below.
        hd720p = self._fetch_hd720p_profile(instance)
        floor_resolution = (self._profile_max_resolution(hd720p) or 720) if hd720p is not None else 720

        self._ensure_plan_cols(df)
        # Clear any stale downgrade plan from a prior run so the ledger reflects THIS
        # run's decision (leave delete/acquire/upgrade plans untouched).
        _stale = df["planned_action"] == "downgrade"
        if _stale.any():
            df.loc[_stale, ["planned_action", "plan_reason", "plan_reclaim_gb"]] = None

        _, U = self._space_targets(instance)
        # TIERED, NOT CONCURRENT — see machine_learning/space/reclaim_ledger.py. Every space
        # pass reads the SAME free-space figure from the SAME shared mount, so before this
        # each planned its full deficit independently: Radarr standard 396 GB, Radarr ultra
        # 205 GB and this pass 476 GB, all against one 922 GB pool, none aware the others
        # had already committed to part of it. Adding what earlier passes planned makes the
        # chain drain a single shared deficit instead of three passes racing one number.
        _planned = planned_reclaim_gb(self.global_cache, exclude=f"sonarr:{instance}:downgrade")
        need_gb = max(0.0, U - (float(free_space_gb) + _planned))
        if _planned:
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': {_planned:.0f} GB already planned by earlier "
                f"passes this run — need ~{need_gb:.0f} GB (not {max(0.0, U - float(free_space_gb)):.0f}).")
        ceiling = self._score_ceiling()
        now = datetime.now(tz=timezone.utc)
        watch_cutoff = now - timedelta(days=self.RECENT_WATCH_DAYS)
        air_cutoff   = now - timedelta(days=self.RECENT_AIR_DAYS)

        # DECISION: the brain (space.downgrade_planner.plan_series_downgrades) steps the
        # lowest-watchability series DOWN the resolution ladder one tier at a time, spread
        # across the pool, until ~need_gb is reclaimed (no series crushed straight to 720p).
        # The service APPLIES each per-series target (PUT + SeriesSearch + stamp) below.
        # EXHAUSTIVE (space_exhaustive_downgrade, DEFAULT ON): plan EVERY series above the
        # 720p floor down to it — no score ceiling, no early stop at a partial need_gb —
        # because the episode delete pool now only accepts files already AT/BELOW that floor.
        _exhaustive = exhaustive_downgrade(self.config)
        candidates, _pstats = plan_series_downgrades(
            df, ranked_profiles,
            need_gb=need_gb,
            ceiling=ceiling,
            watch_cutoff=watch_cutoff,
            air_cutoff=air_cutoff,
            keep_tags=self.DOWNGRADE_KEEP_TAGS,   # GLD-TVQ-01 — keep tags gate DELETE, not quality
            default_runtime_min=self.DEFAULT_RUNTIME_MIN,
            floor_resolution=floor_resolution,
            exhaustive=_exhaustive,
        )
        stats.update(_pstats)
        # Publish this pass's projected reclaim so LATER passes (the coordinator, a second
        # instance) plan against the remaining deficit rather than the same one. Projected,
        # not realized: the replacements have not imported yet, and a later pass must not
        # re-plan space this one has already committed to freeing.
        record_planned_reclaim(self.global_cache, f"sonarr:{instance}:downgrade",
                               float(_pstats.get("est_reclaim_gb", 0.0) or 0.0))
        if not candidates:
            _why = ("every series is keep-tagged / hot-universe / recent / already at the "
                    f"{floor_resolution}p floor — the downgrade pool is EXHAUSTED, so deletion "
                    "is now the only lever left" if _exhaustive
                    else f"score<{ceiling:.0f}, not keep/hot-universe/recent/at-floor")
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': {free_space_gb:.0f}GB free (<{U:.0f}GB) but no "
                f"downgrade candidates ({_why})."
            )
            return stats

        # Apply each per-series step-down target (the planner already spread to ~need_gb).
        # fallback_eids collects episodes whose guid grab failed AFTER their file was
        # deleted — a blind EpisodeSearch is effective for exactly those (the cutoff-met
        # blocker died with the file). realize_budget is the shared inline cap across all
        # candidate series this pass.
        fallback_eids: list[int] = []
        # Cooldown ledger: loaded once, mutated across every candidate series, saved once.
        # Same shared module the Radarr passes use, under sonarr/{instance}/.
        from scripts.support.utilities.stepdown_cooldown import (
            clear as _clear, cooldown_left as _cooldown_left, ledger_key as _lkey,
            prune as _prune, stamp_failure as _stamp_failure, wait_days as _wait_days,
        )
        _ledger = dict((self.global_cache.get(_lkey("sonarr", instance))
                        if self.global_cache else None) or {})
        realize_budget = self._realize_cap()
        # BANDWIDTH GUARD: in exhaustive mode the TIGHTER of the two caps wins — the TV
        # inline cap (tv_downgrade_realize_cap, bounds slow interactive searches) and the
        # cross-service per-run re-grab cap (space_downgrade_max_regrabs_per_run). Files
        # over it keep their copies, count `deferred`, and re-qualify next run — and since
        # they are still above the floor they stay excluded from deletion.
        _regrab_cap = downgrade_regrab_cap(self.config) if _exhaustive else 0
        if _regrab_cap > 0:
            realize_budget = min(realize_budget, _regrab_cap)
        realize_budget_start = realize_budget   # for the summary table's cap description
        if _exhaustive:
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': exhaustive step-down — every series above the "
                f"{floor_resolution}p floor is planned down to it "
                f"({_pstats.get('over_ceiling_included', 0)} admitted over the score ceiling); "
                f"stopping at {U:.0f}GB free NET of in-flight re-grabs; realize budget "
                f"{realize_budget}/run."
            )
        # How many episode rows each file backs — a multi-episode file is never replaced
        # by a single-episode grab (it would orphan the siblings).
        fid_rowcount: dict = {}
        if "episode_file_id" in df.columns:
            try:
                fid_rowcount = {
                    int(k): int(v)
                    for k, v in df["episode_file_id"].dropna().value_counts().items()
                }
            except Exception:
                fid_rowcount = {}
        plan_changed = changed = False
        reclaimed = 0.0

        for c in candidates:
            # ── IN-FLIGHT ACCOUNTING (exhaustive only) ────────────────────────────
            # dry_run has no picks to size, so it models the planner's projected reclaim
            # (= deleted size − replacement size, i.e. already net); the live pass uses the
            # realized/in-flight split the realize helper maintains from the actual picks.
            if _exhaustive:
                # The stop condition counts the SHARED deficit too: once free space net of
                # this pass's in-flight re-grabs PLUS what other passes have already planned
                # reaches the band top, there is nothing left for this pass to cover.
                _net_free = float(free_space_gb) + _planned + (
                    reclaimed if self.dry_run
                    else stats["realized_reclaim_gb"] - stats["inflight_regrab_gb"])
                if _net_free >= U:
                    stats["stopped_at_target"] += 1
                    continue
            reason = f"{c['reason']} → {c['target_name']}"
            # Stamp the series-level downgrade on ONE representative episode row with
            # the WHOLE-series reclaim. The plan ledger (plan_summary.py) counts rows
            # and sums plan_reclaim_gb per planned_action, so stamping every episode
            # row of the series would inflate BOTH the count (~n_eps) and the GB freed.
            self._stamp_plan(df, c["indices"][0], "downgrade", reason, c["reclaim"])
            plan_changed = True
            reclaimed += c["reclaim"]
            stats["est_reclaim_gb"] = round(reclaimed, 1)

            if self.dry_run:
                # debug: stamped into the decision ledger above → rendered in the
                # end-of-run "Change plan" grid; live log keeps the summary table.
                self.logger.log_debug(
                    f"  📉 [dry_run] Would step down '{c['title']}' ({c['n_eps']} ep, "
                    f"{c['cur_gib']:.1f}GB → {c['target_name']}, ~{c['reclaim']:.1f}GB reclaim) — {c['reason']}"
                )
                stats["downgraded"] += 1
                continue

            try:
                payload = self.sonarr_api._make_request(instance, f"series/{c['sid']}", fallback=None)
                if not payload or not isinstance(payload, dict):
                    self.logger.log_warning(f"  ⚠️ Could not fetch series payload for '{c['title']}' (id={c['sid']})")
                    stats["failed"] += 1
                    continue
                payload["qualityProfileId"] = c["target_id"]
                self.sonarr_api._make_request(instance, f"series/{c['sid']}", method="PUT", payload=payload)
                changed = True
                stats["downgraded"] += 1
                self.logger.log_info(
                    f"  📉 Stepped down '{c['title']}' ({c['n_eps']} ep, {c['cur_gib']:.1f}GB → "
                    f"{c['target_name']}, ~{c['reclaim']:.1f}GB projected) — {c['reason']}"
                )
                # REALIZE: the profile flip alone reclaims nothing (Sonarr will not
                # replace a file that already exceeds the new cutoff). Walk this
                # series' oversized files: verify a smaller release exists → delete →
                # grab it. Bounded by the shared inline budget.
                # Target resolution comes from the planner's chosen profile (the
                # candidate carries the profile dict, not a bare resolution); fall
                # back to the pass's 720p floor if the profile can't be read.
                _t_res = self._profile_max_resolution(c.get("target_profile")) or floor_resolution
                realize_budget = self._realize_stepdown_files(
                    instance, df, ef, c, int(_t_res),
                    fid_rowcount, budget=realize_budget,
                    fallback_eids=fallback_eids, stats=stats,
                    exhaustive=_exhaustive,
                    free_base_gb=float(free_space_gb) if _exhaustive else None,
                    target_u_gb=U if _exhaustive else None,
                    ledger=_ledger,
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Downgrade failed for '{c['title']}' (id={c['sid']}): {e}")
                stats["failed"] += 1

        # ── blind-search fallback pool (chunked) ──────────────────────────────────
        # ONLY for episodes whose guid grab failed after their file was deleted. The
        # old blanket per-series SeriesSearch is gone: with the file still present it
        # was rejected as cutoff-met (the phantom-reclaim bug), and once the file IS
        # deleted the targeted grab above already handles the replacement.
        for i in range(0, len(fallback_eids), self.SEARCH_CHUNK):
            _batch = fallback_eids[i:i + self.SEARCH_CHUNK]
            try:
                self.sonarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "EpisodeSearch", "episodeIds": _batch},
                )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Blind EpisodeSearch fallback failed for {len(_batch)} episode(s): {e}")
        if fallback_eids:
            self.logger.log_info(
                f"  🔍 Blind EpisodeSearch fallback for {len(fallback_eids)} episode(s) "
                f"whose guid grab did not take (files already removed).")

        if plan_changed or changed:
            ef.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        target_status = "met" if _pstats.get("target_met") else "NOT met"
        _rows = [
            ["stepped down",     stats["downgraded"]],
            ["projected GB",     stats["est_reclaim_gb"]],
            ["files realized",   stats["realized"]],
            ["realized GB",      round(stats["realized_reclaim_gb"], 2)],
            ["no smaller release", stats["no_release"]],
            ["on cooldown",      stats.get("cooldown_skipped", 0)],
            ["grab fallback",    stats["grab_fallback"]],
            ["deferred (cap)",   stats["deferred"]],
            ["multi-episode file", stats["skipped_multi_ep"]],
            ["candidates",       stats["candidates"]],
            ["score over ceil",  stats["skipped_high_score"]],
            ["keep-tagged",      stats["skipped_protected"]],
            ["hot-universe",     stats.get("skipped_universe", 0)],
            ["recent",           stats["skipped_recent"]],
            ["at/below floor",   stats["skipped_already"]],
            ["failed",           stats["failed"]],
        ]
        _descs = [
            "series whose profile was stepped down a tier",
            "planner's projected reclaim once every oversized file is replaced",
            "episode files ACTUALLY deleted + re-grabbed smaller this pass",
            "REAL space freed now (sum of the deleted files) — the rest lands as files replace",
            "files KEPT: no release below the current resolution — backs off, re-probes later",
            "files skipped without an indexer call: still inside their step-down backoff",
            "grab did not take; file already removed, blind EpisodeSearch queued",
            f"files over the inline cap ({realize_budget_start}/pass) — next run picks them up",
            "files backing several episodes — never single-grabbed (would orphan siblings)",
            "series the planner picked as candidates",
            "series skipped for watchability score over the ceiling",
            "series skipped because keep-tagged",
            "series skipped — hot franchise/universe credit holds them at tier",
            "series skipped for a recent watch or air date",
            "series already at or below the 720p floor",
            "series whose PUT/search call errored",
        ]
        if _exhaustive:
            _rows += [
                ["over-ceiling included", stats.get("over_ceiling_included", 0)],
                ["stepped below 720",     stats["below_floor_picks"]],
                ["in-flight re-grab GB",  round(stats["inflight_regrab_gb"], 2)],
                ["target reached",        stats["stopped_at_target"]],
            ]
            _descs += [
                "series admitted despite a score over the delete ceiling — EVERYTHING shrinks "
                "before anything is deleted",
                "no >=720 release exists for the episode, so the pass fell below the 720p floor",
                "projected size of the smaller replacements queued THIS run — subtracted from free "
                "space so the pass never downgrades against phantom headroom",
                "items left untouched: free space NET of the in-flight re-grabs reached the band top",
            ]
        # rows and descriptions are PARALLEL lists with no structural link. The Radarr twin
        # silently lost two entries and mislabelled every row after them; guard both.
        if len(_descs) != len(_rows):
            self.logger.log_warning(
                f"[SpacePressure-TV] table description mismatch: {len(_rows)} row(s) but "
                f"{len(_descs)} description(s) - rows beyond the shorter list would be "
                f"mislabelled. Padding; fix the two lists in run_downgrades.")
            _descs = (_descs + [""] * len(_rows))[:len(_rows)]
        self.logger.log_table(
            ["Outcome", "Count"], _rows,
            title=f"[SpacePressure-TV] {prefix}'{instance}' "
                  f"(free {free_space_gb:.0f}GB, target {U:.0f}GB, need ~{need_gb:.0f}GB, "
                  f"target {target_status}{'; exhaustive' if _exhaustive else ''})",
            caption="Per-pass result of the TV space-pressure step-down: how many low-watchability "
                    "series were downgraded toward HD-720p, the space reclaimed, and what was skipped "
                    "and why."
                    + (" EXHAUSTIVE: every series above the 720p floor is planned down to it "
                       "(deletion is the true last resort); the pass stops once free space net of "
                       f"the in-flight re-grabs reaches {U:.0f}GB, and realizes at most "
                       f"{realize_budget_start} file(s)/run." if _exhaustive else ""),
            descriptions=_descs,
        )
        # Ledger saves regardless of whether anything else changed: a cooldown stamped
        # this run must survive, or the backoff resets every pass.
        if self.global_cache:
            _prune(_ledger, self.config)
            self.global_cache.set(_lkey("sonarr", instance), _ledger)
        return stats

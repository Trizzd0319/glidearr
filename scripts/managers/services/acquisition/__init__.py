"""
AcquisitionManager — turn recommendations/watchlist (+ MAL anime) into *arr adds.
================================================================================
Runs in main.py's final phase (after Sonarr/Radarr). It finally *consumes* the
Trakt recommendations/watchlist the app already fetches (and MAL anime once that
service is wired): gather → dedup vs library → score → resolve instance/quality/
size → add **monitored, search OFF**.

Every add is gated: it does nothing unless ``acquisition.enabled`` is true, and it
never writes when ``dry_run`` is set (candidates are reported as "would-add").
A decision matrix (score, instance, profile, estimated size, decision) is logged
so the choices are transparent.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.acquisition.adder import Adder
from scripts.managers.services.acquisition.candidates import CandidateGatherer
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.acquisition.resolver import Resolver
from scripts.managers.services.acquisition.scorer import AcquisitionScorer
from scripts.support.utilities.decorators.timing import timeit
from datetime import datetime, timezone

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import deletions_enabled, space_targets


class AcquisitionManager(BaseManager, ComponentManagerMixin):
    parent_name = "AcquisitionManager"

    # global_cache key for the deferred-search backlog (titles added under space pressure
    # with search OFF, awaiting free space >= U to be searched/grabbed).
    _DEFERRED_KEY = "acquisition/deferred_search"
    _DEFERRED_MAX = 500            # cap the backlog — keep the newest (bounds chronic-pressure growth)
    _DEFERRED_MAX_ATTEMPTS = 5     # abandon an item after this many failed deferred-search attempts

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "AcquisitionManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt = kwargs.get("trakt")
        self.mal = kwargs.get("mal")
        self.plex = kwargs.get("plex")
        self.sonarr = kwargs.get("sonarr")
        self.radarr = kwargs.get("radarr")

    def prepare(self) -> None:
        pass

    # ── space-pressure deferral ──────────────────────────────────────────────────
    def _space_band(self, gw, inst, cache: dict) -> "tuple[float, float]":
        """(free_gb, U) for an instance — U is the band top from space_targets
        (free_space_limit + headroom, or 25% of the total drive when unset). FAIL-OPEN: an
        unreadable instance yields free=inf (disk_free_gb returns inf on error) so
        free < U is False and the add is never blocked by a transient error. Memoised per
        (service, instance) for the run."""
        key = (getattr(gw, "service", "?"), str(inst))
        if key in cache:
            return cache[key]
        try:
            free = float(gw.im.disk_free_gb(inst))
        except Exception:
            free = float("inf")
        try:
            total = gw.im.disk_total_gb(inst)
        except Exception:
            total = None
        # Warn once (per service+instance) when the floor is being defaulted to 25%-of-total
        # because free_space_limit is unset — same as every other space gate.
        alert_unconfigured_floor(self.config, self.logger,
                                 getattr(gw, "service", "acquire").capitalize(), inst, total)
        _, U = space_targets(self.config, total_gb=total)
        cache[key] = (free, U)
        return cache[key]

    def _space_ok(self, gw, inst, cache: dict) -> bool:
        """True when an instance is at/above its pressure band (free >= U) — comfortable enough
        to take the 4K bonus copy. FAIL-OPEN via ``_space_band`` (free=inf on a read error)."""
        if gw is None:
            return True
        free, U = self._space_band(gw, inst, cache)
        return free >= U

    def _acquisition_paused(self, gw, inst, cache: dict) -> bool:
        """True when NEW media must not be acquired: free space is in/below the
        pressure band AND deletion is not armed (no consent / no free_space_limit), so
        space can never be reclaimed — a deferred add would strand forever. When
        deletion IS armed, callers defer instead (the freed space lets a later run
        search the title). FAIL-OPEN: an unreadable instance yields free=inf, so this
        returns False and acquisition is never blocked by a transient error."""
        if gw is None or deletions_enabled(self.config):
            return False
        free, U = self._space_band(gw, inst, cache)
        return free < U

    def _trigger_search(self, gw, inst, item: dict) -> bool:
        """Issue the deferred MoviesSearch/SeriesSearch. Returns True only on a truthy
        *arr response — _make_request swallows HTTP/transport errors and returns the None
        fallback (no exception), so a falsy result means the command was rejected/failed and
        the item must STAY queued for a retry, not be silently dropped."""
        aid = item.get("arr_id")
        if aid is None:
            return False
        try:
            if item.get("type") == "show":
                resp = gw.command(inst, {"name": "SeriesSearch", "seriesId": int(aid)})
            else:
                resp = gw.command(inst, {"name": "MoviesSearch", "movieIds": [int(aid)]})
        except Exception as e:
            self.logger.log_warning(f"[acquire] deferred search failed for '{item.get('title')}': {e}")
            return False
        if not resp:
            self.logger.log_warning(
                f"[acquire] deferred search for '{item.get('title')}' ({inst}) returned no "
                f"command — keeping queued for retry."
            )
            return False
        self.logger.log_info(f"[acquire] deferred search → '{item.get('title')}' ({inst})")
        return True

    def _flush_deferred(self, gateways: dict, band_cache: dict) -> dict:
        """Search any previously-deferred titles whose instance now has space (free >= U).
        Items on still-pressured instances stay queued (no attempt counted). A search that
        is attempted but fails (command rejected / stale id / instance hiccup) is retried,
        and abandoned after ``_DEFERRED_MAX_ATTEMPTS`` so a permanently-gone id can't retry
        forever. Pure drain — safe to call regardless of the defer toggle. Returns stats."""
        stats = {"pending": 0, "searched": 0, "abandoned": 0, "still_deferred": 0}
        if not self.global_cache:
            return stats
        q = self.global_cache.get(self._DEFERRED_KEY)
        q = q if isinstance(q, list) else []
        stats["pending"] = len(q)
        if not q:
            return stats

        remaining: list[dict] = []
        for item in q:
            gw = gateways.get(item.get("service"))
            inst = item.get("instance")
            if gw is None or not gw.available:
                remaining.append(item)
                continue
            free, U = self._space_band(gw, inst, band_cache)
            if free < U:
                remaining.append(item)            # still under pressure — keep waiting (no attempt)
                continue
            if self.dry_run:
                self.logger.log_info(
                    f"[acquire] dry_run — would search deferred '{item.get('title')}' on "
                    f"{inst} ({free:.0f} >= {U:.0f} GB)"
                )
                remaining.append(item)            # nothing actually searched in dry_run
                stats["searched"] += 1
                continue
            if self._trigger_search(gw, inst, item):
                stats["searched"] += 1
                continue
            # Attempted but failed — count it; abandon after the retry budget so a stale id
            # (e.g. the title was deleted) can't re-POST a doomed command every run forever.
            item["attempts"] = int(item.get("attempts", 0)) + 1
            if item["attempts"] >= self._DEFERRED_MAX_ATTEMPTS:
                stats["abandoned"] += 1
                self.logger.log_warning(
                    f"[acquire] abandoning deferred '{item.get('title')}' after "
                    f"{item['attempts']} failed search attempts."
                )
            else:
                remaining.append(item)

        stats["still_deferred"] = len(remaining)
        if not self.dry_run:
            self.global_cache.set(self._DEFERRED_KEY, remaining)
        if stats["pending"]:
            self.logger.log_info(
                f"[Acquisition] deferred-search flush: {stats['searched']} searched, "
                f"{stats['abandoned']} abandoned, {stats['still_deferred']} still deferred "
                f"(of {stats['pending']})."
            )
        return stats

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> None:
        acq = (self.config.get("acquisition", {}) if self.config else {}) or {}
        if not acq.get("enabled"):
            self.logger.log_debug("[Acquisition] disabled (acquisition.enabled=false) — skipping.")
            return

        gateways = {
            "sonarr": ArrGateway("sonarr", getattr(self.sonarr, "instance_manager", None), self.config, self.logger),
            "radarr": ArrGateway("radarr", getattr(self.radarr, "instance_manager", None), self.config, self.logger),
        }
        gatherer = CandidateGatherer(self.trakt, self.mal, self.logger,
                                     acq.get("sources", {}), limit=int(acq.get("recommendation_limit", 20)),
                                     plex=self.plex, global_cache=self.global_cache)
        resolver = Resolver(gateways, self.config, self.logger)
        scorer = AcquisitionScorer(self.global_cache, self.logger)
        adder = Adder(gateways, self.logger, dry_run=self.dry_run,
                      monitored=bool(acq.get("monitored", True)),
                      search=bool(acq.get("search_on_add", False)))

        # Space-pressure deferral: don't pile new downloads on while free is inside the
        # pressure band [T, U). Under pressure a title is still ADDED (monitored, at its
        # resolved profile) but with search OFF and queued; once free recovers above U a
        # later run searches it. Default ON; set acquisition.defer_under_pressure=false to
        # add+search regardless of pressure.
        defer_enabled = bool(acq.get("defer_under_pressure", True))
        band_cache: dict = {}
        # ALWAYS drain the existing backlog (idempotent, space-gated) — disabling the
        # feature must not strand titles already added-but-unsearched. Only the NEW
        # deferral-on-add below is gated by defer_enabled.
        flush_stats = self._flush_deferred(gateways, band_cache)

        raw = gatherer.gather()
        self.logger.log_info(f"[Acquisition] {len(raw)} candidate(s) from enabled sources.")

        prepared, skipped = [], {}
        for cand in raw:
            enriched = resolver.prepare(cand)
            reason = enriched.get("skip_reason")
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            sc = scorer.score(enriched)
            enriched["score"], enriched["matrix"] = sc["total"], sc["matrix"]
            # Now that the score exists, re-pick the quality profile from it (the
            # matrix) — higher score → higher quality tier — instead of the default
            # first ("Any") profile. No-op if acquisition.quality_profile is pinned.
            resolver.resolve_quality(enriched, sc["total"])
            # Dual-version: when 4k_policy=='both' + a distinct 4K instance, RE-CAP this
            # primary copy to the <=1080 score-adaptive baseline (the 2160p copy goes on the
            # 4K instance via the companion add below). No-op for highest_only / no 4K instance.
            resolver.apply_hd_baseline(enriched)
            prepared.append(enriched)

        min_score = int(acq.get("min_score", 0) or 0)
        eligible = [e for e in prepared if e["score"] >= min_score]
        eligible.sort(key=lambda x: x["score"], reverse=True)
        cap = int(acq.get("max_adds_per_run", 10) or 0)
        selected = eligible[:cap] if cap > 0 else eligible

        rows, added, would, failed, deferred = [], 0, 0, 0, 0
        new_deferred: list[dict] = []
        for e in selected:
            svc = "sonarr" if e.get("type") == "show" else "radarr"
            gw = gateways.get(svc)

            # No way to reclaim space (deletion not consented/armed) and we're below the
            # pressure band → skip the add entirely. A deferred title would never get its
            # space freed, so pause new acquisition instead of stranding it in *arr.
            if self._acquisition_paused(gw, e.get("instance"), band_cache):
                skipped["space_full_no_deletion"] = skipped.get("space_full_no_deletion", 0) + 1
                rows.append([
                    str(e.get("title") or e.get("ext_id"))[:34],
                    e.get("type"), e.get("score"), str(e.get("instance")),
                    (e.get("quality_profile") or {}).get("name"),
                    self._size_str(e), "skipped (full)",
                ])
                continue

            under_pressure = False
            if defer_enabled and gw is not None:
                free, U = self._space_band(gw, e.get("instance"), band_cache)
                under_pressure = free < U

            # Under pressure: add at the resolved profile but with search OFF, and queue
            # it for a deferred search once free recovers above U. A dual-version baseline
            # searches ON when not pressured — both the 1080p floor and the 4K bonus must
            # actually grab a file (search_on_add stays the policy for ordinary single adds).
            base_search = True if e.get("dual_baseline") else None
            res = adder.add(e, search=False if under_pressure else base_search)
            action = res.get("action")

            if under_pressure and action in ("added", "would-add"):
                deferred += 1
                action = "deferred"   # surfaced in the decision table
                if res.get("ok") and not self.dry_run:
                    aid = (res.get("result") or {}).get("id")
                    if aid is not None:
                        new_deferred.append({
                            "service": svc, "instance": e.get("instance"), "arr_id": aid,
                            "title": e.get("title"), "type": e.get("type"),
                            "profile": (e.get("quality_profile") or {}).get("name"),
                            "queued_at": datetime.now(timezone.utc).isoformat(),
                            "attempts": 0,
                        })
            else:
                added += action == "added"
                would += action == "would-add"
            failed += action == "add-failed"

            rows.append([
                str(e.get("title") or e.get("ext_id"))[:34],
                e.get("type"),
                e.get("score"),
                str(e.get("instance")),
                (e.get("quality_profile") or {}).get("name"),
                self._size_str(e),
                action,
            ])
            self.logger.log_debug(f"[acquire] '{e.get('title')}' matrix={e.get('matrix')}")

            # Dual-version companion (4k_policy=='both'): add the 2160p copy on the 4K instance
            # ON TOP of the <=1080 baseline just added. Make-before-break — gated on the baseline
            # POSTing OK and NOT being deferred, so the durable floor lands (and searches) first;
            # if the standard instance is pressured this run, the 4K copy waits for the reconcile
            # sweep. plan_uhd_companion returns None unless dual is active + UHD is warranted +
            # the 4K instance has space + the title isn't already a 4K copy.
            if svc == "radarr" and res.get("ok") and not under_pressure:
                companion = resolver.plan_uhd_companion(
                    e, space_ok=lambda inst, _gw=gw: self._space_ok(_gw, inst, band_cache))
                if companion is not None:
                    cres = adder.add(companion, search=True)
                    caction = cres.get("action")
                    added += caction == "added"
                    would += caction == "would-add"
                    failed += caction == "add-failed"
                    rows.append([
                        str(companion.get("title") or companion.get("ext_id"))[:34],
                        "movie", companion.get("score"),
                        f"{companion.get('instance')} [4k]",
                        (companion.get("quality_profile") or {}).get("name"),
                        self._size_str(companion),
                        f"{caction} [4k]",
                    ])

        # Persist the new deferrals onto the backlog (live runs only), bounding its length
        # so chronic pressure can't grow it without limit (keep the newest).
        if new_deferred and self.global_cache:
            q = self.global_cache.get(self._DEFERRED_KEY)
            q = q if isinstance(q, list) else []
            q.extend(new_deferred)
            if len(q) > self._DEFERRED_MAX:
                q = q[-self._DEFERRED_MAX:]
            self.global_cache.set(self._DEFERRED_KEY, q)
        if deferred:
            self.logger.log_info(
                f"[Acquisition] {deferred} title(s) deferred under space pressure "
                f"(added monitored, search OFF; will search when free >= U)."
            )

        if rows:
            self.logger.log_table(
                ["title", "type", "score", "instance", "profile", "~size", "decision"],
                rows, title="Acquisition decisions",
            )
        else:
            self.logger.log_info("[Acquisition] no new candidates to add.")
        if skipped:
            self.logger.log_info("[Acquisition] skipped: "
                                 + ", ".join(f"{k}×{v}" for k, v in skipped.items()))

        stats = {
            "candidates": len(raw), "eligible": len(eligible), "selected": len(selected),
            "added": added, "would_add": would, "failed": failed, "skipped": skipped,
            "deferred": deferred, "deferred_flush": flush_stats,
        }
        if self.global_cache:
            try:
                self.global_cache.set("acquisition/run_stats", stats)
            except Exception:
                pass

    @staticmethod
    def _size_str(e: dict) -> str:
        size = e.get("expected_size_gb")
        if not size:
            return "?"
        suffix = "/ep" if e.get("size_unit") == "per-episode" else ""
        return f"~{size}GB{suffix}"

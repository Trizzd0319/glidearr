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

import math

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.acquisition.demand import demand_priority, demand_score
from scripts.managers.machine_learning.acquisition import decision_log, space_budget
from scripts.managers.machine_learning.ledger.pending_plan import record_pending
from scripts.managers.machine_learning.playlists.per_user import genre_match
from scripts.managers.machine_learning.space.routing_targets import uhd_remote_play_ok
from scripts.managers.machine_learning.space.tightness import tightness_with_hysteresis
from scripts.managers.services.acquisition import breakdown as acq_breakdown
from scripts.managers.services.acquisition.adder import Adder
from scripts.managers.services.acquisition.candidates import CandidateGatherer
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.acquisition.resolver import Resolver
from scripts.managers.services.acquisition.scorer import AcquisitionScorer
from scripts.managers.services.plex.playlists.universe_order import (
    saga_display_name,
    saga_membership_index,
)
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.decorators.timing import timeit
from datetime import datetime, timezone

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import deletions_enabled, space_targets


def _finite(val, default: float) -> float:
    """Coerce to a finite float, else ``default`` (for config knobs / scores that may be missing)."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if (f == f and f not in (float("inf"), float("-inf"))) else default


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

    # ── demand-aware ordering (acquisition.demand.*) ─────────────────────────────
    def _demand_rank(self, eligible: list, gateways: dict, band_cache: dict, acq: dict) -> None:
        """Reorder ``eligible`` IN PLACE by demand-aware priority — ``watchability × demand^t``, where
        ``demand`` is how many household users would watch the title (breadth) and ``t`` is its routed
        instance's space-tightness. A roomy instance (``t=0``) keeps pure watchability order, so the
        result is byte-identical to score-desc when no instance is tight (or no user has affinity)."""
        dcfg = acq.get("demand", {}) or {}
        band = _finite(dcfg.get("band", 0.30), 0.30)
        threshold = _finite(dcfg.get("threshold", 0.15), 0.15)
        affinities = self._per_user_affinities()
        if not affinities:
            # No user roster at all → no breadth signal; degrade gracefully to watchability order
            # rather than collapsing every candidate's demand to 0 under tightness.
            eligible.sort(key=lambda x: _finite(x.get("score"), 0.0), reverse=True)
            return
        tight_any = False
        for e in eligible:
            gw = gateways.get("sonarr" if e.get("type") == "show" else "radarr")
            t = self._instance_tightness(gw, e.get("instance"), band, band_cache)
            base = _finite(e.get("score"), 0.0)
            if t <= 0.0:
                e["demand_priority"] = base                          # roomy → watchability-led
                continue
            tight_any = True
            demand = demand_score(e.get("genres") or [], affinities,
                                  popularity=self._votes_to_unit(e.get("votes")), threshold=threshold)
            e["demand_priority"] = demand_priority(base, demand, t)
        eligible.sort(key=lambda x: x.get("demand_priority", _finite(x.get("score"), 0.0)), reverse=True)
        # Fairness floor — only under scarcity (so roomy runs stay byte-identical to score-desc).
        if tight_any:
            self._reserve_fairness(eligible, affinities, int(acq.get("max_adds_per_run", 10) or 0),
                                   threshold)

    def _reserve_fairness(self, eligible: list, affinities: list, cap: int, threshold: float) -> None:
        """Guarantee each active user's TOP on-taste pick a place in the top ``cap``, so demand-weighting
        under scarcity can't permanently starve a single user's niche taste. Each reserved pick keeps its
        own demand-priority position; it just can't be cut. No-op when the cap doesn't bind."""
        if cap <= 0 or cap >= len(eligible):
            return
        reserved_ids = set()
        for aff in affinities:
            if not aff:
                continue
            best, best_m = None, -1.0
            for e in eligible:                              # eligible is priority-sorted → ties keep the
                m = genre_match(e.get("genres") or [], aff)  # first (highest-priority) on-taste pick
                if isinstance(m, (int, float)) and m >= threshold and m > best_m:
                    best, best_m = e, m
            if best is not None:
                reserved_ids.add(id(best))
        if not reserved_ids:
            return
        reserved = [e for e in eligible if id(e) in reserved_ids]        # already demand-priority order
        nonreserved = [e for e in eligible if id(e) not in reserved_ids]
        keep = reserved[:cap]
        fill = nonreserved[:max(0, cap - len(keep))]

        def _pri(x):
            return x.get("demand_priority", _finite(x.get("score"), 0.0))
        top = sorted(keep + fill, key=_pri, reverse=True)
        rest = sorted(reserved[cap:] + nonreserved[len(fill):], key=_pri, reverse=True)
        eligible[:] = top + rest

    def _instance_tightness(self, gw, inst, band: float, cache: dict) -> float:
        """Acquisition space-tightness ``t∈[0,1]`` for an instance: 0 with comfortable headroom above
        the free-space floor ``T`` (``free_space_limit``, or 25% of the drive), 1 at/below it, with
        CROSS-RUN hysteresis (the previous ``t`` is persisted) so the mode can't oscillate as free space
        hovers at the band edge between runs. FAIL-OPEN (free=inf → t=0). Memoised per (service, instance)
        in the shared band cache (distinct key)."""
        if gw is None:
            return 0.0
        svc = getattr(gw, "service", "?")
        key = (svc, str(inst), "tight")
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
        T, _ = space_targets(self.config, total_gb=total)
        pkey = f"acquisition/demand/tightness/{svc}/{inst}"
        prev = _finite(self.global_cache.get(pkey), 0.0) if self.global_cache else 0.0
        t = tightness_with_hysteresis(free, T, prev, band=band)
        cache[key] = t
        if self.global_cache:
            try:
                self.global_cache.set(pkey, t)
            except Exception:
                pass
        return t

    def _per_user_affinities(self) -> list:
        """The household's per-user genre affinities (one ``{genre: weight}`` dict per tracked user) for
        the demand breadth signal — from ``PlexUsersManager.tracked_users`` × the
        ``tautulli/users/<safe>/affinity`` caches. A user with no history yields ``{}`` (demand then uses
        the popularity prior for them). ``[]`` when there's no user roster (cold household)."""
        um = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        tracked = list(getattr(um, "tracked_users", []) or []) if um else []
        out = []
        for u in tracked:
            safe = u.get("safe_user")
            aff = (self.global_cache.get(f"tautulli/users/{safe}/affinity")
                   if (safe and self.global_cache) else None)
            out.append((aff.get("genres") if isinstance(aff, dict) else None) or {})
        return out

    @staticmethod
    def _votes_to_unit(votes) -> float:
        """TMDb vote count → a 0–1 popularity prior (log-scaled, ~50k votes → 1.0; matches the scorer)."""
        try:
            v = float(votes)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, math.log10(v + 1) / math.log10(50000)) if v > 0 else 0.0

    def _space_ok(self, gw, inst, cache: dict) -> bool:
        """True when an instance is at/above its pressure band (free >= U) — comfortable enough
        to take the 4K bonus copy. FAIL-OPEN via ``_space_band`` (free=inf on a read error)."""
        if gw is None:
            return True
        free, U = self._space_band(gw, inst, cache)
        return free >= U

    def _uhd_space_ok(self, gw, inst, cache: dict) -> bool:
        """GLD-ACQ-30 UHD sub-policy (operator ruling 2026-08-07, post-1.9TB-wipe): a
        2160p acquisition needs MORE than merely being above the pressure band — free
        space must clear the band's upper edge by ``routing.movies.uhd_headroom_multiplier``
        (default 1.25 = the operator's "25% higher than the floor"). 4K is the
        biggest-ticket, least-reversible grab and must be the FIRST lane to pause as
        space tightens — on the day the pilot wave + replacements drove the array back
        under the min-free wall, the 4K lane held the same bar as everything else.
        FAIL-OPEN like ``_space_band`` (unknown free ⇒ ok)."""
        try:
            mult = float((((self.config.get("routing") or {}).get("movies") or {})
                          .get("uhd_headroom_multiplier", 1.25)) or 1.25)
        except (TypeError, ValueError, AttributeError):
            mult = 1.25
        free, U = self._space_band(gw, inst, cache)
        return free >= U * mult

    def _decision_detail(self, msg, *, reset=False):
        """Write one line to the dedicated acquisition decision log (`GLD-ACQS-22`).

        `support/logs/acquisition/decisions.log` -- the nested path needs no plumbing
        because `log_to_file` already does `path.parent.mkdir(parents=True)`. Same
        contract as the routing manager's `_detail`: never echoes to the run log or the
        console, and no-ops when the logger has no file sink, so a None logger in tests
        cannot turn a diagnostic into a crash.
        """
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("acquisition/decisions", msg, reset=reset)

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

    def _space_budget_context(self, eligible: list, acq: dict, gateways: dict,
                              band_cache: dict) -> tuple:
        """``(mode, ctx, kept_ledger)`` -- the per-run byte-budget snapshot.

        mode: 'off' (feature disabled; legacy behaviour byte-identical),
              'budget' (ctx + the TTL-surviving ledger to persist back), or
              'fallback' (enabled but the snapshot could not be built --
              unreadable free space or a corrupt/unreadable ledger -- so the
              caller reverts to the BOUNDED count cap; see the fail-direction
              note at the call site).

        Pools are priced from the instances the eligible candidates actually
        route to, via the same memoised ``_space_band`` every other gate uses.
        Under ``shared_pool`` (default -- one Unraid array behind TRaSH
        hardlinks) the single budget is the MINIMUM headroom across those
        instances minus total in-flight: the most conservative read of a pool
        that is physically one filesystem. An ABSENT ledger key is genuinely
        'nothing ever committed' (first run) and reads as empty; a PRESENT but
        wrong-typed value, or a read that raises, is unknown -- unknown
        in-flight would be undercounted as zero, so both force the fallback."""
        cfg = space_budget.config_for(acq)
        if not cfg.get("enabled"):
            return "off", None, None
        pools: dict = {}
        for e in eligible or []:
            svc = "sonarr" if e.get("type") == "show" else "radarr"
            gw = gateways.get(svc)
            if gw is None or not getattr(gw, "available", True):
                continue          # no gateway -> those adds fail downstream anyway
            inst = str(e.get("instance"))
            free, U = self._space_band(gw, inst, band_cache)
            if math.isinf(free):
                self.logger.log_warning(
                    f"[Acquisition] space budget: free space unreadable for "
                    f"{svc}/{inst} -- falling back to the count cap this run.")
                return "fallback", None, None
            pools[(svc, inst)] = max(0.0, float(free) - float(U))
        try:
            raw = self.global_cache.get(space_budget.LEDGER_KEY) if self.global_cache else None
        except Exception as e:
            self.logger.log_warning(
                f"[Acquisition] space budget: committed ledger unreadable ({e}) -- "
                f"falling back to the count cap this run.")
            return "fallback", None, None
        if raw and not isinstance(raw, list):
            # Only a TRUTHY non-list is corruption. This cache returns {} for a MISSING key
            # ({} is the documented missing sentinel in GlobalCacheManager.get's compat
            # wrapper), so falsy values are the first-run-empty case, not damage. The first
            # live run proved the distinction: the strict `is not None` check read the {}
            # sentinel as corrupt, fell back to the count cap, and -- because fallback never
            # persists the ledger -- could NEVER arm the budget on any subsequent run either.
            # The tell that should have caught it: the deferred-queue read above does the
            # tolerant `isinstance` coercion for exactly this reason.
            self.logger.log_warning(
                "[Acquisition] space budget: committed ledger is corrupt (not a list) -- "
                "falling back to the count cap this run.")
            return "fallback", None, None
        now_ts = datetime.now(timezone.utc).timestamp()
        kept, expired = space_budget.reconcile(
            raw or [], now_ts, float(cfg["committed_ttl_hours"]) * 3600.0)
        if expired:
            self.logger.log_debug(
                f"[Acquisition] space budget: {len(expired)} committed entr"
                f"{'y' if len(expired) == 1 else 'ies'} expired past TTL and released.")
        infl = space_budget.inflight_by_pool(
            kept, bool(cfg.get("shared_pool", True)), float(cfg["default_movie_gb"]))
        if cfg.get("shared_pool", True):
            head = min(pools.values()) if pools else 0.0
            budgets = {space_budget.SHARED_POOL:
                       max(0.0, head - infl.get(space_budget.SHARED_POOL, 0.0))}
        else:
            budgets = {k: max(0.0, v - infl.get(k, 0.0)) for k, v in pools.items()}
        return "budget", space_budget.BudgetContext(budgets, cfg, now_ts), kept

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
                # The deferred add pulls its bytes NOW, not when it was queued -- this is
                # the moment they become in-flight, so this is where the budget's ledger
                # learns about them. The queued record carries its price ('gb'); an old
                # record from before that field existed prices at the module default.
                _fc = space_budget.config_for(
                    (self.config.get("acquisition", {}) if self.config else {}) or {})
                if _fc.get("enabled") and self.global_cache is not None:
                    _gb = item.get("gb")
                    _cand = {"type": item.get("type"), "instance": inst,
                             "ext_id": item.get("arr_id"), "title": item.get("title"),
                             "expected_size_gb": _gb}
                    _price, _ = space_budget.charge_gb(_cand, _fc)
                    try:
                        _led = self.global_cache.get(space_budget.LEDGER_KEY)
                        _led = _led if isinstance(_led, list) else []
                        _led.append(space_budget.make_entry(
                            _cand, _price, datetime.now(timezone.utc).timestamp()))
                        self.global_cache.set(space_budget.LEDGER_KEY, _led)
                    except Exception as _fe:
                        self.logger.log_warning(
                            f"[acquire] space-budget commit for deferred "
                            f"'{item.get('title')}' failed ({_fe}); in-flight undercount.")
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

    # ── targeted single-title acquire (hybrid universe walk) ─────────────────────
    def _persist_deferred(self, item: dict) -> None:
        """Append ONE item to the deferred-search backlog (same shape + cap as run()'s batch
        write). No-op without a global_cache or in dry_run (the caller guards dry_run)."""
        if not self.global_cache:
            return
        q = self.global_cache.get(self._DEFERRED_KEY)
        q = q if isinstance(q, list) else []
        q.append(item)
        if len(q) > self._DEFERRED_MAX:
            q = q[-self._DEFERRED_MAX:]
        self.global_cache.set(self._DEFERRED_KEY, q)

    def _find_movie_record(self, gw, tmdb) -> "tuple[dict | None, str | None]":
        """The full Radarr movie record (id/hasFile/monitored) for a tmdbId, scanning EVERY
        configured Radarr instance (cached library reads). Scanning all instances — not just
        default+anime — closes the dedup blind spot where a saga film already owned on a 4K/other
        instance would be re-added on default. Returns (record, instance) or (None, None)."""
        insts = [k for k in ((self.config or {}).get("radarr_instances", {}) or {})
                 if k != "default_instance"]
        # default first (most movies live there), then the rest, de-duped.
        di = gw.default_instance()
        ordered = list(dict.fromkeys([di] + insts))
        for inst in ordered:
            for rec in gw.library_items(inst):
                if isinstance(rec, dict) and str(rec.get("tmdbId")) == str(tmdb):
                    return rec, inst
        return None, None

    def _grab_existing(self, gw, rec, inst, tmdb, band_cache) -> dict:
        """A film already present in Radarr on ``inst``: already-owned if it has a file, else a real
        GRAB (monitor + search) that honours ``dry_run`` and the free-space band exactly like a fresh
        add. Shared by the routed-instance "already in library" result AND the all-instance
        cross-mount dedup, so a film owned on ANY instance is searched in-place, never re-added on
        default. The search is issued on ``inst`` — the mount the film actually lives on."""
        if rec.get("hasFile"):
            return {"action": "already-owned", "title": rec.get("title")}
        title = rec.get("title") or str(tmdb)
        if self.dry_run:
            self.logger.log_info(
                f"[acquire][universe] [dry-run] would search owned-no-file '{title}'.")
            return {"action": "would-search", "title": title}
        if self._acquisition_paused(gw, inst, band_cache):
            return {"action": "paused", "title": title}
        if not rec.get("monitored"):
            gw.put(inst, f"movie/{rec.get('id')}", {**rec, "monitored": True})
        free, U = self._space_band(gw, inst, band_cache)
        if free < U:
            # under pressure (deletions armed): leave it monitored + queue the search for when free
            # recovers, rather than grabbing into a full disk now.
            self._persist_deferred({
                "service": "radarr", "instance": inst, "arr_id": rec.get("id"), "title": title,
                "type": "movie", "profile": None,
                "queued_at": datetime.now(timezone.utc).isoformat(), "attempts": 0,
            })
            return {"action": "deferred", "title": title}
        ok = self._trigger_search(gw, inst, {"type": "movie", "arr_id": rec.get("id"),
                                             "title": title})
        return {"action": "searched" if ok else "search-failed", "title": title}

    def ensure_owned_and_grab(self, tmdb_id, *, gateways=None, band_cache=None, search=True) -> dict:
        """Ensure ONE specific film (by tmdbId) is owned + grabbed — the entry point the hybrid
        universe walk uses to acquire the next FILM in a saga (see
        ``services/coordinator/universe_acquisition.md``).

        EXPLICIT INTENT: unlike :meth:`run`'s recommendation adds, this BYPASSES ``min_score`` /
        ``max_adds_per_run`` — a saga member is a deliberate target, not a scored suggestion. It
        ALWAYS honours ``dry_run`` and the free-space band: it PAUSES (never strands) when free <
        reserve and deletions are off, and DEFERS the search under pressure (add-monitored-now,
        search-when-space-recovers), exactly like ``run``. Reuses the run() primitives (Resolver
        dedup+lookup+routing, Adder, ``_trigger_search``, the deferred backlog). A coordinator may
        pass shared ``gateways``/``band_cache`` for per-run caching.

        Returns ``{"action", "title"?, "reason"?}`` where action ∈ {already-owned, already-present,
        searched, search-failed, would-search, added, would-add, deferred, paused, add-failed,
        skipped}."""
        if tmdb_id is None:
            return {"action": "skipped", "reason": "no tmdb"}
        if gateways is None:
            gateways = {"radarr": ArrGateway(
                "radarr", getattr(self.radarr, "instance_manager", None), self.config, self.logger)}
        band_cache = band_cache if band_cache is not None else {}
        gw = gateways.get("radarr")
        if gw is None or not gw.available:
            return {"action": "skipped", "reason": "no radarr instance"}

        resolver = Resolver(gateways, self.config, self.logger)
        adder = Adder(gateways, self.logger, dry_run=self.dry_run, monitored=True, search=False)
        enriched = resolver.prepare({"type": "movie", "ids": {"tmdb": int(tmdb_id)}})
        reason = enriched.get("skip_reason")

        # Cross-mount dedup FIRST. prepare() only checks the single ROUTED instance, so a saga film
        # already owned on a DIFFERENT Radarr instance (4K/anime/other mount, separate DB) slips
        # through as "not in library" and would be re-added on default. Scan EVERY instance by exact
        # tmdb up front: a hit is grabbed-in-place (owned-no-file) or already-owned, never re-POSTed.
        # The walk only calls us for titles unowned IN PLEX, so a present record is an
        # added-but-not-imported title — grab it if it has no file, else it's truly owned.
        rec, found_inst = self._find_movie_record(gw, int(tmdb_id))
        if rec is not None:
            return self._grab_existing(gw, rec, found_inst, int(tmdb_id), band_cache)
        # prepare() said it IS in library but our all-instance scan found no record (cache race) →
        # present, no-op (never blind-add on a stale "already in library").
        if reason == "already in library":
            return {"action": "already-present"}

        if reason:                                       # no lookup match etc. → fail closed
            return {"action": "skipped", "reason": reason, "title": enriched.get("title")}

        # FOOTGUN GUARD: never add the wrong film on a fuzzy lookup — require the exact tmdb.
        if str(enriched.get("ext_id")) != str(int(tmdb_id)):
            return {"action": "skipped", "reason": "tmdb mismatch", "title": enriched.get("title")}

        inst = enriched.get("instance")
        title = enriched.get("title") or enriched.get("ext_id")
        # No way to reclaim space (deletion not armed, free < reserve) → pause; a deferred add
        # would strand forever.
        if self._acquisition_paused(gw, inst, band_cache):
            return {"action": "paused", "title": title}
        free, U = self._space_band(gw, inst, band_cache)
        under_pressure = free < U
        res = adder.add(enriched, search=False if under_pressure else search)
        action = res.get("action")
        if under_pressure and action in ("added", "would-add"):
            if res.get("ok") and not self.dry_run:
                aid = (res.get("result") or {}).get("id")
                if aid is not None:
                    self._persist_deferred({
                        "service": "radarr", "instance": inst, "arr_id": aid, "title": title,
                        "type": "movie", "profile": (enriched.get("quality_profile") or {}).get("name"),
                        "queued_at": datetime.now(timezone.utc).isoformat(), "attempts": 0,
                    })
            return {"action": "deferred", "title": title}
        return {"action": action, "title": title}

    def rehome_to_standard(self, tmdb_id, *, std_inst, target_profile_id,
                           gateways=None, band_cache=None) -> dict:
        """FORK-D: add ONE film (by tmdbId) on the STANDARD instance at a specific,
        watchability-matched, sub-4K ``target_profile_id`` and grab it NOW — the rehome half
        of evicting a cold 4K-only film (the 4K copy is deleted later, only once this standard
        copy imports). Differs from :meth:`ensure_owned_and_grab` in two deliberate ways:

          1. Dedup is STANDARD-ONLY. ``ensure_owned_and_grab`` scans every instance
             (_find_movie_record) and would find the 4K copy (hasFile=true) on the 4K instance
             and return already-owned — defeating the rehome. Here we scan only ``std_inst``.
          2. It SEARCHES immediately even under floor pressure (does NOT defer). Below-floor is a
             RESERVE breach, not a full disk — there's ample absolute space for the small
             standard copy, and deferring would deadlock (space only recovers once the 4K is
             evicted, which needs this standard copy first). Net footprint drops after eviction.

        Honours ``dry_run`` (Adder/_trigger_search no-op → would-add/would-search). Returns
        ``{"action","title"?,"reason"?,"standard_id"?}`` with action ∈ {already-owned, searched,
        search-failed, would-search, added, would-add, add-failed, skipped}."""
        if tmdb_id is None:
            return {"action": "skipped", "reason": "no tmdb"}
        if target_profile_id is None:
            return {"action": "skipped", "reason": "no target profile"}
        if gateways is None:
            gateways = {"radarr": ArrGateway(
                "radarr", getattr(self.radarr, "instance_manager", None), self.config, self.logger)}
        band_cache = band_cache if band_cache is not None else {}
        gw = gateways.get("radarr")
        if gw is None or not gw.available:
            return {"action": "skipped", "reason": "no radarr instance"}
        tmdb = int(tmdb_id)
        # Honour the backup gate too: a real run whose backup pre-flight failed DEGRADES to
        # dry-run, so a rehome must NOT POST adds/searches then (the bare self.dry_run would).
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)

        # (1) STANDARD-ONLY dedup. A hit means the film is already on standard: grab it in place
        # if it has no file (forcing the search — never defer, see the docstring), else owned.
        rec = next((r for r in gw.library_items(std_inst)
                    if isinstance(r, dict) and str(r.get("tmdbId")) == str(tmdb)), None)
        if rec is not None:
            if rec.get("hasFile"):
                return {"action": "already-owned", "title": rec.get("title"), "standard_id": rec.get("id")}
            title = rec.get("title") or str(tmdb)
            if eff_dry:
                return {"action": "would-search", "title": title, "standard_id": rec.get("id")}
            # Retune the EXISTING fileless record to the watchability-matched sub-4K target + monitor
            # BEFORE searching, so the rehome grabs a <=1080 baseline even if the record sat at a
            # 4K-capable profile — otherwise a 4K would land on standard and the eviction frees nothing.
            gw.put(std_inst, f"movie/{rec.get('id')}",
                   {**rec, "monitored": True, "qualityProfileId": int(target_profile_id)})
            ok = self._trigger_search(gw, std_inst,
                                      {"type": "movie", "arr_id": rec.get("id"), "title": title})
            return {"action": "searched" if ok else "search-failed", "title": title,
                    "standard_id": rec.get("id")}

        # Not on standard → resolve lookup metadata, then FORCE the standard instance + the
        # watchability-matched profile + a standard-instance root folder.
        resolver = Resolver(gateways, self.config, self.logger)
        adder = Adder(gateways, self.logger, dry_run=eff_dry, monitored=True, search=True)
        enriched = resolver.prepare({"type": "movie", "ids": {"tmdb": tmdb}})
        reason = enriched.get("skip_reason")
        if reason == "already in library":
            # prepare dedups its ROUTED instance (anime films route to the anime instance); our
            # standard scan above already cleared standard, so this hit is on another instance and
            # prepare returned without lookup metadata → can't build the add. Skip (4K stays).
            return {"action": "skipped", "reason": "routed-instance dup", "title": enriched.get("title")}
        if reason:
            return {"action": "skipped", "reason": reason, "title": enriched.get("title")}
        if str(enriched.get("ext_id")) != str(tmdb):
            return {"action": "skipped", "reason": "tmdb mismatch", "title": enriched.get("title")}

        enriched["instance"] = std_inst
        qp = dict(enriched.get("quality_profile") or {})
        qp["id"] = int(target_profile_id)
        enriched["quality_profile"] = qp
        # Root folder for the STANDARD instance's movie bucket (NOT the routed instance's — a
        # 4K/anime route would otherwise point at the wrong folder). Fall back to prepare's
        # value (already standard's for a default-routed non-anime film) when unconfigured.
        _mrf = (self.config or {}).get("movieRootFolders", {}) or {}
        enriched["root_folder"] = (_mrf.get(enriched.get("route_category"))
                                   or _mrf.get("standard")
                                   or enriched.get("root_folder"))
        title = enriched.get("title") or enriched.get("ext_id")

        res = adder.add(enriched, search=True)   # grab the small standard copy NOW (no defer)
        aid = (res.get("result") or {}).get("id") if isinstance(res.get("result"), dict) else None
        return {"action": res.get("action"), "title": title, "standard_id": aid}

    # ── show (Sonarr) twin of the movie grab — the hybrid universe walk's TV add-by-tvdb ──────
    def _find_series_record(self, gw, tvdb) -> "tuple[dict | None, str | None]":
        """The full Sonarr series record for a tvdbId, scanning every configured Sonarr instance
        (cached library reads). Closes the same dedup blind spot as :meth:`_find_movie_record` so a
        saga show already on another instance is never re-added. Returns (record, instance) or
        (None, None). Shows are keyed by ``tvdbId`` (NOT tmdbId — that's the movie identity)."""
        insts = [k for k in ((self.config or {}).get("sonarr_instances", {}) or {})
                 if k != "default_instance"]
        di = gw.default_instance()
        ordered = list(dict.fromkeys([di] + insts))
        for inst in ordered:
            for rec in gw.library_items(inst):
                if isinstance(rec, dict) and str(rec.get("tvdbId")) == str(tvdb):
                    return rec, inst
        return None, None

    def _grab_existing_show(self, gw, rec, inst, tvdb, band_cache) -> dict:
        """A series already present in Sonarr on ``inst``: already-owned if it has any downloaded
        episode, else a real GRAB (monitor + SeriesSearch) honouring ``dry_run`` + the free-space
        band, exactly like :meth:`_grab_existing` for movies. NOTE: a series record has NO ``hasFile``
        — ownership is ``statistics.episodeFileCount > 0``."""
        if (rec.get("statistics") or {}).get("episodeFileCount", 0) > 0:
            return {"action": "already-owned", "title": rec.get("title")}
        title = rec.get("title") or str(tvdb)
        if self.dry_run:
            self.logger.log_info(
                f"[acquire][universe] [dry-run] would search owned-no-file show '{title}'.")
            return {"action": "would-search", "title": title}
        if self._acquisition_paused(gw, inst, band_cache):
            return {"action": "paused", "title": title}
        if not rec.get("monitored"):
            gw.put(inst, f"series/{rec.get('id')}", {**rec, "monitored": True})
        free, U = self._space_band(gw, inst, band_cache)
        if free < U:
            self._persist_deferred({
                "service": "sonarr", "instance": inst, "arr_id": rec.get("id"), "title": title,
                "type": "show", "profile": None,
                "queued_at": datetime.now(timezone.utc).isoformat(), "attempts": 0,
            })
            return {"action": "deferred", "title": title}
        ok = self._trigger_search(gw, inst, {"type": "show", "arr_id": rec.get("id"),
                                             "title": title})
        return {"action": "searched" if ok else "search-failed", "title": title}

    def ensure_show_owned_and_grab(self, tvdb_id, *, gateways=None, band_cache=None, search=True) -> dict:
        """Ensure ONE specific series (by tvdbId) is owned + grabbed — the SHOW twin of
        :meth:`ensure_owned_and_grab`, used by the hybrid universe walk to acquire the next SHOW in a
        saga. Same contract: bypasses min_score/max_adds, honours dry_run + the free-space band on the
        Sonarr mount (the ONLY place a TV add can hit a full Sonarr disk — the next-episode walk only
        prefetches already-owned series). Reuses Resolver/Adder show routing. Returns the same action
        shape as the movie path."""
        if tvdb_id is None:
            return {"action": "skipped", "reason": "no tvdb"}
        if gateways is None:
            gateways = {"sonarr": ArrGateway(
                "sonarr", getattr(self.sonarr, "instance_manager", None), self.config, self.logger)}
        band_cache = band_cache if band_cache is not None else {}
        gw = gateways.get("sonarr")
        if gw is None or not gw.available:
            return {"action": "skipped", "reason": "no sonarr instance"}

        resolver = Resolver(gateways, self.config, self.logger)
        adder = Adder(gateways, self.logger, dry_run=self.dry_run, monitored=True, search=False)
        enriched = resolver.prepare({"type": "show", "ids": {"tvdb": int(tvdb_id)}})
        reason = enriched.get("skip_reason")

        rec, found_inst = self._find_series_record(gw, int(tvdb_id))
        if rec is not None:
            return self._grab_existing_show(gw, rec, found_inst, int(tvdb_id), band_cache)
        if reason == "already in library":
            return {"action": "already-present"}
        if reason:
            return {"action": "skipped", "reason": reason, "title": enriched.get("title")}

        # FOOTGUN GUARD: never add the wrong series on a fuzzy lookup — require the exact tvdb.
        if str(enriched.get("ext_id")) != str(int(tvdb_id)):
            return {"action": "skipped", "reason": "tvdb mismatch", "title": enriched.get("title")}

        inst = enriched.get("instance")
        title = enriched.get("title") or enriched.get("ext_id")
        if self._acquisition_paused(gw, inst, band_cache):
            return {"action": "paused", "title": title}
        free, U = self._space_band(gw, inst, band_cache)
        under_pressure = free < U
        res = adder.add(enriched, search=False if under_pressure else search)
        action = res.get("action")
        if under_pressure and action in ("added", "would-add"):
            if res.get("ok") and not self.dry_run:
                aid = (res.get("result") or {}).get("id")
                if aid is not None:
                    self._persist_deferred({
                        "service": "sonarr", "instance": inst, "arr_id": aid, "title": title,
                        "type": "show", "profile": (enriched.get("quality_profile") or {}).get("name"),
                        "queued_at": datetime.now(timezone.utc).isoformat(), "attempts": 0,
                    })
            return {"action": "deferred", "title": title}
        return {"action": action, "title": title}

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
        scorer = AcquisitionScorer(self.global_cache, self.logger, self.config)
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
        # Per-candidate decision record (`GLD-ACQS-22`). A candidate can be lost at SIX
        # different points below, each knowing different things about it, and until now
        # only the aggregate counts survived. With free space well above the band the
        # byte budget refuses nothing, so `max_adds_per_run` becomes the SOLE limiter
        # and the ranking alone decides what the household gets -- a ranking that was
        # nowhere on disk. Written to its own file for the same reason routing's plan is:
        # a thousand per-title lines would bury the run narrative an operator reads.
        _dl = decision_log.new_log()

        prepared, skipped = [], {}
        for cand in raw:
            enriched = resolver.prepare(cand)
            reason = enriched.get("skip_reason")
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                # FATE 1: rejected before scoring. No score exists yet, and recording a
                # blank one is correct -- an invented score in a ranking audit is worse
                # than an absent one.
                decision_log.record(
                    _dl, title=(enriched.get("title") or cand.get("title") or cand.get("ext_id")),
                    disposition="skipped", media=enriched.get("type") or cand.get("type"),
                    reason=reason)
                continue
            sc = scorer.score(enriched)
            enriched["score"], enriched["matrix"] = sc["total"], sc["matrix"]
            enriched["evidence"] = sc.get("evidence", {})   # raw drivers for the why breakdown
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
        # FATE 2: scored, but under the floor. This is the one disposition where the
        # score is the WHOLE reason, so it is named in the row.
        for e in prepared:
            if e["score"] < min_score:
                decision_log.record(
                    _dl, title=e.get("title") or e.get("ext_id"), disposition="refused",
                    score=e.get("score"), media=e.get("type"), instance=e.get("instance"),
                    reason=f"below min_score ({min_score})")
        # Demand-aware ordering (acquisition.demand.enabled, default OFF → plain score-desc,
        # byte-identical). As an instance's free space nears the floor, weight a candidate by how many
        # household users would watch it (breadth) so the capped budget fills with broad-appeal media;
        # a roomy instance keeps pure watchability order. Reorders BEFORE the cap so demand decides
        # which titles make the cut, not just their order.
        if bool((acq.get("demand", {}) or {}).get("enabled", False)):
            self._demand_rank(eligible, gateways, band_cache, acq)
        else:
            eligible.sort(key=lambda x: x["score"], reverse=True)
        cap = int(acq.get("max_adds_per_run", 10) or 0)
        # Byte budget (acquisition.space_budget.enabled): fund candidates in priority order
        # out of max(0, free-U) minus the bytes RECENT runs committed that have not landed
        # yet, instead of slicing the top N. FAIL DIRECTION IS INVERTED here: everywhere
        # else space gates fail OPEN (free=inf on a read error so a transient never blocks
        # an add) because "open" under a count cap still means at most N adds. Under an
        # uncapped byte budget "open" would mean unlimited-with-no-budget, so any gap in
        # the information the budget needs (unreadable free space, corrupt ledger) falls
        # back to the BOUNDED legacy count cap -- and a configured cap of 0 (uncapped)
        # falls back to 10, never to unlimited.
        sb_mode, sb_ctx, sb_kept = self._space_budget_context(eligible, acq, gateways, band_cache)
        if sb_mode == "budget":
            selected, sb_skipped = space_budget.select(eligible, sb_ctx)
            if sb_skipped:
                skipped["space_budget"] = skipped.get("space_budget", 0) + len(
                    [s for s in sb_skipped if s.get("skip_reason") == "space_budget"])
                hm = len([s for s in sb_skipped if s.get("skip_reason") == "space_budget_hard_max"])
                if hm:
                    skipped["space_budget_hard_max"] = skipped.get("space_budget_hard_max", 0) + hm
                worst = sorted((s for s in sb_skipped if s.get("space_charge_gb")),
                               key=lambda s: -float(s["space_charge_gb"]))[:5]
                if worst:
                    self.logger.log_info(
                        "[Acquisition] space budget refused "
                        + ", ".join(f"'{s.get('title')}' (~{s['space_charge_gb']:.0f}GB)"
                                    for s in worst)
                        + (f" +{len(sb_skipped) - len(worst)} more" if len(sb_skipped) > len(worst) else "")
                        + " -- retried next run as budget refreshes.")
                # FATE 3: the byte budget said no. The main log names only the five
                # largest; every one lands here with the charge that priced it out, and
                # `space_budget_hard_max` is kept DISTINCT from `space_budget` because
                # they mean different things -- "no room" versus "room, but the count
                # cap stopped us" -- and conflating them is how `GLD-ACQS-20` reported
                # 122 hard-max refusals as "0 refused".
                for s in sb_skipped:
                    _r = s.get("skip_reason")
                    decision_log.record(
                        _dl, title=s.get("title") or s.get("ext_id"),
                        disposition="capped" if _r == "space_budget_hard_max" else "refused",
                        score=s.get("score"), media=s.get("type"), instance=s.get("instance"),
                        gb=s.get("space_charge_gb") or s.get("expected_size_gb"),
                        tier=(s.get("quality_profile") or {}).get("name"),
                        reason=("hard_max_adds reached" if _r == "space_budget_hard_max"
                                else "space budget exhausted"))
        else:
            if sb_mode == "fallback":
                cap = cap or 10   # enabled-but-unbuildable NEVER degrades to unlimited
            selected = eligible[:cap] if cap > 0 else eligible
            # FATE 4: THE SILENT ONE. `eligible[:cap]` discards the remainder with no
            # counter, no log line and no record anywhere -- 1,007 titles on the
            # 2026-08-22 run. It is also the disposition that matters most right now:
            # with free space far above the band nothing is refused on budget, so this
            # slice IS the decision, and the ranking that produced it was unrecoverable.
            if cap > 0 and len(eligible) > cap:
                for e in eligible[cap:]:
                    decision_log.record(
                        _dl, title=e.get("title") or e.get("ext_id"), disposition="capped",
                        score=e.get("score"), media=e.get("type"), instance=e.get("instance"),
                        gb=e.get("space_charge_gb") or e.get("expected_size_gb"),
                        tier=(e.get("quality_profile") or {}).get("name"),
                        reason=f"max_adds_per_run ({cap}) reached")

        # Stage-C remote-play gate (default OFF): when routing.movies.transcode_gate is on,
        # only emit the 4K bonus copy if a likely household device can DIRECT-PLAY a 2160p HEVC
        # file (learned from Tautulli transcode history). Computed ONCE per run — it's a
        # household-global, candidate-independent read — and passed into plan_uhd_companion.
        # Flag OFF → True (the companion is emitted exactly as before).
        uhd_crp = uhd_remote_play_ok(
            self.config,
            self.global_cache.get("tautulli/transcode_fingerprint") if self.global_cache else None,
            self.global_cache.get("tautulli/platforms") if self.global_cache else None,
        )

        # Saga attribution: a recommendation add that happens to be a member of an engaged
        # saga (an MCU film, a One Chicago show) is tagged with its saga so the decision table
        # and breakdown say WHICH universe it belongs to. Built once from the cached universe
        # source; empty (and the 'saga' column omitted) when the universe feature is unbuilt.
        saga_idx = saga_membership_index(
            self.global_cache.get("plex/playlists/universe_source") if self.global_cache else None)
        has_saga = bool(saga_idx)

        rows, added, would, failed, deferred = [], 0, 0, 0, 0
        elevated: list[dict] = []          # acted-on titles, for the why/elevation breakdown
        new_deferred: list[dict] = []
        for e in selected:
            svc = "sonarr" if e.get("type") == "show" else "radarr"
            gw = gateways.get(svc)
            # Top score drivers, with the genre component rendered as the actual matched genre
            # names (from evidence) rather than a bare "genre 71" score.
            why = scorer.reason(e.get("matrix"), evidence=e.get("evidence"))
            # Which saga(s) this title belongs to — full names in the table and the breakdown.
            # Stamped on `e` so the elevation breakdown can reuse it.
            saga_names = [saga_display_name(k) for k in self._saga_keys_for(e, saga_idx)]
            saga_cell = " / ".join(saga_names) if saga_names else "-"
            e["saga_names"] = saga_names

            # No way to reclaim space (deletion not consented/armed) and we're below the
            # pressure band → skip the add entirely. A deferred title would never get its
            # space freed, so pause new acquisition instead of stranding it in *arr.
            if self._acquisition_paused(gw, e.get("instance"), band_cache):
                skipped["space_full_no_deletion"] = skipped.get("space_full_no_deletion", 0) + 1
                # FATE 5: made the cut, then the instance turned out to have no way to
                # reclaim space. Distinct from a budget refusal: this title was FUNDED
                # and lost to a condition discovered later.
                decision_log.record(
                    _dl, title=e.get("title") or e.get("ext_id"), disposition="skipped",
                    score=e.get("score"), media=e.get("type"), instance=e.get("instance"),
                    gb=e.get("space_charge_gb") or e.get("expected_size_gb"),
                    tier=(e.get("quality_profile") or {}).get("name"),
                    reason="instance full and deletion not consented")
                row = [
                    str(e.get("title") or e.get("ext_id"))[:34],
                    e.get("type"), e.get("score"), str(e.get("instance")),
                    (e.get("quality_profile") or {}).get("name"),
                    self._size_str(e), "skipped (full)", why,
                ]
                if has_saga:
                    row.insert(2, saga_cell)
                rows.append(row)
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
                            # The byte price for the flush-time ledger commit: a deferred add
                            # pulls its bytes only when _flush_deferred finally searches it,
                            # so the budget must charge THERE, and there is where this rides.
                            "gb": e.get("space_charge_gb") or e.get("expected_size_gb"),
                            "queued_at": datetime.now(timezone.utc).isoformat(),
                            "attempts": 0,
                        })
            else:
                added += action == "added"
                would += action == "would-add"
                if action == "added" and sb_ctx is not None:
                    # Cross-run in-flight accounting -- only a REAL add commits bytes.
                    sb_ctx.commit(e, e.get("space_charge_gb"))
            failed += action == "add-failed"
            # FATE 6: the outcome of actually trying. `would-fund` rather than `funded`
            # on a dry run, because a preview that reports titles as acquired is how a
            # dry run gets mistaken for a live one.
            decision_log.record(
                _dl, title=e.get("title") or e.get("ext_id"),
                disposition={"added": "funded", "would-add": "would-fund",
                             "deferred": "deferred"}.get(action, "skipped"),
                score=e.get("score"), media=e.get("type"), instance=e.get("instance"),
                gb=e.get("space_charge_gb") or e.get("expected_size_gb"),
                tier=(e.get("quality_profile") or {}).get("name"),
                reason=("searched on add" if action == "added" and not under_pressure else
                        "added with search OFF (under pressure)" if action == "deferred" else
                        res.get("error") or action))

            # Change-plan accounting (GLD-SPC-01). An acquisition has NO Parquet row to
            # stamp -- the title is not in the library yet -- so the decision ledger
            # structurally cannot see it, and the end-of-run grid was omitting the
            # largest byte flow in the run while titling itself "every planned action".
            # Recorded for would-add too: the grid is a PREVIEW, and a dry run that
            # showed nothing here is exactly how the omission stayed invisible.
            if action in ("added", "would-add", "deferred"):
                _gb = e.get("space_charge_gb") or e.get("expected_size_gb")
                try:
                    _gb = -abs(float(_gb))          # signed: -GiB consumed
                except (TypeError, ValueError):
                    _gb = None
                if _gb is not None:
                    record_pending(
                        self.global_cache, service=svc, instance=str(e.get("instance")),
                        action="acquire", title=str(e.get("title") or e.get("ext_id")),
                        gb=_gb, ext_id=e.get("ext_id"), reason=why)

            row = [
                str(e.get("title") or e.get("ext_id"))[:34],
                e.get("type"),
                e.get("score"),
                str(e.get("instance")),
                (e.get("quality_profile") or {}).get("name"),
                self._size_str(e),
                action,
                why,
            ]
            if has_saga:
                row.insert(2, saga_cell)
            rows.append(row)
            self.logger.log_debug(f"[acquire] '{e.get('title')}' matrix={e.get('matrix')}")
            if action in ("added", "would-add", "deferred"):
                # Stamp the outcome onto the candidate BEFORE it joins `elevated`. `action` is a
                # loop local and was never written back, so breakdown.SCHEMA's `decision` column
                # would be null on every row -- a structurally-always-empty field in the frame the
                # website reads, indistinguishable from "this run had no decisions".
                e["decision"] = action
                elevated.append(e)

            # Dual-version companion (4k_policy=='both'): add the 2160p copy on the 4K instance
            # ON TOP of the <=1080 baseline just added. Make-before-break — gated on the baseline
            # POSTing OK and NOT being deferred, so the durable floor lands (and searches) first;
            # if the standard instance is pressured this run, the 4K copy waits for the reconcile
            # sweep. plan_uhd_companion returns None unless dual is active + UHD is warranted +
            # the 4K instance has space + the title isn't already a 4K copy.
            if svc == "radarr" and res.get("ok") and not under_pressure:
                # GLD-ACQ-30 UHD sub-policy: the COMPANION path uses the STRICTER
                # headroom callback — the 2160p bonus copy only lands when free space
                # clears U × uhd_headroom_multiplier, not merely the band.
                companion = resolver.plan_uhd_companion(
                    e, space_ok=lambda inst, _gw=gw: self._uhd_space_ok(_gw, inst, band_cache),
                    can_remote_play=uhd_crp)
                if companion is not None:
                    # The 4K copy is the single largest file class in the system and it is
                    # planned HERE, after selection -- so the live budget must price it or
                    # the budget governs everything except the biggest bytes.
                    c_ok = True
                    if sb_ctx is not None:
                        c_ok, _c_gb, _c_def = sb_ctx.try_charge(companion)
                    if not c_ok:
                        crow = [
                            str(companion.get("title") or companion.get("ext_id"))[:34],
                            "movie", companion.get("score"),
                            f"{companion.get('instance')} [4k]",
                            (companion.get("quality_profile") or {}).get("name"),
                            self._size_str(companion),
                            "skipped [4k] (space budget)",
                            why,
                        ]
                        if has_saga:
                            crow.insert(2, saga_cell)
                        rows.append(crow)
                    else:
                        cres = adder.add(companion, search=True)
                        caction = cres.get("action")
                        added += caction == "added"
                        would += caction == "would-add"
                        failed += caction == "add-failed"
                        if caction == "added" and sb_ctx is not None:
                            sb_ctx.commit(companion, companion.get("space_charge_gb"))
                        crow = [
                            str(companion.get("title") or companion.get("ext_id"))[:34],
                            "movie", companion.get("score"),
                            f"{companion.get('instance')} [4k]",
                            (companion.get("quality_profile") or {}).get("name"),
                            self._size_str(companion),
                            f"{caction} [4k]",
                            why,
                        ]
                        if has_saga:
                            crow.insert(2, saga_cell)
                        rows.append(crow)

        # Persist the new deferrals onto the backlog (live runs only), bounding its length
        # so chronic pressure can't grow it without limit (keep the newest).
        if new_deferred and self.global_cache:
            q = self.global_cache.get(self._DEFERRED_KEY)
            q = q if isinstance(q, list) else []
            q.extend(new_deferred)
            if len(q) > self._DEFERRED_MAX:
                q = q[-self._DEFERRED_MAX:]
            self.global_cache.set(self._DEFERRED_KEY, q)

        # Persist the committed-bytes ledger: TTL-survivors from the read plus this run's
        # commits, live runs only. A failed write is LOUD -- the next run would under-count
        # in-flight bytes and over-commit, which is the exact failure this ledger prevents.
        if sb_mode == "budget" and not self.dry_run and self.global_cache is not None \
                and (sb_ctx.commits or sb_kept is not None):
            try:
                self.global_cache.set(space_budget.LEDGER_KEY, (sb_kept or []) + sb_ctx.commits)
            except Exception as _sb_e:
                self.logger.log_warning(
                    f"[Acquisition] space-budget ledger write failed ({_sb_e}); next run "
                    f"will under-count in-flight bytes (over-commit risk).")
        if sb_mode == "budget":
            s = sb_ctx.stats
            self.logger.log_info(
                f"[Acquisition] space budget: {s['funded']} funded (~{s['charged_gb']:.0f} GB"
                + (f", {s['defaulted_charges']} at default price" if s['defaulted_charges'] else "")
                + f"), {s['skipped_space']} refused (~{s['skipped_gb']:.0f} GB)"
                + (f", {s['skipped_hard_max']} capped (hard_max_adds)"
                   if s.get('skipped_hard_max') else "")
                + (f", {s['uncharged_no_pool']} uncharged (no pool)" if s['uncharged_no_pool'] else "")
                + f"; in-flight carried: {len(sb_kept or [])} entr{'y' if len(sb_kept or []) == 1 else 'ies'}.")
        if deferred:
            self.logger.log_info(
                f"[Acquisition] {deferred} title(s) deferred under space pressure "
                f"(added monitored, search OFF; will search when free >= U)."
            )

        # ── the per-candidate record (`GLD-ACQS-22`) ────────────────────────────
        # `reset=True` on the first line: ONE fresh plan per run, exactly like
        # routing.log. A decision log that appends across runs would show a ranking
        # from three runs ago beside this run's summary and read as a contradiction.
        #
        # Wrapped whole: this is a diagnostic, and a diagnostic that can fail the pass
        # it documents is worse than no diagnostic. Everything above has already
        # happened -- titles are added, bytes are committed -- so there is nothing left
        # to protect by raising.
        try:
            _summary = decision_log.summarise(_dl)
            if _summary["total"]:
                _lines = decision_log.render(
                    _dl,
                    instance_label=", ".join(sorted({str(g) for g in gateways})) or None,
                    hard_max=(int(acq.get("max_adds_per_run", 10) or 0) or None),
                    free_gb=None)
                for _i, _line in enumerate(_lines):
                    self._decision_detail(_line, reset=(_i == 0))
                self.logger.log_info(
                    f"[Acquisition] decision log: {_summary['total']} candidate(s) — "
                    f"{_summary['funded']} funded, {_summary['capped']} capped, "
                    f"{_summary['refused']} refused, {_summary['skipped']} skipped — "
                    f"full ranking in support/logs/acquisition/decisions.log")
        except Exception as _dl_e:
            self.logger.log_debug(f"[Acquisition] decision log not written: {_dl_e}")

        if rows:
            headers = ["title", "type", "score", "instance", "profile", "~size", "decision", "why"]
            if has_saga:
                headers.insert(2, "saga")
            self.logger.log_table(
                headers, rows, title="Acquisition decisions",
                caption="Why each candidate was added: score, routed instance/profile, est. size, "
                        "and the saga it belongs to (if any). Profile reasoning below.",
            )
        else:
            self.logger.log_info("[Acquisition] no new candidates to add.")
        if elevated:
            self._log_elevation_breakdown(elevated, scorer)
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
    def _saga_keys_for(e: dict, saga_idx: dict) -> list:
        """The saga key(s) this candidate belongs to, via the reverse membership index keyed by
        (media, native-id). Movies key by tmdbId, shows by tvdbId — exactly the ext_id the resolver
        resolved. Empty when the title is in no saga (or the index is empty)."""
        if not saga_idx:
            return []
        try:
            eid = int(e.get("ext_id"))
        except (TypeError, ValueError):
            return []
        return saga_idx.get((e.get("type"), eid), [])

    @staticmethod
    def _size_str(e: dict) -> str:
        size = e.get("expected_size_gb")
        if not size:
            return "?"
        suffix = "/ep" if e.get("size_unit") == "per-episode" else ""
        return f"~{size}GB{suffix}"

    # NB: the vote/feed humanizers that used to live here were the prose emitter's ONLY
    # callers. They now live once in acquisition/breakdown.py (_fmt_votes / _fmt_feed /
    # _short_feed) -- kept single-sourced deliberately so a label change cannot drift
    # between the log tables and the website frame (P-E).

    def _log_elevation_breakdown(self, elevated: list, scorer) -> None:
        """Render the "why was this elevated" breakdown and persist its frame.

        Delegates to :mod:`acquisition.breakdown`: ONE canonical record per acted-on title
        (``breakdown.SCHEMA``), projected into the boxed tables -- evidence, score
        decomposition, profile rationale key, cohort roll-up, signal coverage, genre
        affinity, taste profile. The prose stanzas this replaced repeated two RUN-CONSTANT
        things on every row (the household genre weight and the ~110-char profile
        rationale); both are now emitted once as legends aggregated off the same records,
        and the score decomposition is strictly new (it was only ever a log_debug dump).

        The records are also stashed on the global cache under ``acquisition/breakdown`` as
        the single frame the website generator reads. Cache write failures are swallowed:
        the log tables are the contract here, the frame is a by-product.
        """
        if not elevated:
            return
        weights = dict(getattr(scorer, "_weights", {}) or {})
        taste = scorer.taste_profile() if scorer is not None else {}
        records = acq_breakdown.build_records(elevated, weights)
        acq_breakdown.render(records, taste, weights, self.logger)
        if self.global_cache:
            try:
                self.global_cache.set(
                    "acquisition/breakdown",
                    acq_breakdown.to_payload(records, taste, weights))
            except Exception:
                pass

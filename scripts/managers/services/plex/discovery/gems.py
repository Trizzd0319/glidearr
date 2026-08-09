"""plex/discovery/gems.py — the "Hidden Gems" per-profile shelf BUILDER (READ-ONLY).
================================================================================
Surfaces the OWNED backlog nobody is working through: for each opted-in Home profile, the
titles that profile OWNS, has NEVER played, and that match its TASTE — ranked by a
taste-only score that deliberately drops the engagement signals (see
``machine_learning/discovery/gems.py`` for the full reasoning), diversity-capped so one saga
cannot fill the shelf, and cached as a plan the existing write-back renders.

This manager only GATHERS the inputs (owned movies incl. their persisted score breakdown, the
Plex owned inventory, per-user watch history + library grants + age tier, the other cached
plans, the recommendation ledger) and runs the PURE pipeline. It performs NO Plex writes, NO
acquisition and NO deletion of its own.

It also closes the measurement loop, which is the point of the surface: every published pick
is recorded as a recommendation event, and every previously-recorded pick is joined against
what the profile actually played. A pick is a HIT if it was played within the measurement
window (30 days), a MISS once the window elapses with no play, PENDING before then. Two
tables per run — the shelf summary (house style, matching the playlist builders' columns +
median taste) and the hit-rate table — carry the numbers; per-item detail goes to the
support/logs/playlists.log mirror, never the shell.

WHY A SIBLING MODULE RATHER THAN AN EXTENSION OF ``DiscoveryShelfBuilderManager``: they share
the word "discovery" and nothing else. The anniversary shelf is CALENDAR-scoped (this week in
any past year), household-scored ONCE with a re-weighted AcquisitionScorer, and its point is
NET-NEW titles to acquire. This shelf is TASTE-scoped over the owned library only, scores
from the persisted watchability breakdown (no scorer run at all), never proposes an
acquisition, and owns a label-generating measurement loop the anniversary shelf has no
concept of. Folding them together would mean one class with two disjoint halves; instead it
subclasses ``MoviePlaylistBuilderManager`` — the manager whose owned-movie loader, cert
gating, watch-history join, delete-shield publisher and summary-grid emitter it genuinely
reuses.

Default-OFF behind ``plex.playlists.hidden_gems.enabled`` -> byte-identical when unset (no
plans cached, no events written, no tables, no shield key).
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.managers.machine_learning.discovery.gems import (
    DEFAULT_WINDOW_DAYS,
    apply_diversity_caps,
    classify_outcome,
    gem_candidates,
    hit_rate_summary,
    median_taste,
)
from scripts.managers.machine_learning.labels import recommendations as rec_ledger
from scripts.managers.machine_learning.playlists.cert_gate import (
    ADULT,
    cert_allowed,
    cert_summary,
    is_restricted,
    tier_level,
)
from scripts.managers.services.plex._common import anon_label
from scripts.managers.services.plex.playlists.movie_builder import MoviePlaylistBuilderManager
from scripts.managers.services.plex.playlists.movie_resolver import _norm as _norm_movie
from scripts.managers.services.plex.playlists.movie_resolver import movie_play_times

_INVENTORY_KEY = "plex/movies/owned_inventory"
_PLAN_KEY = "plex/playlists/gems_plan"                  # + /{safe_user} — rendered by writeback
# Union of movie tmdbIds this shelf published — the space coordinator SHIELDS them from the
# delete pool. Its own key (not merged into the Up Next ones) so the shield can be reasoned
# about per surface and the ML snapshot's ``in_up_next`` column keeps meaning "Up Next".
_PROTECTED_KEY = "plex/playlists/protected_movie_tmdbs/hidden_gems"

# Other per-user plans whose picks must NOT be re-surfaced here (never double-surface a title
# the household is already being shown). TV plans are skipped — they carry no movie tmdbs.
#
# THE EXCLUSION IS ONE-WAY, AND HIDDEN GEMS IS THE SIDE THAT YIELDS. This shelf's
# entire claim is "nothing has shown you this" - owned, never played, never
# surfaced. Every other family is free to pick what it likes; a title appearing
# anywhere else simply stops being hidden, so it leaves HERE. Making the exclusion
# mutual would be wrong in both directions: it would let an arbitrary run order
# decide which shelf got a title, and it would let this shelf's picks suppress a
# list that had a better reason to show them.
#
# ``affinity_plan`` (Because You Watched) is the newest case and the clearest one.
# It exists precisely to FOLLOW UP on something already surfaced and finished, so
# by construction its picks are the opposite of hidden. Without this line the two
# shelves would draw from the same owned-and-unwatched pool with overlapping taste
# signals and put the same film in front of the household twice, under two names,
# with two different explanations.
_OTHER_PLAN_KEYS = (
    "plex/playlists/movie_plan", "plex/playlists/combined_plan",
    "plex/playlists/glide_plan", "plex/playlists/touchgo_plan",
    "plex/playlists/fresh_movie_plan", "plex/playlists/twih_movie_plan",
    "plex/playlists/affinity_plan", "plex/playlists/tonight_plan",
)

_SHELF_LABEL = "Hidden Gems"


class HiddenGemsShelfBuilderManager(MoviePlaylistBuilderManager):
    parent_name = "PlexManager"

    # The base loader's projection PLUS what the gem pipeline reads: the persisted signal
    # breakdown (the taste score's only input) and the credits the per-person diversity cap
    # walks. See MoviePlaylistBuilderManager._OWNED_MOVIE_COLUMNS.
    _OWNED_MOVIE_COLUMNS = MoviePlaylistBuilderManager._OWNED_MOVIE_COLUMNS + (
        "watchability_breakdown", "cast_names", "director_names")

    # House-style summary table (the playlist builders' columns) + the one number that says how
    # strong the picks were. Consumed by the inherited :meth:`_emit_summary_grid`.
    _SUMMARY_COLS = ("Shelf", "Profile", "Items", "Allowed", "Strictest", "Unrated", "Cert",
                     "Median taste")
    _SUMMARY_CAPTION = (
        "One row per shelf x profile. Allowed = the certification ceiling this profile's "
        "parental-controls tier permits; Strictest = the most mature certification actually "
        "present in the generated shelf; Unrated = items carrying no recognised certification "
        "(admitted via the Common Sense age fallback - never a violation, but the leak path "
        "worth watching); Cert = OK, or VIOLATION when the shelf holds something the age gate "
        "should have rejected; Median taste = median TASTE-ONLY score (0-100) of the picks - "
        "engagement signals excluded, so it measures fit, not familiarity. Per-item picks are "
        "mirrored to support/logs/playlists.log.")

    _HITRATE_COLS = ("Profile", "Published", "Pending", "Matured", "Hits", "Hit rate",
                     "Median days", "Confidence")
    _HITRATE_CAPTION = (
        "Did the shelf work? Every published pick is a real prospective label: HIT = this "
        "profile played it within the measurement window, MISS = the window closed with no "
        "play, PENDING = the window is still open (never counted as a failure). Hit rate is "
        "over MATURED picks only. Confidence annotates the sample: 'directional' under 30 "
        "matured picks means read the order, not the magnitude.")

    # ── run (I/O gather -> pure pipeline -> cache + measure) ────────────────────
    def run(self) -> dict:
        cfg = self._gems_cfg()
        if not cfg.get("enabled"):
            self.logger.log_debug("[HiddenGems] hidden_gems disabled — skipped.")
            return {"enabled": False}

        tracked = [u for u in self._tracked_users() if self._opted_in(u, cfg)]
        if not tracked:
            self.logger.log_info("[HiddenGems] enabled but no opted-in users — nothing to build.")
            return {"enabled": True, "users": 0, "built": 0}

        size, per_franchise, per_person = self._shape(cfg)
        window = self._window_days(cfg)
        min_pct = self._play_min_pct(cfg)
        # ONE clock for the whole pass: the stamp written onto every event and the "now" the
        # outcome join measures against MUST agree, or a pick's window would start at a
        # different instant than it is judged from.
        now_dt = self._now()
        now_ts = now_dt.timestamp()
        recommended_at = now_dt.replace(microsecond=0).isoformat()
        profile_ages = self._profile_ages()
        base_dir = self._events_base_dir()
        ledger = self._load_ledger(base_dir)

        inventory = self._cache_get(_INVENTORY_KEY, {}) or {}
        owned = self._load_owned_movies() if inventory else []
        if not (inventory and owned):
            # Nothing to BUILD from — but the ledger may still hold picks whose windows are
            # maturing, and those labels are the point of the surface, so they are still
            # measured and reported. Deliberately NO cache writes on this path: republishing an
            # empty plan (or an empty delete shield) off a transiently-missing scan would tear
            # down a healthy shelf and un-protect yesterday's picks.
            self.logger.log_warning(
                "[HiddenGems] no plex/movies/owned_inventory — enable plex.movies.enabled and "
                "run the movie scan." if not inventory else
                "[HiddenGems] no owned movies (Radarr movie_files) — nothing to build.")
            self._emit_hit_rate_grid(
                self._measure_all(tracked, ledger, {}, profile_ages, now_ts, window, min_pct),
                window)
            return {"enabled": True, "users": len(tracked), "built": 0, "can_build": False}

        csm_ages = self._movie_csm_ages()
        users_mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        rk_by_tmdb = {t: str(v.get("rating_key")) for t, v in
                      ((self._coerce_int(k), v) for k, v in inventory.items())
                      if t is not None and isinstance(v, dict) and v.get("rating_key")}
        section_by_tmdb = {t: str(v.get("section")) for t, v in
                           ((self._coerce_int(k), v) for k, v in inventory.items())
                           if t is not None and isinstance(v, dict) and v.get("section") is not None}
        rk_to_tmdb = self._inventory_rk_to_tmdb(inventory)

        protected: set = set()
        events: list = []
        outcomes: list = []                     # (profile_handle, outcome, days) for the table
        self._begin_summary()
        built = 0
        for idx, u in enumerate(tracked, 1):
            safe = u.get("safe_user")
            if not safe:
                continue
            level = self._level(u, profile_ages)
            who = self._who(u, idx, level)
            uid = u.get("tautulli_user_id")
            history = self._history_for(uid)
            watched = self._watched_movies_for(uid)
            allowed = users_mgr.allowed_sections(u) if users_mgr else set()

            preds = {"seen": self._seen_pred(watched, rk_by_tmdb),
                     "age_ok": self._age_pred(level, csm_ages),
                     "reachable": self._reachable_pred(rk_by_tmdb, section_by_tmdb, allowed)}
            # HELD slots first: picks published on an earlier run whose window is still open
            # stay on the shelf (a pick is offered for its whole window, not for one run), then
            # free slots are topped up with the best new candidates.
            open_now = rec_ledger.open_picks(ledger, profile=safe, now_ts=now_ts,
                                             window_days=window)
            held = self._held_picks(open_now, owned, size, preds)
            excluded = self._plan_tmdbs(safe, rk_to_tmdb)
            excluded |= {r["tmdb_id"] for r in open_now}
            ranked, sel = gem_candidates(owned, excluded_ids=excluded, **preds)
            shelf, caps = apply_diversity_caps(ranked, size=size,
                                               max_per_franchise=per_franchise,
                                               max_per_person=per_person, held=held)
            fresh = shelf[len(held):]              # ONLY new picks become recommendation events
            items = [{"rating_key": rk_by_tmdb[p["tmdb_id"]], "ordinal": p["rank"],
                      "score": p["taste_score"], "title": p.get("title"),
                      "tmdb_id": p["tmdb_id"], "year": p.get("year"),
                      "reason": self._why(p)}
                     for p in shelf if p["tmdb_id"] in rk_by_tmdb]
            if self.global_cache:
                self.global_cache.set(f"{_PLAN_KEY}/{safe}",
                                      {"family": "hidden_gems", "items": items})
            protected.update(p["tmdb_id"] for p in shelf)
            events.extend(rec_ledger.build_events(fresh, profile=safe, window_days=window,
                                                  recommended_at=recommended_at))

            self.logger.log_info(
                f"[HiddenGems] {who} -> {len(shelf)} pick(s) ({len(held)} held, {len(fresh)} new) "
                f"from {sel['eligible']} eligible (owned {sel['considered']}, watched "
                f"{sel['watched']}, already-planned/in-window {sel['excluded']}, age-gated "
                f"{sel['age_gated']}, unscored {sel['no_breakdown']}; capped "
                f"{caps['capped_franchise']} saga / {caps['capped_person']} person).")
            self._add_gem_summary_row(who=who, picks=shelf, level=level)
            self._mirror_picks(u.get("title") or safe, shelf)
            outcomes.extend((who, o, d) for o, d in
                            self._join_outcomes(ledger, safe, history, rk_by_tmdb,
                                                now_ts, window, min_pct))
            built += 1

        self._publish_protected_movie_tmdbs(_PROTECTED_KEY, protected)
        appended = self._append_events(base_dir, events)
        self._emit_summary_grid(f"[dry-run] {_SHELF_LABEL} - per-profile summary")
        self._emit_hit_rate_grid(outcomes, window)
        self.logger.log_info(
            f"[HiddenGems] built {built} shelf plan(s), published {len(events)} pick(s) "
            f"({appended} new ledger row(s)) — read-only, no Plex writes.")
        return {"enabled": True, "users": len(tracked), "built": built,
                "picks": len(events), "events_appended": appended, "can_build": True}

    # ── per-profile identity + gating ──────────────────────────────────────────
    def _level(self, user, profile_ages) -> int:
        return tier_level(user.get("restriction_profile"),
                          profile_ages.get(user.get("title"))
                          or profile_ages.get(user.get("safe_user")))

    def _who(self, user, idx: int, level: int) -> str:
        """The de-identified handle every RUN-LOG line uses (the real profile name only ever
        reaches the local playlists.log mirror) — the same convention as the playlist builders."""
        tier = self._TIER_NAMES[level] if 0 <= level < len(self._TIER_NAMES) else str(level)
        return anon_label(user.get("title"), tier, idx)

    def _held_picks(self, open_now, owned, size, preds) -> list:
        """The still-open picks from earlier runs, re-materialised as shelf candidates in their
        ORIGINAL published order (oldest first) — the shelf's held slots.

        Re-validated through the SAME predicates a new candidate faces, so a held pick leaves
        the shelf the moment it is watched (that is a HIT — it has done its job), leaves the
        library, falls outside the profile's granted sections, or its certification stops
        fitting the tier. It stays in the ledger either way: the shelf and the measurement are
        independent, and a pick removed from the shelf is still measured to the end of its
        window."""
        if not open_now:
            return []
        wanted = {r["tmdb_id"] for r in open_now}
        rows = [r for r in owned if self._coerce_int(r.get("tmdb_id")) in wanted]
        cands, _stats = gem_candidates(rows, **preds)
        order = {r["tmdb_id"]: i for i, r in enumerate(open_now)}
        cands.sort(key=lambda c: order.get(c["tmdb_id"], len(order)))
        return cands[:size]

    # ── per-profile predicates (the pure pipeline's inputs) ─────────────────────
    def _seen_pred(self, watched, rk_by_tmdb):
        """"has THIS PROFILE finished this movie?" — per-user, NOT household: a film dad
        watched is still a gem for the kid. Matches ALL the identities the Tautulli watched-set
        carries (ratingKey + the ``(title, year)`` tuple that survives a Plex re-scan), exactly
        like the movie/anniversary builders — a bare-ratingKey check goes inert after a re-scan
        and would re-surface finished titles as "gems". An unmatched profile has an EMPTY set,
        so the shelf fails OPEN (shows owned) rather than hiding itself."""
        def seen(row) -> bool:
            tmdb = self._coerce_int(row.get("tmdb_id"))
            rk = rk_by_tmdb.get(tmdb)
            if rk is not None and str(rk) in watched:
                return True
            return (_norm_movie(row.get("title")), self._coerce_int(row.get("year"))) in watched
        return seen

    def _age_pred(self, level: int, csm_ages):
        """Parental-controls gate for one profile — the SAME ``cert_allowed`` + Common-Sense-age
        fallback the playlist builders use (fail-CLOSED for a restricted profile). An adult
        profile short-circuits to "everything allowed"."""
        if not is_restricted(level):
            return lambda row: True

        def ok(row) -> bool:
            return cert_allowed(row.get("certification"), level,
                                csm_age=csm_ages.get(self._coerce_int(row.get("tmdb_id"))))
        return ok

    def _reachable_pred(self, rk_by_tmdb, section_by_tmdb, allowed):
        """A title is only a gem for this profile if it resolves to a Plex ratingKey (nothing to
        open otherwise) AND — when a library grant resolved — sits in a section the profile was
        actually shared, so a viewer granted a SUBSET of the movie libraries gets a properly
        scoped shelf.

        An EMPTY grant set is treated as "unscoped", not "denied": ``allowed_sections`` also
        returns ``set()`` when there is simply no ``plex/sections`` index (or no users manager),
        and failing closed there would hand every profile an empty shelf on a perfectly healthy
        install. This is exactly what the Up Next / Fresh Arrivals builders this class extends
        already do — they apply NO section scoping at all — and every pick is still age-gated,
        so the shelf is never less safe than the playlists beside it. (The anniversary shelf
        fails closed instead because its picks can trigger acquisition.)"""
        allowed = {str(a) for a in (allowed or set())}

        def ok(row) -> bool:
            tmdb = self._coerce_int(row.get("tmdb_id"))
            if tmdb is None or tmdb not in rk_by_tmdb:
                return False
            if not allowed:
                return True
            sec = section_by_tmdb.get(tmdb)
            return sec is not None and sec in allowed
        return ok

    def _plan_tmdbs(self, safe, rk_to_tmdb) -> set:
        """Movie tmdbs already on this profile's OTHER cached plans (Up Next / combined / mood
        lists / Fresh Arrivals / Anniversary Picks). Excluded so the household is never shown
        the same title twice in one sweep — and so a play can be attributed to ONE surface."""
        out: set = set()
        for key in _OTHER_PLAN_KEYS:
            plan = self._cache_get(f"{key}/{safe}", None)
            if not isinstance(plan, dict):
                continue
            for it in (plan.get("items") or []):
                t = rk_to_tmdb.get(str((it or {}).get("rating_key")))
                if t is not None:
                    out.add(t)
        return out

    @staticmethod
    def _why(pick) -> str:
        """The one-line rationale mirrored next to a pick. Deliberately names the SHELF's
        premise (owned, never played, taste-matched) rather than re-deriving a genre string —
        the taste score already is the explanation, and its value is printed alongside."""
        fr = pick.get("franchise_label")
        base = "owned, never played, matches your taste"
        return f"{base} ({fr})" if fr else base

    # ── the measurement loop ────────────────────────────────────────────────────
    def _measure_all(self, tracked, ledger, rk_by_tmdb, profile_ages, now_ts, window,
                     min_pct) -> list:
        """``[(profile_handle, outcome, days)]`` across every tracked profile — the measurement
        pass on its own, so previously-published picks are still judged on a run that could not
        BUILD a shelf (a missing scan must not silently drop labels)."""
        out: list = []
        for idx, u in enumerate(tracked, 1):
            safe = u.get("safe_user")
            if not safe:
                continue
            who = self._who(u, idx, self._level(u, profile_ages))
            out.extend((who, o, d) for o, d in self._join_outcomes(
                ledger, safe, self._history_for(u.get("tautulli_user_id")), rk_by_tmdb,
                now_ts, window, min_pct))
        return out

    def _join_outcomes(self, ledger, safe, history, rk_by_tmdb, now_ts, window, min_pct) -> list:
        """``[(outcome, days_to_watch)]`` for every pick previously published to this profile.

        Joins the recommendation ledger against the profile's OWN play times: for each event we
        take the FIRST play at or after ``recommended_at`` (not the latest — a pick watched on
        day 5 and rewatched on day 40 is a hit) and classify it against the window the pick was
        PUBLISHED with. Rows for other profiles/surfaces are skipped."""
        if ledger is None or getattr(ledger, "empty", True):
            return []
        plays = movie_play_times(history, min_pct=min_pct)
        out: list = []
        for rec in ledger.to_dict("records"):
            if str(rec.get("profile")) != str(safe):
                continue
            rec_ts = rec_ledger.iso_to_epoch(rec.get("recommended_at"))
            tmdb = self._coerce_int(rec.get("tmdb_id"))
            if rec_ts is None or tmdb is None:
                continue
            try:
                row_window = int(rec.get("window_days") or window)
            except (TypeError, ValueError):
                row_window = window
            first = self._first_play(plays, tmdb, rec, rk_by_tmdb, rec_ts)
            out.append(classify_outcome(rec_ts, first, now_ts, window_days=row_window))
        return out

    def _first_play(self, plays, tmdb, rec, rk_by_tmdb, since_ts):
        """Earliest play time at/after ``since_ts`` across every identity this title can carry
        (its current ratingKey plus the ``(title, year)`` tuple recorded on the event, which
        still resolves after the file has been deleted or Plex re-scanned). ``None`` = no play."""
        identities = []
        rk = rk_by_tmdb.get(tmdb)
        if rk is not None:
            identities.append(str(rk))
        title, year = _norm_movie(rec.get("title")), self._coerce_int(rec.get("year"))
        if title and year is not None:
            identities.append((title, year))
        best = None
        for ident in identities:
            for ts in plays.get(ident) or ():
                if ts >= since_ts:                  # the per-identity list is ascending
                    if best is None or ts < best:
                        best = ts
                    break
        return best

    def _emit_hit_rate_grid(self, outcomes, window) -> None:
        """ONE measurement table per run: a row per profile plus an ALL row. Silent when the
        ledger holds nothing for this household yet (a first run has published picks but has
        nothing to measure — that is not an empty result worth a table)."""
        if not outcomes:
            return
        by_profile: dict = {}
        for who, outcome, days in outcomes:
            by_profile.setdefault(who, []).append((outcome, days))
        rows = [self._hit_rate_row(who, entries, window)
                for who, entries in sorted(by_profile.items())]
        if len(rows) > 1:
            rows.append(self._hit_rate_row(
                "ALL", [e for entries in by_profile.values() for e in entries], window))
        grid = getattr(self.logger, "log_grid", None)
        title = f"[dry-run] {_SHELF_LABEL} - {window}-day pick outcomes"
        if not callable(grid):
            for r in rows:
                self.logger.log_info(f"[HiddenGems] {title} | " + " | ".join(r))
            return
        try:
            import inspect
            has_caption = "caption" in inspect.signature(grid).parameters
        except (TypeError, ValueError):
            has_caption = False
        if has_caption:
            grid(list(self._HITRATE_COLS), rows, title=title, cap=44,
                 caption=self._HITRATE_CAPTION)
        else:
            grid(list(self._HITRATE_COLS), rows, title=title, cap=44)

    @staticmethod
    def _hit_rate_row(who, entries, window) -> list:
        s = hit_rate_summary(entries, window_days=window)
        return [str(who), str(s["published"]), str(s["pending"]), str(s["matured"]),
                str(s["hits"]),
                ("-" if s["hit_rate"] is None else f"{100 * s['hit_rate']:.1f}%"),
                ("-" if s["median_days_to_watch"] is None else f"{s['median_days_to_watch']:.1f}"),
                s["confidence"]]

    # ── summary row (the SAME cert evidence the playlist builders publish) ───────
    def _add_gem_summary_row(self, *, who, picks, level: int = ADULT) -> None:
        """Accumulate ONE (shelf x profile) row for the inherited summary grid. Cells 0-6 are
        byte-identical to ``PlexPlaylistBuilderManager._add_summary_row``'s — same
        :func:`cert_summary` / :func:`tier_ceiling` helpers, same VIOLATION formatting — so the
        gating evidence reads the same everywhere; cell 7 adds the median taste score."""
        rows = getattr(self, "_summary_rows", None)
        if rows is None:
            rows = self._summary_rows = []
        ev = cert_summary([p.get("certification") for p in picks], level)
        flag = "OK" if not ev["violations"] else f"VIOLATION x{ev['violations']}"
        med = median_taste(picks)
        rows.append([_SHELF_LABEL, str(who), str(len(picks)), ev["ceiling"], ev["strictest"],
                     str(ev["unknown"]), flag, "-" if med is None else f"{med:.1f}"])

    def _mirror_picks(self, title, picks) -> None:
        """Full per-item detail into support/logs/playlists.log (the local operator drill-down,
        which keeps the real profile name), NOT the shell. Mirrors
        ``PlexPlaylistBuilderManager._log_preview``'s file half."""
        to_file = getattr(self.logger, "log_to_file", None)
        if not (callable(to_file) and picks):
            return
        to_file("playlists", f"[dry-run] '{title}' {_SHELF_LABEL} - {len(picks)} pick(s)")
        for p in picks:
            year = f" ({p['year']})" if p.get("year") else ""
            to_file("playlists",
                    f"  {p['rank'] + 1} | {p.get('title') or p['tmdb_id']}{year} | "
                    f"{p['taste_score']:.1f} | {self._why(p)}")

    # ── ledger I/O (fault-isolated — a measurement failure never breaks a run) ───
    def _events_base_dir(self):
        try:
            return rec_ledger._resolve_base_dir(self.global_cache)
        except Exception:
            return None

    def _load_ledger(self, base_dir):
        if base_dir is None:
            return None
        try:
            return rec_ledger.load_events(base_dir)
        except Exception as e:
            self.logger.log_debug(f"[HiddenGems] recommendation ledger unreadable: {e}")
            return None

    def _append_events(self, base_dir, events) -> int:
        if base_dir is None or not events:
            return 0
        try:
            return rec_ledger.append_events(base_dir, events)
        except Exception as e:
            self.logger.log_warning(f"[HiddenGems] recommendation ledger append failed: {e}")
            return 0

    @staticmethod
    def _now():
        """The run's single UTC instant — both the ``recommended_at`` stamp and the outcome
        join's "now" derive from it. One seam, so a test freezes the clock once."""
        return datetime.now(tz=timezone.utc)

    def _history_for(self, user_id) -> list:
        """This profile's raw Tautulli history (the same 24h-cached fetch the watched-set uses,
        so this is a cache hit). ``[]`` on any miss -> every outcome reads as "no play yet"."""
        if user_id is None or not self.registry:
            return []
        hm = self.registry.get("manager", "TautulliWatchHistoryManager")
        if hm is None:
            taut = self.registry.get("manager", "TautulliManager")
            hm = getattr(taut, "watch_history", None) if taut else None
        if not hm or not hasattr(hm, "get_all_history_cached"):
            return []
        try:
            return list(hm.get_all_history_cached(user_id) or [])
        except Exception:
            return []

    # ── config knobs (plex.playlists.hidden_gems.*) ─────────────────────────────
    def _gems_cfg(self) -> dict:
        return (self._pl_cfg().get("hidden_gems", {}) or {})

    def _opted_in(self, user, cfg) -> bool:
        """In scope when listed in ``opt_in_users`` (by title or safe_user); an EMPTY list means
        the feature is on household-wide -> every tracked user (mirrors the anniversary shelf)."""
        want = cfg.get("opt_in_users") or []
        if not want:
            return True
        want = {str(w).strip().lower() for w in want if str(w).strip()}
        return (str(user.get("title", "")).strip().lower() in want
                or str(user.get("safe_user", "")).strip().lower() in want)

    def _shape(self, cfg):
        """``(size, max_per_franchise, max_per_person)`` — how big the shelf is and how much of
        it any one saga / person may own. Defaults 25 / 2 / 3; ``<= 0`` on either cap disables
        that cap (size is floored at 1)."""
        return (self._int_cfg(cfg, "size", 25, minimum=1),
                self._int_cfg(cfg, "max_per_franchise", 2),
                self._int_cfg(cfg, "max_per_person", 3))

    def _window_days(self, cfg) -> int:
        """The measurement window AND the re-surface cooldown, in days (default 30, Robert's
        spec). One knob for both by design: a pick owns its slot for exactly as long as it is
        being measured, so a play can never be ambiguous between two recommendations."""
        return self._int_cfg(cfg, "window_days", DEFAULT_WINDOW_DAYS, minimum=1)

    def _play_min_pct(self, cfg) -> float:
        """Completion percentage that counts as "played" for the outcome join. Defaults to 85 —
        the SAME floor ``watched_movie_keys`` uses for candidate eligibility, and they must
        agree: a looser outcome rule would score a pick a HIT while the eligibility rule still
        considers it never-watched, so it would keep re-surfacing after succeeding. Lower it
        (e.g. 5) to count "pressed play" instead of "finished it"."""
        try:
            v = float(cfg.get("play_min_pct", 85.0))
        except (TypeError, ValueError):
            return 85.0
        return min(100.0, max(0.0, v))

    @staticmethod
    def _int_cfg(cfg, key, default, *, minimum=None) -> int:
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, v) if minimum is not None else v

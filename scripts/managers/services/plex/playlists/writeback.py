"""
plex/playlists/writeback.py — per-user playlist WRITE path (DESIGN P5, default-off).
================================================================================
The one place a per-user "Up Next" plan becomes a REAL Plex playlist on that member's
account. Everything upstream (builder/movie_builder/combined_builder) is BUILD+CACHE+
preview only; this manager reads those cached plans and, ONLY when armed, performs the
create/add/remove/move/delete calls — on each member's OWN account, never the owner's.

DEFAULT-OFF / FAIL-CLOSED is the whole contract. With ``plex.playlists.writeback.enabled``
false (the default) OR ``dry_run`` true, :meth:`writeback_armed` returns False and the
manager runs the full preview/diff/re-resolution but performs ZERO Plex writes — behaviour
is byte-identical to today (asserted by a call-log test). The build/preview gate is the
existing ``_cap_enabled``; ONLY the actual write verbs consult ``writeback_armed``.

Per managed user the LOCAL PMS rejects the raw switch authToken (401) — we write with the
per-server ``accessToken`` derived once per run (PlexUsersManager.server_write_token). We
NEVER fall back to the owner token for a non-admin (that would create the playlist on the
owner's account), and we NEVER delete a playlist that is not OUR managed anchor.

Safety rails, all P0 (see the PR brief):
  1. fail-closed arm gate (config AND not dry_run);
  2. per-server write token, never owner-for-managed, assert-checked;
  3. a persisted managed-anchor map (safe_user → our playlist ratingKey) — find-or-create
     resolves by cached ratingKey FIRST, title-match adoption only as a 404 fallback and
     only when the playlist is owned by that user;
  4. ratingKey RE-RESOLUTION vs the FRESH owned inventory before writing (drift counted/
     logged; a large-drift user is skipped with a re-run note);
  5. an IN-PLACE add/remove/move diff (stable ratingKeys), delete+recreate only as a last
     resort and create-new-then-delete-old; steady-state (current == desired) is a no-op;
  6. orphan cleanup against the LIVE HOME ROSTER (a PIN-mint failure leaves the playlist
     alone — present in the roster, absent from tracked_users);
  7. an armed/disarmed summary banner every run + an audit-log line per write.
"""
from __future__ import annotations

import time
from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.playlists.cert_gate import tier_level
from scripts.managers.services.plex._common import anon_label, metadata_items, parse_item
from scripts.managers.services.plex.playlists import recorder

# cert_gate level → label, mirroring PlexPlaylistBuilderManager._TIER_NAMES so the de-identified
# handle this manager logs ('T - adult 1') matches the one the builders log for the same profile.
_TIER_NAMES = ("little_kid", "older_kid", "teen", "adult")

# The three per-user plan families the builders cache (key + /{safe_user}). Each maps to one
# managed playlist; the combined plan is the household default when movies are enabled.
_TV_PLAN_KEY = "plex/playlists/tv_plan"
_MOVIE_PLAN_KEY = "plex/playlists/movie_plan"
_COMBINED_PLAN_KEY = "plex/playlists/combined_plan"

# Additional opt-in per-user playlists the builders cache. Each becomes its OWN managed Plex
# playlist (title suffix below), written only when its build flag produced a cached plan.
_GLIDE_PLAN_KEY = "plex/playlists/glide_plan"          # The Long Glide (in-progress sagas)
_TOUCHGO_PLAN_KEY = "plex/playlists/touchgo_plan"      # Touch & Go (low-commitment standalones)
_FRESH_PLAN_KEY = "plex/playlists/fresh_movie_plan"    # Fresh Arrivals (genuinely-new acquisitions)
_TWIH_MOVIE_PLAN_KEY = "plex/playlists/twih_movie_plan"  # Anniversary Picks (movies, this week in history)
_TWIH_SHOW_PLAN_KEY = "plex/playlists/twih_show_plan"    # On This Week (shows, this week in history)
_GEMS_PLAN_KEY = "plex/playlists/gems_plan"              # Hidden Gems (owned + never played + taste-matched)
_TONIGHT_PLAN_KEY = "plex/playlists/tonight_plan"        # Tonight (per-profile weekday habit, built for TOMORROW)
_AFFINITY_PLAN_KEY = "plex/playlists/affinity_plan"      # Because You Watched (seeded from discovery COMPLETIONS)

#: family suffix -> ledger surface. Recording happens HERE, not in the builders,
#: because this is the only component that knows a placement actually REACHED
#: Plex. A builder caches a plan; a plan is not a thing anybody saw.
#:
#: Each of these would otherwise manufacture a MISS - a pick entered into its
#: 30-day window, never displayed, maturing into evidence that the shelf does not
#: work:
#:   * dry_run          the plan was previewed, never published
#:   * create failed    "create failed for '...' - skipped"
#:   * recreate failed  old list kept, new one never minted
#:   * no write token   the profile was skipped entirely
#:   * empty plan       the playlist was DELETED, not shown
#:
#: Deferral is NOT one of them: a deferred playlist is live and holding the items
#: it was written with, and those were recorded on the run that wrote them.
#:
#: Up Next / The Long Glide / Touch & Go are absent on purpose - the next episode
#: of a show already being watched is not a discovery. Hidden Gems is absent
#: because it records through its own publish path, and a second writer would
#: double every row and corrupt the hit rate it has been accumulating.
_RECORD_SURFACE = {
    "Anniversary Picks": "anniversary",
    "On This Week": "anniversary",
    "Tonight": "tonight",
    "Because You Watched": "because_you_watched",
}

# The default ALWAYS-written family — combined > tv > movie precedence, titled "Up Next".
# Its suffix is the one that keeps the LEGACY anchor key (== safe_user), so it's a shared
# constant: _anchor_id and every suffix default reference it, never a bare literal.
_UP_NEXT_SUFFIX = "Up Next"
_UP_NEXT = {"suffix": _UP_NEXT_SUFFIX, "keys": (_COMBINED_PLAN_KEY, _TV_PLAN_KEY, _MOVIE_PLAN_KEY)}

_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"

# safe_user → our playlist ratingKey (the managed ANCHOR). Persisted so find-or-create
# resolves by ratingKey first and we never delete a playlist we don't own.
_ANCHOR_KEY = "plex/playlists/managed_anchor"          # + /{safe_user}

# Per-family poster art (uploaded to each managed playlist when ``plex.playlists.branding.enabled``).
# suffix → asset slug under support/assets/playlists/; MUST stay in lock-step with the generator
# scripts/support/tools/generate_playlist_logos.py. The upload is version-gated (see _apply_branding)
# so it happens once per playlist and re-fires only when the PNG itself changes.
# Managed playlist titles carry NO username — each list lives on that member's OWN account, so the
# owner is unambiguous (and the title stays non-identifying). The DISPLAY title is the bare suffix
# ("Up Next"); the front-pinning '!' lives ONLY in the Plex titleSort key, so the visible name stays
# clean while the list still sorts to the FRONT. '!' (ASCII 0x21) sorts ahead of every digit/letter
# (verified live — '_' would sort AFTER capitals, so it would NOT pin to the front).
_SORT_PREFIX = "!"
_TITLE_KEY = "plex/playlists/titled"                   # + /{anchor_id} → last-set titleSort (edit gate)

_BRAND_KEY = "plex/playlists/branded"                  # + /{anchor_id} → last-uploaded asset version
# Bump when the upload CONTRACT changes (endpoint, format) so persisted version tokens from an older,
# broken scheme no longer match and every poster re-uploads once. v1 used the wrong /playlists/{rk}/
# posters endpoint (404, silently cached as done); v2 is /library/metadata/{rk}/posters + 2xx-verified.
_BRAND_SCHEME = "2"
_BRAND_ASSETS = {
    _UP_NEXT_SUFFIX: "up_next",
    "The Long Glide": "the_long_glide",
    "Touch & Go": "touch_and_go",
    "Fresh Arrivals": "fresh_arrivals",
    "Anniversary Picks": "anniversary_picks",
    "On This Week": "on_this_week",
    # GLD-PLY-18: "Hidden Gems" is in _all_families() and enabled in config, but had NO entry
    # here -- so _branding_asset returned None and it was the one live playlist that never got a
    # poster (visible in playlists.log: a 'would be titled' line with no matching 'poster would be
    # set'). The asset now exists as a template, so the mapping is filled in.
    "Hidden Gems": "hidden_gems",
    # Tonight is per-PROFILE and built for a specific upcoming day, so unlike
    # every other family here its poster carries a live date band and must be
    # regenerated whenever the date rolls.
    "Tonight": "tonight",
    # Seeded from what the household FINISHED off a discovery shelf, so its copy
    # ("because you watch {{GENRE}}") is answerable from the plan itself.
    "Because You Watched": "because_you_watched",
}
_ASSETS_DIR = (Path(__file__).resolve().parents[4] / "support" / "assets"
               / "posters" / "playlists")
# PLAYLISTS, not collections. This module uploads to a PLAYLIST, which Plex renders in a
# 1:1 tile and centre-crops anything taller -- so it must read the SQUARE 1000x1000 set.
# The 2:3 portrait set under posters/collections/ is consumed by
# plex/collections/posters.CollectionPosterManager, because a collection is a library item
# in the 2:3 grid. Pointing this at the collections folder would upload portrait art to
# playlist tiles and Plex would crop away the title band.

# Drift fraction above which a user is skipped (the plan is too stale to write safely; a
# re-run after the next inventory scan resolves it).
_DRIFT_SKIP_RATIO = 0.5

# When the in-place diff would touch MORE than the whole desired list, fall back to a clean
# recreate (create-new-then-delete-old) rather than dribbling N removes + N adds.
# GLD-PLY-13: this threshold is only meaningful because ``move`` is now the MINIMUM number of
# displaced items (see _min_moves). It previously counted the WHOLE survivor list on any re-rank,
# which made n_changes == len(desired) + removes and tripped this fallback on nearly every run.
_RECREATE_RATIO = 1.0

# -- rewrite cadence (GLD-PLY-15) ----------------------------------------------------------------
# Epoch seconds of the last ARMED item write, per playlist.
_LASTWRITE_KEY = "plex/playlists/last_write"           # + /{anchor_id}

# A re-ranked plan yields a large in-place diff EVERY run (observed live: +22/-0/~78 on a 100-item
# "Up Next"), so writing every run churns each member's playlist for pure ordering noise. The ITEM
# write is therefore rate-limited to once per this many hours. 20, not 24, so a DAILY-scheduled run
# is never blocked by a few minutes of clock drift. <= 0 disables the gate entirely.
# Config: plex.playlists.writeback.min_interval_hours.
_MIN_REWRITE_INTERVAL_HOURS = 20.0

# The escape hatch: the fraction of the list added (vs desired) or removed (vs current) that forces
# a write through BEFORE the interval elapses -- "a lot got watched off it" / "re-ranking pulled in
# a lot of new material". 0.40 sits deliberately ABOVE this install's observed steady-state add
# churn (~0.22) so routine re-ranking defers and only a genuine overhaul overrides.
# Config: plex.playlists.writeback.churn_override_ratio.
_CHURN_OVERRIDE_RATIO = 0.40

# A pure RE-ORDER can never trip the churn override above (it is measured on adds/removes only,
# by design). But a re-order that changes what sits at the TOP of the list is exactly what the
# household actually sees -- e.g. session warmth promoting a series watched a few hours ago from
# #43 to #10 (GLD-PLY-17). So the head of the list gets its own override: if the SET of items in
# the first _HEAD_SIZE slots differs from what is live by this fraction, write through now.
# Reordering that stays below the fold is still absorbed by the interval.
# Config: plex.playlists.writeback.head_size / head_churn_ratio.
_HEAD_SIZE = 10
_HEAD_CHURN_RATIO = 0.30


def _min_moves(cur_order, desired_order) -> list:
    """The MINIMUM set of items that must be repositioned to turn ``cur_order`` into
    ``desired_order`` (both permutations of the same set): every item OUTSIDE the longest
    subsequence that is already in the right relative order. Items in that subsequence need no
    move -- the rest get carried around them.

    GLD-PLY-13. The previous implementation returned the ENTIRE survivor list whenever the order
    differed at all, so ``n_changes`` came to len(desired) + removes on any re-rank and tripped
    _RECREATE_RATIO on nearly every run -- turning "delete+recreate as a last resort" into the
    DEFAULT path (measured on this install: 4 of 6 "Up Next" lists recreated every run, each
    minting a fresh ratingKey).

    O(n log n) patience-sorting LIS. Verified exhaustively against brute force for every
    permutation up to n=7, plus randomized replay up to n=60.
    """
    pos = {rk: i for i, rk in enumerate(desired_order)}
    seq = [pos[rk] for rk in cur_order if rk in pos]
    tails: list = []                  # tails[k] = index into seq of the smallest tail of a
    prev: list = [-1] * len(seq)      # length-(k+1) increasing run; prev[] threads the chain
    for i, v in enumerate(seq):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if seq[tails[mid]] < v:
                lo = mid + 1
            else:
                hi = mid
        prev[i] = tails[lo - 1] if lo > 0 else -1
        if lo == len(tails):
            tails.append(i)
        else:
            tails[lo] = i
    keep, k = set(), (tails[-1] if tails else -1)
    while k >= 0:
        keep.add(seq[k])
        k = prev[k]
    return [rk for rk in desired_order if pos[rk] not in keep]


class PlaylistWritebackManager(BaseManager):
    """Reads the cached per-user plans and (only when armed) writes them to Plex."""

    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── arm gate (P0 #1) ──────────────────────────────────────────────────────
    def writeback_armed(self) -> bool:
        """The SINGLE fail-closed gate the write verbs consult: config
        ``plex.playlists.writeback.enabled`` AND NOT ``self.dry_run``. Missing keys read
        False. This is deliberately NOT ``_cap_enabled`` — build/preview run on that gate;
        only the actual create/add/move/delete calls run on this one."""
        wb = (self._pl_cfg().get("writeback", {}) or {})
        return bool(wb.get("enabled", False)) and not bool(self.dry_run)

    def _pl_cfg(self) -> dict:
        return ((self.config.get("plex", {}) if self.config else {}) or {}).get("playlists", {}) or {}

    def _all_families(self) -> list:
        """Every managed playlist family + whether it's currently ENABLED. "Up Next" is always on;
        the mood lists + Fresh Arrivals follow their build flag. We iterate ALL of them (not just
        the enabled ones) so that turning a family OFF tears its leftover playlist down rather than
        orphaning it on the member account (a disabled family is driven through _handle_empty)."""
        mood = bool((self._pl_cfg().get("mood_lists", {}) or {}).get("enabled", False))
        fresh = bool((self._pl_cfg().get("fresh_arrivals", {}) or {}).get("enabled", False))
        disc = bool((self._pl_cfg().get("this_week_in_history", {}) or {}).get("enabled", False))
        gems = bool((self._pl_cfg().get("hidden_gems", {}) or {}).get("enabled", False))
        tonight = bool((self._pl_cfg().get("tonight", {}) or {}).get("enabled", False))
        affinity = bool((self._pl_cfg().get("because_you_watched", {}) or {}).get("enabled", False))
        return [
            (_UP_NEXT, True),
            ({"suffix": "The Long Glide", "keys": (_GLIDE_PLAN_KEY,)}, mood),
            ({"suffix": "Touch & Go", "keys": (_TOUCHGO_PLAN_KEY,)}, mood),
            ({"suffix": "Fresh Arrivals", "keys": (_FRESH_PLAN_KEY,)}, fresh),
            ({"suffix": "Anniversary Picks", "keys": (_TWIH_MOVIE_PLAN_KEY,)}, disc),
            ({"suffix": "On This Week", "keys": (_TWIH_SHOW_PLAN_KEY,)}, disc),
            ({"suffix": "Hidden Gems", "keys": (_GEMS_PLAN_KEY,)}, gems),
            ({"suffix": "Tonight", "keys": (_TONIGHT_PLAN_KEY,)}, tonight),
            ({"suffix": "Because You Watched", "keys": (_AFFINITY_PLAN_KEY,)}, affinity),
        ]

    # ── run (I/O gather → tested core) ────────────────────────────────────────
    def run(self) -> dict:
        users_mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        tracked = list(getattr(users_mgr, "tracked_users", []) or []) if users_mgr else []
        roster = self._cache_get("plex/users", []) or []         # live HOME roster (PII-minimized)
        tv_inv = self._cache_get(_TV_INVENTORY_KEY, {}) or {}
        movie_inv = self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {}
        return self._writeback(tracked, roster, users_mgr, tv_inv, movie_inv)

    def _writeback(self, tracked, roster, users_mgr, tv_inv, movie_inv) -> dict:
        """The orchestration core (pure given its inputs + the fake-able users_mgr/plex_api):
        per tracked user re-resolve → diff → (armed) write; then orphan-cleanup against the
        live roster; then the summary banner. Returns the per-run counters."""
        armed = self.writeback_armed()
        excluded = self._excluded_users()
        valid_rks = self._valid_rating_keys(tv_inv, movie_inv)
        stats = {"armed": armed, "created": 0, "updated": 0, "deleted": 0,
                 "skipped": 0, "users": len(tracked), "orphans": 0, "branded": 0, "retitled": 0,
                 "recreated": 0, "deferred": 0, "sort_repaired": 0}
        # Per-run memo for the live titleSort listing; see _live_sort_title. Reset
        # here rather than in __init__ so a manager reused across runs cannot carry
        # one run's server state into the next.
        self._sort_memo = {}
        # safe_user → de-identified handle, so run-log lines below never print the real profile
        # name (the dedicated playlists.log preview keeps it). Built once from the tracked order.
        self._anon_by_safe = {u.get("safe_user"): self._anon(u, i)
                              for i, u in enumerate(tracked, 1) if u.get("safe_user")}

        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                stats["skipped"] += 1
                continue
            if self._is_excluded(u, excluded):
                self.logger.log_info(f"[Writeback] '{self._who(u)}' excluded (exclude_users) — skipped.")
                stats["skipped"] += 1
                continue
            # One managed playlist per family. An ENABLED family is written (same safety rails);
            # a DISABLED family is torn down (delete its leftover playlist) so toggling a feature
            # off doesn't strand a managed playlist on the member account.
            for fam, enabled in self._all_families():
                if enabled:
                    self._process_family(u, safe, fam, valid_rks, users_mgr, armed, stats)
                else:
                    self._handle_empty(u, users_mgr, armed, stats, fam)

        self._cleanup_orphans(roster, tracked, excluded, users_mgr, armed, stats)
        self._banner(stats)
        return stats

    def _process_family(self, u, safe, fam, valid_rks, users_mgr, armed, stats):
        """Write ONE family's playlist for ONE user (the old per-user body, parameterized by
        ``fam`` = ``{"suffix", "keys"}``)."""
        desired = self._desired_items(safe, valid_rks, fam["keys"])
        if desired is None:
            # Too-stale-to-write (large mid-rescan drift): LEAVE the existing playlist untouched
            # and let the next clean run rewrite it — do NOT delete it.
            return
        # An empty plan (e.g. a RESTRICTED user age-gated to nothing, or a family with no cached
        # plan) => never leave an empty playlist: delete any existing managed anchor for it.
        if not desired:
            self._handle_empty(u, users_mgr, armed, stats, fam)
            return

        token = self._write_token(u, users_mgr)
        if token is None:
            self.logger.log_warning(
                f"[Writeback] no per-server write token for '{self._who(u)}' — skipped (counted).")
            stats["skipped"] += 1
            return
        # P0 #2: never write a non-admin's playlist with the owner token.
        if not u.get("is_admin"):
            owner = self.plex_api.token if self.plex_api else None
            if owner is not None and token == owner:
                self.logger.log_error(
                    f"[Writeback] refusing to write '{self._who(u)}' with the OWNER token "
                    f"(non-admin) — skipped.")
                stats["skipped"] += 1
                return

        self._writeback_user(u, safe, desired, token, armed, stats, fam)

    # ── per-user write (find-or-create → re-resolve → diff → apply) ───────────
    def _writeback_user(self, user, safe, desired, token, armed, stats, fam=_UP_NEXT):
        suffix = fam["suffix"]
        title = self._playlist_title(user, suffix)
        anchor = self._find_or_create_anchor(safe, title, token, armed, desired, stats, suffix)
        if anchor is None:
            # Not armed (no real create happened) — we've already counted a would-create and
            # logged the preview; nothing more to do this run.
            return

        # Branding is applied ONCE per path, on the ratingKey that actually survives this run
        # (version-gated, so it uploads once and no-ops thereafter; default-off, so byte-identical
        # when branding is off). We deliberately do NOT brand up front: the recreate path mints a
        # fresh ratingKey and brands THAT, so an early brand on the doomed old list was a wasted
        # second upload.
        rk = anchor["rating_key"]
        # Clean display title + '!' titleSort (front-pin), set once, gated. Runs for fresh creates
        # (the POST can't set titleSort) AND migrates older '{name} {suffix}' / '!{suffix}' titles.
        self._ensure_title(safe, suffix, rk, token, armed, stats)
        if anchor.get("created"):
            self._apply_branding(safe, suffix, rk, token, armed, stats)   # fresh list → brand it
            self._stamp_write(safe, suffix, armed)   # starts the GLD-PLY-15 rewrite interval
            self._record_surfaced(safe, suffix, desired, armed, stats)
            return                       # freshly created with the desired items, in order

        current = self._current_items(rk, token)
        desired_rks = [it["rating_key"] for it in desired]
        if [c["rating_key"] for c in current] == desired_rks:
            self.logger.log_debug(f"[Writeback] '{title}' already in steady state — no item write.")
            # STILL RECORDED: the playlist is live and holding exactly these items,
            # which is what "surfaced" means. Dedup on (date, surface, profile,
            # entity_id) makes the repeat free, and this is the branch a
            # held-open shelf sits in for most of its window.
            self._record_surfaced(safe, suffix, desired, armed, stats)
            self._apply_branding(safe, suffix, rk, token, armed, stats)   # ensure the poster (gated)
            return                       # P0 #5: current == desired → no item write

        plan = self._diff(current, desired_rks)
        # GLD-PLY-15: rate-limit the ITEM write to once per interval unless the churn is a genuine
        # overhaul. Title/poster stay OUTSIDE the gate -- they are one-time, version-gated
        # migrations and holding them back for a day buys nothing.
        if self._cadence_defer(safe, suffix, plan, current, desired_rks, stats):
            self._apply_branding(safe, suffix, rk, token, armed, stats)
            return
        n_changes = len(plan["add"]) + len(plan["remove"]) + len(plan["move"])
        if n_changes > max(len(desired_rks), 1) * _RECREATE_RATIO:
            # Diff exceeds the whole list → cheaper + safer to recreate (new-then-old). _recreate
            # force-brands the NEW ratingKey, so we don't brand the about-to-be-deleted old one here.
            self._recreate(user, safe, title, rk, desired_rks, token, armed, stats, suffix,
                           desired=desired)
            return

        if not armed:
            self._detail(
                f"[Writeback] [disarmed] '{title}' would update "
                f"(+{len(plan['add'])}/-{len(plan['remove'])}/~{len(plan['move'])}).")
            self._apply_branding(safe, suffix, rk, token, armed, stats)   # disarmed → preview only
            stats["updated"] += 1
            return

        self._apply_diff(rk, current, desired_rks, plan, token)
        self._stamp_write(safe, suffix, armed)
        stats["updated"] += 1
        self._audit(user, "replace", rk, len(desired_rks))
        self._apply_branding(safe, suffix, rk, token, armed, stats)       # in-place update → brand
        self._record_surfaced(safe, suffix, desired, armed, stats)

    def _record_surfaced(self, safe, suffix, desired, armed, stats):
        """Record this family's items into the recommendation ledger — ARMED ONLY,
        and only from a path where the playlist demonstrably reached Plex.

        See ``_RECORD_SURFACE`` for why this lives here rather than in the
        builders, and for the list of failure modes that would otherwise each
        manufacture a miss.

        Identity comes from the PLAN ITEMS, which carry ``tmdb_id`` /
        ``tvdb_join_key`` captured at selection time (`GLD-PLY-24`) plus the
        ``rating_key`` as surfaced. Nothing is re-derived from a ratingKey here:
        a re-scan retires them, and a wrongly-attributed ledger row is
        undetectable downstream.
        """
        surface = _RECORD_SURFACE.get(suffix)
        if not surface or not armed or not desired:
            return
        base_dir = recorder.base_dir_of(self.global_cache)
        if base_dir is None:
            return
        res = recorder.record_surface(
            base_dir=base_dir, picks=list(desired), profile=safe,
            surface=surface, logger=self.logger)
        stats["recorded"] = stats.get("recorded", 0) + res["recorded"]
        stats["unrecordable"] = stats.get("unrecordable", 0) + res["skipped"]

    def _find_or_create_anchor(self, safe, title, token, armed, desired, stats, suffix="Up Next") -> dict | None:
        """Resolve OUR managed playlist for this user (P0 #3). Cached ratingKey FIRST; on a
        404 fall back to a title-match and adopt ONLY when the playlist is owned by this user.
        Create one when neither resolves. Returns ``{"rating_key", "created"}`` or None when
        disarmed (the create is previewed + counted but not performed)."""
        cached_rk = self._anchor_get(safe, suffix)
        if cached_rk is not None and self._playlist_exists(cached_rk, token):
            return {"rating_key": cached_rk, "created": False}

        adopted = self._adopt_by_title(title, token)
        if adopted is not None:
            self._anchor_set(safe, adopted, suffix)
            return {"rating_key": adopted, "created": False}

        # Nothing to adopt → create.
        desired_rks = [it["rating_key"] for it in desired]
        if not armed:
            self._detail(
                f"[Writeback] [disarmed] '{title}' would be CREATED with {len(desired_rks)} item(s).")
            stats["created"] += 1
            return None
        rk = self._create_playlist(title, desired_rks, token)
        if rk is None:
            self.logger.log_warning(f"[Writeback] create failed for '{title}' — skipped.")
            stats["skipped"] += 1
            return None
        self._anchor_set(safe, rk, suffix)
        stats["created"] += 1
        self._audit({"title": title, "safe_user": safe}, "create", rk, len(desired_rks))
        return {"rating_key": rk, "created": True}

    def _recreate(self, user, safe, title, old_rk, desired_rks, token, armed, stats,
                  suffix="Up Next", desired=None):
        """Delete+create fallback, CREATE-NEW-THEN-DELETE-OLD so a failed create never loses
        the user's playlist (P0 #5). Only the new anchor is ever deleted on the next pass."""
        if not armed:
            self._detail(
                f"[Writeback] [disarmed] '{title}' would be RECREATED ({len(desired_rks)} item(s)).")
            stats["updated"] += 1
            stats["recreated"] = stats.get("recreated", 0) + 1
            return
        new_rk = self._create_playlist(title, desired_rks, token)
        if new_rk is None:
            self.logger.log_warning(
                f"[Writeback] recreate failed for '{self._who(user)}' ({suffix}) — keeping old playlist.")
            stats["skipped"] += 1
            return
        self._anchor_set(safe, new_rk, suffix)    # repoint the anchor BEFORE deleting the old one
        self.plex_api.delete_playlist(old_rk, token=token)
        # GLD-PLY-14: the new ratingKey has NEITHER a titleSort NOR a poster, and BOTH gates are
        # keyed on anchor_id -- which does NOT change across a recreate. So both must be FORCED, or
        # the fresh list silently keeps Plex's default sort key (losing the '!' front-pin) and no
        # art, permanently, because the stale "done" markers still match. Branding already did this;
        # the title gate was the missed half.
        self._ensure_title(safe, suffix, new_rk, token, armed, stats, force=True)
        self._apply_branding(safe, suffix, new_rk, token, armed, stats, force=True)
        self._stamp_write(safe, suffix, armed)
        stats["updated"] += 1
        stats["recreated"] = stats.get("recreated", 0) + 1
        self._audit(user, "replace", new_rk, len(desired_rks))
        # Recorded only HERE, after the new list exists. The early-return above
        # (create failed → old playlist kept) deliberately records nothing: those
        # items were never re-published, and a recorded placement nobody saw
        # matures into a manufactured miss.
        self._record_surfaced(safe, suffix, desired, armed, stats)

    # ── empty-plan + orphan handling ──────────────────────────────────────────
    def _handle_empty(self, user, users_mgr, armed, stats, fam=_UP_NEXT):
        """Empty plan (e.g. a restricted user whose owned set age-gates to nothing, or a family
        with no cached plan): never write an empty playlist — delete any existing managed anchor
        for THIS family + log (P0/brief)."""
        suffix = fam["suffix"]
        safe = user.get("safe_user")
        cached_rk = self._anchor_get(safe, suffix)
        if cached_rk is None:
            self.logger.log_debug(f"[Writeback] '{self._who(user)}' empty '{suffix}' plan — nothing to write.")
            return
        token = self._write_token(user, users_mgr)
        if not armed:
            self._detail(
                f"[Writeback] [disarmed] '{user.get('title')}' empty '{suffix}' plan would DELETE "
                f"its managed playlist.")
            stats["deleted"] += 1
            return
        if token is None:
            self.logger.log_warning(
                f"[Writeback] '{self._who(user)}' empty plan but no write token — skipped (counted).")
            stats["skipped"] += 1
            return
        self.plex_api.delete_playlist(cached_rk, token=token)
        self._anchor_clear(safe, suffix)
        stats["deleted"] += 1
        self.logger.log_info(f"[Writeback] '{self._who(user)}' empty '{suffix}' plan — deleted managed playlist.")
        self._audit(user, "delete", cached_rk, 0)

    def _cleanup_orphans(self, roster, tracked, excluded, users_mgr, armed, stats):
        """Delete a managed playlist ONLY when its owning uuid is genuinely absent from the
        LIVE HOME ROSTER (P0 #6). A PIN-mint failure (in roster, absent from tracked_users)
        must LEAVE the playlist alone — we key orphan-detection on the roster, never on
        tracked_users. The anchor map is keyed by safe_user; we map roster uuids → safe_user
        via the persisted roster + tracked set, and only sweep anchors whose user has truly
        vanished from the household.

        ⚠️ THE DELETE ITSELF PROBABLY CANNOT SUCCEED, AND IS COUNTED AS IF IT DID.
        The departed user's per-server token can no longer be minted, so this falls back to
        ``self.plex_api.token`` — the OWNER token. Everywhere else this module treats that as
        forbidden precisely because it does not work on a managed member's account
        (``_process_family`` refuses it outright: "refusing to write … with the OWNER token").
        A managed user's playlist lives on THEIR account, so the owner token should 404/403.

        The return value of ``delete_playlist`` is then DISCARDED and ``stats["deleted"]`` is
        incremented regardless, and an audit line is written claiming a deletion happened. So
        the banner and the audit trail would both report orphan deletions that did not occur.

        Contrast ``_apply_branding`` in this same file, which gets this exactly right: it only
        caches success on a verified 2xx, with the reasoning spelled out — *"a silent 404/401
        must NOT mark the list branded (else it never retries)"*. The same discipline belongs
        here.

        NOT FIXED HERE because the right answer is a product decision, not a code tweak:
          * a departed member's playlist may simply be unreachable — Plex removes the Home
            user and their library with them, in which case there is nothing to delete and
            the sweep should stop counting it; or
          * ``delete_playlist`` should report success/failure and the counter + audit line
            should follow it, leaving the anchor in place on failure so a later run retries.
        Either way the counter must stop asserting something it has not verified.
        """
        anchors = self._all_anchors()
        if not anchors:
            return
        live_safe = self._roster_safe_users(roster, tracked, users_mgr)
        for aid, rk in list(anchors.items()):
            safe = aid.split("::", 1)[0]      # anchor_id is 'safe' (Up Next) or 'safe::suffix'
            if safe in live_safe:
                continue                 # still in the household (tracked OR pin-skipped) → leave alone
            stats["orphans"] += 1
            if not armed:
                self.logger.log_info(
                    f"[Writeback] [disarmed] orphan playlist '{aid}' would be DELETED.")
                continue
            token = self.plex_api.token if self.plex_api else None
            self.plex_api.delete_playlist(rk, token=token)
            self._anchor_clear_by_id(aid)
            stats["deleted"] += 1
            self.logger.log_info(f"[Writeback] deleted orphan playlist '{aid}' (departed user '{safe}').")
            self._audit({"title": safe, "safe_user": safe}, "delete", rk, 0)

    # ── re-resolution (P0 #4) ─────────────────────────────────────────────────
    def _desired_items(self, safe, valid_rks, keys=None) -> list:
        """The user's desired playlist as ``[{"rating_key": str}]`` AFTER re-resolving the
        cached plan against the FRESH owned inventory (P0 #4). ``keys`` is the family's cache-key
        precedence (default Up Next = combined > tv > movie); a mood/fresh family passes its own.

        Re-resolution: each plan item's ratingKey must still exist in the fresh inventory's
        resolved-key set (``valid_rks``) — a stale key means the item was re-scanned / removed
        since the plan was built. Drift is counted/logged. Returns ``None`` when drift exceeds
        _DRIFT_SKIP_RATIO (too stale to write → the caller LEAVES the playlist alone for a re-run);
        ``[]`` when the plan is genuinely empty (→ the caller tears the playlist down); else the
        kept items."""
        plan = self._load_plan(safe, keys)
        items = (plan or {}).get("items") or []
        if not items:
            return []
        kept, dropped = [], 0
        for it in items:
            rk = str(it.get("rating_key")) if it.get("rating_key") is not None else None
            if rk is not None and rk in valid_rks:
                kept.append({"rating_key": rk})
            else:
                dropped += 1
        total = len(items)
        if dropped:
            who = (getattr(self, "_anon_by_safe", {}) or {}).get(safe, safe)
            self.logger.log_info(
                f"[Writeback] '{who}' plan drift: {dropped}/{total} item(s) no longer resolve "
                f"to a current Plex ratingKey.")
        if total and dropped / total > _DRIFT_SKIP_RATIO:
            self.logger.log_warning(
                f"[Writeback] '{safe}' drift {dropped}/{total} exceeds "
                f"{int(_DRIFT_SKIP_RATIO * 100)}% — skipping this user (re-run after the next scan).")
            return None        # None = too-stale-to-write → the caller LEAVES the playlist alone
        return kept            # [] = genuinely empty → the caller tears the playlist down

    def _load_plan(self, safe, keys=None) -> dict | None:
        """The cached plan to write for a family, in ``keys`` precedence (default Up Next =
        combined > tv > movie; combined is the cross-medium household default, the standalone
        plans cover single-medium installs; a mood/fresh family has a single key)."""
        for key in (keys or _UP_NEXT["keys"]):
            plan = self._cache_get(f"{key}/{safe}", None)
            if isinstance(plan, dict) and plan.get("items"):
                return plan
        return None

    @staticmethod
    def _valid_rating_keys(tv_inv, movie_inv) -> set:
        """The set of ratingKeys the FRESH owned inventory currently resolves to (TV episodes
        keyed by ``tvdb:s:e``, movies by ``str(tmdb)``) — the re-resolution oracle."""
        out: set = set()
        for inv in (tv_inv or {}, movie_inv or {}):
            for v in inv.values():
                rk = (v or {}).get("rating_key") if isinstance(v, dict) else None
                if rk is not None:
                    out.add(str(rk))
        return out

    # ── in-place diff (P0 #5) ─────────────────────────────────────────────────
    @staticmethod
    def _diff(current, desired_rks) -> dict:
        """Compute the add/remove/move plan from the live playlist (``current`` =
        ``[{"rating_key", "playlist_item_id"}]``) to ``desired_rks`` (ordered). Stable
        ratingKeys: items already present keep their playlistItemID; only the genuine delta
        is added/removed and the survivors re-ordered."""
        cur_by_rk = {c["rating_key"]: c for c in current}
        desired_set = set(desired_rks)
        remove = [c for c in current if c["rating_key"] not in desired_set]
        add = [rk for rk in desired_rks if rk not in cur_by_rk]
        # GLD-PLY-13: "move" is the MINIMUM set of survivors that must be repositioned -- the
        # complement of the longest already-correctly-ordered subsequence. It used to be the WHOLE
        # survivor list whenever the order differed at all, which is what tripped _RECREATE_RATIO on
        # every re-ranked run. _apply_diff still re-walks the full desired order (deterministic and
        # idempotent); this count is the DRIFT METRIC the recreate threshold and the preview read.
        survivors = [rk for rk in desired_rks if rk in cur_by_rk]
        cur_order = [c["rating_key"] for c in current if c["rating_key"] in desired_set]
        move = _min_moves(cur_order, survivors)
        return {"add": add, "remove": remove, "move": move}

    def _apply_diff(self, rk, current, desired_rks, plan, token):
        """Apply the diff with stable ratingKeys: remove the deletes, append the adds, then
        re-order the whole desired list front-to-back. Re-GET between phases so the
        playlistItemIDs the move/remove verbs need stay valid."""
        for c in plan["remove"]:
            self.plex_api.remove_playlist_item(rk, c["playlist_item_id"], token=token)
        if plan["add"]:
            self.plex_api.add_playlist_items(rk, plan["add"], token=token)
        # Re-read so every survivor + freshly-added item carries a current playlistItemID, then
        # walk the desired order placing each after its predecessor (omitting after_id == front).
        live = self._current_items(rk, token)
        by_rk = {c["rating_key"]: c["playlist_item_id"] for c in live}
        prev_id = None
        for want in desired_rks:
            pid = by_rk.get(want)
            if pid is None:
                continue
            self.plex_api.move_playlist_item(rk, pid, after_id=prev_id, token=token)
            prev_id = pid

    # ── Plex reads/writes (thin wrappers — fake-able in tests) ────────────────
    def _current_items(self, rating_key, token=None) -> list:
        """The live playlist members as ``[{"rating_key", "playlist_item_id"}]`` in order.
        ``playlistItemID`` is the per-playlist handle the remove/move verbs take (distinct
        from the item's ratingKey). ``token`` scopes the read to the playlist's OWNER (a
        managed user's per-server token) so a per-user playlist is actually readable."""
        resp = self.plex_api.get_playlist_items(rating_key, token=token)
        out = []
        for raw in metadata_items(resp):
            if not isinstance(raw, dict):
                continue
            rk = raw.get("ratingKey") or raw.get("ratingkey")
            pid = raw.get("playlistItemID") or raw.get("playlistItemId")
            if rk is None or pid is None:
                continue
            out.append({"rating_key": str(rk), "playlist_item_id": str(pid)})
        return out

    def _playlist_exists(self, rating_key, token) -> bool:
        """True when the cached anchor ratingKey still resolves (the items endpoint returns a
        non-None body). A 404 yields None → the find-or-create falls back to title adoption."""
        resp = self.plex_api.get_playlist_items(rating_key, token=token)
        return resp is not None

    def _adopt_by_title(self, title, token) -> str | None:
        """Title-match adoption fallback (P0 #3): scan THIS user's playlists (token-scoped) for
        a video playlist whose title matches ours and adopt it. Because the scan is token-
        scoped it can only ever return a playlist this user OWNS — we never adopt (or later
        delete) a playlist that is not theirs."""
        resp = self.plex_api.get_playlists(token=token)
        for d in metadata_items(resp):
            if not isinstance(d, dict):
                continue
            if str(d.get("playlistType", "video")).lower() not in ("video", ""):
                continue
            if (d.get("title") or "") == title and d.get("ratingKey") is not None:
                return str(d.get("ratingKey"))
        return None

    def _create_playlist(self, title, rating_keys, token) -> str | None:
        """Create the playlist then re-GET /playlists to capture the new ratingKey (create
        returns XML so the JSON-only client yields no body — mirrors the api docstring)."""
        if not rating_keys:
            return None
        self.plex_api.create_playlist(title, rating_keys, token=token)
        resp = self.plex_api.get_playlists(token=token)
        newest = None
        for d in metadata_items(resp):
            if isinstance(d, dict) and (d.get("title") or "") == title and d.get("ratingKey") is not None:
                newest = str(d.get("ratingKey"))     # last match wins (most-recently created)
        return newest

    # ── anchor map (P0 #3) ────────────────────────────────────────────────────
    @staticmethod
    def _anchor_id(safe, suffix=_UP_NEXT_SUFFIX) -> str:
        """The anchor key id. "Up Next" keeps the LEGACY id (== safe_user) so existing managed
        playlists + their cached anchors keep resolving; the extra families namespace it
        (``safe::suffix``) so each playlist gets its own independent anchor."""
        return safe if suffix == _UP_NEXT_SUFFIX else f"{safe}::{suffix}"

    def _anchor_get(self, safe, suffix=_UP_NEXT_SUFFIX):
        # An anchor is always stored as str(ratingKey). The file cache returns {} (not None) for a
        # MISSING key, so treat anything that isn't a non-empty string as "no anchor" — otherwise a
        # phantom {} reads as an existing playlist (e.g. _handle_empty would log a bogus "would DELETE").
        val = self._cache_get(f"{_ANCHOR_KEY}/{self._anchor_id(safe, suffix)}", None)
        return val if (isinstance(val, str) and val) else None

    def _anchor_set(self, safe, rating_key, suffix="Up Next"):
        if not self.global_cache:
            return
        aid = self._anchor_id(safe, suffix)
        try:
            self.global_cache.set(f"{_ANCHOR_KEY}/{aid}", str(rating_key))
            idx = dict(self._cache_get(f"{_ANCHOR_KEY}/_index", {}) or {})
            idx[aid] = str(rating_key)
            self.global_cache.set(f"{_ANCHOR_KEY}/_index", idx)
        except Exception:
            pass

    def _anchor_clear(self, safe, suffix="Up Next"):
        self._anchor_clear_by_id(self._anchor_id(safe, suffix))

    def _anchor_clear_by_id(self, aid):
        if not self.global_cache:
            return
        try:
            self.global_cache.set(f"{_ANCHOR_KEY}/{aid}", None)
            idx = self._cache_get(f"{_ANCHOR_KEY}/_index", {}) or {}
            if aid in idx:
                idx = dict(idx)
                idx.pop(aid, None)
                self.global_cache.set(f"{_ANCHOR_KEY}/_index", idx)
        except Exception:
            pass

    def _all_anchors(self) -> dict:
        """Every persisted ``anchor_id → ratingKey`` (for the orphan sweep). ``anchor_id`` is the
        safe_user for Up Next, or ``safe::suffix`` for an extra family."""
        index = self._cache_get(f"{_ANCHOR_KEY}/_index", None)
        if isinstance(index, dict):
            return {k: v for k, v in index.items() if v is not None}
        return {}

    # -- rewrite cadence (GLD-PLY-15) ------------------------------------------
    def _cadence_cfg(self, key, default) -> float:
        """A cadence knob, config-derived with a documented module constant as the fallback."""
        wb = (self._pl_cfg().get("writeback", {}) or {})
        try:
            return float(wb.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _last_write(self, safe, suffix):
        """Epoch seconds of the last ARMED item write for this playlist, or None when there has
        never been one.

        P-C discipline: the file cache returns {} for a MISSING key, and a missing stamp MUST read
        as "never written" (-> the write is allowed). Reading it as "written just now" would
        suppress the very first write for a whole interval -- the same absent/empty conflation
        that has been a live bug three times in this repo."""
        val = self._cache_get(f"{_LASTWRITE_KEY}/{self._anchor_id(safe, suffix)}", None)
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _stamp_write(self, safe, suffix, armed):
        """Record that the items were written. ONLY when armed -- a disarmed preview performs no
        Plex write at all, so stamping it would defer the first REAL write by a full interval."""
        if not armed or not self.global_cache:
            return
        try:
            self.global_cache.set(f"{_LASTWRITE_KEY}/{self._anchor_id(safe, suffix)}", time.time())
        except Exception:
            pass

    def _cadence_defer(self, safe, suffix, plan, current, desired_rks, stats) -> bool:
        """True when this playlist was rewritten INSIDE the interval and the pending diff is only
        routine churn -- re-ranking noise, which can wait for the daily rewrite.

        The override is measured on ADDS and REMOVES only, deliberately never on moves: a pure
        re-order is precisely the noise this gate exists to absorb, so it must not be able to
        override the gate. Removes (vs the CURRENT list) are the "watched off it" signal; adds
        (vs the DESIRED list) are the "re-ranking pulled in new material" signal. Either one
        crossing the ratio is a genuine overhaul and writes through immediately."""
        hours = self._cadence_cfg("min_interval_hours", _MIN_REWRITE_INTERVAL_HOURS)
        if hours <= 0:
            return False                          # gate explicitly disabled
        last = self._last_write(safe, suffix)
        if last is None:
            return False                          # never written -> always write
        age_h = (time.time() - last) / 3600.0
        if age_h >= hours:
            return False                          # interval elapsed -> the daily rewrite
        ratio = self._cadence_cfg("churn_override_ratio", _CHURN_OVERRIDE_RATIO)
        add_frac = len(plan["add"]) / max(len(desired_rks), 1)
        rem_frac = len(plan["remove"]) / max(len(current), 1)
        if add_frac >= ratio or rem_frac >= ratio:
            self._detail(
                f"[Writeback] '{suffix}' written {age_h:.1f}h ago, but churn overrides the "
                f"{hours:.0f}h interval (+{add_frac:.0%} new / -{rem_frac:.0%} gone, "
                f"override at {ratio:.0%}).")
            return False
        head_frac, head_n = self._head_churn(current, desired_rks)
        head_ratio = self._cadence_cfg("head_churn_ratio", _HEAD_CHURN_RATIO)
        if head_frac >= head_ratio:
            self._detail(
                f"[Writeback] '{suffix}' written {age_h:.1f}h ago, but the TOP {head_n} changed "
                f"({head_frac:.0%} new up there, override at {head_ratio:.0%}) -- writing now.")
            return False
        self._detail(
            f"[Writeback] '{suffix}' deferred -- written {age_h:.1f}h ago, next in "
            f"{hours - age_h:.1f}h (+{len(plan['add'])}/-{len(plan['remove'])}/"
            f"~{len(plan['move'])}; churn +{add_frac:.0%}/-{rem_frac:.0%} below {ratio:.0%}).")
        stats["deferred"] = stats.get("deferred", 0) + 1
        return True

    def _head_churn(self, current, desired_rks):
        """``(fraction, head_n)`` -- how much of the TOP of the list is about to change.

        Compares the SET of the first ``head_size`` desired ratingKeys against the set of the
        first ``head_size`` live ones. A set comparison, deliberately: shuffling three items that
        are ALREADY in the top ten is invisible noise and scores 0, while an item arriving from
        #43 scores. That is the distinction the interval exists to draw (GLD-PLY-17).

        Note this is the ONLY override that a pure re-order can trip -- the add/remove override
        above cannot see a move at all.
        """
        head_n = int(self._cadence_cfg("head_size", _HEAD_SIZE))
        if head_n <= 0:
            return 0.0, 0
        want = desired_rks[:head_n]
        if not want:
            return 0.0, head_n
        live = {c["rating_key"] for c in current[:head_n]}
        newcomers = sum(1 for rk in want if rk not in live)
        return newcomers / len(want), head_n

    # ── helpers ────────────────────────────────────────────────────────────────
    def _write_token(self, user, users_mgr):
        if users_mgr is None or not hasattr(users_mgr, "server_write_token"):
            return None
        return users_mgr.server_write_token(user)

    def _playlist_title(self, user, suffix=_UP_NEXT_SUFFIX) -> str:
        # The CLEAN display title — bare suffix, no username, no sort prefix. The front-pinning
        # prefix lives only in _sort_title (the Plex titleSort key).
        return suffix

    @staticmethod
    def _sort_title(suffix=_UP_NEXT_SUFFIX) -> str:
        return f"{_SORT_PREFIX}{suffix}"

    def _excluded_users(self) -> set:
        raw = self._pl_cfg().get("exclude_users")
        if not raw:
            return set()
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip().lower() for x in raw if str(x).strip()}

    @staticmethod
    def _is_excluded(user, excluded) -> bool:
        if not excluded:
            return False
        for v in (user.get("title"), user.get("safe_user")):
            if v and str(v).strip().lower() in excluded:
                return True
        return False

    def _roster_safe_users(self, roster, tracked, users_mgr) -> set:
        """The safe_users still in the household: every tracked user PLUS every roster uuid
        whose safe_key maps to an anchor (so a pin-skipped user — in roster, not tracked —
        keeps its playlist). Built from the persisted roster (carries uuid) joined to the
        users_mgr safe-map when available."""
        live = {u.get("safe_user") for u in tracked if u.get("safe_user")}
        safe_by_uuid = getattr(users_mgr, "_safe_by_uuid", None) or {}
        identity = self._cache_get("plex/identity_map", {}) or {}
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            uuid = entry.get("uuid")
            safe = safe_by_uuid.get(uuid) or (identity.get(uuid, {}) or {}).get("safe_key")
            if safe:
                live.add(safe)
        return live

    def _audit(self, user, action, rating_key, n_items):
        """Audit-log a real write against a non-admin account (P0 #7). Owner writes are routine
        and stay at debug; managed-user mutations are the privacy-sensitive ones to record."""
        if user.get("is_admin"):
            return
        fn = getattr(self.logger, "log_audit", None)
        msg = (f"plex playlist {action} for '{user.get('title') or user.get('safe_user')}' "
               f"(rk={rating_key}, items={n_items})")
        if callable(fn):
            fn(msg)
        else:
            self.logger.log_info(f"[AUDIT] {msg}")

    def _detail(self, msg):
        """Per-user/per-family preview detail → the DEDICATED ``support/logs/playlists.log`` (rotated
        fresh each run), NOT the main run log — so a multi-profile × N-family dry-run doesn't flood
        it. No-op when the logger lacks the file sink (e.g. a None logger in tests). This file is a
        LOCAL operator drill-down, so messages here KEEP the real profile name."""
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("playlists", msg)

    def _anon(self, u, idx):
        """De-identified profile handle (``'{initial} - {tier} {n}'``) for the SHAREABLE run log —
        same format the builders log, so an operator can cross-reference. Real names never reach
        the run log; they stay in the local playlists.log preview + the audit trail."""
        ages = self._pl_cfg().get("profile_ages", {}) or {}
        level = tier_level(u.get("restriction_profile"),
                           ages.get(u.get("title")) or ages.get(u.get("safe_user")))
        tier = _TIER_NAMES[level] if 0 <= level < len(_TIER_NAMES) else "unknown"
        return anon_label(u.get("title"), tier, idx)

    def _who(self, user):
        """The de-identified handle for ``user`` for run-log lines (looked up from the per-run map
        built in :meth:`_writeback`; falls back to an index-less label for any off-map caller)."""
        amap = getattr(self, "_anon_by_safe", {}) or {}
        return amap.get(user.get("safe_user")) or anon_label(user.get("title"), "unknown", 0)

    def _banner(self, stats):
        """The armed/disarmed summary banner, logged EVERY run (P0 #7)."""
        state = "ARMED" if stats["armed"] else "disarmed (dry-run/disabled — no Plex writes)"
        self.logger.log_info(
            f"[Writeback] {state}: {stats['created']} create / {stats['updated']} update "
            f"({stats.get('recreated', 0)} of them full recreates) / "
            f"{stats['deleted']} delete / {stats.get('deferred', 0)} deferred / "
            f"{stats['skipped']} skipped / {stats.get('branded', 0)} branded "
            f"/ {stats.get('retitled', 0)} retitled "
            + (f"({stats['sort_repaired']} sort key(s) REPAIRED after server drift) "
               if stats.get("sort_repaired") else "")
            + f"(over {stats['users']} user(s), {stats['orphans']} orphan(s)) "
            f"— per-playlist detail in support/logs/playlists.log.")

    # ── title (rename) gate ──────────────────────────────────────────────────────
    def _ensure_title(self, safe, suffix, rk, token, armed, stats, force=False):
        """Give a managed playlist its CLEAN display title (bare suffix, no username) and a
        ``!``-prefixed titleSort (front-pin) ONCE. Gated on the persisted last-set titleSort so a
        steady run edits nothing; a fresh create gets the titleSort its POST can't set, and a list
        carrying an old ``'{name} {suffix}'`` / ``'!{suffix}'`` title is migrated on the first armed
        run. Honors the arm gate (disarmed only previews) and the per-user ``token``.

        ``force`` bypasses the gate after a recreate (GLD-PLY-14): the gate key is the anchor_id,
        which SURVIVES a recreate, so a fresh ratingKey would otherwise inherit a stale "already
        titled" marker and never get its titleSort."""
        title = self._playlist_title({}, suffix)         # clean: just the suffix
        sort = self._sort_title(suffix)                  # '!{suffix}'
        key = f"{_TITLE_KEY}/{self._anchor_id(safe, suffix)}"

        # VERIFY AGAINST PLEX, not against our own marker. The cache records what we
        # last SENT; it is not evidence of what the server currently holds. A shelf
        # renamed by hand, a restored database, or any Plex-side reset drops the
        # titleSort while the marker still reads "done" -- and the list then sits in
        # plain alphabetical order forever, because the one thing that could repair
        # it has convinced itself there is nothing to repair.
        #
        # The read is one token-scoped playlist listing, the same call
        # ``_adopt_by_title`` already makes, and it runs OUTSIDE the cadence gate:
        # a titleSort edit changes no membership and costs one idempotent call, so
        # there is no reason for it to wait behind an item-rewrite budget.
        live = self._live_sort_title(rk, token)
        if live is not None and live != sort:
            if self._cache_get(key, None) == sort:
                self.logger.log_info(
                    f"[Writeback] '{suffix}' sort key drifted on the server "
                    f"(Plex has {live!r}, expected {sort!r}) - repairing.")
                stats["sort_repaired"] = stats.get("sort_repaired", 0) + 1
            force = True                 # server disagrees: the marker is not evidence
        elif live is not None and live == sort:
            # Server is already correct. Re-seed the marker so a cache clear does not
            # cost a needless rewrite, and skip.
            if self._cache_get(key, None) != sort:
                self._title_set(safe, suffix, sort)
            return

        if not force and self._cache_get(key, None) == sort:
            return
        if not armed:
            self._detail(f"[Writeback] [disarmed] '{suffix}' would be titled '{title}' (sort '{sort}').")
            return
        if rk is None or token is None:
            return
        if not self.plex_api.edit_playlist(rk, title=title, title_sort=sort, token=token):
            self.logger.log_warning(
                f"[Writeback] retitle of '{title}' failed (rk {rk}) — not cached, will retry next run.")
            return
        self._title_set(safe, suffix, sort)
        stats["retitled"] = stats.get("retitled", 0) + 1
        self._detail(f"[Writeback] titled '{title}' (sort '{sort}').")

    def _live_sort_title(self, rk, token):
        """The titleSort Plex ACTUALLY holds for ``rk``, or None when unreadable.

        None means "could not determine" and the caller must fall back to the
        cached marker - never treat it as "empty", or a transient API hiccup would
        rewrite every title on every run (P-C).
        """
        if rk is None or not self.plex_api:
            return None
        try:
            resp = self.plex_api.get_playlists(token=token)
        except Exception:
            return None
        for d in metadata_items(resp):
            if isinstance(d, dict) and str(d.get("ratingKey")) == str(rk):
                return str(d.get("titleSort") or "")
        return None

    def _title_set(self, safe, suffix, sort):
        if not self.global_cache:
            return
        try:
            self.global_cache.set(f"{_TITLE_KEY}/{self._anchor_id(safe, suffix)}", sort)
        except Exception:
            pass

    # ── poster branding (default-off, version-gated) ─────────────────────────────
    def _branding_cfg(self) -> dict:
        return (self._pl_cfg().get("branding", {}) or {})

    def _branding_enabled(self) -> bool:
        """The opt-in gate for uploading per-family poster art. Default False, so with it unset the
        whole branding path is inert and the write-back is byte-identical to before."""
        return bool(self._branding_cfg().get("enabled", False))

    def _assets_dir(self) -> Path:
        """Where the poster PNGs live — the bundled support/assets/playlists/ dir, overridable via
        ``plex.playlists.branding.assets_dir`` (an operator who keeps custom art elsewhere)."""
        override = self._branding_cfg().get("assets_dir")
        return Path(override) if override else _ASSETS_DIR

    def _branding_asset(self, suffix, safe=None) -> Path | None:
        """The poster Path for a family, PREFERRING this profile's own render.

        ``assets/posters/playlists/by_user/{safe}/{slug}.png`` when
        ``PlaylistPosterRenderManager`` produced one, else the shared default.

        The per-user file exists because five of the eight families are personal -
        Up Next's lead shows, The Long Glide's sagas, Tonight's picks - and one
        shared PNG cannot say "next up in Andor . The Bear" for one profile and
        something else for another. Before this, every poster shipped its SPEC
        default and a playlist holding 3 days of content announced "12 just
        landed".

        FALLING BACK IS DELIBERATE, not a failure path: a profile whose render was
        skipped (no rasteriser, no plan) keeps the generic art rather than losing
        its poster. None only when there is no mapping or no file at all, which is
        skipped quietly - branding is best-effort and never a write-back blocker.
        """
        slug = _BRAND_ASSETS.get(suffix)
        if not slug:
            return None
        base = self._assets_dir()
        for path in ((base / "by_user" / str(safe) / f"{slug}.png") if safe else None,
                     base / f"{slug}.png"):
            if path is None:
                continue
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    @staticmethod
    def _asset_version(path: Path) -> str | None:
        """A cheap content-change token (size + mtime) so a re-themed PNG re-uploads but an unchanged
        one never does. None on a stat failure → the caller treats it as 'no asset' and skips."""
        try:
            st = path.stat()
            return f"{_BRAND_SCHEME}:{st.st_size}-{st.st_mtime_ns}"
        except OSError:
            return None

    def _apply_branding(self, safe, suffix, rk, token, armed, stats, force=False):
        """Upload a family's poster to ITS managed playlist, once. Version-gated against
        ``_BRAND_KEY/{anchor_id}`` so a steady-state run re-uploads nothing; ``force`` bypasses the
        gate after a recreate (the new ratingKey has no poster yet). Honors the same arm gate as the
        item writes — disarmed only previews — and the same per-user ``token`` so the poster lands on
        the member's own list, never the owner's."""
        if not self._branding_enabled():
            return
        asset = self._branding_asset(suffix, safe)
        if asset is None:
            return
        version = self._asset_version(asset)
        if version is None:
            return
        key = f"{_BRAND_KEY}/{self._anchor_id(safe, suffix)}"
        if not force and self._cache_get(key, None) == version:
            return                       # already branded with this exact art → no re-upload
        if not armed:
            self._detail(f"[Writeback] [disarmed] '{suffix}' poster would be set. "
                         f"{self._poster_text(suffix)}")
            return
        if rk is None or token is None:
            return
        try:
            data = asset.read_bytes()
        except OSError:
            self.logger.log_warning(f"[Writeback] could not read poster '{asset.name}' — skipped.")
            return
        if not data:
            return
        # Only cache the version on a VERIFIED 2xx — a silent 404/401 must NOT mark the list branded
        # (else it never retries). upload_playlist_poster reads the real HTTP status for us.
        if not self.plex_api.upload_playlist_poster(rk, data, token=token):
            self.logger.log_warning(
                f"[Writeback] poster upload failed for '{suffix}' (rk {rk}) — not cached, will retry next run.")
            return
        if self.global_cache:
            try:
                self.global_cache.set(key, version)
            except Exception:
                pass
        stats["branded"] = stats.get("branded", 0) + 1
        self._detail(f"[Writeback] '{suffix}' poster set ({len(data)} bytes). "
                     f"{self._poster_text(suffix)}")

    def _poster_text(self, suffix) -> str:
        """The words printed ON the poster for ``suffix``, for the preview log.

        This module uploads a PNG, so by the time it sees a poster the text is
        pixels and unreadable. generate_posters writes ``_poster_text.json``
        beside the PNGs at render time; this reads it back so a dry run can show
        WHAT a poster says rather than only that one would be sent.

        Returns '' when the manifest is absent (a poster set generated before
        manifests existed) - missing text must degrade to a quieter log line,
        never to an error on the branding path, which is best-effort throughout.
        """
        slug = _BRAND_ASSETS.get(suffix)
        if not slug:
            return ""
        try:
            import json
            man = json.loads((self._assets_dir() / "_poster_text.json")
                             .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        t = man.get(slug) or {}
        parts = [t.get("title"), t.get("date"), t.get("why")]
        return "[" + " | ".join(p for p in parts if p) + "]" if any(parts) else ""

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

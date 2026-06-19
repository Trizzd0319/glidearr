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

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.services.plex._common import metadata_items, parse_item

# The three per-user plan families the builders cache (key + /{safe_user}). Each maps to one
# managed playlist; the combined plan is the household default when movies are enabled.
_TV_PLAN_KEY = "plex/playlists/tv_plan"
_MOVIE_PLAN_KEY = "plex/playlists/movie_plan"
_COMBINED_PLAN_KEY = "plex/playlists/combined_plan"

_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"
_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"

# safe_user → our playlist ratingKey (the managed ANCHOR). Persisted so find-or-create
# resolves by ratingKey first and we never delete a playlist we don't own.
_ANCHOR_KEY = "plex/playlists/managed_anchor"          # + /{safe_user}

# Drift fraction above which a user is skipped (the plan is too stale to write safely; a
# re-run after the next inventory scan resolves it).
_DRIFT_SKIP_RATIO = 0.5

# When the in-place diff would touch MORE than the whole desired list, fall back to a clean
# recreate (create-new-then-delete-old) rather than dribbling N removes + N adds.
_RECREATE_RATIO = 1.0

_TITLE_SUFFIX = "Up Next"                              # the managed-playlist title shape


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
                 "skipped": 0, "users": len(tracked), "orphans": 0}

        for u in tracked:
            safe = u.get("safe_user")
            if not safe:
                stats["skipped"] += 1
                continue
            if self._is_excluded(u, excluded):
                self.logger.log_info(f"[Writeback] '{u.get('title')}' excluded (exclude_users) — skipped.")
                stats["skipped"] += 1
                continue

            desired = self._desired_items(safe, valid_rks)
            # An empty plan for a RESTRICTED user => do not leave an empty playlist: delete any
            # existing managed anchor and move on (logged). For an unrestricted user an empty
            # plan is treated the same (nothing to surface).
            if not desired:
                self._handle_empty(u, users_mgr, armed, stats)
                continue

            token = self._write_token(u, users_mgr)
            if token is None:
                self.logger.log_warning(
                    f"[Writeback] no per-server write token for '{u.get('title')}' — skipped (counted).")
                stats["skipped"] += 1
                continue
            # P0 #2: never write a non-admin's playlist with the owner token.
            if not u.get("is_admin"):
                owner = self.plex_api.token if self.plex_api else None
                if owner is not None and token == owner:
                    self.logger.log_error(
                        f"[Writeback] refusing to write '{u.get('title')}' with the OWNER token "
                        f"(non-admin) — skipped.")
                    stats["skipped"] += 1
                    continue

            self._writeback_user(u, safe, desired, token, armed, stats)

        self._cleanup_orphans(roster, tracked, excluded, users_mgr, armed, stats)
        self._banner(stats)
        return stats

    # ── per-user write (find-or-create → re-resolve → diff → apply) ───────────
    def _writeback_user(self, user, safe, desired, token, armed, stats):
        title = self._playlist_title(user)
        anchor = self._find_or_create_anchor(safe, title, token, armed, desired, stats)
        if anchor is None:
            # Not armed (no real create happened) — we've already counted a would-create and
            # logged the preview; nothing more to do this run.
            return
        if anchor.get("created"):
            return                       # freshly created with the desired items, in order

        rk = anchor["rating_key"]
        current = self._current_items(rk, token)
        desired_rks = [it["rating_key"] for it in desired]
        if [c["rating_key"] for c in current] == desired_rks:
            self.logger.log_debug(f"[Writeback] '{title}' already in steady state — no write.")
            return                       # P0 #5: current == desired → skip entirely

        plan = self._diff(current, desired_rks)
        n_changes = len(plan["add"]) + len(plan["remove"]) + len(plan["move"])
        if n_changes > max(len(desired_rks), 1) * _RECREATE_RATIO:
            # Diff exceeds the whole list → cheaper + safer to recreate (new-then-old).
            self._recreate(user, safe, title, rk, desired_rks, token, armed, stats)
            return

        if not armed:
            self.logger.log_info(
                f"[Writeback] [disarmed] '{title}' would update "
                f"(+{len(plan['add'])}/-{len(plan['remove'])}/~{len(plan['move'])}).")
            stats["updated"] += 1
            return

        self._apply_diff(rk, current, desired_rks, plan, token)
        stats["updated"] += 1
        self._audit(user, "replace", rk, len(desired_rks))

    def _find_or_create_anchor(self, safe, title, token, armed, desired, stats) -> dict | None:
        """Resolve OUR managed playlist for this user (P0 #3). Cached ratingKey FIRST; on a
        404 fall back to a title-match and adopt ONLY when the playlist is owned by this user.
        Create one when neither resolves. Returns ``{"rating_key", "created"}`` or None when
        disarmed (the create is previewed + counted but not performed)."""
        cached_rk = self._anchor_get(safe)
        if cached_rk is not None and self._playlist_exists(cached_rk, token):
            return {"rating_key": cached_rk, "created": False}

        adopted = self._adopt_by_title(title, token)
        if adopted is not None:
            self._anchor_set(safe, adopted)
            return {"rating_key": adopted, "created": False}

        # Nothing to adopt → create.
        desired_rks = [it["rating_key"] for it in desired]
        if not armed:
            self.logger.log_info(
                f"[Writeback] [disarmed] '{title}' would be CREATED with {len(desired_rks)} item(s).")
            stats["created"] += 1
            return None
        rk = self._create_playlist(title, desired_rks, token)
        if rk is None:
            self.logger.log_warning(f"[Writeback] create failed for '{title}' — skipped.")
            stats["skipped"] += 1
            return None
        self._anchor_set(safe, rk)
        stats["created"] += 1
        self._audit({"title": title, "safe_user": safe}, "create", rk, len(desired_rks))
        return {"rating_key": rk, "created": True}

    def _recreate(self, user, safe, title, old_rk, desired_rks, token, armed, stats):
        """Delete+create fallback, CREATE-NEW-THEN-DELETE-OLD so a failed create never loses
        the user's playlist (P0 #5). Only the new anchor is ever deleted on the next pass."""
        if not armed:
            self.logger.log_info(
                f"[Writeback] [disarmed] '{title}' would be RECREATED ({len(desired_rks)} item(s)).")
            stats["updated"] += 1
            return
        new_rk = self._create_playlist(title, desired_rks, token)
        if new_rk is None:
            self.logger.log_warning(f"[Writeback] recreate failed for '{title}' — keeping old playlist.")
            stats["skipped"] += 1
            return
        self._anchor_set(safe, new_rk)            # repoint the anchor BEFORE deleting the old one
        self.plex_api.delete_playlist(old_rk, token=token)
        stats["updated"] += 1
        self._audit(user, "replace", new_rk, len(desired_rks))

    # ── empty-plan + orphan handling ──────────────────────────────────────────
    def _handle_empty(self, user, users_mgr, armed, stats):
        """Empty plan (e.g. a restricted user whose owned set age-gates to nothing): never
        write an empty playlist — delete any existing managed anchor + log (P0/brief)."""
        safe = user.get("safe_user")
        cached_rk = self._anchor_get(safe)
        if cached_rk is None:
            self.logger.log_debug(f"[Writeback] '{user.get('title')}' empty plan — nothing to write.")
            return
        token = self._write_token(user, users_mgr)
        if not armed:
            self.logger.log_info(
                f"[Writeback] [disarmed] '{user.get('title')}' empty plan would DELETE its "
                f"managed playlist.")
            stats["deleted"] += 1
            return
        if token is None:
            self.logger.log_warning(
                f"[Writeback] '{user.get('title')}' empty plan but no write token — skipped (counted).")
            stats["skipped"] += 1
            return
        self.plex_api.delete_playlist(cached_rk, token=token)
        self._anchor_clear(safe)
        stats["deleted"] += 1
        self.logger.log_info(f"[Writeback] '{user.get('title')}' empty plan — deleted managed playlist.")
        self._audit(user, "delete", cached_rk, 0)

    def _cleanup_orphans(self, roster, tracked, excluded, users_mgr, armed, stats):
        """Delete a managed playlist ONLY when its owning uuid is genuinely absent from the
        LIVE HOME ROSTER (P0 #6). A PIN-mint failure (in roster, absent from tracked_users)
        must LEAVE the playlist alone — we key orphan-detection on the roster, never on
        tracked_users. The anchor map is keyed by safe_user; we map roster uuids → safe_user
        via the persisted roster + tracked set, and only sweep anchors whose user has truly
        vanished from the household."""
        anchors = self._all_anchors()
        if not anchors:
            return
        live_safe = self._roster_safe_users(roster, tracked, users_mgr)
        for safe, rk in list(anchors.items()):
            if safe in live_safe:
                continue                 # still in the household (tracked OR pin-skipped) → leave alone
            stats["orphans"] += 1
            if not armed:
                self.logger.log_info(
                    f"[Writeback] [disarmed] orphan playlist for '{safe}' would be DELETED.")
                continue
            token = self.plex_api.token if self.plex_api else None
            self.plex_api.delete_playlist(rk, token=token)
            self._anchor_clear(safe)
            stats["deleted"] += 1
            self.logger.log_info(f"[Writeback] deleted orphan playlist for departed user '{safe}'.")
            self._audit({"title": safe, "safe_user": safe}, "delete", rk, 0)

    # ── re-resolution (P0 #4) ─────────────────────────────────────────────────
    def _desired_items(self, safe, valid_rks) -> list:
        """The user's desired playlist as ``[{"rating_key": str}]`` AFTER re-resolving the
        cached plan against the FRESH owned inventory (P0 #4). The combined plan wins when
        present (household default with movies on); else the TV plan; else the movie plan.

        Re-resolution: each plan item's ratingKey must still exist in the fresh inventory's
        resolved-key set (``valid_rks``) — a stale key means the item was re-scanned / removed
        since the plan was built. Drift is counted/logged; if drift exceeds _DRIFT_SKIP_RATIO
        the whole user is skipped with a re-run note (return ``[]`` would write an empty list,
        so we return a sentinel-free empty here and the caller treats large drift as skip)."""
        plan = self._load_plan(safe)
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
            self.logger.log_info(
                f"[Writeback] '{safe}' plan drift: {dropped}/{total} item(s) no longer resolve "
                f"to a current Plex ratingKey.")
        if total and dropped / total > _DRIFT_SKIP_RATIO:
            self.logger.log_warning(
                f"[Writeback] '{safe}' drift {dropped}/{total} exceeds "
                f"{int(_DRIFT_SKIP_RATIO * 100)}% — skipping this user (re-run after the next scan).")
            return []
        return kept

    def _load_plan(self, safe) -> dict | None:
        """The cached plan to write, combined > tv > movie (combined is the cross-medium
        household default; the standalone plans cover the single-medium installs)."""
        for key in (_COMBINED_PLAN_KEY, _TV_PLAN_KEY, _MOVIE_PLAN_KEY):
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
        # A "move" is any surviving item whose position changes once removes/adds settle.
        survivors = [rk for rk in desired_rks if rk in cur_by_rk]
        cur_order = [c["rating_key"] for c in current if c["rating_key"] in desired_set]
        move = survivors if survivors != cur_order else []
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
    def _anchor_get(self, safe):
        return self._cache_get(f"{_ANCHOR_KEY}/{safe}", None)

    def _anchor_set(self, safe, rating_key):
        if not self.global_cache:
            return
        try:
            self.global_cache.set(f"{_ANCHOR_KEY}/{safe}", str(rating_key))
            idx = self._cache_get(f"{_ANCHOR_KEY}/_index", {}) or {}
            idx = dict(idx)
            idx[safe] = str(rating_key)
            self.global_cache.set(f"{_ANCHOR_KEY}/_index", idx)
        except Exception:
            pass

    def _anchor_clear(self, safe):
        if not self.global_cache:
            return
        try:
            self.global_cache.set(f"{_ANCHOR_KEY}/{safe}", None)
            idx = self._cache_get(f"{_ANCHOR_KEY}/_index", {}) or {}
            if safe in idx:
                idx = dict(idx)
                idx.pop(safe, None)
                self.global_cache.set(f"{_ANCHOR_KEY}/_index", idx)
        except Exception:
            pass

    def _all_anchors(self) -> dict:
        """Every persisted ``safe_user → ratingKey`` anchor (for the orphan sweep) from the
        index maintained alongside the per-user keys."""
        index = self._cache_get(f"{_ANCHOR_KEY}/_index", None)
        if isinstance(index, dict):
            return {k: v for k, v in index.items() if v is not None}
        return {}

    # ── helpers ────────────────────────────────────────────────────────────────
    def _write_token(self, user, users_mgr):
        if users_mgr is None or not hasattr(users_mgr, "server_write_token"):
            return None
        return users_mgr.server_write_token(user)

    def _playlist_title(self, user) -> str:
        return self._title_for(user.get("title") or user.get("safe_user") or "User")

    @staticmethod
    def _title_for(display) -> str:
        return f"{display} {_TITLE_SUFFIX}"

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

    def _banner(self, stats):
        """The armed/disarmed summary banner, logged EVERY run (P0 #7)."""
        state = "ARMED" if stats["armed"] else "disarmed (dry-run/disabled — no Plex writes)"
        self.logger.log_info(
            f"[Writeback] {state}: {stats['created']} create / {stats['updated']} update / "
            f"{stats['deleted']} delete / {stats['skipped']} skipped "
            f"(over {stats['users']} user(s), {stats['orphans']} orphan(s)).")

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

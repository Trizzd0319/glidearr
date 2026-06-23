"""
plex/users — Home enum + per-user token mint + identity crosswalk (DESIGN P0).
================================================================================
The mandatory infra every per-user Plex signal depends on, and the only place
multi-Plex-user identity resolves.

SECURITY — per-user minted tokens are held IN-MEMORY ONLY (DESIGN §6.2). They live
in the shared ``user_tokens`` dict for the duration of the run and are discarded.
We **never** create a ``plex/users/<u>/token`` cache key — the pre-commit
``secret_scan`` only sees staged git diffs and the cache is gitignored, so it would
NOT catch a token written to cache; in-memory-only is the *only* defense. Do not
"helpfully" add a token cache here.

Every minted token is registered with the logger scrubber the instant it is minted
(a bare token matches no ``_SECRET_SCRUB_PATTERN``). PINs are credentials: read from
gitignored config / SecretStore, never logged, never cached; PIN-less-unavailable
users are SKIPPED and COUNTED so the union shrinks visibly.

The crosswalk (``build_identity_map``) fails CLOSED on ambiguity — it never writes
user A's data under user B's key.
"""
from __future__ import annotations

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache.key_builder import _sanitize_part
from scripts.managers.services.plex._common import anon_label
from scripts.support.utilities.logger.logger import LoggerManager

_USERS_KEY = "plex/users"
_IDENTITY_KEY = "plex/identity_map"
_SECTIONS_KEY = "plex/sections"             # global library section index (PlexLibrariesManager)
_USER_SECTIONS_KEY = "plex/user_sections"   # NET-NEW per-user allowlist: safe_user → [section keys]


class PlexUsersManager(BaseManager):
    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)
        # Shared in-memory token table (keyed by safe_user). NEVER persisted.
        self.user_tokens: dict = kwargs.get("user_tokens", {})
        # In-memory working roster for THIS run, read by the per-user fetchers via
        # ``registry.get("manager", "PlexUsersManager").tracked_users``. Holds NO
        # token (those stay in user_tokens) and NO email (PII dropped).
        self.tracked_users: list = []
        self._safe_by_uuid: dict = {}
        self.scope_ok = False

    def prepare(self):
        pass

    # ── per-server write token (PR-5 playlist write-back) ─────────────────────
    def server_write_token(self, tracked_user: dict) -> str | None:
        """The token the LOCAL PMS accepts for WRITING this member's playlists, derived
        from their in-memory account authToken via ``PlexAPI.server_access_token`` and
        cached per user FOR THIS RUN (in-memory only — never persisted, like every minted
        token here).

        The owner reuses the account token directly (it already writes owner-side). For a
        managed user the raw switch authToken 401s on the local PMS, so we exchange it for
        the per-server ``accessToken``; if that exchange yields nothing we return ``None``
        and the caller SKIPS — we NEVER fall back to the owner token for a non-admin (that
        would write their playlist onto the owner's account)."""
        safe = tracked_user.get("safe_user")
        if not safe:
            return None
        if tracked_user.get("is_admin"):
            # The owner's account token IS the local-PMS write token for owner playlists.
            return self.user_tokens.get(safe) or (self.plex_api.token if self.plex_api else None)
        cache = getattr(self, "_server_tokens", None)
        if cache is None:
            cache = self._server_tokens = {}
        if safe in cache:
            return cache[safe]
        auth = self.user_tokens.get(safe)
        token = self.plex_api.server_access_token(auth) if (auth and self.plex_api) else None
        if token:
            LoggerManager.register_secrets([token])   # scrub from every later log line
        cache[safe] = token
        return token

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        stats = {"scope_ok": False, "users_tracked": 0, "users_pin_skipped": 0}
        self.tracked_users = []
        self.user_tokens.clear()
        self._safe_by_uuid = {}
        self._server_tokens = {}      # per-run cache of derived per-server write tokens
        self._section_grants = {}     # safe_user → "ALL" | set(section-id str): plex.tv share grants
        self._user_sections = {}      # safe_user → [section keys]: the persisted allowlist artifact

        # 1. Token-scope probe — the HARD gate (DESIGN §4.2). On failure: warn once,
        #    write empty roster, degrade to owner-only; NEVER fall through to broader
        #    scope, NEVER abort the run.
        if not self._probe_scope():
            self.logger.log_warning(
                "[PlexUsers] account-scope probe failed (token not account-owner-scoped) — "
                "per-user surface disabled this run; degrading to owner-only.")
            self._degrade_owner_only()
            stats["users_tracked"] = len(self.tracked_users)
            return stats
        self.scope_ok = True
        stats["scope_ok"] = True

        # 2. Enumerate Home/managed users (schema-tolerant; soft-empty on drift).
        roster = self._enumerate_home_users()
        if not roster:
            self.logger.log_info("[PlexUsers] no Home users enumerated — using owner-only.")
            self._degrade_owner_only()
            stats["users_tracked"] = len(self.tracked_users)
            return stats

        # Stable, COLLISION-FREE safe_user per uuid (the cache-path + token-table key).
        # Two display names that sanitize identically must NOT share a key, or one
        # user's token/cache silently overwrites another's — fail-OPEN attribution.
        self._safe_by_uuid = self._build_safe_map(roster)

        # 3. Mint per-user tokens (in-memory), handling PINs.
        pin_skipped = self._mint_tokens(roster)
        stats["users_pin_skipped"] = pin_skipped

        # 4. Crosswalk → identity_map (needs Tautulli users + rating_groups).
        tautulli_users = self._tautulli_users()
        rating_groups = (self.config.get("rating_groups", {}) if self.config else {}) or {"household": {}}
        identity_map = self.build_identity_map(roster, tautulli_users, rating_groups)

        # 5. Build the in-memory tracked set (only users with a usable token) +
        #    persist the PII-minimized roster and identity map.
        self._build_tracked(roster, identity_map)
        self._section_grants = self._resolve_section_grants(roster)
        self._persist(roster, identity_map)
        self._warn_ungated_managed_users(roster)
        stats["users_tracked"] = len(self.tracked_users)
        self.logger.log_info(
            f"[PlexUsers] {len(self.tracked_users)} user(s) tracked "
            f"({pin_skipped} pin-skipped) of {len(roster)} Home user(s).")
        return stats

    def _warn_ungated_managed_users(self, roster: list) -> None:
        """Surface managed (kid/teen) Home profiles whose age tier could NOT be resolved.

        Plex's /api/v2/home/users payload frequently OMITS ``restrictionProfile``; without it
        (and without a ``plex.playlists.profile_ages`` override) the playlist age-gate falls
        OPEN to ADULT, so a managed child profile silently receives the full, unfiltered
        household plan. We never guess a tier (that would over-gate a legitimate adult Home
        member like a spouse), so instead we warn LOUDLY — once per run — DE-IDENTIFYING each profile in the
        log (initial + tier + number, never the full name — a name is PII), numbered so the
        operator can tell which to add a profile_ages entry for."""
        pl = ((self.config.get("plex", {}) if self.config else {}).get("playlists", {}) or {})
        overrides = {str(k).strip().lower() for k in (pl.get("profile_ages") or {})}
        ungated = []
        for u in roster:
            if not u.get("is_managed") or u.get("restriction_profile"):
                continue                                  # not managed, or Plex gave us a tier
            title = (u.get("title") or "").strip()
            if title.lower() in overrides or str(u.get("uuid") or "").lower() in overrides:
                continue                                  # operator already set an explicit age
            ungated.append(title or str(u.get("uuid") or "?"))
        if ungated:
            # De-identify for the log: '{initial} - unknown {n}', sorted for a stable number.
            labels = [anon_label(t, "unknown", i) for i, t in enumerate(sorted(ungated), 1)]
            self.logger.log_warning(
                f"[PlexUsers] age tier UNKNOWN for {len(labels)} managed profile(s): "
                f"{', '.join(labels)}. Plex reported no restrictionProfile and no "
                "plex.playlists.profile_ages override is set, so their Up Next playlists fail "
                "OPEN to ADULT (unfiltered). Add a profile_ages entry (little_kid / older_kid / "
                "teen) for any that are children.")

    # ── network steps ─────────────────────────────────────────────────────────
    def _probe_scope(self) -> bool:
        if not self.plex_api or not self.plex_api.configured:
            return False
        return bool(self.plex_api.get_account())

    def _enumerate_home_users(self) -> list:
        resp = self.plex_api.get_home_users()
        users = self._parse_home_users(resp)
        self.logger.log_debug(f"[PlexUsers] {len(users)} Home user(s) enumerated.")
        return users

    @staticmethod
    def _parse_home_users(resp) -> list:
        """Defensive parse tolerant of missing admin/guest/restricted/protected flags
        (DESIGN §6.3). Accepts ``{"users":[...]}`` or a bare list."""
        if isinstance(resp, dict):
            raw = resp.get("users") or resp.get("Users") or (resp.get("MediaContainer") or {}).get("User") or []
        elif isinstance(resp, list):
            raw = resp
        else:
            raw = []
        out = []
        for u in raw:
            if not isinstance(u, dict):
                continue
            uuid = u.get("uuid") or u.get("id")
            if uuid is None:
                continue  # fail-closed: no stable key → skip
            out.append({
                "uuid": str(uuid),
                "id": u.get("id"),
                "title": u.get("title") or u.get("username") or u.get("friendlyName") or "",
                "email": u.get("email") or "",
                "is_admin": bool(u.get("admin") or u.get("isAdmin")),
                "is_managed": bool(u.get("restricted") or u.get("guest") or u.get("restrictedProfile")),
                "protected": bool(u.get("protected") or u.get("hasPassword")),
                # Plex parental-controls age tier (little_kid / older_kid / teen) when set —
                # drives age-appropriate playlist gating. Only the canonical name carries
                # the tier STRING (restrictedProfile above is a managed-or-not bool).
                "restriction_profile": (u.get("restrictionProfile")
                                        or u.get("restriction_profile") or None),
            })
        return out

    def _mint_tokens(self, roster: list) -> int:
        """Mint one per-user token per run. Owner reuses the account token; others
        switch. Returns the count of users skipped for a missing PIN."""
        pins = (self.config.get("plex", {}) if self.config else {}).get("pins", {}) or {}
        safe_map = self._ensure_safe_map(roster)
        pin_skipped = 0
        for u in roster:
            safe = safe_map[u["uuid"]]
            if u["is_admin"]:
                # The account-owner token already scopes to the owner's watchlist.
                self.user_tokens[safe] = self.plex_api.token
                continue
            pin = None
            if u["protected"]:
                pin = self._pin_for(pins, u["uuid"], u["title"])
                if not pin:
                    pin_skipped += 1
                    self.logger.log_info(
                        f"[PlexUsers] '{u['title']}' is PIN-protected and no PIN is configured "
                        f"— skipped (counted).")
                    continue
            resp = self.plex_api.switch_home_user(u["uuid"], pin=pin)
            token = self._extract_token(resp)
            if token:
                LoggerManager.register_secrets([token])   # scrub it from every later log line
                self.user_tokens[safe] = token
            else:
                self.logger.log_debug(f"[PlexUsers] could not mint a token for '{u['title']}' — skipped.")
        return pin_skipped

    @staticmethod
    def _pin_for(pins: dict, uuid: str, title: str):
        """Resolve a profile PIN from the config map. Accepts both the secure nested
        shape ``{title: {"pin": "1234"}}`` (onboarding) and a flat ``{title: "1234"}``."""
        raw = pins.get(uuid) or pins.get(title)
        if isinstance(raw, dict):
            return raw.get("pin")
        return raw

    @staticmethod
    def _extract_token(resp) -> str | None:
        """Pull the minted token from the /switch response. ``authenticationToken`` is
        the documented field (python-plexapi reads exactly that); ``authToken`` is the
        v2-JSON variant; also tolerate a nested ``user`` envelope. Checking all of them
        avoids silently dropping every managed user (→ owner-only) if the live shape
        differs from one assumed key."""
        if not isinstance(resp, dict):
            return None
        user = resp.get("user") if isinstance(resp.get("user"), dict) else {}
        return (resp.get("authToken") or resp.get("authenticationToken")
                or resp.get("authentication_token") or resp.get("token")
                or user.get("authToken") or user.get("authenticationToken"))

    def _tautulli_users(self) -> list:
        """Pull the Tautulli user list (Tautulli ran just before Plex). Best-effort —
        on failure the crosswalk degrades everyone to the household wildcard."""
        try:
            tau = self.registry.get("manager", "TautulliManager") if self.registry else None
            users = getattr(tau, "users", None)
            if users and hasattr(users, "get_all_users"):
                return users.get_all_users() or []
        except Exception as e:
            self.logger.log_debug(f"[PlexUsers] Tautulli user list unavailable: {e}")
        return []

    # ── crosswalk (pure) ──────────────────────────────────────────────────────
    @staticmethod
    def build_identity_map(home_roster: list, tautulli_users: list, rating_groups: dict) -> dict:
        """Plex-uuid → {tautulli_username, tautulli_user_id, rating_groups, matched_via}.

        Cascade (DESIGN §4.1), fail-CLOSED: Plex numeric id ↔ Tautulli user_id →
        email → title/username → unmatched (household wildcard). Email is used here
        for matching but is NOT written to the persisted map by the caller."""
        by_id, by_email, by_name = {}, {}, {}
        for t in (tautulli_users or []):
            if not isinstance(t, dict):
                continue
            uid = t.get("user_id")
            if uid is not None:
                by_id[str(uid)] = t
            em = (t.get("email") or "").strip().lower()
            if em:
                by_email[em] = t
            for nm in (t.get("username"), t.get("friendly_name")):
                if nm:
                    by_name[str(nm).strip().lower()] = t

        out = {}
        for u in (home_roster or []):
            match, via = None, None
            if u.get("id") is not None and str(u["id"]) in by_id:
                match, via = by_id[str(u["id"])], "plex_id"
            elif (u.get("email") or "").strip().lower() in by_email:
                match, via = by_email[(u["email"]).strip().lower()], "email"
            elif (u.get("title") or "").strip().lower() in by_name:
                match, via = by_name[(u["title"]).strip().lower()], "title"

            tautulli_username = (match or {}).get("username") if match else None
            groups = PlexUsersManager._groups_for(tautulli_username or u.get("title"), rating_groups)
            out[u["uuid"]] = {
                "tautulli_username": tautulli_username,
                "tautulli_user_id": (match or {}).get("user_id") if match else None,
                "rating_groups": groups,
                "matched_via": via or "unmatched",
            }
        return out

    @staticmethod
    def _groups_for(username, rating_groups: dict) -> list:
        """Groups a user belongs to. A memberless group is a household-wide wildcard
        counting every user (DESIGN §4.1 convention)."""
        groups = []
        for gname, gcfg in (rating_groups or {"household": {}}).items():
            members = (gcfg or {}).get("members") if isinstance(gcfg, dict) else None
            if not members:
                groups.append(gname)  # memberless = wildcard
            elif username and any(str(m).strip().lower() == str(username).strip().lower()
                                  for m in members):
                groups.append(gname)
        return groups or ["household"]

    # ── tracked set + persistence ──────────────────────────────────────────────
    def _build_tracked(self, roster: list, identity_map: dict):
        safe_map = self._ensure_safe_map(roster)
        for u in roster:
            safe = safe_map[u["uuid"]]
            if safe not in self.user_tokens:
                continue  # no usable token (pin-skipped / mint failed) → not tracked
            ident = identity_map.get(u["uuid"], {})
            self.tracked_users.append({
                "uuid": u["uuid"],
                "title": u["title"],
                "safe_user": safe,
                "is_admin": u["is_admin"],
                "restriction_profile": u.get("restriction_profile"),   # age tier (parental controls)
                "tautulli_username": ident.get("tautulli_username"),
                # stable Tautulli user_id — needed to fetch that user's watch history
                # (the playlist builder filters per-user "already watched" on it). Absent
                # for an unmatched Home profile → builder falls back to household.
                "tautulli_user_id": ident.get("tautulli_user_id"),
                "rating_groups": ident.get("rating_groups", ["household"]),
            })

    # ── per-user library allowlist (the NET-NEW library-scope dimension) ──────────
    def allowed_sections(self, tracked_user) -> set:
        """The Plex library SECTION KEYS a tracked user may see — the per-user library allowlist
        for content generation. Owner/admin → ALL sections. A non-admin is scoped to their plex.tv
        share grant (``allLibraries`` → all; an explicit section list → those) and FAILS CLOSED to
        an empty set when no grant resolves, UNLESS the operator sets
        ``plex.playlists.this_week_in_history.trust_home_managed`` (then all sections — still age-gated
        downstream). Resolved against the warm ``plex/sections`` index; persisted (section keys ONLY —
        never a token/email) under ``plex/user_sections``."""
        keys = self._all_section_keys()
        if not keys:
            return set()
        if tracked_user.get("is_admin"):
            return self._record_user_sections(tracked_user, set(keys))
        grant = (getattr(self, "_section_grants", {}) or {}).get(tracked_user.get("safe_user"))
        if grant == "ALL":
            return self._record_user_sections(tracked_user, set(keys))
        if isinstance(grant, set) and grant:
            return self._record_user_sections(tracked_user, {k for k in keys if str(k) in grant})
        if self._trust_home_managed():
            return self._record_user_sections(tracked_user, set(keys))
        return self._record_user_sections(tracked_user, set())          # fail-closed

    def _record_user_sections(self, tracked_user, sections: set) -> set:
        safe = tracked_user.get("safe_user")
        if safe:
            self._user_sections[safe] = sorted(str(s) for s in sections)
            if self.global_cache:
                try:
                    self.global_cache.set(_USER_SECTIONS_KEY, dict(self._user_sections))
                except Exception:
                    pass
        return sections

    def _all_section_keys(self) -> set:
        """All library section KEYS from the warm ``plex/sections`` index ({key: {...}})."""
        if not self.global_cache:
            return set()
        try:
            sections = self.global_cache.get(_SECTIONS_KEY) or {}
        except Exception:
            return set()
        return {str(k) for k in sections} if isinstance(sections, dict) else set()

    def _twih_cfg(self) -> dict:
        pl = ((self.config.get("plex", {}) if self.config else {}) or {}).get("playlists", {}) or {}
        return (pl.get("this_week_in_history", {}) or {})

    def _trust_home_managed(self) -> bool:
        return bool(self._twih_cfg().get("trust_home_managed", False))

    def _resolve_section_grants(self, roster) -> dict:
        """Best-effort per-user library grants from plex.tv ``shared_servers``, keyed by safe_user:
        ``"ALL"`` (allLibraries) or a set of granted section-id strings. ``{}`` when the shelf feature
        is off, the call yields nothing, or no user matches → callers then fail closed. Owner/admin are
        not scoped here (they always get all). One external call, gated on the feature flag so a
        disabled shelf adds NO plex.tv traffic."""
        if not self._twih_cfg().get("enabled", False):
            return {}
        api = self.plex_api
        if api is None or not hasattr(api, "get_shared_servers"):
            return {}
        try:
            raw = api.get_shared_servers(fallback=None)
        except Exception:
            return {}
        entries = self._shared_entries(raw, getattr(api, "get_machine_id", lambda: None)())
        if not entries:
            return {}
        by_ident: dict = {}
        for e in entries:
            grant = self._entry_grant(e)
            for tok in self._entry_idents(e):
                by_ident.setdefault(tok, grant)
        safe_map = self._ensure_safe_map(roster)
        out: dict = {}
        for u in roster:
            if u.get("is_admin"):
                continue
            for tok in self._user_idents(u):
                if tok in by_ident:
                    out[safe_map[u["uuid"]]] = by_ident[tok]
                    break
        return out

    @staticmethod
    def _shared_entries(raw, machine_id=None) -> list:
        """Normalize the shared_servers payload (list / ``sharedServers`` / MediaContainer) to a list
        of share dicts, restricted to THIS server when a ``machineIdentifier`` is present."""
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = (raw.get("sharedServers") or raw.get("SharedServer")
                     or (raw.get("MediaContainer") or {}).get("SharedServer") or [])
        else:
            items = []
        out = []
        for e in items:
            if not isinstance(e, dict):
                continue
            mid = e.get("machineIdentifier") or e.get("machineId")
            if machine_id and mid and str(mid) != str(machine_id):
                continue                                 # a share to a DIFFERENT server
            out.append(e)
        return out

    @staticmethod
    def _entry_grant(e):
        """A share entry → ``"ALL"`` (allLibraries) or the set of granted section-id strings."""
        if e.get("allLibraries") or e.get("allLibrariesAccess"):
            return "ALL"
        ids: set = set()
        for sid in (e.get("librarySectionIDs") or e.get("librarySectionIds") or []):
            ids.add(str(sid))
        sections = e.get("sections") or e.get("Section") or []
        if isinstance(sections, dict):
            sections = [sections]
        for s in sections:
            if not isinstance(s, dict):
                continue
            if not (s.get("shared", True) in (True, 1, "1", "true")):
                continue
            for f in ("key", "id", "librarySectionID", "sectionKey"):
                if s.get(f) is not None:
                    ids.add(str(s.get(f)))
        return ids

    @staticmethod
    def _entry_idents(e) -> set:
        """Every identity token a share entry exposes (lowercased), for matching to a Home user."""
        user = e.get("user") if isinstance(e.get("user"), dict) else {}
        toks = [e.get("email"), e.get("invitedEmail"), e.get("username"), e.get("userID"),
                e.get("userId"), e.get("id"), user.get("email"), user.get("id"),
                user.get("username"), user.get("uuid")]
        return {str(t).strip().lower() for t in toks if t not in (None, "")}

    @staticmethod
    def _user_idents(u) -> set:
        """Every identity token a Home user exposes (lowercased), for matching to a share entry."""
        toks = [u.get("email"), u.get("id"), u.get("uuid"), u.get("title")]
        return {str(t).strip().lower() for t in toks if t not in (None, "")}

    def _degrade_owner_only(self):
        """Scope-fail / no-Home fallback: record the account token as the single
        household ("owner") user. NOTE: on a HARD scope-fail the watchlist pass is
        gated off (account_scope_ok is False), so the Discover union is NOT fetched —
        this records the owner-only roster only. Both the public roster and the
        identity map are reset so the two on-disk artifacts stay consistent (a prior
        run's populated identity_map must not linger past a degrade)."""
        safe = "owner"
        self.user_tokens[safe] = self.plex_api.token if self.plex_api else ""
        self._safe_by_uuid = {"owner": safe}
        self.tracked_users = [{
            "uuid": "owner", "title": "owner", "safe_user": safe, "is_admin": True,
            "tautulli_username": None, "rating_groups": ["household"],
        }]
        if self.global_cache:
            try:
                self.global_cache.set(_USERS_KEY, [])
                self.global_cache.set(_IDENTITY_KEY, {})
            except Exception:
                pass

    def _persist(self, roster: list, identity_map: dict):
        """Write the PII-minimized roster + identity map. NO email, NO token on disk."""
        if not self.global_cache:
            return
        safe_map = self._ensure_safe_map(roster)
        public_roster = [{
            "uuid": u["uuid"],
            "title": u["title"],
            "is_admin": u["is_admin"],
            "is_managed": u["is_managed"],
            "protected": u["protected"],
            "token_scope_ok": safe_map[u["uuid"]] in self.user_tokens,
        } for u in roster]
        # identity map: keep the join fields; email was only used in-memory for matching.
        public_identity = {
            uuid: {
                "tautulli_username": v.get("tautulli_username"),
                "tautulli_user_id": v.get("tautulli_user_id"),
                "rating_groups": v.get("rating_groups"),
                "matched_via": v.get("matched_via"),
                "safe_key": safe_map.get(uuid),
            }
            for uuid, v in identity_map.items()
        }
        try:
            self.global_cache.set(_USERS_KEY, public_roster)
            self.global_cache.set(_IDENTITY_KEY, public_identity)
        except Exception as e:
            self.logger.log_warning(f"[PlexUsers] persist failed: {e}")

    # ── helpers ────────────────────────────────────────────────────────────────
    def _safe(self, user: dict) -> str:
        """Sanitize a Plex display name for a cache-path segment (externally
        controlled → route through the traversal-rejecting sanitizer). Falls back to
        the uuid when the name sanitizes to empty/./.. ."""
        for candidate in (user.get("title"), user.get("uuid")):
            if not candidate:
                continue
            try:
                return _sanitize_part(str(candidate))
            except ValueError:
                continue
        return "unknown"

    def _build_safe_map(self, roster: list) -> dict:
        """{uuid: safe_user} guaranteeing each tracked user a DISTINCT cache/token key.
        Base is the sanitized title (human-readable); on collision (two titles that
        sanitize identically) the loser is disambiguated with a uuid fragment, so
        attribution can never cross users (fail-CLOSED)."""
        out, used = {}, set()
        for u in roster:
            base = self._safe(u)
            safe, n = base, 1
            while safe in used:
                frag = str(u.get("uuid") or "")[:8]
                suffix = frag if n == 1 and frag else f"{frag}-{n}"
                try:
                    safe = _sanitize_part(f"{base}-{suffix}")
                except ValueError:
                    safe = f"{base}-{n}"
                n += 1
            used.add(safe)
            out[u["uuid"]] = safe
        return out

    def _ensure_safe_map(self, roster: list) -> dict:
        """The per-run safe_user map, built lazily so standalone helper calls (tests)
        work without run() having seeded it."""
        if not getattr(self, "_safe_by_uuid", None):
            self._safe_by_uuid = self._build_safe_map(roster)
        return self._safe_by_uuid

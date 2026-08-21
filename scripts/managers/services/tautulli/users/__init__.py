from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.affinity.account_links import (
    account_keys,
    malformed_groups,
    parse_links,
)
from scripts.managers.machine_learning.affinity.genre_affinity import (
    aggregate_affinity,
    per_user_affinity,
)

# The Plex users pass persists its Home<->Tautulli crosswalk here every run
# ({uuid: {tautulli_username, tautulli_user_id, rating_groups, matched_via, safe_key}}).
# It is the FAMILY definition for household-affinity scoping: Plex Home membership,
# resolved to the stable Tautulli user_id the history rows already carry. Read, never
# written, from this manager. NOTE the run order: this manager aggregates affinity
# BEFORE PlexUsersManager runs, so the map read here is the PREVIOUS run's -- on the
# first run after enabling family_only the map may be absent and scoping degrades
# loudly to legacy all-users for that one run.
_FAMILY_IDENTITY_KEY = "plex/identity_map"

# Top-level config: groups of Tautulli accounts that are the SAME PERSON, graded as
# one viewer. See TautulliUsersManager._account_links for the shape and the reason it
# is deliberately NOT applied to family/household scoping.
_LINK_CFG_KEY = "account_links"


class TautulliUsersManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.tautulli_api = kwargs.get("tautulli_api")

    def get_all_users(self) -> list:
        """Return list of user dicts from Tautulli."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_users()
        users = ((resp or {}).get("response") or {}).get("data", []) or []
        self.logger.log_info(f"[TautulliUsers] {len(users)} users retrieved.")
        return users

    def get_user_watch_time_stats(self, user_id) -> list:
        """Real-time watch time stats for a single user."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_user_watch_time_stats(user_id=user_id)
        return ((resp or {}).get("response") or {}).get("data", []) or []

    def get_user_player_stats(self, user_id) -> list:
        """Real-time player stats for a single user."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_user_player_stats(user_id=user_id)
        return ((resp or {}).get("response") or {}).get("data", []) or []

    def _affinity_half_life(self):
        """Optional recency half-life (days) for affinity decay — config
        ``scoring.affinity_half_life_days``. None/0 = legacy raw counts (default).

        ⚠️  SETTING THIS IS AN AXIS TRANSLATION — IT IS NOT A LOCAL CHANGE.

        Decay reweights every affinity contribution by ``exp(-age_days/half_life)``,
        which shifts the whole ``watchability_score`` distribution downward (older
        watches stop counting at full weight). ``likelihood`` consumes that score at
        GAIN 1.0, so every boundary calibrated against the current distribution moves
        out from under itself the moment this is non-zero.

        The last time the axis translated — Group D v2 replacing a near-constant +12
        bonus with a transcode-risk penalty — it cost a coordinated re-anchor:

          * the DELETE family 20 -> 17 (``tv_delete_ceiling``, ``series_demote``, and
            the movie delete floor) — see machine_learning/thresholds/registry.py;
          * ``likelihood.untouched_base`` 12 -> 25, after untouched titles reaching
            1080p collapsed 456 -> 8 (-98.2%);
          * the monitor threshold deliberately LEFT at 35, because it is crossed only
            by file-owning series and stubs never reach it either way (the asymmetry
            is documented in sonarr/series/quality.py — do not "fix" it).

        That collapse was caught by manual measurement, NOT by any check: there is
        still no axis-drift detector (GLD-LIK-01). So before enabling this:

          1. re-measure the score distribution with decay on;
          2. re-anchor the delete family and ``untouched_base`` against it;
          3. prefer ``thresholds`` percentile mode where available — it is immune to
             translation (measured 0.0% / +4.9% drift vs -98.2% for absolute cutoffs).

        Evidence for the half-life itself is thin (n~931, n_pos<<100), so an
        aggressive value discards most of the signal it is meant to weight. See D32.
        """
        return ((self.config or {}).get("scoring", {}) or {}).get("affinity_half_life_days")

    def _compute_affinity_from_entries(
        self, history_entries: list, metadata_index: dict
    ) -> dict:
        """Core affinity computation. Delegates to the brain
        (machine_learning.affinity.genre_affinity.aggregate_affinity) — kept as a
        thin method for internal / back-compat callers. Pure."""
        return aggregate_affinity(history_entries, metadata_index,
                                  half_life_days=self._affinity_half_life())

    def _family_ids(self) -> tuple:
        """``(family_ids, reason)`` -- the Plex HOME Tautulli id set, read from the
        crosswalk the Plex users pass persisted last run. ``reason`` is ``""`` on
        success, else a short machine cause (``"unreadable: <err>"`` / ``"no-ids"``)
        for the CALLER to render.

        This helper never logs, deliberately: two consumers read it every run
        (household scoping and the per-account grading table) and a warning in here
        would fire twice for one condition.

        ONE source of truth on purpose (P-E). The per-account table classifies
        accounts with the EXACT id set that scoped the aggregate, so a row can never
        claim an account is family while the aggregate treated it as an outsider.
        """
        try:
            raw = self.global_cache.get(_FAMILY_IDENTITY_KEY) if self.global_cache else None
        except Exception as e:
            return set(), f"unreadable: {e}"
        ids = set()
        if isinstance(raw, dict):
            for v in raw.values():
                uid = v.get("tautulli_user_id") if isinstance(v, dict) else None
                if uid is not None:
                    ids.add(str(uid))
        return ids, ("" if ids else "no-ids")

    def _family_scope(self, history_entries: list) -> tuple:
        """``(entries, note)`` -- household-affinity scoping (``household_affinity.family_only``).

        OFF (default): entries returned untouched, note empty -- byte-identical legacy.
        ON: keep only plays whose ``user_id`` belongs to a Plex HOME member, per the
        identity map the Plex users pass persisted last run. Non-family accounts (shared
        friends streaming remotely) stop steering the HOUSEHOLD genre/actor/director
        maps -- their per-user matrices are untouched: ``compute_per_user_genre_affinity``
        receives the full history independently, so an outsider keeps their own grading
        while losing their vote on the family's.

        Fail direction -- OPEN, loudly. If the map is absent (first run after enabling;
        this cache returns {} for missing keys), unreadable, or carries no Tautulli ids,
        scoping is skipped for the run with a warning. The household affinity feeds the
        acquisition genre signal, playlists and watch-likelihood system-wide; an empty
        aggregate would be far worse than one run of the drift this feature removes.

        A play with NO ``user_id`` is unknown-owner and is KEPT (counted in the note):
        family plays dominate this library and Tautulli history reliably carries the id,
        so dropping unknowns would starve the household of real family plays to guard
        against a rare leak -- the wrong trade here, and stated rather than implied."""
        cfg = ((self.config or {}).get("household_affinity", {}) or {})
        if not cfg.get("family_only"):
            return history_entries, ""
        family_ids, reason = self._family_ids()
        if reason.startswith("unreadable"):
            self.logger.log_warning(
                f"[TautulliUsers] household affinity UNSCOPED this run - identity map "
                f"unreadable ({reason.split(': ', 1)[1]}); all accounts counted.")
            return history_entries, ""
        if not family_ids:
            self.logger.log_warning(
                "[TautulliUsers] household affinity UNSCOPED this run - family_only is on "
                "but the Plex identity map carries no Tautulli ids yet (first run after "
                "enabling, or the Plex users pass has not persisted). All accounts "
                "counted; scoping engages next run.")
            return history_entries, ""
        kept, excluded_plays, excluded_users, unknown = [], 0, set(), 0
        for e in (history_entries or []):
            uid = e.get("user_id") if isinstance(e, dict) else None
            if uid is None:
                unknown += 1
                kept.append(e)
            elif str(uid) in family_ids:
                kept.append(e)
            else:
                excluded_plays += 1
                excluded_users.add(str(e.get("user") or uid))
        note = (f" [family-scoped: {len(kept)}/{len(history_entries or [])} plays from "
                f"{len(family_ids)} family id(s); excluded {excluded_plays} play(s) "
                f"across {len(excluded_users)} non-family account(s)"
                + (f"; {unknown} unknown-owner play(s) kept" if unknown else "") + "]")
        return kept, note

    def compute_genre_affinity(self, history_entries: list, metadata_index: dict) -> dict:
        """Household genre/actor/director affinity from pre-fetched history and
        metadata. The COMPUTATION lives in the brain (genre_affinity.aggregate_affinity);
        the service keeps FETCH + this summary log + the cache-write (TautulliManager).
        When ``household_affinity.family_only`` is on, the history is scoped to Plex
        HOME members first (see ``_family_scope``) -- per-user matrices are unaffected."""
        scoped, note = self._family_scope(history_entries)
        result = aggregate_affinity(scoped, metadata_index,
                                    half_life_days=self._affinity_half_life())
        self.logger.log_info(
            f"[TautulliUsers] Genre affinity: "
            f"{len(result.get('genres', {}))} genres, "
            f"{len(result.get('actors', {}))} actors, "
            f"{len(result.get('directors', {}))} directors." + note
        )
        return result

    def _account_links(self) -> tuple:
        """``(alias, groups)`` for ``account_links`` — delegates to the pure brain module
        (``machine_learning/affinity/account_links``), which the PLAYLIST builders also
        read for the watched-set half of the same join (``GLD-TAUT-16``).

        One parser on purpose. A builder that disagreed with this grader about who is
        linked would merge one half of a viewer and not the other, producing exactly the
        half-joined state the join exists to remove — and it would look like a data
        problem, not a config-parsing one.

        DELIBERATELY NOT applied to household/family scoping. Family means Plex HOME
        membership, and letting a link drag a non-Home account into the family aggregate
        would widen the household maps through a knob whose stated purpose is per-viewer
        grading -- exactly the leak ``family_only`` exists to close.
        """
        return parse_links(self.config or {})

    @staticmethod
    def _user_keys(user: dict) -> set:
        """Every key an ``account_links`` member could name this account by — the pure
        module owns the field list so a roster field added for one consumer cannot go
        unrecognised by the other."""
        return account_keys(user)

    def _link_history(self, history_entries: list, user_list: list) -> tuple:
        """``(entries, note)`` -- rewrite each linked account's plays onto its group
        PRIMARY so the brain groups them into ONE matrix.

        Returns the ORIGINAL list untouched when no links are configured, so the
        default is byte-identical. Entries are shallow-copied before rewriting: the
        caller's list is shared with ``compute_genre_affinity`` (household), and
        mutating ``user_id`` in place would silently re-route plays through the family
        filter -- the household aggregate must keep seeing real account ids.
        """
        alias, groups = self._account_links()
        # Warn ONCE per run, here rather than inside `_account_links` -- that is called
        # three times a run (scoping, fan-out, the gradings table), and the previous
        # inline parser warned from all three.
        for _bad in malformed_groups(self.config or {}):
            self.logger.log_warning(f"[TautulliUsers] account_links: {_bad}")
        if not alias:
            return history_entries, ""
        # Resolve each group's primary to a concrete (user_id, username) from the
        # roster. A group whose primary is not a real account is dropped with a
        # warning rather than silently aliasing plays onto a name nothing reads.
        primary_ident, member_keys = {}, {}
        for user in (user_list or []):
            for k in self._user_keys(user):
                if alias.get(k):
                    member_keys[k] = user
        for grp in groups:
            primary = grp[0]
            user = member_keys.get(primary)
            if user is None:
                self.logger.log_warning(
                    f"[TautulliUsers] account_links: primary '{primary}' matches no "
                    f"Tautulli account - that group is IGNORED this run.")
                continue
            primary_ident[primary] = (user.get("user_id"), user.get("username"))

        moved, out = 0, []
        for e in (history_entries or []):
            if not isinstance(e, dict):
                out.append(e)
                continue
            k_id = str(e.get("user_id")).strip().lower() if e.get("user_id") is not None else ""
            k_nm = str(e.get("user") or "").strip().lower()
            primary = alias.get(k_id) or alias.get(k_nm)
            ident = primary_ident.get(primary) if primary else None
            if ident is None or (k_id and k_id == str(ident[0]).strip().lower()):
                out.append(e)          # unlinked, or already the primary
                continue
            merged = dict(e)
            merged["user_id"], merged["user"] = ident[0], ident[1]
            out.append(merged)
            moved += 1
        note = (f" [account-links: {len(primary_ident)} group(s), "
                f"{moved} play(s) merged onto a primary]") if moved else ""
        return out, note

    def _fan_out_links(self, result: dict, user_list: list) -> dict:
        """Give every member of a link group the PRIMARY's merged matrix.

        Without this the join would be half-done in the WORST direction: the
        primary would hold the merged grading and the secondary, whose plays were
        just rewritten away, would match nothing, drop out of the result entirely,
        and fall back to the household defaults -- so linking two accounts would
        make one of them WORSE than leaving them apart. Fanning out is what makes
        both profiles render the same recommendations.

        Each member gets its own COPY. Sharing one dict across usernames would let
        any consumer that annotates a matrix in place silently write through to the
        other account, which is the kind of aliasing that is invisible until it is
        a bug in a different package.
        """
        alias, groups = self._account_links()
        if not alias:
            return result
        by_key = {}
        for user in (user_list or []):
            uname = str(user.get("username") or user.get("user_id") or "")
            for k in self._user_keys(user):
                by_key[k] = uname
        for grp in groups:
            primary_name = by_key.get(grp[0])
            merged = result.get(primary_name) if primary_name else None
            if merged is None:
                continue      # primary has no history this window; nothing to share
            for mkey in grp[1:]:
                member_name = by_key.get(mkey)
                if member_name and member_name != primary_name:
                    result[member_name] = {k: (dict(v) if isinstance(v, dict) else v)
                                           for k, v in merged.items()}
        return result

    def compute_per_user_genre_affinity(
        self,
        history_entries: list,
        metadata_index: dict,
        user_list: list,
    ) -> dict:
        """Per-user affinity matrices — one signal per Tautulli account. The
        grouping + computation live in the brain
        (genre_affinity.per_user_affinity); the service keeps the logging.

        Returns a dict keyed by username (users with zero matching history entries
        are omitted)::

            {"Trizzd": {"genres": {...}, "actors": {...}, ...}, "Aiden": {...}}
        """
        linked, link_note = self._link_history(history_entries, user_list)
        result = per_user_affinity(linked, metadata_index, user_list,
                                   half_life_days=self._affinity_half_life())
        result = self._fan_out_links(result, user_list)
        for username, affinity in result.items():
            self.logger.log_debug(
                f"[TautulliUsers] Per-user affinity for '{username}': "
                f"{len(affinity.get('genres', {}))} genres."
            )
        self.logger.log_info(
            f"[TautulliUsers] Per-user affinity computed for {len(result)} user(s)."
            + link_note
        )
        self._log_per_user_affinity_table(result, user_list)
        return result

    @staticmethod
    def _top_genres(affinity: dict, n: int = 3) -> str:
        """Top-n genre names with weights, highest first, as one plain-ASCII cell.
        A graded account with an EMPTY genre map renders 'none' -- it was scored and
        nothing matched, which is a different fact from never having been scored."""
        genres = (affinity or {}).get("genres") or {}
        if not genres:
            return "none"
        top = sorted(genres.items(), key=lambda kv: -float(kv[1] or 0.0))[:n]
        return ", ".join(f"{name} {float(w):.2f}" for name, w in top)

    def _log_per_user_affinity_table(self, result: dict, user_list: list) -> None:
        """One row per Tautulli account: what that account was graded, and whether its
        plays also STEER the household maps.

        The affinity summary line asserts that outsiders keep their own grading while
        losing their vote on the family's; before this table that claim was invisible
        -- the run printed a household total and a bare COUNT of per-user matrices, so
        an outsider being graded and an outsider being silently dropped looked
        identical in the log.

        Classification joins on the Tautulli ``user_id`` against ``_family_ids`` -- the
        SAME set that scoped the aggregate, never the display name -- so a row cannot
        contradict the household numbers printed above it. When the id set is
        unavailable (or ``family_only`` is off) the Scope column renders '-' rather
        than guessing: mislabelling a family member as an outsider, in a table whose
        entire purpose is to evidence correct classification, would be worse than
        showing nothing.

        P-C: the brain OMITS zero-entry users from ``result``, so absence means 'no
        history in this window' and is rendered as such -- kept distinct from 'graded,
        nothing matched' (``none``) and from an account never offered to the grader at
        all (absent from ``user_list``, hence absent here).
        """
        if not user_list:
            return
        family_only = bool(
            ((self.config or {}).get("household_affinity", {}) or {}).get("family_only"))
        family_ids, reason = self._family_ids() if family_only else (set(), "off")
        classify = bool(family_ids)
        # Link membership, so two accounts showing identical numbers reads as the
        # declared join it is, not as a coincidence the operator has to guess at.
        alias, _groups = self._account_links()
        link_label = {}
        if alias:
            for user in user_list:
                for k in self._user_keys(user):
                    if k in alias:
                        primary = alias[k]
                        link_label[str(user.get("username") or user.get("user_id") or "")] = (
                            "primary" if k == primary else f"-> {primary}")
                        break

        rows, outside_graded = [], 0
        for user in user_list:
            username = str(user.get("username") or user.get("user_id") or "")
            if not username:
                continue
            uid = str(user.get("user_id") or "")
            if not classify:
                scope, is_out = "-", False
            elif uid and uid in family_ids:
                scope, is_out = "family", False
            else:
                scope, is_out = "outside", True
            affinity = result.get(username)
            link = link_label.get(username, "-")
            if affinity is None:
                rows.append([username[:22], scope, link, "-", "-", "-", "no history in window"])
                continue
            outside_graded += 1 if is_out else 0
            rows.append([
                username[:22], scope, link,
                len(affinity.get("genres", {}) or {}),
                len(affinity.get("actors", {}) or {}),
                len(affinity.get("directors", {}) or {}),
                self._top_genres(affinity),
            ])
        if not rows:
            return

        # family first, then outsiders, then unclassified; graded before ungraded,
        # richest grading first -- so the rows this table exists to evidence
        # (outsiders WITH a grading) sit together and are easy to eyeball.
        order = {"family": 0, "outside": 1, "-": 2}
        rows.sort(key=lambda r: (order.get(r[1], 3),
                                 1 if r[3] == "-" else 0,
                                 -(r[3] if isinstance(r[3], int) else 0)))

        if not family_only:
            caption = ("Per-account gradings. household_affinity.family_only is OFF - every "
                       "account below also counts toward the household maps.")
        elif classify:
            caption = (f"Per-account gradings. 'family' accounts steer the household maps; "
                       f"'outside' accounts are graded here but excluded from them "
                       f"({outside_graded} outside account(s) graded this run).")
        else:
            caption = ("Per-account gradings. family_only is ON but the Plex identity map is "
                       f"unavailable this run ({reason}) - scoping is INACTIVE and every "
                       "account below counted toward the household maps.")

        self.logger.log_table(
            ["Account", "Scope", "Link", "Genres", "Actors", "Directors", "Top genres"],
            rows,
            title="[TautulliUsers] per-account affinity gradings",
            caption=caption,
        )

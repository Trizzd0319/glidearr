
"""
plex/playlists/smart_shelves.py — self-maintaining LABEL-driven Plex shelves.
================================================================================
Account-wide shelves that Plex keeps current on its own. glidearr decides membership
and writes a LABEL; a SMART playlist filters on that label and re-evaluates itself on
every view. No per-run membership diff, no anchor map, no orphan sweep, no per-user
tokens — the three sources of complexity in the per-user write-back path.

    glidearr computes membership
      -> label the items            (PUT /library/sections/{sec}/all?type=N&id=RK)
        -> Plex indexes the label   (GET /library/sections/{sec}/label?type=N)
          -> smart playlist filters (POST /playlists?smart=1&uri=...&label={id})

TWO FAMILIES, deliberately independent (no shared state, no dual requirement):
  * ANNIVERSARY  — released/aired this ISO week in ANY prior year ("this week in
                   history"). Rotates weekly as the week number advances.
  * FRESH        — released/aired within the last N days (default 30). Note this is
                   the RELEASE date, not when the file was added: a 2019 film grabbed
                   yesterday is not "fresh".

WHY LABELS AND NOT A PLEX DATE FILTER
Plex can filter ``originallyAvailableAt`` itself, which would need no labels at all —
but that date is REGION-DEPENDENT. Dragon Ball Z is 1989 in Japan and 1996 in the US,
and which one a library holds depends on the agent and language settings, so the same
filter means different things for different titles. Worse, Plex has no day-of-year
operator at all, so "this week in any year" is not expressible as a filter under any
circumstances. Labels move both decisions into glidearr, which already holds
authoritative dates from Sonarr/Radarr, and reduce Plex's job to ``label == X``.
One mechanism, one place to be wrong.

VERIFIED LIVE before this module was written (see PlexAPI's label section):
  * episode labels (type=4) work and create NO field lock — so a weekly rotation
    across hundreds of episodes never freezes metadata against agent refresh;
  * an episode label is only visible in the section vocabulary under ``?type=4``;
  * Plex returns a ``fastKey`` — the filter query, pre-built;
  * ``contentRating`` filters a smart playlist correctly (an R-rated film present in
    an unfiltered shelf, absent from a ``G,TV-Y,TV-G`` one), so age-gating stays
    available even though these two families are account-wide;
  * on this library a single ISO week yields ~33 qualifying movies on the standard
    instance and 1 on the 4K instance — which is exactly why the minimum below exists.

MINIMUM SIZE, BOTH WAYS. Mirrors Kometa's ``minimum_items``: below the floor the shelf
is not built, and an EXISTING one is torn down. A three-item "Anniversary Picks" reads
as broken rather than sparse, and a shelf that qualified last rotation but not this one
must disappear rather than linger. Plex cannot help here — a smart playlist counts
itself at view time and has no notion of "too few to bother" — so the floor is applied
by this module at label time.

DRY-RUN. Every write (labels and playlists) is gated on ``dry_run``; a preview run logs
what it would label and build and touches nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_MOVIE_INVENTORY_KEY = "plex/movies/owned_inventory"    # tmdb(str) -> {rating_key, title, year}
_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"     # "tvdb:s:e" -> {rating_key, series_title, title}
_SECTIONS_KEY = "plex/sections"                         # section id -> {title, type, locations}
# The SURFACED set, plan-shaped, per family+section. Written every run — including dry_run —
# so the engagement loop has the artefact it measures against.
#
# WHY THIS EXISTS: making a shelf self-maintaining removed the thing that made it
# MEASURABLE. ``machine_learning/playlists/engagement.update_engagement`` works by diffing
# LAST RUN'S CACHED PLAN against what a profile actually watched, producing
# surfaced/engaged/skipped/dormant — which is what ``saga_boost`` then uses to de-rate a
# shelf someone keeps walking past. A smart playlist has no cached plan (that is the whole
# point: Plex resolves membership itself), so without this key anything moved onto the smart
# mechanism would become invisible to that loop. Recording the surfaced set restores it:
# self-maintaining in Plex, still measured here.
_SMART_PLAN_KEY = "plex/playlists/smart_plan"           # + /{family}/{section}

# Plex library search types. 1=movie, 2=show, 4=episode. An anniversary/fresh SHOW shelf
# labels and surfaces EPISODES (type 4), never the series: "the episode that aired this
# week" is the unit, and a series-level label would surface a whole show for one episode.
_TYPE_MOVIE = 1
_TYPE_EPISODE = 4
# Plex search type for a SHOW. Needed because a smart COLLECTION in a TV section holds
# shows, not episodes -- Plex has no episode-level collection -- so the collection twin of
# an episode shelf filters on type 2 while its playlist filters on type 4.
_TYPE_SHOW = 2
# Plex's search type for a COLLECTION itself (18), needed to edit a collection's own
# metadata -- titleSort included -- through the section-edit endpoint.
_TYPE_COLLECTION = 18

# Sort prefix. Plex orders a library's collections and the playlist list alphabetically by
# titleSort, so a shelf named "Tonight" lands under T among every other collection. "!!"
# (ASCII 0x21, twice) sorts ahead of every digit and letter, and ahead of the single "!"
# the per-user playlists use -- so the account-wide shelves sit at the very top, then the
# personal lists, then everything else. The VISIBLE title is untouched; only titleSort
# carries the prefix, exactly as writeback._SORT_PREFIX does for the per-user lists.
_SHELF_SORT_PREFIX = "!!"

# Order WITHIN the pinned block, lowest first. Encoded as a digit after the prefix so the
# shelves keep a deliberate reading order rather than an alphabetical accident: what to
# watch right now, then what just arrived, then the retrospective.
_SHELF_SORT_ORDER = {"fresh": 1, "anniversary": 2}

# Appended to a shelf's filter so it only ever surfaces things NOT yet watched. Plex
# evaluates this at view time per PROFILE, so one shelf reads correctly for everyone --
# which a membership list computed by glidearr could never do.
_UNWATCHED_FILTER = "unwatched=1"

# Label vocabulary. Namespaced so an operator can see at a glance which labels glidearr
# owns, and so a read-modify-write can tell ITS labels from Kometa's (which this library
# uses heavily — 'Kometa', 'Universe Collections', 'Streaming', 'Hulu Movies' …).
_LABEL_PREFIX = "Glidearr: "
_FAMILIES = {
    "anniversary": {
        "label": f"{_LABEL_PREFIX}Anniversary",
        "title": "This Week In History",
        "blurb": "this week in history",
    },
    "fresh": {
        "label": f"{_LABEL_PREFIX}Fresh Arrivals",
        "title": "Just Landed",
        "blurb": "just landed",
    },
}

# THESE TITLES MUST NOT COLLIDE WITH THE PER-USER FAMILIES in
# ``playlists/writeback._all_families``. They did: the shelves were called
# "Anniversary Picks" and "Fresh Arrivals", which are also two of the per-user
# playlist names, so a live run produced FOUR things called Anniversary Picks and
# THREE called Fresh Arrivals in one list - different content, different art, no
# way for the household to tell them apart.
#
# The SHELVES were renamed rather than the playlists because these are
# account-wide library shelves and can carry a library-shelf name, while the
# per-user lists are personal and their names are what the household already
# knows. Renaming leaves the OLD shelves orphaned in Plex under their previous
# titles - delete those by hand once, they will not be re-created.

# TONIGHT IS NOT HERE, deliberately. It was briefly a family in this module,
# selected by RUNTIME off ``owned_inventory[*].duration_ms``. Both halves of that
# were wrong and it has been removed rather than left disabled:
#
#   * the DATA never existed. Plex returns ``duration`` on /library/metadata/{rk}
#     but NOT on the /library/sections/{key}/all listing this scan uses, so the
#     field was None for all 14,574 episodes and the shelf selected nothing.
#   * the RULE was superseded. Tonight is defined by WEEKDAY HABIT - "Raina
#     watches Rick and Morty on Tuesdays" - which lives in
#     machine_learning/playlists/habits.py. Runtime is not part of it.
#   * the SHAPE was wrong. A habit is per-PROFILE, and everything in this module
#     is account-wide and label-driven. One shelf cannot hold a different five
#     shows for each member, so Tonight belongs in the per-user write-back path
#     alongside Up Next, not here.
#
# Leaving a disabled copy would have meant two definitions of Tonight in the
# tree with the dead one wired up, which is how the wrong one wins.

_DEFAULT_FRESH_DAYS = 30
_DEFAULT_MIN_ITEMS = 5
# Per-family floor defaults. These differ because the two families draw from completely
# different pools and a single number cannot serve both:
#
#   ANNIVERSARY draws on the WHOLE BACK CATALOGUE — every title ever released, filtered to
#     one ISO week. On this library that is ~33 movies a week, so a floor of 5 only ever
#     fires on a genuinely thin section (the 4K library returned 1 this week) where a shelf
#     would look broken.
#   FRESH draws only on what was RELEASED in the last 30 days. Even a busy month rarely
#     yields five theatrical releases the household owns; measured live, this week it was 3.
#     A floor of 5 there would hide the shelf almost permanently, which is the opposite of
#     what a "just landed" shelf is for.
#
# 1, not 0: a floor of 0 would build an EMPTY shelf, since the check is ``count < floor``.
_FAMILY_MIN_ITEMS = {"anniversary": _DEFAULT_MIN_ITEMS, "fresh": 1}
_METADATA_BATCH = 50        # ratingKeys per /library/metadata/{rk,rk,…} call

#: What this module CREATED, published for whatever needs to act on it later:
#: ``{rating_key: {title, section, family, slug}}``.
#:
#: EXISTS BECAUSE TITLE MATCHING NEVER WORKED. CollectionPosterManager kept its own
#: hand-written map of twelve titles and looked collections up by name. Ten of those
#: were per-user PLAYLIST families (Up Next, Tonight, Hidden Gems ...) that are never
#: collections at all, and the two that could be were created here as
#: "Just Landed — Movies", never the bare "Fresh Arrivals" it searched for. The
#: result was `12 collection(s) not found` on every run since the feature shipped -
#: a lookup that could not succeed, reported as if the collections were merely absent.
#:
#: The ratingKey is in hand at creation. Publishing it removes the name from the
#: contract entirely, so a rename here can never again silently orphan the art.
_MANAGED_KEY = "plex/collections/managed"

#: (family, medium) -> poster slug. Explicit because one family wears DIFFERENT art
#: per medium: the anniversary shelf is "Anniversary Picks" over movies and "On This
#: Week" over shows, which is a real editorial distinction, not a naming accident.
_FAMILY_POSTER = {
    ("anniversary", "movie"): "shelf_this_week",
    ("anniversary", "episode"): "shelf_this_week",
    ("fresh", "movie"): "shelf_just_landed",
    ("fresh", "episode"): "shelf_just_landed",
}


class PlexSmartShelvesManager(BaseManager):
    """Labels qualifying items and keeps one smart playlist per section per family."""

    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")

    def prepare(self):
        pass

    # ── config ────────────────────────────────────────────────────────────────
    def _cfg(self) -> dict:
        pl = ((self.config.get("plex", {}) if self.config else {}) or {}).get("playlists", {}) or {}
        return pl.get("smart_shelves", {}) or {}

    def _enabled(self) -> bool:
        """Default OFF. The existing managed ``Anniversary Picks`` / ``Fresh Arrivals``
        playlists keep working untouched until an operator opts in, so both can run side by
        side for comparison before either is retired."""
        return bool(self._cfg().get("enabled", False))

    def _min_items(self, family: str | None = None) -> int:
        """The member floor for ``family``, or the global default when unnamed.

        Precedence: ``smart_shelves.<family>.min_items`` -> the family's built-in default
        (see ``_FAMILY_MIN_ITEMS``) -> ``smart_shelves.min_items`` -> 5. The per-family
        default matters more than it looks: Anniversary and Fresh draw on the back catalogue
        and the last 30 days respectively, so one number cannot be right for both.
        """
        cfg = self._cfg()
        if family:
            fam_cfg = cfg.get(family, {}) or {}
            if "min_items" in fam_cfg:
                try:
                    return max(1, int(fam_cfg["min_items"]))
                except (TypeError, ValueError):
                    pass
            if family in _FAMILY_MIN_ITEMS and "min_items" not in cfg:
                return _FAMILY_MIN_ITEMS[family]
        try:
            return max(1, int(cfg.get("min_items", _DEFAULT_MIN_ITEMS)))
        except (TypeError, ValueError):
            return _DEFAULT_MIN_ITEMS

    def _fresh_days(self) -> int:
        try:
            return max(1, int(self._cfg().get("fresh_window_days", _DEFAULT_FRESH_DAYS)))
        except (TypeError, ValueError):
            return _DEFAULT_FRESH_DAYS

    def _families_enabled(self) -> list:
        """Which families to build. Both default ON once the feature itself is enabled —
        they are independent shelves with no shared state, so one can be turned off without
        affecting the other."""
        cfg = self._cfg()
        return [k for k in _FAMILIES if bool((cfg.get(k, {}) or {}).get("enabled", True))]

    # ── home-screen promotion ───────────────────────────────────────────
    # Reads plex.playlists.smart_shelves.home_collections. Two Plex facts shape this:
    #   * promoting a collection to Home is a PLEX PASS feature. Without one the POST is
    #     accepted and nothing appears, so a silent no-op is the expected failure.
    #   * a MANAGED (kid) profile cannot render a promoted collection at all, so the
    #     shared-home flag reaches adults and skips the kid profiles regardless.
    def _home_cfg(self) -> dict:
        return (self._cfg().get("home_collections", {}) or {})

    def _home_families(self) -> set:
        """Families that additionally get a promotable COLLECTION. Default: none — a
        collection is an extra object in the library, so it is opt-in per family rather
        than a side effect of enabling shelves."""
        raw = self._home_cfg().get("families")
        if not raw:
            return set()
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip().lower() for x in raw if str(x).strip()}

    def _promote_home(self) -> bool:
        return bool(self._home_cfg().get("promote_home", True))

    def _promote_shared(self) -> bool:
        return bool(self._home_cfg().get("promote_shared", True))

    def _unwatched_only(self) -> bool:
        """``smart_shelves.unwatched_only`` (default TRUE). Appends ``unwatched=1`` to every
        shelf filter, so a shelf never offers something the viewer has already seen.

        This is the one thing a smart shelf does that a computed membership list CANNOT:
        Plex evaluates the filter per PROFILE at view time, so the same shelf shows Trizzd
        his unwatched items and Raina hers, from one object. glidearr has no way to express
        that in a fixed member list.

        Caveat worth knowing: on a TV COLLECTION (type 2 = shows) ``unwatched`` means the
        SHOW has unwatched episodes, not that the show is untouched. That is the useful
        reading for a shelf, but it is not the same predicate as on an episode playlist.
        """
        return bool(self._cfg().get("unwatched_only", True))

    def _shelf_sort_title(self, family: str, title: str) -> str:
        """``'!!<n> <title>'`` -- the titleSort that pins a shelf to the top of the listing."""
        return f"{_SHELF_SORT_PREFIX}{_SHELF_SORT_ORDER.get(family, 9)} {title}"

    def _with_unwatched(self, query: str) -> str:
        """Append the unwatched clause to a filter query, if enabled and not already there."""
        q = str(query or "").strip()
        if not q or not self._unwatched_only() or "unwatched=" in q:
            return q
        return f"{q}&{_UNWATCHED_FILTER}"

    # ── membership (pure, given the frames) ───────────────────────────────────
    @staticmethod
    def _qualifies_anniversary(ts, now) -> bool:
        """Same ISO week as today, in a STRICTLY EARLIER year. The year test is what makes
        it an anniversary rather than a new release — without it every brand-new title would
        also qualify and the shelf would duplicate Fresh Arrivals."""
        if ts is None:
            return False
        try:
            return (ts.isocalendar()[1] == now.isocalendar()[1]) and (ts.year < now.year)
        except (AttributeError, ValueError):
            return False

    @staticmethod
    def _qualifies_fresh(ts, now, days) -> bool:
        """Released/aired within the last ``days``. A FUTURE date is excluded: an
        unaired episode with a scheduled air date would otherwise sit in "just landed"."""
        if ts is None:
            return False
        try:
            delta = (now - ts).days
            return 0 <= delta <= days
        except (TypeError, ValueError):
            return False

    # ── membership (I/O: the *arr parquet caches) ─────────────────────────────
    def _movie_members(self, family, now) -> set:
        """tmdb ids of owned MOVIES qualifying for ``family``, across every Radarr instance.

        Reads ``in_cinemas_date`` — the theatrical release — not ``date_added``. A 2019 film
        grabbed yesterday is not a fresh arrival, and a film added this week did not have its
        anniversary this week.
        """
        import pandas as pd
        out: set = set()
        mfm = self.registry.get("manager", "RadarrCacheMovieFilesManager") if self.registry else None
        if mfm is None or not hasattr(mfm, "load"):
            return out
        for inst in self._radarr_instances():
            try:
                df = mfm.load(inst)
            except Exception:
                continue
            if df is None or getattr(df, "empty", True) or "tmdb_id" not in df.columns:
                continue
            col = "in_cinemas_date" if "in_cinemas_date" in df.columns else None
            if col is None:
                continue
            dates = pd.to_datetime(df[col], errors="coerce", utc=True)
            for tmdb, ts in zip(df["tmdb_id"], dates):
                if pd.isna(tmdb) or pd.isna(ts):
                    continue
                keep = (self._qualifies_anniversary(ts, now) if family == "anniversary"
                        else self._qualifies_fresh(ts, now, self._fresh_days()))
                if keep:
                    try:
                        out.add(str(int(tmdb)))
                    except (TypeError, ValueError):
                        continue
        return out

    def _tvdb_by_series_id(self, inst) -> dict:
        """``{sonarr series_id: tvdbId}`` from the Sonarr SERIES cache.

        Needed because ``episode_files.parquet`` carries only Sonarr's internal
        ``series_id`` — there is no tvdb column on it — while
        ``plex/episodes/owned_inventory`` is keyed ``"tvdb:s:e"``. Without this bridge an
        episode cannot be resolved to its Plex ratingKey at all.

        Same accessor the TV playlist builder uses (``sonarr_cache.series.iter_all_series``),
        so the shape is already proven in this codebase rather than assumed here.
        """
        out: dict = {}
        sonarr = self.registry.get("manager", "SonarrManager") if self.registry else None
        series_mgr = getattr(getattr(sonarr, "sonarr_cache", None), "series", None)
        if series_mgr is None or not hasattr(series_mgr, "iter_all_series"):
            self.logger.log_warning(
                "[SmartShelves] Sonarr series cache unavailable — cannot map series_id → tvdb, "
                "so the TV half is skipped. Not an empty library.")
            return out
        try:
            for s in series_mgr.iter_all_series(inst):
                if not isinstance(s, dict):
                    continue
                sid, tv = s.get("id"), s.get("tvdbId")
                if sid is not None and tv:
                    try:
                        out[int(sid)] = int(tv)
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            self.logger.log_warning(f"[SmartShelves] series cache read failed for '{inst}': {e}")
        return out

    def _episode_members(self, family, now) -> set:
        """``{"tvdb:s:e"}`` inventory keys of owned EPISODES qualifying for ``family``.

        SOURCE: ``sonarr/{inst}/episodes/by_series/{seriesId}.json`` — Sonarr's FULL episode
        list per series, already written by the Sonarr run. Each row carries ``airDateUtc``,
        ``seasonNumber``, ``episodeNumber`` and ``hasFile``.

        ⚠️ NOT ``episode_files.parquet``, which the first version of this used. That cache
        answers a different question — it tracks FILES HELD, and with ~93% of this library
        pilot-only it carried an air date for **643 of 12,618 rows (5%)**. An ISO week over
        that subset yielded FOUR qualifying episodes where the full library gives ~230, so the
        TV shelf looked broken when the data was simply in another cache. The parquet was not
        under-populated by accident; per-episode air dates for unowned episodes were never its
        job.

        ``hasFile`` gates ownership here rather than a join against
        ``plex/episodes/owned_inventory``: an anniversary shelf must only surface something
        playable, and the flag is on the same row. The Plex inventory still resolves the
        ratingKey later — an episode Sonarr holds but Plex has not indexed simply drops there.
        """
        import glob
        import json
        import os
        from datetime import datetime as _dt

        out: set = set()
        base = self._cache_base()
        if base is None:
            return out
        for inst in self._sonarr_instances():
            tvdb_of = self._tvdb_by_series_id(inst)
            if not tvdb_of:
                continue                      # already warned by the helper
            root = os.path.join(str(base), "sonarr", str(inst), "episodes", "by_series")
            files = glob.glob(os.path.join(root, "*.json"))
            if not files:
                # LOUD: an absent cache is a schema/ordering problem, not an empty library.
                # The silent version of this check is what hid the whole TV half last time.
                self.logger.log_warning(
                    f"[SmartShelves] no per-series episode cache under {root} — the TV half "
                    f"cannot be built for '{inst}'. Run the Sonarr pass first.")
                continue
            n_rows = 0
            for path in files:
                try:
                    with open(path, encoding="utf-8") as fh:
                        rows = json.load(fh)
                except Exception:
                    continue
                for r in (rows if isinstance(rows, list) else list((rows or {}).values())):
                    if not isinstance(r, dict) or not r.get("hasFile"):
                        continue              # not owned -> never surfaced
                    raw = r.get("airDateUtc")
                    if not raw:
                        continue
                    try:
                        ts = _dt.fromisoformat(str(raw).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                    n_rows += 1
                    keep = (self._qualifies_anniversary(ts, now) if family == "anniversary"
                            else self._qualifies_fresh(ts, now, self._fresh_days()))
                    if not keep:
                        continue
                    tv = tvdb_of.get(r.get("seriesId"))
                    if tv is None:
                        continue
                    try:
                        out.add(f"{tv}:{int(r['seasonNumber'])}:{int(r['episodeNumber'])}")
                    except (TypeError, ValueError, KeyError):
                        continue
            self.logger.log_debug(
                f"[SmartShelves] '{inst}': {len(files)} series file(s), {n_rows} owned+dated "
                f"episode(s) considered for '{family}'.")
        return out

    def _cache_base(self):
        """The cache root (``key_builder.base_dir``). None when unavailable — the caller then
        skips rather than guessing a relative path, which only resolves when the process CWD
        happens to be the repo root (a bug the saga-progress reader already hit)."""
        try:
            return self.global_cache.key_builder.base_dir if self.global_cache else None
        except Exception:
            return None

    def _radarr_instances(self) -> list:
        cfg = (self.config or {}).get("radarr_instances", {}) or {}
        return [k for k, v in cfg.items() if k != "default_instance" and isinstance(v, dict)]

    def _sonarr_instances(self) -> list:
        cfg = (self.config or {}).get("sonarr_instances", {}) or {}
        return [k for k, v in cfg.items() if k != "default_instance" and isinstance(v, dict)]

    # ── Plex identity: members -> ratingKeys -> sections ──────────────────────
    def _rating_keys(self, movie_ids: set, episode_keys: set) -> tuple:
        """``(movie_rks, episode_rks)`` from the OWNED inventories the Plex passes already
        build. An id with no inventory entry is silently dropped — it means glidearr owns the
        file but Plex has not indexed it (or the scan has not run), and labelling something
        Plex cannot see would be a no-op anyway."""
        m_inv = self._cache_get(_MOVIE_INVENTORY_KEY, {}) or {}
        e_inv = self._cache_get(_TV_INVENTORY_KEY, {}) or {}
        m_rks = {str(v["rating_key"]) for k, v in m_inv.items()
                 if str(k) in movie_ids and isinstance(v, dict) and v.get("rating_key")}
        e_rks = {str(v["rating_key"]) for k, v in e_inv.items()
                 if str(k) in episode_keys and isinstance(v, dict) and v.get("rating_key")}
        return m_rks, e_rks

    def _sections_for(self, rating_keys) -> dict:
        """``{rating_key: section_id}``, batched.

        A label write is scoped to ONE section, and a library can hold several of a medium
        (this install has 'Movies' plus a separate 'TV Shows-Anime'), so the items have to be
        grouped before anything is written. ``/library/metadata/{rk,rk,…}`` accepts a
        comma-separated list, so this costs ceil(n/50) calls rather than n."""
        out: dict = {}
        keys = [str(k) for k in rating_keys]
        for i in range(0, len(keys), _METADATA_BATCH):
            batch = keys[i:i + _METADATA_BATCH]
            resp = self.plex_api.get_pms_metadata(",".join(batch), fallback={}) or {}
            mc = resp.get("MediaContainer", resp) if isinstance(resp, dict) else {}
            for item in (mc.get("Metadata") or []):
                if not isinstance(item, dict):
                    continue
                rk = item.get("ratingKey")
                sec = item.get("librarySectionID")
                if rk is not None and sec is not None:
                    out[str(rk)] = str(sec)
        return out

    # ── labelling (read-modify-write) ─────────────────────────────────────────
    def _apply_labels(self, label: str, by_section: dict, item_type: int, dry: bool) -> dict:
        """Add ``label`` to every listed item, and REMOVE it from anything that carries it but
        is no longer a member (the rotation).

        READ-MODIFY-WRITE, NON-NEGOTIABLE. ``set_item_labels`` replaces an item's WHOLE label
        set, and this library uses labels heavily for other purposes — 'Kometa', 'Universe
        Collections', 'Streaming', 'Hulu Movies'. Writing a bare ``[label]`` would strip every
        one of them, silently, from each item the rotation touched. So each write is
        current-labels ∪ {ours} (or minus ours), never a bare assignment.
        """
        stats = {"added": 0, "removed": 0, "failed": 0, "unchanged": 0}
        for section, rks in by_section.items():
            want = set(rks)
            # Anything currently carrying our label in this section, so last rotation's
            # members can be cleared. Absent label -> nothing to clear (not an error).
            current = self._labelled_in_section(section, label, item_type)
            # ALREADY CORRECT, and reported. Without this the summary reads
            # "0 labelled / 98 cleared", which is indistinguishable from a broken
            # ADD path - and looks alarming - when it is the ordinary weekly
            # rotation: everything qualifying is already tagged from a prior armed
            # run, and only the expiring members need clearing. The count that makes
            # the line self-explanatory was the one it did not print.
            stats["unchanged"] += len(want & current)
            for rk in sorted(want - current):
                if not self._retag(section, rk, label, item_type, add=True, dry=dry):
                    stats["failed"] += 1
                else:
                    stats["added"] += 1
            for rk in sorted(current - want):
                if not self._retag(section, rk, label, item_type, add=False, dry=dry):
                    stats["failed"] += 1
                else:
                    stats["removed"] += 1
        return stats

    def _labelled_in_section(self, section, label, item_type) -> set:
        """ratingKeys in ``section`` currently carrying ``label``. Uses the label's own
        filter query, so this is one call regardless of library size. Empty when the label
        does not exist yet (a first run) — which is correct, not a failure."""
        q = self.plex_api.label_filter_query(section, label, item_type=item_type)
        if not q:
            return set()
        resp = self.plex_api.get_section_all(section, plex_type=item_type, size=1000,
                                             extra_params=dict(p.split("=", 1) for p in q.split("&")
                                                               if "=" in p), fallback={}) or {}
        mc = resp.get("MediaContainer", resp) if isinstance(resp, dict) else {}
        return {str(i.get("ratingKey")) for i in (mc.get("Metadata") or [])
                if isinstance(i, dict) and i.get("ratingKey") is not None}

    def _retag(self, section, rk, label, item_type, *, add: bool, dry: bool) -> bool:
        cur = set(self.plex_api.get_item_labels(rk) or [])
        new = (cur | {label}) if add else (cur - {label})
        if new == cur:
            return True
        if dry:
            verb = "add" if add else "remove"
            self.logger.log_debug(f"[SmartShelves] [dry_run] would {verb} '{label}' on rk {rk}")
            return True
        return self.plex_api.set_item_labels(section, rk, sorted(new), item_type=item_type)

    # ── run ───────────────────────────────────────────────────────────────────
    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> dict:
        stats = {"enabled": self._enabled(), "shelves": 0, "labelled": 0, "cleared": 0}
        # Collections created THIS run, published under _MANAGED_KEY so the poster
        # pass acts on ratingKeys rather than guessing at titles.
        managed: dict = {}
        if not self._enabled():
            self.logger.log_debug("[SmartShelves] disabled — skipping.")
            return stats
        if not self.plex_api:
            self.logger.log_warning("[SmartShelves] no Plex API — skipping.")
            return stats

        dry = bool(getattr(self, "dry_run", False))
        now = datetime.now(timezone.utc)

        for fam in self._families_enabled():
            meta = _FAMILIES[fam]
            floor = self._min_items(fam)          # per-family: see _FAMILY_MIN_ITEMS
            m_ids = self._movie_members(fam, now)
            e_keys = self._episode_members(fam, now)
            m_rks, e_rks = self._rating_keys(m_ids, e_keys)

            for rks, itype, medium in ((m_rks, _TYPE_MOVIE, "movie"),
                                       (e_rks, _TYPE_EPISODE, "episode")):
                if not rks:
                    continue
                by_section: dict = {}
                for rk, sec in self._sections_for(rks).items():
                    by_section.setdefault(sec, set()).add(rk)

                lab = self._apply_labels(meta["label"], by_section, itype, dry)
                stats["labelled"] += lab["added"]
                stats["cleared"] += lab["removed"]
                stats["unchanged"] = stats.get("unchanged", 0) + lab.get("unchanged", 0)
                stats["label_failed"] = stats.get("label_failed", 0) + lab.get("failed", 0)

                for section, members in by_section.items():
                    title = self._shelf_title(meta["title"], section)
                    # Record the surfaced set BEFORE the floor check: a shelf suppressed for
                    # being too small was still COMPUTED, and an empty/short plan is a real
                    # measurement ("nothing worth surfacing this rotation"), not a gap. Writing
                    # only above the floor would leave the engagement history unable to tell a
                    # thin week from a run that never happened.
                    self._record_surfaced(fam, section, title, members, medium)
                    if dry:
                        verdict = "would build" if len(members) >= floor else f"below floor {floor}"
                        self.logger.log_info(
                            f"[SmartShelves] [dry_run] '{title}' ({medium}, section {section}): "
                            f"{len(members)} member(s) — {verdict}.")
                        continue
                    q = self.plex_api.label_filter_query(section, meta["label"], item_type=itype)
                    res = self.plex_api.ensure_smart_playlist(
                        title, section, self._with_unwatched(q or ""),
                        member_count=len(members),
                        item_type=itype, min_items=floor)
                    if res.get("action") in ("created", "exists"):
                        stats["shelves"] += 1
                        # Pin to the FRONT of the playlist listing. Gated on nothing: the
                        # edit is idempotent and cheap, and a shelf that lost its sort key
                        # (renamed by hand, recreated) silently drops back among the T's.
                        if res.get("rating_key"):
                            self.plex_api.edit_playlist(
                                res["rating_key"], title=title,
                                title_sort=self._shelf_sort_title(fam, title))
                            # Publish the shelf PLAYLIST too, not just its collection
                            # twin. Without this the seven per-section shelves wear
                            # Plex's auto-mosaic - four of them truncating to an
                            # identical "This Week In History - TV Sho...", telling
                            # the household nothing.
                            managed[str(res["rating_key"])] = {
                                "title": title, "section": str(section),
                                "family": fam, "kind": "playlist",
                                "library": self._section_name(section),
                                "count": len(members),
                                "slug": _FAMILY_POSTER.get(
                                    (fam, "movie" if itype == _TYPE_MOVIE else "episode")),
                            }

                    # HOME SCREEN. Plex will not pin a playlist — only a collection — so a
                    # family that wants to reach Home gets a smart COLLECTION on the same
                    # label, alongside its playlist. Same label, same members, different
                    # object; the collection is the one Plex can promote.
                    #
                    # ITEM TYPE DIFFERS FROM THE PLAYLIST. A playlist can hold episodes
                    # (type 4); a collection in a TV section holds SHOWS (type 2) because
                    # Plex has no episode-level collection. So a TV shelf surfaces the
                    # SERIES on Home, not the individual episode. That is a real difference
                    # in what the household sees and is why this is not a silent mirror.
                    if fam in self._home_families():
                        ctype = _TYPE_MOVIE if itype == _TYPE_MOVIE else _TYPE_SHOW
                        cq = self.plex_api.label_filter_query(section, meta["label"],
                                                              item_type=ctype)
                        cres = self.plex_api.ensure_smart_collection(
                            title, section, self._with_unwatched(cq or ""),
                            member_count=len(members),
                            item_type=ctype, min_items=floor,
                            promote_home=self._promote_home(),
                            promote_shared=self._promote_shared())
                        if cres.get("action") in ("created", "exists"):
                            stats["collections"] = stats.get("collections", 0) + 1
                            if cres.get("rating_key"):
                                self.plex_api.set_item_title_sort(
                                    section, cres["rating_key"],
                                    self._shelf_sort_title(fam, title),
                                    item_type=_TYPE_COLLECTION)
                                # PUBLISH WHAT WE MADE. The ratingKey is in hand at the
                                # moment of creation, so downstream art has no reason to
                                # go looking for the collection by NAME. See
                                # _MANAGED_KEY for why that matters.
                                managed[str(cres["rating_key"])] = {
                                    "title": title, "section": str(section),
                                    "family": fam, "kind": "collection",
                                    "library": self._section_name(section),
                                    "count": len(members),
                                    "slug": _FAMILY_POSTER.get(
                                        (fam, "movie" if itype == _TYPE_MOVIE
                                         else "episode")),
                                }
                        if cres.get("promoted"):
                            stats["promoted"] = stats.get("promoted", 0) + 1
                        if cres.get("action") == "failed":
                            self.logger.log_warning(
                                f"[SmartShelves] collection '{title}' (section {section}) "
                                f"failed — label filter unresolved or create rejected.")

        if not dry:
            # Replace wholesale, never merge: a collection that fell below its floor
            # was DELETED this run, and a merged map would keep pointing art at a
            # ratingKey that no longer exists. Untouched under dry_run, so a preview
            # still sees what the last armed run actually built.
            self._cache_set(_MANAGED_KEY, managed)

        self.logger.log_info(
            f"[SmartShelves] {'[dry_run] ' if dry else ''}{stats['shelves']} shelf/shelves live · "
            f"{stats.get('collections', 0)} collection(s) · "
            f"{stats.get('promoted', 0)} pinned to Home · "
            f"{stats['labelled']} {'would be ' if dry else ''}labelled · "
            f"{stats.get('unchanged', 0)} already correct · "
            f"{stats['cleared']} {'would be ' if dry else ''}cleared"
            + (f" · {stats['label_failed']} label write(s) FAILED"
               if stats.get("label_failed") else "") + ".")
        return stats

    def _record_surfaced(self, family: str, section, title: str, rating_keys, medium: str) -> None:
        """Persist this section's surfaced set in the shape ``update_engagement`` consumes.

        WRITTEN EVEN UNDER dry_run, deliberately — same contract as the per-user builders
        ("BUILD+CACHE only, NO Plex writes"). The plan is a LOCAL annotation, not a Plex
        mutation, and if it only appeared on live runs the engagement history would start
        empty the moment write-back was armed, which is exactly when the measurement first
        matters.

        Shape mirrors ``PlexPlaylistBuilderManager._serialize`` so a single consumer can read
        either source: ``items`` of ``{rating_key, ordinal, group_key, group_kind}``.
        ``group_key`` is the FAMILY (not a per-title group) because a shelf is engaged with or
        skipped as a whole — that is the unit ``saga_boost`` de-rates.

        ``score``/``reason`` are None: a smart shelf has no per-item ranking to report, and
        inventing one would misrepresent how membership was decided (a date filter, not a
        score). A consumer must treat None as "not ranked", never as zero.
        """
        if not self.global_cache:
            return
        rks = sorted(str(r) for r in rating_keys)
        plan = {
            "family": family,
            "title": title,
            "medium": medium,
            "section": str(section),
            "considered": len(rks),
            "dropped_watched": 0,          # a smart shelf does not filter on watched state
            "truncated": False,
            "coverage": {},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": [{"rating_key": rk, "ordinal": i, "group_key": family,
                       "group_kind": "shelf", "score": None, "reason": None}
                      for i, rk in enumerate(rks)],
        }
        try:
            self.global_cache.set(f"{_SMART_PLAN_KEY}/{family}/{section}", plan)
        except Exception as e:
            self.logger.log_debug(f"[SmartShelves] could not record surfaced set for {title}: {e}")

    def _section_name(self, section) -> str:
        """The library's display name, e.g. 'TV Shows-Anime'. '' when unknown.

        Stored on the managed record rather than parsed back out of the shelf
        title: ``_shelf_title`` only appends ' — {name}' when a medium has SEVERAL
        sections, so a single-section library produces a bare title with nothing
        to split on. Re-deriving it downstream would work for anime and silently
        fail for movies.
        """
        secs = self._cache_get(_SECTIONS_KEY, {}) or {}
        entry = secs.get(str(section)) or secs.get(section) or {}
        return str((entry or {}).get("title") or "") if isinstance(entry, dict) else ""

    def _shelf_title(self, base: str, section) -> str:
        """Per-section title. A smart playlist's uri points at ONE section and Plex has no
        cross-section smart playlist, so a library with separate 'TV Shows' and
        'TV Shows-Anime' gets one shelf each — suffixed with the section name so they are
        distinguishable. A single-section medium keeps the bare title."""
        secs = self._cache_get(_SECTIONS_KEY, {}) or {}
        entry = secs.get(str(section)) or secs.get(section) or {}
        name = (entry or {}).get("title") if isinstance(entry, dict) else None
        same_type = [s for s in secs.values()
                     if isinstance(s, dict) and s.get("type") == (entry or {}).get("type")]
        return base if len(same_type) <= 1 or not name else f"{base} — {name}"

    def _cache_set(self, key, value) -> None:
        """Best-effort cache write. A failure here costs the next run its poster
        targets, never the shelves themselves, so it warns rather than raising."""
        if not self.global_cache:
            return
        try:
            self.global_cache.set(key, value)
        except Exception as e:
            self.logger.log_warning(f"[SmartShelves] could not cache '{key}': {e}")

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

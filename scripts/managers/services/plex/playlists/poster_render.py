
"""plex/playlists/poster_render.py — per-USER posters, filled from the real plan.

THE GAP THIS CLOSES. ``generate_posters`` renders every family once, with no
values, so every poster shipped its SPEC default: "next up in your series",
"12 just landed this week", "continuing your sagas". A live run put those on
eight real playlists - one of which held 3 days 14hr while announcing "12 just
landed". The whole tokenisation was built, tested, wired and never fed. The
templates were never the problem; nothing ever passed them a value.

WHY PER-USER AND NOT ONE SHARED ASSET. Five of the eight families are personal:
Up Next's lead shows, The Long Glide's sagas, Touch & Go's and Hidden Gems'
genres, Tonight's picks. One shared PNG cannot say "next up in Andor · The Bear"
for Trizzd and something else for Raina. So each profile gets its own render
under ``assets/posters/playlists/by_user/{safe_user}/``.

That costs nothing structurally: ``writeback._apply_branding`` already keys its
version gate on the ANCHOR ID, which is already per-user, so a per-user asset
slots into the existing gate with no change to its logic.

RUNS BETWEEN THE BUILDERS AND THE WRITE-BACK. The plans have to exist (builders)
and the PNGs have to exist before anything uploads them (write-back). Anywhere
else in the order and it renders from stale plans or renders too late to matter.

DEGRADES, NEVER BLOCKS. No rasteriser, no template, no plan, an unreadable
cache - each is a skip with a logged reason. Posters are decoration; a missing
one must never cost the household its playlists. The shared default asset stays
in place as the fallback, so a profile that fails to render keeps the generic
poster rather than losing its art.
"""
from __future__ import annotations

from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.support.tools import posters as P

_TV_INVENTORY_KEY = "plex/episodes/owned_inventory"

#: Written by ``playlists/tonight_builder`` from the Radarr rows it already reads.
#: Keep in step with ``tonight_builder._GENRES_KEY`` - the two ends of one handoff.
_GENRES_KEY = "plex/playlists/genres_by_rating_key"

#: Written beside the shared default PNGs to record WHAT they were rendered from
#: and WHEN. Both halves matter: a template version bump changes the artwork, and
#: a date roll invalidates the four dated families regardless of any code change.
_BASE_MARKER = "_base.json"

#: family suffix -> (poster slug, plan cache key). Mirrors
#: ``writeback._BRAND_ASSETS`` + ``_all_families``; keep the three in step.
_FAMILY_PLANS = {
    "Up Next": ("up_next", ("plex/playlists/combined_plan",
                            "plex/playlists/tv_plan",
                            "plex/playlists/movie_plan")),
    "The Long Glide": ("the_long_glide", ("plex/playlists/glide_plan",)),
    "Touch & Go": ("touch_and_go", ("plex/playlists/touchgo_plan",)),
    "Fresh Arrivals": ("fresh_arrivals", ("plex/playlists/fresh_movie_plan",)),
    "Anniversary Picks": ("anniversary_picks", ("plex/playlists/twih_movie_plan",)),
    "On This Week": ("on_this_week", ("plex/playlists/twih_show_plan",)),
    "Hidden Gems": ("hidden_gems", ("plex/playlists/gems_plan",)),
    "Tonight": ("tonight", ("plex/playlists/tonight_plan",)),
}

#: slug -> (kwarg, source). ``source`` says WHAT KIND of label the copy asks for,
#: which is not the same question as what the plan happens to carry.
#:
#: THIS DISTINCTION WAS MISSING AND IT SHOWED. Every family was labelled with
#: series titles, so "Touch & Go" - whose copy reads "one-offs in {GENRE}" -
#: shipped "one-offs in The West Wing . Suits . rocky". Those are shows and a
#: franchise, not genres. A title where a genre belongs is not a cosmetic slip;
#: the sentence asserts a category and then names something from a different one.
_LIST_SPEC = {
    "up_next":        ("shows", "title"),
    "the_long_glide": ("franchises", "group"),
    "touch_and_go":   ("genres", "genre"),
    "hidden_gems":    ("genres", "genre"),
    "tonight":        ("genres", "genre"),
}


class PlaylistPosterRenderManager(BaseManager):
    """Render one poster per profile per family, filled from that profile's plan."""

    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── gates ────────────────────────────────────────────────────────────────
    def _pl_cfg(self) -> dict:
        return ((self.config.get("plex", {}) if self.config else {}) or {}).get("playlists", {}) or {}

    def _enabled(self) -> bool:
        """Follows ``plex.playlists.branding.enabled`` - if posters are not being
        uploaded there is nothing for a per-user render to feed."""
        return bool((self._pl_cfg().get("branding", {}) or {}).get("enabled", False))

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        stats = {"enabled": self._enabled(), "rendered": 0, "skipped": 0,
                 "no_plan": 0, "failed": 0, "users": 0}
        if not stats["enabled"]:
            return stats

        backends = P.available_backends()
        if not backends:
            self.logger.log_warning(
                "[PosterRender] no SVG rasteriser (pip install resvg-py) - per-user "
                "posters skipped; the shared default art still uploads.")
            return stats

        # Same reasoning as CollectionPosterManager: templates are generated, and
        # the engine must not depend on an operator having run the CLI after a
        # design change. Idempotent when the version stamps already match.
        built = P.ensure_templates()
        if built:
            self.logger.log_info(
                f"[PosterRender] rebuilt {built} stale/missing poster template(s).")

        # The SHARED DEFAULT set, before any per-user work. These are what
        # writeback falls back to when a profile has no plan for a family, so they
        # have to exist on a fresh install with no operator step - and they have
        # to be re-rendered when the date rolls, because Tonight, Fresh Arrivals,
        # Anniversary Picks and On This Week print a live date and a stale one
        # confidently announces the wrong evening.
        base = self._ensure_base_posters(force=bool(built))
        if base:
            self.logger.log_info(
                f"[PosterRender] rendered {base} shared default poster(s).")
        stats["base_rendered"] = base

        users = self._tracked()
        stats["users"] = len(users)
        titles = self._series_titles()
        genres = self._genres_by_rk()

        for u in users:
            safe = u.get("safe_user")
            if not safe:
                continue
            out_dir = P.ASSETS / "playlists" / "by_user" / str(safe)
            for suffix, (slug, keys) in _FAMILY_PLANS.items():
                plan = self._plan(keys, safe)
                if not plan or not plan.get("items"):
                    stats["no_plan"] += 1
                    continue
                values = self._values(slug, plan, titles, genres)
                try:
                    svg = P.render(slug, "playlist", **values)
                    png = P.rasterize(svg, "playlist")
                except Exception as exc:
                    # A single family failing must not cost the profile its other
                    # seven posters, nor the run its playlists.
                    stats["failed"] += 1
                    self.logger.log_warning(
                        f"[PosterRender] '{suffix}' for '{safe}' failed: "
                        f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    wrote = P.write_if_changed(out_dir / f"{slug}.png", png)
                except OSError as exc:
                    stats["failed"] += 1
                    self.logger.log_warning(f"[PosterRender] cannot write {out_dir}: {exc}")
                    continue
                if wrote:
                    stats["rendered"] += 1
                    self._detail(f"[PosterRender] '{safe}' {suffix}: "
                                 f"{P.extract_text(svg).get('why', '')}")
                else:
                    # Byte-identical to what is on disk: mtime untouched, so the
                    # upload gate stays closed. This branch IS the steady state.
                    stats["unchanged"] = stats.get("unchanged", 0) + 1

        self.logger.log_info(
            f"[PosterRender] {stats['rendered']} per-user poster(s) written, "
            f"{stats.get('unchanged', 0)} unchanged (gated), over "
            f"{stats['users']} profile(s) · {stats['no_plan']} family/families with no "
            f"plan · {stats['failed']} failed (backend {backends[0]}).")
        return stats

    # ── value derivation ─────────────────────────────────────────────────────
    def _ensure_base_posters(self, *, force: bool = False) -> int:
        """Render the shared default poster set when it is missing, stale or dated-out.

        Returns the number written. Replaces the ``generate_posters`` CLI as the
        thing that produces these - an operator step is not a dependency the
        engine can carry, because nobody runs a tool before every scheduled run
        and the dated families need re-rendering EVERY DAY.

        Three triggers, and each catches a case the others miss:
          * marker ABSENT      - fresh install, or the assets were never built
          * template_version   - the design changed; artwork must follow
          * date rolled        - the four dated families print yesterday

        PLAYLIST CANVAS ONLY. The collection set used to be rendered here too and
        is no longer consumed: ``CollectionPosterManager`` renders each shelf's
        art per section at upload time, because the library name is printed on it
        and a static file cannot carry a per-section value. Rendering an unused
        set would imply it still mattered.
        """
        from datetime import date
        out = P.ASSETS / "playlists"
        marker = out / _BASE_MARKER
        today = date.today().isoformat()
        version = self._template_version()

        if not force:
            try:
                import json
                prev = json.loads(marker.read_text(encoding="utf-8"))
                if (prev.get("date") == today
                        and str(prev.get("template_version")) == version
                        and all((out / f"{s}.png").is_file() for s in self._base_slugs())):
                    return 0
            except (OSError, ValueError):
                pass                      # absent or unreadable -> render

        written = 0
        ran = False
        for slug in self._base_slugs():
            try:
                # count=NO_VALUE, not omitted. Omitting keeps the SPEC default and
                # this set would go on claiming "12 just landed this week" for a
                # profile that has no plan and therefore no number - the same
                # fabrication the per-user path was rebuilt to stop making. There
                # is no honest count here, so the line carries none.
                svg = P.render(slug, "playlist", count=P.NO_VALUE)
                png = P.rasterize(svg, "playlist")
            except Exception as exc:
                # One family failing must not cost the other eleven their art.
                self.logger.log_warning(
                    f"[PosterRender] default '{slug}' failed: "
                    f"{type(exc).__name__}: {exc}")
                continue
            ran = True
            try:
                if P.write_if_changed(out / f"{slug}.png", png):
                    written += 1
            except OSError as exc:
                self.logger.log_warning(f"[PosterRender] cannot write {out}: {exc}")
                return written

        if ran:
            # The marker records that the PASS ran today at this version, not that
            # files changed - on a date roll the four undated families are
            # byte-identical and rightly unwritten, but tomorrow's skip check
            # still needs today's date recorded or the pass re-runs every time.
            try:
                import json
                marker.write_text(json.dumps(
                    {"date": today, "template_version": version,
                     "count": written}, indent=2), encoding="utf-8")
            except OSError:
                # A missing marker only means we re-render next run. Harmless, and
                # far better than failing the pass over a bookkeeping file.
                pass
        return written

    def _base_slugs(self) -> list:
        """The slugs a shared default is actually CONSUMED for.

        Not ``P.SPEC``, which is every family the design system can draw. Two
        groups have to be left out or the asset directory fills with files that
        look meaningful and are not:

          * SHELF slugs (``shelf_this_week``, ``shelf_just_landed``) print a
            LIBRARY name. A static default could only carry the "Library"
            placeholder, and CollectionPosterManager renders them per section at
            upload time anyway.
          * families with no per-user playlist (``franchise_run``,
            ``kids_safe``, ...) - writeback's ``_BRAND_ASSETS`` never asks for
            them, so nothing would ever read the file.

        Derived from ``_FAMILY_PLANS`` so this list cannot drift from the set
        write-back actually uploads.
        """
        return [slug for slug, _keys in _FAMILY_PLANS.values()]

    def _template_version(self) -> str:
        try:
            from scripts.support.tools import poster_templates
            return str(poster_templates.TEMPLATE_VERSION)
        except Exception:
            return "?"

    def _values(self, slug: str, plan: dict, titles: dict, genres: dict) -> dict:
        """Token values for one family from one profile's plan.

        COUNT is ``len(plan["items"])`` - the number the poster should have been
        printing all along, and the one it was inventing.

        The ranked list is drawn from the SOURCE the copy asks for (see
        ``_LIST_SPEC``), not from whatever is easiest to reach. A genre family
        gets genres; a show family gets titles. Where the requested source yields
        nothing the key is omitted entirely, so the template falls back to its
        shipped default rather than printing the wrong category.
        """
        items = plan.get("items") or []
        values = {"count": len(items)}
        spec = _LIST_SPEC.get(slug)
        if not spec:
            return values
        kwarg, source = spec

        seen, labels = set(), []
        for it in items:
            for label in self._labels_for(it, source, titles, genres):
                fold = label.casefold()
                if fold in seen:
                    continue         # one entry per show/genre, not per episode
                seen.add(fold)
                labels.append(label)
            if len(labels) >= 3:
                break
        if labels:
            values[kwarg] = labels[:3]
        return values

    def _labels_for(self, item: dict, source: str, titles: dict, genres: dict) -> list:
        """The label(s) one plan item contributes, for the requested source."""
        rk = str(item.get("rating_key") or "")
        if source == "genre":
            return list(genres.get(rk) or ())
        if source == "title" and rk in titles:
            return [titles[rk]]
        gk = str(item.get("group_key") or "")
        if ":" in gk:
            gk = gk.split(":", 1)[1]
        gk = gk.strip()
        if not gk or gk.isdigit():
            return []                # a bare id is not a label
        return [gk]

    def _genres_by_rk(self) -> dict:
        """``{rating_key: [genre, ...]}``, published by ``tonight_builder``.

        READ, NEVER RE-DERIVED. The Radarr parquet already gets parsed for genres
        in ``tonight_builder._movie_maps``; a second reader here would be a third
        implementation of the same lookup and the first to drift. Two silent
        failures this session started exactly that way - a consumer inventing a
        cache key that nothing writes.

        Empty is normal and harmless: the genre-copy families fall back to their
        shipped defaults rather than printing the wrong category.
        """
        out: dict = {}
        for rk, g in (self._cache_get(_GENRES_KEY, {}) or {}).items():
            vals = list(g) if isinstance(g, (list, tuple)) else ([g] if g else [])
            clean = [str(x).strip() for x in vals if str(x).strip()]
            if clean:
                out[str(rk)] = clean
        return out

    def _series_titles(self) -> dict:
        """``{rating_key: series_title}`` from the owned-episode inventory, so an
        episode plan can print "Andor" rather than a ratingKey."""
        out = {}
        for row in (self._cache_get(_TV_INVENTORY_KEY, {}) or {}).values():
            if isinstance(row, dict) and row.get("rating_key") and row.get("series_title"):
                out[str(row["rating_key"])] = str(row["series_title"])
        return out

    def _plan(self, keys, safe) -> dict | None:
        """First cached plan that has items, in the family's key precedence."""
        for key in keys:
            plan = self._cache_get(f"{key}/{safe}", None)
            if isinstance(plan, dict) and plan.get("items"):
                return plan
        return None

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _tracked(self) -> list:
        mgr = self.registry.get("manager", "PlexUsersManager") if self.registry else None
        return list(getattr(mgr, "tracked_users", []) or []) if mgr else []

    def _detail(self, msg):
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("playlists", msg)

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

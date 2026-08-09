"""plex/collections/posters.py — write generated posters onto Plex COLLECTIONS.

The collection-side twin of ``plex/playlists/writeback._apply_branding``. Both
push a generated PNG to ``POST /library/metadata/{ratingKey}/posters``; both gate
on the asset's content version so a steady run uploads nothing. The gate itself
lives in :mod:`scripts.support.tools.poster_sync` so there is exactly ONE
implementation of "upload once, cache only on a verified 2xx, retry on failure".

WHAT DIFFERS FROM THE PLAYLIST PATH — and it is only two things:

* **Canvas.** A collection is a library ITEM and renders in the 2:3 grid beside
  the movies; a playlist renders in a 1:1 tile. So this reads
  ``assets/posters/collections/<slug>.png`` (1000x1500), never the square one.
* **Token.** A collection belongs to the library, i.e. to the OWNER. There is no
  per-member token dance here — which is why this module is a fraction of the
  size of the playlist write-back, with no anchor map, no per-user re-resolution
  and no orphan sweep.

DEFAULT-OFF / FAIL-CLOSED, same contract as the playlist path: with
``plex.collections.posters.enabled`` false (the default) OR ``dry_run`` true this
performs the full resolve + preview and ZERO Plex writes.

RESOLUTION IS BY RATING KEY, HANDED OVER BY THE CREATOR — never by title.

That is the whole fix for a bug this module shipped with. It used to carry its
own map of twelve collection TITLES and search every section for them. Ten of
those twelve (Up Next, Tonight, Hidden Gems, The Long Glide, Touch & Go,
Franchise Run, Household Picks, Kids Safe, Because You Watched) name per-user
PLAYLIST families that are never collections at all. The two that genuinely can
be collections are created by ``smart_shelves`` as "Just Landed — Movies" and
"This Week In History — TV Shows", never the bare titles this searched for. So
every run reported ``12 collection(s) not found``: a lookup that could not
succeed, phrased as though the collections were merely absent.

Two independent definitions of "which collections exist and what art do they
wear" is the P-E pattern, and they drifted the instant either side was renamed.
``smart_shelves`` now publishes ``{rating_key: {title, section, family, slug}}``
under ``plex/collections/managed`` at the moment of creation — it already holds
the ratingKey — and this module simply walks it. The NAME is out of the contract
entirely, so a future rename cannot silently orphan the art.

This module still ONLY sets artwork. It never mints, populates or deletes a
collection, so a poster bug can never destroy membership.
"""
from __future__ import annotations

from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.support.tools import poster_sync
from scripts.support.tools import posters as P

#: Written by ``playlists/smart_shelves`` at creation time:
#: ``{rating_key: {title, section, family, slug}}``. Consumed here; this module
#: performs no name resolution of its own. Keep the constant in step with
#: ``smart_shelves._MANAGED_KEY`` — they are the two ends of one handoff.
_MANAGED_KEY = "plex/collections/managed"

_ASSETS = (Path(__file__).resolve().parents[4]
           / "support" / "assets" / "posters")

#: Where the per-section shelf art is written. One PNG per managed shelf, because
#: the library name is ON the poster and a static file cannot carry a variable.
_RENDER_DIR = _ASSETS / "shelves"

#: ratingKey -> last-uploaded version token, namespaced away from the playlist
#: gate so the two kinds never read each other's "done" marker (they are
#: different files on different canvases; one being current says nothing about
#: the other). Keyed by ratingKey rather than slug because one slug can dress
#: several collections — the same family in Movies and in TV Shows.
_POSTER_KEY = "plex/collections/postered"


class CollectionPosterManager(BaseManager):
    """Set the generated poster on each managed collection. Artwork only."""

    parent_name = "PlexManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.plex_api = kwargs.get("plex_api")
        self.dry_run = kwargs.get("dry_run", False)

    def prepare(self):
        pass

    # ── gates ────────────────────────────────────────────────────────────────
    def _cfg(self) -> dict:
        col = ((self.config.get("plex", {}) if self.config else {}) or {}).get("collections", {}) or {}
        return col.get("posters", {}) or {}

    def armed(self) -> bool:
        """The single fail-closed gate the upload consults: enabled AND not dry_run."""
        return bool(self._cfg().get("enabled", False)) and not bool(self.dry_run)

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        # Templates are generated artefacts. Rebuild any that are missing or
        # version-stale BEFORE rendering - this manager renders per section, so
        # it cannot rely on an operator having run generate_posters after a
        # design change. Idempotent: writes nothing when the stamps match.
        built = P.ensure_templates()
        if built:
            self.logger.log_info(
                f"[CollectionPosters] rebuilt {built} stale/missing poster template(s).")
        return self._sync(self._cache_get(_MANAGED_KEY, {}) or {})

    def _sync(self, managed: dict) -> dict:
        """``managed`` is ``{rating_key: {title, section, family, kind, library,
        count, slug}}``. Tested core.

        No title matching, no section scan: smart_shelves put the ratingKey, the
        art slug and the library name into the cache at the moment it built the
        shelf, so this is a straight walk over what demonstrably exists.

        RENDERS PER ENTRY rather than reading a shipped PNG. The library name is
        printed ON the poster - that is the whole reason these exist, since four
        "This Week In History" shelves truncate identically in the grid - and a
        static asset cannot carry a per-section value. Rendering here rather than
        in ``poster_render`` is forced by the run order: smart_shelves publishes
        the map at line 541 and ``poster_render`` has already gone at 517.
        """
        armed = self.armed()
        stats = {"armed": armed, "matched": 0, "uploaded": 0, "gated": 0,
                 "render_failed": 0, "no_slug": 0}

        for rk, rec in (managed or {}).items():
            if not isinstance(rec, dict):
                continue
            slug = rec.get("slug")
            if not slug:
                # A family with no art mapped is a gap in smart_shelves._FAMILY_POSTER,
                # not a failure here - report it rather than invent a fallback poster.
                stats["no_slug"] += 1
                continue
            asset = self._render(rk, rec, slug)
            if asset is None:
                stats["render_failed"] += 1
                continue
            stats["matched"] += 1
            done = poster_sync.sync_poster(
                plex_api=self.plex_api, cache=self.global_cache, logger=self.logger,
                asset=asset, rating_key=rk,
                cache_key=f"{_POSTER_KEY}/{rk}",
                armed=armed, token=None,      # owner token: a shelf is the library's
                label=rec.get("title") or slug, detail=self._detail)
            if done:
                stats["uploaded"] += 1
            else:
                stats["gated"] += 1

        state = "ARMED" if armed else "disarmed (dry-run/disabled — no Plex writes)"
        if not managed:
            self.logger.log_info(
                f"[CollectionPosters] {state}: no managed shelves yet — smart_shelves "
                f"publishes them once it has built one on an armed run.")
            return stats
        self.logger.log_info(
            f"[CollectionPosters] {state}: {stats['uploaded']} uploaded / "
            f"{stats['gated']} already-current or skipped / "
            f"{stats['matched']} of {len(managed)} managed shelf/shelves"
            + (f" / {stats['render_failed']} render(s) failed"
               if stats["render_failed"] else "")
            + (f" / {stats['no_slug']} with no art mapped" if stats["no_slug"] else "")
            + ".")
        return stats

    def _render(self, rk, rec: dict, slug: str):
        """Render this shelf's poster and return its Path, or None.

        The canvas follows the OBJECT: a collection is a library item in the 2:3
        grid, a playlist renders in a 1:1 tile and Plex centre-crops anything
        taller. Getting this wrong would silently crop the title band off every
        playlist shelf.

        Returns None on any failure - no rasteriser, an unfillable token, an
        unwritable directory. The shelf then keeps whatever art it already has,
        which is a worse-looking row and not a broken one.
        """
        kind = "collection" if rec.get("kind") == "collection" else "playlist"
        values = {"library": rec.get("library") or "Library"}
        if rec.get("count") is not None:
            values["count"] = rec["count"]
        try:
            svg = P.render(slug, kind, **values)
            png = P.rasterize(svg, kind)
        except Exception as exc:
            self.logger.log_warning(
                f"[CollectionPosters] render failed for '{rec.get('title')}': "
                f"{type(exc).__name__}: {exc}")
            return None
        out = _RENDER_DIR / kind
        try:
            path = out / f"{slug}-{rk}.png"
            # write_if_changed, not write_bytes: an unconditional rewrite gives the
            # file a fresh mtime every pass, and poster_sync versions on size+mtime
            # - so every shelf would re-upload on every run and the gate would
            # never hold. Identical values render to identical bytes (deterministic
            # backend), so the steady state is a skipped write and a gated upload.
            P.write_if_changed(path, png)
        except OSError as exc:
            self.logger.log_warning(f"[CollectionPosters] cannot write {out}: {exc}")
            return None
        self._detail(f"[CollectionPosters] rendered '{rec.get('title')}' — "
                     f"{P.extract_text(svg).get('title', '')} · "
                     f"{P.extract_text(svg).get('why', '')}")
        return path

    def _cache_get(self, key, default):
        if not self.global_cache:
            return default
        try:
            val = self.global_cache.get(key)
            return val if val is not None else default
        except Exception:
            return default

    def _detail(self, msg):
        if self.logger and hasattr(self.logger, "log_to_file"):
            self.logger.log_to_file("playlists", msg)

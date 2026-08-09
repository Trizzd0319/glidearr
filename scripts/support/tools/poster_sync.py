"""poster_sync.py — the ONE way a generated poster reaches a Plex item.

Both consumers upload to the SAME endpoint, because a playlist and a collection
are both library-metadata items::

    POST /library/metadata/{ratingKey}/posters

(``PlexAPI.upload_playlist_poster`` is that call. The name is a historical
misnomer — its own docstring records that ``/playlists/{rk}/posters`` 404s and
the generic ``/library/metadata`` path is what works. It is not
playlist-specific and this module uses it for collections too.)

WHY THIS IS A MODULE AND NOT A METHOD ON EACH MANAGER. The upload is the easy
half. The half worth writing once is the GATE:

* a poster must upload ONCE, not every run — keyed on the asset's content
  version (size + mtime), so a re-themed PNG re-uploads and an unchanged one
  never does;
* the version may only be cached on a VERIFIED 2xx. This is the exact bug the
  playlist path already shipped and fixed: v1 posted to the wrong endpoint, got
  a silent 404, cached it as done, and therefore never retried. A failed upload
  must leave the cache untouched;
* a fresh ratingKey inherits nothing — after a recreate the gate key still says
  "done" for an item that has no art, so callers need ``force``;
* disarmed runs preview and write nothing.

Duplicating that in a second manager is how the two drift, and the failure is
silent in both directions. One implementation, two thin callers.
"""
from __future__ import annotations

from pathlib import Path

#: Bump when the upload CONTRACT changes (endpoint, format) so persisted version
#: tokens minted under an older, broken scheme no longer match and every poster
#: re-uploads exactly once. v1 used /playlists/{rk}/posters (404, cached as done);
#: v2 is /library/metadata/{rk}/posters + 2xx-verified; v3 adds the collection
#: kind, whose posters are a different canvas and so a different artifact.
SCHEME = "3"


def asset_version(path: Path) -> str | None:
    """A cheap content-change token for ``path``: scheme, size and mtime.

    None on a stat failure, which the caller must treat as "no asset" and skip —
    never as "unchanged", or a vanished file would read as already-done.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{SCHEME}:{st.st_size}-{st.st_mtime_ns}"


def sync_poster(*, plex_api, cache, logger, asset: Path, rating_key, cache_key: str,
                armed: bool, token: str | None = None, force: bool = False,
                label: str = "", detail=None) -> bool:
    """Upload ``asset`` to ``rating_key``'s poster slot, once. True if uploaded.

    ``cache_key``   where the version token is persisted (per item, per kind).
    ``armed``       False => preview only, zero writes, nothing cached.
    ``token``       scopes the write. A PLAYLIST on a managed member's account
                    needs that member's per-server token; a COLLECTION is a
                    library item on the owner's server and takes the owner token.
    ``force``       bypass the gate — required after a recreate, where the gate
                    key survives but the ratingKey does not.
    ``detail``      optional callable for the verbose per-item log sink.

    Returns False for every non-upload outcome (missing asset, gated, disarmed,
    failed) so a caller can count uploads honestly rather than asserting work it
    did not verify.
    """
    def _say(msg):
        if detail:
            detail(msg)
        elif logger:
            logger.log_debug(msg)

    if asset is None or not asset.is_file():
        return False                       # no art for this family — skip quietly
    version = asset_version(asset)
    if version is None:
        return False

    if not force and cache is not None:
        try:
            if cache.get(cache_key) == version:
                return False               # already carries exactly this art
        except Exception:
            pass                           # unreadable cache => attempt the upload

    if not armed:
        _say(f"[Poster] [disarmed] '{label or asset.stem}' poster would be set "
             f"({asset.name}).")
        return False

    if rating_key is None or plex_api is None:
        return False
    try:
        data = asset.read_bytes()
    except OSError:
        if logger:
            logger.log_warning(f"[Poster] could not read '{asset.name}' — skipped.")
        return False
    if not data:
        return False

    # Only cache on a VERIFIED 2xx. upload_playlist_poster reads the real HTTP
    # status rather than trusting the empty-200 body, so a 404/401 lands here as
    # False and the version stays uncached — which is what makes it retry.
    if not plex_api.upload_playlist_poster(rating_key, data, token=token):
        if logger:
            logger.log_warning(
                f"[Poster] upload failed for '{label or asset.stem}' (rk {rating_key}) — "
                f"not cached, will retry next run.")
        return False

    if cache is not None:
        try:
            cache.set(cache_key, version)
        except Exception:
            pass
    _say(f"[Poster] '{label or asset.stem}' poster set ({len(data)} bytes).")
    return True

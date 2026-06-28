"""
shared_storage.py — best-effort pre-flight: do two *arr instances back onto ONE mount?
================================================================================
The cross-instance MOVE (``CrossInstanceMove``) relies on the destination instance being able to
``DownloadedMoviesScan importMode=Move`` the SOURCE folder — which only works when both instances
mount the same physical storage tree (the destination can see the source file). glidearr talks only
to the Radarr HTTP APIs (no filesystem / mount / inode visibility), so this can never be PROVEN over
the API; it can only be evidenced. We use two cheap, independent signals:

  1. A common mount ancestor — the instances' root-folder paths share a meaningful prefix
     (e.g. ``/data/media/movies/standard`` and ``/data/media/movies/4k`` → ``/data/media/movies``).
  2. Equal backing capacity — both instances report the SAME disk ``total_gb`` (the size of the
     filesystem, which is stable; free space fluctuates and is not used here).

Both present → confirmed. Either missing/unreadable → NOT confirmed (FAIL-CLOSED): the caller then
degrades the move to log-only rather than issuing a Move scan that can never complete (which would
otherwise churn a scan every run on a non-shared deployment — harmless, but pointless). On the
operator's confirmed-shared stack this returns True and the move proceeds.

PURE-ish: reads only through the passed ``ArrGateway`` (root folders + ``gw.im.disk_total_gb``); no
config, no writes. Returns ``(confirmed: bool, reason: str)`` so the caller can log WHY.
"""
from __future__ import annotations

# A common ancestor of at least this many path segments counts as a shared mount root. Two segments
# (e.g. ``/data/media``) is enough to distinguish a real shared tree from an incidental ``/`` match.
_MIN_COMMON_DEPTH = 2


def _segments(path) -> list:
    """Normalised, non-empty path segments (slash-tolerant). ``/data/media/movies`` →
    ``['data','media','movies']``; ``''`` / ``None`` → ``[]``."""
    if not path:
        return []
    norm = str(path).replace("\\", "/").strip()
    return [s for s in norm.split("/") if s]


def _common_depth(a, b) -> int:
    """Length of the shared leading-segment prefix of two paths (the depth of their common
    ancestor). ``/data/media/movies/standard`` vs ``/data/media/movies/4k`` → 3."""
    sa, sb = _segments(a), _segments(b)
    n = 0
    for x, y in zip(sa, sb):
        if x != y:
            break
        n += 1
    return n


def _common_ancestor(a, b) -> str:
    sa = _segments(a)
    depth = _common_depth(a, b)
    return "/" + "/".join(sa[:depth]) if depth else ""


def _totals_match(ta, tb) -> bool:
    """Two disk totals are the same backing filesystem when they agree within rounding noise
    (1 GB or 1%, whichever is larger)."""
    try:
        ta, tb = float(ta), float(tb)
    except (TypeError, ValueError):
        return False
    if ta <= 0 or tb <= 0 or ta == float("inf") or tb == float("inf"):
        return False
    return abs(ta - tb) <= max(1.0, 0.01 * max(ta, tb))


def shared_storage_confirmed(gw, src_inst, dst_inst, *, min_common_depth: int = _MIN_COMMON_DEPTH):
    """Best-effort: do ``src_inst`` and ``dst_inst`` share one backing mount? Returns
    ``(confirmed, reason)``. FAIL-CLOSED — any unreadable signal returns ``(False, …)`` so the
    caller degrades a cross-instance move to log-only instead of churning an impossible Move scan."""
    try:
        src_roots = [f.get("path") for f in (gw.root_folders(src_inst) or []) if isinstance(f, dict)]
        dst_roots = [f.get("path") for f in (gw.root_folders(dst_inst) or []) if isinstance(f, dict)]
    except Exception as e:                                   # pragma: no cover - defensive
        return False, f"root folders unreadable ({e})"
    src_roots = [p for p in src_roots if p]
    dst_roots = [p for p in dst_roots if p]
    if not src_roots or not dst_roots:
        return False, f"no root folder reported for {'source' if not src_roots else 'dest'} instance"

    best_depth, best_anc = 0, ""
    for a in src_roots:
        for b in dst_roots:
            d = _common_depth(a, b)
            if d > best_depth:
                best_depth, best_anc = d, _common_ancestor(a, b)
    if best_depth < min_common_depth:
        return False, ("root folders share no common mount ancestor "
                       f"(deepest match '{best_anc or '/'}') → likely separate storage")

    im = getattr(gw, "im", None)
    if im is None or not hasattr(im, "disk_total_gb"):
        return False, "no disk-capacity reader available to confirm a shared mount"
    try:
        ta = im.disk_total_gb(src_inst)
        tb = im.disk_total_gb(dst_inst)
    except Exception as e:
        return False, f"disk capacity unreadable ({e})"
    if not _totals_match(ta, tb):
        return False, (f"disk capacity differs ({ta} vs {tb} GB) → separate mounts despite "
                       f"common ancestor '{best_anc}'")
    return True, f"shared mount confirmed: common root '{best_anc}', equal capacity ~{float(ta):.0f} GB"

"""bin_verify.py — is a file in the recycle bin REDUNDANT, or is it the last copy?

`bin_forecast.py` answers "how much space will the bin give back". It counts
every deleted file as reclaimable, which is correct arithmetic about DISK and is
not a statement about SAFETY.

The distinction matters the moment anything acts on that number. An *arr deletes
a file for one of two very different reasons:

    an UPGRADE replaced it       -> a better copy exists; the bin copy is spare
    the upgrade FAILED / partial -> the bin copy is the ONLY copy

Both look identical in the delete history. Purging the second is data loss, and
it is silent: the movie simply has no file afterwards and nothing says why.

So this module classifies each binned entry against what the library looks like
NOW:

    REDUNDANT   the movie has a file, and it is a DIFFERENT file than the one
                binned (`movie_file_id` changed), and that file has a real size
    LAST_COPY   the movie has NO file - whatever was binned was not replaced
    UNKNOWN     the movie is not in the parquet at all, or the ids do not
                resolve well enough to say

UNKNOWN IS NOT REDUNDANT, and that asymmetry is the entire point of the module.
A verifier that guesses in the permissive direction is worse than no verifier,
because it converts "we do not know" into "safe to delete" while wearing the
authority of a check. Every ambiguity here resolves to LAST_COPY or UNKNOWN, and
only an affirmative, positively-evidenced replacement earns REDUNDANT.

WHY `movie_file_id` AND NOT SIZE OR QUALITY. A re-grab of the same release
produces the same size and the same quality string, so neither distinguishes
"replaced" from "never left". The file id is minted per imported file and always
changes on a genuine replacement, which makes it the only field that answers the
question being asked.

TIMING MATTERS TOO. A file id that changed BEFORE the delete says nothing about
this delete - it is evidence of some earlier upgrade. Only a current file added
AT OR AFTER the binned file's deletion can be its replacement, which is why
`date_added` is consulted rather than trusted implicitly.

PURE. No I/O, no manager, no API. History rows + a movie frame in, verdicts out.
"""
from __future__ import annotations

VERDICT_REDUNDANT = "redundant"
VERDICT_LAST_COPY = "last_copy"
VERDICT_UNKNOWN = "unknown"

#: Grace applied when comparing the replacement's ``date_added`` against the
#: delete timestamp. An *arr writes the import and the delete within the same
#: operation but not the same instant, and clock skew between the *arr host and
#: this one is real. Generous on purpose: too SMALL a window misclassifies a
#: genuine replacement as LAST_COPY, which is the safe direction but makes the
#: verifier useless by rejecting everything.
REPLACEMENT_GRACE_SECONDS = 3600.0

_GB = 1024.0 ** 3


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None                       # NaN-safe


def _ts(value):
    """Unix seconds from an epoch or an ISO-8601 string, or None."""
    if value is None:
        return None
    num = _num(value)
    if num is not None and not isinstance(value, str):
        return num
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return num


def movie_index(rows) -> dict:
    """``{movie_id: {file_id, has_file, size, added, title}}`` from the Radarr frame.

    Keyed on ``movie_id`` - Radarr's own internal id - because that is what its
    history rows carry. tmdb is the stable identity everywhere else in glidearr,
    but the join being made here is history-to-library within one Radarr, and
    within that boundary ``movie_id`` is both correct and unambiguous.
    """
    out = {}
    for r in (rows or ()):
        if not isinstance(r, dict):
            continue
        mid = r.get("movie_id")
        if mid in (None, ""):
            continue
        out[str(mid)] = {
            "file_id": (str(r.get("movie_file_id"))
                        if r.get("movie_file_id") not in (None, "") else None),
            "has_file": bool(r.get("has_file")),
            "size": _num(r.get("size_bytes")) or 0.0,
            "added": _ts(r.get("date_added") or r.get("added_at")),
            "title": r.get("title"),
            "quality": r.get("quality_name"),
        }
    return out


def _binned_file_id(row):
    """The file id that was DELETED, from a history row. None when absent."""
    data = (row or {}).get("data") or {}
    for key in ("movieFileId", "movie_file_id", "fileId"):
        for src in (data, row or {}):
            v = (src or {}).get(key)
            if v not in (None, ""):
                return str(v)
    return None


def verify(row, index, *, grace: float = REPLACEMENT_GRACE_SECONDS) -> dict:
    """Classify ONE binned history row. Returns ``{verdict, reason, ...}``.

    Never raises and never returns REDUNDANT on incomplete evidence - see the
    module docstring on why the permissive direction is the dangerous one.
    """
    mid = (row or {}).get("movieId") or ((row or {}).get("data") or {}).get("movieId")
    base = {"movie_id": (str(mid) if mid not in (None, "") else None),
            "binned_file_id": _binned_file_id(row),
            "deleted_at": _ts((row or {}).get("date"))}

    if base["movie_id"] is None:
        return dict(base, verdict=VERDICT_UNKNOWN,
                    reason="history row carries no movieId")
    cur = index.get(base["movie_id"])
    if cur is None:
        # The movie left the library entirely. The bin copy may be the only trace
        # of it, so this is emphatically not 'safe to delete'.
        return dict(base, verdict=VERDICT_UNKNOWN,
                    reason="movie is not in the library frame")
    base["title"] = cur.get("title")

    if not cur["has_file"] or not cur["file_id"]:
        return dict(base, verdict=VERDICT_LAST_COPY,
                    reason="movie currently has NO file - nothing replaced it")

    if base["binned_file_id"] is None:
        # Cannot prove the current file is a DIFFERENT one. It very likely is,
        # but 'likely' is exactly what this module refuses to act on.
        return dict(base, verdict=VERDICT_UNKNOWN,
                    reason="delete event does not name the file it removed")

    if cur["file_id"] == base["binned_file_id"]:
        # The library still points at the file that was supposedly deleted -
        # either the delete did not take, or the history row is stale. Either way
        # the bin copy might be live.
        return dict(base, verdict=VERDICT_LAST_COPY,
                    reason="library still references the binned file id")

    if cur["size"] <= 0:
        return dict(base, verdict=VERDICT_LAST_COPY,
                    reason="replacement file has zero size - import likely failed")

    if base["deleted_at"] is not None and cur["added"] is not None:
        if cur["added"] + grace < base["deleted_at"]:
            # The current file predates the delete, so it is not this delete's
            # replacement; something else was removed and never replaced.
            return dict(base, verdict=VERDICT_LAST_COPY,
                        reason="current file predates the deletion")

    return dict(base, verdict=VERDICT_REDUNDANT, replacement_file_id=cur["file_id"],
                replacement_size=cur["size"], replacement_quality=cur.get("quality"),
                reason="a different, sized file was imported at or after the delete")


def verify_all(history, movie_rows, *, is_bin_entry=None,
               grace: float = REPLACEMENT_GRACE_SECONDS) -> list:
    """Classify every binned history row. ``[{verdict, ...}]``.

    ``is_bin_entry`` filters history to deletes that actually reach the bin;
    pass ``bin_forecast.is_bin_entry`` so the two modules agree on what a
    bin entry IS rather than each deciding separately (P-E).
    """
    index = movie_index(movie_rows)
    keep = is_bin_entry or (lambda r: True)
    return [verify(r, index, grace=grace) for r in (history or ()) if keep(r)]


def safe_reclaim_gb(verdicts, sizes_by_file_id=None) -> dict:
    """``{redundant_gb, withheld_gb, counts}`` — reclaim split by verdict.

    ``redundant_gb`` is the ONLY figure a purge may act on. ``withheld_gb`` is
    what a naive forecast would have counted and this module refused, which is
    the number worth logging: it is the measure of what the verifier is buying.
    """
    sizes = sizes_by_file_id or {}
    counts = {VERDICT_REDUNDANT: 0, VERDICT_LAST_COPY: 0, VERDICT_UNKNOWN: 0}
    red = held = 0.0
    for v in (verdicts or ()):
        verdict = v.get("verdict", VERDICT_UNKNOWN)
        counts[verdict] = counts.get(verdict, 0) + 1
        size = _num(sizes.get(v.get("binned_file_id"))) or 0.0
        if verdict == VERDICT_REDUNDANT:
            red += size
        else:
            held += size
    return {"redundant_gb": red / _GB, "withheld_gb": held / _GB, "counts": counts}

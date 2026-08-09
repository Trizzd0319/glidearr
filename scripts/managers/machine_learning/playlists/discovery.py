"""discovery.py — what a surfaced list actually TAUGHT us about someone's taste.

The gap this fills, in one sentence: the household watched Iron Giant off the
Anniversary shelf, enjoyed it, and the system learned nothing beyond "a play
happened".

The play WAS captured - it went into Tautulli history and nudged genre affinity
and the people graph like any other watch. What was lost is that it came from a
list we chose to show. To the taste model, being surfaced something and seeking
it out deliberately are the same event, so a shelf that works and a shelf that is
ignored are indistinguishable in everything downstream of the play.

SOURCE: ``labels/recommendations.py``, the durable ledger.

That choice was made the hard way. This module first read ``provenance.py``, and
the instinct on noticing the duplication was to fold the ESTABLISHED ledger into
the NEW one - exactly backwards. ``labels/recommendations.py`` is parquet-backed,
month-partitioned, deduped on ``(recommended_date, surface, profile,
entity_id)``, surface-parameterised and already carrying live production rows;
``provenance.py`` was an in-memory dict with no dedup that nothing ever wrote to.
The duplicate was the newer one. It is now deleted (`GLD-PLY-25`).

READ-TIME IDENTITY IS SAFE; WRITE-TIME IDENTITY IS NOT.

The ledger stores stable ids (tmdb / ``tvdb:s:e``); Tautulli plays carry Plex
ratingKeys. Something has to bridge them, and the bridge is supplied by the
CALLER as ``entity_of_play``. Doing that inversion HERE, at read time, is
acceptable in a way it was not at write time: a ratingKey retired by a re-scan
yields an unmatched play - an undercount, visible as a lower hit rate - whereas
the same inversion at write time would have recorded a DIFFERENT title that
inherited the number, which is a silently-wrong row nothing downstream can catch.
Fail toward missing, never toward wrong.

WHY A DISCOVERY PLAY IS NOT AN ORDINARY PLAY. Nobody went looking for it. So
completing it reveals LATENT taste rather than confirming known taste, and that
is the more useful signal: known taste already dominates the affinity model by
sheer volume. The counter-argument is real though - it may have been watched
because it was simply there, at the end of a long day.

WHICH IS WHY COMPLETION IS THE GATE, not the play. ``percent_complete`` is on
every Tautulli row. Surfaced-and-finished is enjoyment; surfaced and abandoned at
20% is a misfire, and counting it would teach the model to recommend more of a
thing that was actively rejected. ``DEFAULT_MIN_PCT`` matches the threshold
Hidden Gems already uses for the same judgement, so the system has ONE definition
of "they actually watched it".

THE WEIGHT IS A HYPOTHESIS, NOT A FINDING. ``DEFAULT_DISCOVERY_WEIGHT`` is 1.0 -
no boost - deliberately, and `GLD-PLY-28` is filed to measure it. The last
confidently-reasoned constant in this package (a 0.28 day threshold) was wrong by
4.5x when finally backtested.

PURE. No I/O, no manager, no config. Events + plays in, signal out.
"""
from __future__ import annotations

#: Completion at or above which a surfaced play counts as ENJOYED. Matches
#: ``hidden_gems.play_min_pct`` so "they actually watched it" means one thing
#: across the system rather than two things that drift.
DEFAULT_MIN_PCT = 85.0

#: Multiplier applied to a discovery-sourced play when contributing to affinity.
#: 1.0 = NO BOOST. See the module docstring: the case for >1.0 is untested.
DEFAULT_DISCOVERY_WEIGHT = 1.0

#: Surfaces whose whole purpose is showing you something you were not looking
#: for. A play sourced from Up Next is not a discovery - it is the next episode of
#: a show already being watched, which the affinity model has counted many times.
DISCOVERY_SURFACES = frozenset({"hidden_gems", "anniversary", "tonight"})

#: How long after being surfaced a play still counts as caused by it. Matches the
#: ledger's own ``window_days`` default; a row carrying a LONGER window is
#: honoured at its own length, so shortening a config knob never retroactively
#: invalidates a pick still being measured.
DEFAULT_WINDOW_DAYS = 30

_DAY = 86400.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None                 # NaN-safe


def _pct(row):
    """``percent_complete`` as a float, or None when absent/unusable.

    None is NOT zero. A row with no completion figure is a play we cannot judge,
    and treating it as 0% would silently discard every play from a source that
    does not report progress (P-C).
    """
    return _num((row or {}).get("percent_complete"))


def _norm(text) -> str:
    return " ".join(str(text or "").strip().casefold().split())


def ledger_keys(event) -> list:
    """Every identity a ledger row can be matched on.

    THREE, not one, for the reason ``watched_episode_keys`` carries three: a Plex
    re-scan RETIRES ratingKeys, and a play recorded before it stops matching -
    measured at 11/117 on one series. Here the stale handle is on the PLAY (it was
    written at play time), so no amount of re-looking-up the ITEM helps; the join
    itself has to tolerate the churn.

      entity      the stable tmdb / ``tvdb:s:e`` id - authoritative
      rk          the ratingKey AS SURFACED, which a play soon after will carry
      title/year  survives a re-scan entirely, and is the only one that does
    """
    keys = []
    eid = event.get("entity_id")
    if eid not in (None, ""):
        keys.append(("entity", str(eid)))
    rk = event.get("rating_key")
    if rk not in (None, "") and _num(rk) != _num("nan"):
        text = str(rk).strip()
        if text and text.lower() != "nan":
            keys.append(("rk", text))
    title, year = event.get("title"), event.get("year")
    y = _num(year)
    if title and y is not None:
        keys.append(("ty", _norm(title), int(y)))
    elif title and str(event.get("media") or "").lower() == "show":
        # A SHOW placement usually has no year; its title is the only textual
        # identity it has, and it is matched against a play's grandparent_title.
        keys.append(("show", _norm(title)))
    return keys


def play_keys(row, *, entity_of_play) -> list:
    """Every identity a Tautulli play can be matched on. Mirrors :func:`ledger_keys`.

    THE EPISODE/SHOW HIERARCHY IS NOT OPTIONAL. Surfaces record at the level they
    RECOMMEND at: Anniversary surfaces SHOWS (``tvdb:366358``), because a show has
    an anniversary and an individual episode does not. Plays arrive at EPISODE
    level (``tvdb:366358:1:2``). Matched literally those never meet, and a live
    ledger of 121 real show placements against 979 real plays produced ZERO
    matches - not because nothing was watched, but because the two sides were
    speaking at different depths.

    So an episode play also yields its SHOW key. Watching any episode of a show
    whose anniversary was surfaced IS the outcome that placement was predicting;
    requiring the exact episode would measure something nobody recommended.

    The reverse does NOT hold, deliberately: a show-level placement is satisfied
    by any episode, but an EPISODE-level placement (Tonight surfaces a specific
    next episode) must not be credited by watching some other episode of that
    series. Specific recommendations are held to specific outcomes.

    The episode form uses ``grandparent_title`` + season/episode indices rather
    than the episode's own title, because an episode title is not unique across
    series and a season/episode pair is not unique without one.
    """
    keys = []
    eids = entity_of_play(row)
    # A resolver may return ONE id or SEVERAL. Several is the useful case: a play
    # can legitimately answer placements recorded at different depths, and
    # `plex/playlists/identity.build_resolver` returns every identity it can
    # establish rather than making the caller pick one and lose the others.
    if eids in (None, ""):
        eids = ()
    elif isinstance(eids, (str, bytes)):
        eids = (eids,)
    for eid in eids:
        if eid in (None, ""):
            continue
        eid = str(eid)
        keys.append(("entity", eid))
        # tvdb:<series>:<season>:<episode> -> also tvdb:<series>
        parts = eid.split(":")
        if len(parts) >= 4 and parts[0] == "tvdb":
            keys.append(("entity", ":".join(parts[:2])))
    rk = (row or {}).get("rating_key")
    if rk not in (None, ""):
        keys.append(("rk", str(rk).strip()))
    grk = (row or {}).get("grandparent_rating_key")
    if grk not in (None, ""):
        # A show placement carries the SERIES ratingKey as its secondary id, so a
        # play must offer its grandparent to meet it.
        keys.append(("rk", str(grk).strip()))
    if str((row or {}).get("media_type") or "").lower() == "episode":
        show = row.get("grandparent_title")
        s, e = _num(row.get("parent_media_index")), _num(row.get("media_index"))
        if show and s is not None and e is not None:
            keys.append(("se", _norm(show), int(s), int(e)))
        if show:
            # Show-level title match, for a show placement whose ids all failed.
            keys.append(("show", _norm(show)))
    else:
        title, y = row.get("title"), _num(row.get("year"))
        if title and y is not None:
            keys.append(("ty", _norm(title), int(y)))
    return keys


def plays_by_entity(plays, *, entity_of_play):
    """``{identity_key: {"first": ts, "best_pct": float}}`` over ALL identities.

    One play is indexed under every key it can be recognised by, so a ledger row
    matching on ANY of them finds it. Over-indexing is safe: the keys are
    namespaced by kind, so a ratingKey cannot collide with a title/year pair.

    BEST completion is kept, not the last: someone who bounced at 5% and came back
    to finish it did finish it, and the abandoned first attempt is not the verdict.
    EARLIEST play is kept, because the window test asks whether the recommendation
    was acted on, and the first action answers it.
    """
    out: dict = {}
    for p in (plays or ()):
        if not isinstance(p, dict):
            continue
        ts = _num(p.get("date"))
        if ts is None:
            continue
        pct = _pct(p)
        for key in play_keys(p, entity_of_play=entity_of_play):
            cur = out.setdefault((key, p.get("profile")), {"first": ts, "best_pct": None})
            if ts < cur["first"]:
                cur["first"] = ts
            if pct is not None:
                cur["best_pct"] = pct if cur["best_pct"] is None else max(cur["best_pct"], pct)
    return out


def _match(index, event):
    """The best play for one ledger row, or None. Tries every identity in order."""
    profile = event.get("profile")
    for key in ledger_keys(event):
        hit = index.get((key, profile))
        if hit:
            return hit
    return None


def completions(events, plays, *, entity_of_play, now=None,
                min_pct: float = DEFAULT_MIN_PCT,
                surfaces=DISCOVERY_SURFACES,
                window_days: int = DEFAULT_WINDOW_DAYS) -> list:
    """Picks a list SURFACED that the viewer then FINISHED.

    ``events`` are ledger rows - a list of dicts, or anything with
    ``to_dict("records")`` (a ``recommendations.load_events`` frame passes
    straight in).

    Returns ``[{entity_id, surface, profile, title, media, recommended_at,
    watched_at, days_to_watch, percent_complete, taste_score}]``.

    THE CAUSALITY GUARD IS NOT OPTIONAL. A play STRICTLY BEFORE the row's
    ``recommended_at`` never counts: a watch that predates the recommendation
    cannot have been caused by it, and crediting it would let a list take credit
    for a habit it merely described. That is the same rule
    ``gems.classify_outcome`` enforces, kept identical here on purpose.
    """
    rows = _records(events)
    if not rows:
        return []
    idx = plays_by_entity(plays, entity_of_play=entity_of_play)
    out = []
    for e in rows:
        surface = e.get("surface")
        if surfaces is not None and surface not in surfaces:
            continue
        rec = _epoch(e.get("recommended_at"))
        if rec is None:
            continue
        hit = _match(idx, e)
        if not hit:
            continue
        played = hit["first"]
        if played < rec:
            continue                              # cannot have been caused by it
        try:
            row_window = int(e.get("window_days") or window_days)
        except (TypeError, ValueError):
            row_window = window_days
        if played - rec > max(row_window, window_days) * _DAY:
            continue                              # outside the measurement window
        pct = hit["best_pct"]
        if pct is None or pct < float(min_pct):
            continue
        out.append({
            "entity_id": e.get("entity_id"), "surface": surface,
            "profile": e.get("profile"), "title": e.get("title"),
            "media": e.get("media"), "recommended_at": rec,
            "watched_at": played, "days_to_watch": round((played - rec) / _DAY, 2),
            "percent_complete": pct, "taste_score": e.get("taste_score"),
        })
    return out


def affinity_contributions(rows, *, attributes_of,
                           weight: float = DEFAULT_DISCOVERY_WEIGHT) -> dict:
    """``{profile: {attribute: weight}}`` from discovery completions.

    ``attributes_of(entity_id, row) -> iterable`` is supplied by the caller and
    returns whatever the taste model groups on - genres, people, a franchise.
    Keeping it a callable means this module never learns the shape of the
    affinity model, so a change there cannot break a change here.

    ADDITIVE and per-profile, and deliberately NOT normalised: the caller owns how
    a discovery signal blends with ordinary history, because only the caller knows
    the scale of the thing it is being added to.
    """
    out: dict = {}
    for r in (rows or ()):
        eid = r.get("entity_id")
        if eid is None:
            continue
        bucket = out.setdefault(r.get("profile"), {})
        for attr in (attributes_of(eid, r) or ()):
            if attr in (None, ""):
                continue
            bucket[str(attr)] = bucket.get(str(attr), 0.0) + float(weight)
    return out


def summary(events, plays, *, entity_of_play, now=None,
            min_pct: float = DEFAULT_MIN_PCT,
            window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """``{surface: {surfaced, played, completed, completion_rate}}``.

    The number that answers "is this shelf earning its place" - and note it is
    DIFFERENT from a hit rate, which asks whether a family got a play at all.
    This asks whether the play was any GOOD. A shelf people start and abandon
    scores well on the first and badly here, and that distinction is exactly what
    is worth seeing before deciding a family works.

    ``completion_rate`` is None, never 0.0, for a surface with no plays yet - no
    evidence is not a bad result (P-C).
    """
    rows = _records(events)
    idx = plays_by_entity(plays, entity_of_play=entity_of_play)
    surfaced: dict = {}
    played: dict = {}
    completed: dict = {}
    for e in rows:
        s = e.get("surface")
        surfaced[s] = surfaced.get(s, 0) + 1
        rec = _epoch(e.get("recommended_at"))
        hit = _match(idx, e)
        if rec is None or not hit or hit["first"] < rec:
            continue
        played[s] = played.get(s, 0) + 1
        pct = hit["best_pct"]
        if pct is not None and pct >= float(min_pct):
            completed[s] = completed.get(s, 0) + 1
    return {s: {"surfaced": n, "played": played.get(s, 0),
                "completed": completed.get(s, 0),
                "completion_rate": (completed.get(s, 0) / played[s]) if played.get(s) else None}
            for s, n in surfaced.items()}


# ── input coercion ───────────────────────────────────────────────────────────
def _records(events) -> list:
    """Ledger rows as a list of dicts, from a DataFrame or an iterable."""
    if events is None:
        return []
    to_dict = getattr(events, "to_dict", None)
    if callable(to_dict) and hasattr(events, "columns"):
        if getattr(events, "empty", False):
            return []
        return events.to_dict("records")
    return [e for e in events if isinstance(e, dict)]


def _epoch(value):
    """``recommended_at`` (ISO-8601 or epoch) -> POSIX seconds, or None.

    Accepts both because the ledger stores ISO strings while tests and callers
    frequently hold epochs; a mismatch here would silently drop every row.
    """
    if value is None:
        return None
    num = _num(value)
    if num is not None and not isinstance(value, str):
        return num
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return num

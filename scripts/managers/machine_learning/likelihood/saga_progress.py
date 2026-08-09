"""saga_progress.py — per-user progress through a universe / franchise tree.

A SEPARATE TRACKED ARTIFACT, deliberately. Nothing here feeds scoring, acquisition or
deletion: it is a read-only projection built from data the run already holds, persisted
under one stable key so a future web layer can render it without re-deriving anything.
Adding a field here can never change what the pipeline does.

WHAT IT ANSWERS, per (user, universe):
  * how far through each individual SHOW the user is,
  * how far through the WHOLE universe they are — every episode of every member show AND
    every film, owned or not,
  * how long the remainder would take at that user's own observed viewing rate.

INPUTS (all already produced by the run):
  * ``universes``    — the universe source cache: {key: {shows: [tvdb], movies: [tmdb],
                       titles: {"show:<tvdb>"|"movie:<tmdb>": title}}}
  * ``series_stats`` — {tvdb: {"title", "episode_count", "runtime_minutes"}} from the Sonarr
                       series library cache (``statistics.episodeCount`` + series
                       ``runtime``). This is the DENOMINATOR and it counts EVERY episode
                       that exists, owned or not — the household acquires the gaps as it
                       progresses, so progress is measured against the whole story, not
                       against today's disk.

                       Neither parquet can supply this. episode_files is mostly pilot stubs
                       (episode_number on 647 of 12622 rows), and owned_episodes is a
                       PARTIAL file inventory — it carried 9 Blue Bloods rows where Sonarr
                       reports 293 episodes and 293 episode files.
  * ``watched_keys`` — the per-user watched identity set from
                       ``tv_resolver.watched_episode_keys``; the ``(series, season,
                       episode)`` tuples are what match here.
  * ``movie_stats``  — {tmdb: {"title", "runtime_minutes"}} from the Radarr movie caches.

PURE — no I/O, no manager, no config mutation. Deterministic given its inputs.
"""
from __future__ import annotations

import re
import unicodedata

# Fallbacks for members whose runtime is unknown. NEVER zero: an unowned show or film is
# usually the largest single chunk of remaining time, and zeroing it reports a barely
# started saga as finishable today.
_DEFAULT_EPISODE_MINUTES = 25
_DEFAULT_MOVIE_MINUTES = 110

# Placeholder episode count for a universe member the local library has never seen. Its
# real length is unknowable without a lookup this module deliberately does not make (it is
# PURE), so one nominal season is the floor — wrong, but far closer than zero, and surfaced
# as ``shows_unacquired`` so a UI can caveat it.
_UNACQUIRED_SHOW_EPISODES = 10

# How far back the viewing-rate window looks. Long enough to survive a quiet fortnight,
# short enough that a binge six months ago cannot promise a rate the user no longer has.
_RATE_WINDOW_DAYS = 90

# How many times an auto-derived cluster's key-member a sibling must exceed before it
# takes over the label. 3x separates "this family is really about that show" from "that
# one merely ran a bit longer".
_DOMINANT_MEMBER_RATIO = 3.0


def _norm(s) -> str:
    """Byte-for-byte ``tv_resolver._norm``. DO NOT "improve" this.

    The watched identities this module joins against are built by that function, so any
    divergence scores every episode of an affected series as unwatched — silently, with no
    error. An earlier draft added NFKD accent-folding, which looks strictly better and is
    strictly wrong: it maps "Pokemon" where tv_resolver keeps the accent, orphaning all
    1123 owned episodes of that one series alone.

    If normalisation ever needs to change, change it THERE and let this follow.
    """
    return str(s or "").strip().lower()


def _squash(s) -> str:
    """A title squashed to bare ``[a-z0-9]``, accent-folded — the same shape
    ``universe_order._stem_norm`` / ``_collection_norm`` reduce a title to when they MINT a
    cluster key. Reproduced here rather than imported to keep this module dependency-free;
    it is only ever used for a DISPLAY tiebreak, so drift degrades a label, never a
    decision. (Contrast ``_norm``, which must match tv_resolver byte-for-byte because it
    gates a join.)
    """
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def watched_counts_by_tvdb(watched_keys, title_to_tvdb) -> dict:
    """``{tvdb: distinct_episodes_watched}`` from a user's watched identity set.

    Watched identities are ``(norm_series_title, season, episode)`` — title-keyed, because
    that is what Tautulli and Trakt agree on. ``title_to_tvdb`` maps the normalised series
    title onto its tvdb so the count can join the series stats. Only the 3-tuple form is
    counted; the ``(series, episode_title)`` identity describes the SAME play and would
    double every episode.
    """
    out: dict = {}
    seen: dict = {}
    for k in (watched_keys or ()):
        if not (isinstance(k, tuple) and len(k) == 3):
            continue
        st, season, ep = k
        tv = (title_to_tvdb or {}).get(st)
        if tv is None:
            continue
        bucket = seen.setdefault(tv, set())
        if (season, ep) in bucket:
            continue
        bucket.add((season, ep))
        out[tv] = out.get(tv, 0) + 1
    return out


def series_progress(series_stats, watched_counts, tvdbs=None) -> dict:
    """``{tvdb: {"title", "watched", "total", "remaining", "pct"}}`` against ALL episodes.

    ``total`` is the series' full episode count — owned or not. A show the household holds
    only the pilot of still reports against its whole run, because the gaps get acquired as
    the household progresses. ``watched`` is clamped to ``total``: a rewatch or a stale
    identity must never produce 140%.
    """
    out: dict = {}
    want = None if tvdbs is None else {t for t in (_coerce_int(x) for x in tvdbs) if t is not None}
    for tv, meta in (series_stats or {}).items():
        tv = _coerce_int(tv)
        if tv is None or (want is not None and tv not in want):
            continue
        total = max(0, _coerce_int((meta or {}).get("episode_count")) or 0)
        if not total:
            continue
        w = min(total, max(0, _coerce_int((watched_counts or {}).get(tv)) or 0))
        out[tv] = {"title": (meta or {}).get("title") or "", "watched": w, "total": total,
                   "remaining": total - w, "pct": round(100.0 * w / total, 1)}
    return out


def remaining_minutes(series_stats, progress, tvdbs) -> int:
    """Unwatched EPISODE minutes across ``tvdbs``, using each series' own runtime.

    A series with no runtime falls back to the MEDIAN runtime of the others in the same
    universe, then to the global default — never to zero.
    """
    want = {t for t in (_coerce_int(x) for x in (tvdbs or ())) if t is not None}
    runtimes = [r for r in (_coerce_int(((series_stats or {}).get(t) or {}).get("runtime_minutes"))
                            for t in want) if r]
    med = sorted(runtimes)[len(runtimes) // 2] if runtimes else _DEFAULT_EPISODE_MINUTES
    total = 0
    for tv in want:
        p = (progress or {}).get(tv)
        if not p:
            continue
        rt = _coerce_int(((series_stats or {}).get(tv) or {}).get("runtime_minutes")) or med
        total += p["remaining"] * rt
    return int(total)


def viewing_rate(watch_timestamps, *, runtime_lookup=None,
                 window_days=_RATE_WINDOW_DAYS, now_ts=None) -> dict:
    """The user's OBSERVED consumption rate over the trailing window.

    Returns ``{"episodes_per_day", "minutes_per_day", "episodes_in_window", "window_days",
    "active_days"}``.

    ``episodes_per_day`` is the human-facing figure — "3 a day" / "1 a week" is how a
    household actually experiences a backlog. It divides by the WHOLE window, not by active
    days: a viewer who binges 12 episodes every other Sunday finishes a season in a
    fortnight, not in a day, and dividing by active days would promise the latter.
    ``active_days`` is reported alongside so a UI can label "bingeing" vs "steady" without
    changing the estimate.

    ``minutes_per_day`` is what the ETA actually divides by: a universe mixes 22-minute
    episodes with 150-minute films, so an episode count cannot price the remainder.

    Trailing window ⇒ self-correcting: as the household's habits change, every ETA moves
    with them on the next run.

    All zero when there is no in-window activity: the caller renders that as "no estimate",
    NEVER as an infinite or instant ETA.
    """
    if not watch_timestamps:
        return {"episodes_per_day": 0.0, "minutes_per_day": 0.0, "episodes_in_window": 0,
                "window_days": window_days, "active_days": 0}
    import time as _time
    now = float(now_ts if now_ts is not None else _time.time())
    cutoff = now - (window_days * 86400)
    n = 0
    mins = 0.0
    days: set = set()
    for _ident, ts in watch_timestamps.items():
        try:
            t = float(ts or 0)
        except (TypeError, ValueError):
            continue
        if t < cutoff or t > now:
            continue
        n += 1
        mins += float((runtime_lookup or {}).get(_ident) or _DEFAULT_EPISODE_MINUTES)
        days.add(int(t // 86400))
    return {"episodes_per_day": round(n / float(window_days), 3) if window_days else 0.0,
            "minutes_per_day": round(mins / float(window_days), 1) if window_days else 0.0,
            "episodes_in_window": n, "window_days": window_days, "active_days": len(days)}


def movie_progress(movie_tmdbs, movie_stats, watched_tmdbs, fallback_titles=None) -> dict:
    """``{tmdb: {title,watched,total,remaining,pct,runtime_minutes,owned}}`` for the FILM
    members of a universe.

    ``total`` is always 1 — a film is one item — so the rollup can add films and episodes
    into one denominator. Unowned films still count: the MCU is 45 films whether or not the
    household holds them yet.

    ``fallback_titles`` names the films Radarr has never seen. Without it every unowned film
    renders blank, which for Star Wars and the DC Universe is EVERY film — the household
    owns none of either locally while the universe entry names all 12 and all 17.

    ``owned`` separates "not watched" from "not acquired". Both leave the same hole in the
    percentage, but only one of them is a watch decision.
    """
    out: dict = {}
    for x in (movie_tmdbs or ()):
        tm = _coerce_int(x)
        if tm is None:
            continue
        meta = (movie_stats or {}).get(tm)
        owned = meta is not None
        meta = meta or {}
        w = 1 if tm in (watched_tmdbs or ()) else 0
        out[tm] = {"title": meta.get("title") or (fallback_titles or {}).get(tm) or "",
                   "watched": w, "total": 1, "remaining": 1 - w,
                   "pct": 100.0 if w else 0.0,
                   "runtime_minutes": _coerce_int(meta.get("runtime_minutes")) or 0,
                   "owned": owned}
    return out


def universe_progress(universes, series_stats, watched_counts, *,
                      rate=None, owned_tvdbs=None, min_owned=1,
                      movie_stats=None, watched_tmdbs=None, canonical_name=None) -> list:
    """Per-universe rollup for ONE user, least-finished-first.

    Counts EVERY member of the universe — all episodes of every show AND every film. The
    MCU is Agents of S.H.I.E.L.D. plus its sibling series plus 45 films; the Arrowverse is
    The Flash plus Arrow plus Legends plus the rest. Anything less understates the
    remainder and produces a flattering, useless percentage.

    Only universes the household OWNS at least ``min_owned`` shows of are reported — a
    catalog family present purely as an acquisition target is not progress. Once it
    qualifies, every member counts, owned or not.

    ``eta_days`` is ``None`` (never 0, never infinity) when the user has no observed rate.
    """
    mins_per_day = float((rate or {}).get("minutes_per_day") or 0.0)
    owned = {t for t in (_coerce_int(x) for x in (owned_tvdbs or ())) if t is not None}
    out: list = []
    for key, entry in (universes or {}).items():
        shows = [t for t in (_coerce_int(x) for x in ((entry or {}).get("shows") or []))
                 if t is not None]
        films = [t for t in (_coerce_int(x) for x in ((entry or {}).get("movies") or []))
                 if t is not None]
        if not shows and not films:
            continue
        if owned and shows and len([t for t in shows if t in owned]) < min_owned:
            continue
        prog = series_progress(series_stats, watched_counts, tvdbs=shows)
        mprog = movie_progress(films, movie_stats, watched_tmdbs,
                               fallback_titles=member_titles(entry, "movie"))
        if not prog and not mprog:
            continue

        # Shows the local Sonarr library has never seen carry no episode_count, so
        # series_progress drops them and they silently leave the denominator - a saga
        # reads complete because the household only owns the part it finished. Count each
        # missing member as one UNWATCHED placeholder item so the percentage stays honest
        # and the remainder includes the shows still to acquire.
        missing_shows = [t for t in shows if t not in prog]
        ep_w = sum(p["watched"] for p in prog.values())
        ep_n = sum(p["total"] for p in prog.values())
        mv_w = sum(p["watched"] for p in mprog.values())
        mv_n = len(mprog)
        w = ep_w + mv_w
        n = ep_n + mv_n + len(missing_shows)
        if not n:
            continue

        rem_min = remaining_minutes(series_stats, prog, shows)
        # Film runtimes: each unwatched film's own runtime, else the median of the films we
        # DO know in this universe, else the feature default. Never zero.
        known = [m["runtime_minutes"] for m in mprog.values() if m["runtime_minutes"]]
        med_f = sorted(known)[len(known) // 2] if known else _DEFAULT_MOVIE_MINUTES
        for m in mprog.values():
            if m["remaining"]:
                rem_min += (m["runtime_minutes"] or med_f)
        # A show the library has never seen has no episode count either, so price each as
        # one season at the universe's median episode runtime. A crude floor, but far
        # closer than zero — and it is flagged in `shows_unacquired` so a UI can caveat it.
        if missing_shows:
            _known = [_coerce_int((series_stats.get(t) or {}).get("runtime_minutes"))
                      for t in shows if (series_stats or {}).get(t)]
            _known = [r for r in _known if r]
            _med_e = sorted(_known)[len(_known) // 2] if _known else _DEFAULT_EPISODE_MINUTES
            rem_min += len(missing_shows) * _UNACQUIRED_SHOW_EPISODES * _med_e

        eta = (rem_min / mins_per_day) if mins_per_day > 0 else None
        out.append({
            "universe": key,
            "display": _display_name(key, entry, canonical_name, series_stats),
            "shows_owned": len([t for t in shows if t in owned]) if owned else len(prog),
            "shows_in_universe": len(shows),
            "shows_unacquired": len(missing_shows),
            "movies_in_universe": len(films),
            "movies_unacquired": len([m for m in mprog.values() if not m["owned"]]),
            "episodes_watched": ep_w, "episodes_total": ep_n,
            "movies_watched": mv_w, "movies_total": mv_n,
            "items_watched": w, "items_total": n, "items_remaining": n - w,
            "pct": round(100.0 * w / n, 1),
            "remaining_hours": round(rem_min / 60.0, 1),
            "eta_days": (round(eta, 1) if eta is not None else None),
            "series": sorted(({"tvdb": t, **p} for t, p in prog.items()),
                             key=lambda s: (-s["pct"], s["title"])),
            "movies": sorted(({"tmdb": t, **p} for t, p in mprog.items()),
                             key=lambda m: (-m["pct"], m["title"])),
        })
    # Least-finished first: the useful view is what is still ahead of you.
    out.sort(key=lambda u: (u["pct"], -u["items_remaining"]))
    return out


def member_titles(entry, kind: str = "show") -> dict:
    """``{id: title}`` for the members of a universe entry of the given ``kind``.

    The source stores ``titles`` as a dict keyed ``"show:<tvdb>"`` / ``"movie:<tmdb>"`` —
    NOT a list. An early draft assumed a list and raised on every universe.

    BOTH kinds matter. The entry names EVERY member, owned or not (45/45 MCU films, 12/12
    Star Wars films, 28/28 MCU shows), so this is the only source of a name for the parts
    of a saga the household has not acquired — and for several universes that is all of
    them: Star Wars and the DC Universe own zero of their films locally.
    """
    want = f"{kind}:"
    out: dict = {}
    for k, v in ((entry or {}).get("titles") or {}).items():
        ks = str(k)
        if not ks.startswith(want):
            continue
        i = _coerce_int(ks.split(":", 1)[1])
        if i is not None:
            out[i] = v
    return out


def _display_name(key, entry, canonical=None, series_stats=None) -> str:
    """Human label for a universe.

    ``canonical`` is ``universe_order.saga_display_name`` when the caller can supply it —
    the project's OWN key->name map, so 'star' reads "Star Wars Universe", 'arrow'
    "Arrowverse", 'mcu' "Marvel Cinematic Universe". Used for every real universe key.

    ``tvfran:`` keys are AUTO-DERIVED and squash into nonsense
    ('georgiemandysfirstmarriage', 'mutantturtlessupermanlegen'), and the key names
    whichever member the catalog happened to lead with — not the one anybody would
    recognise. With ``series_stats`` those are named after the member with the most
    EPISODES, the best proxy available for the anchor of a family: the Big Bang Theory
    (278 episodes) rather than its two-season spin-off.

    It is only a proxy. A long-running spin-off can out-episode its parent — Law & Order:
    SVU over the original — which is defensible here since the label is for recognition,
    not precedence.

    An earlier draft used the FIRST member title for EVERYTHING, which labelled the Star
    Wars universe "The Acolyte" and the MCU "Eyes of Wakanda" — correct by the entry's own
    chronological order, useless as a name.
    """
    k = str(key or "")
    if canonical is not None and not k.startswith("tvfran:"):
        try:
            name = canonical(k)
            if name:
                return str(name)
        except Exception:
            pass
    titles = member_titles(entry)
    # NAMING AN AUTO-DERIVED CLUSTER. The ``tvfran:`` entries carry NO titles dict — they
    # are pure tvdb lists — so names come from series_stats.
    #
    # Two signals, and each is wrong alone:
    #   * the KEY was minted from ONE member's title, but which member is incidental —
    #     'georgiemandysfirstmarriage' names a two-season spin-off of The Big Bang Theory;
    #   * EPISODE COUNT finds the biggest, but the biggest is not always the family — it
    #     named the Aqua Teen cluster "The Herculoids", a longer-running stablemate.
    #
    # So: take the key's member UNLESS another member DWARFS it. A sibling with several
    # times the episodes is the one the household would recognise; a merely-longer one is
    # not. The ratio is what separates Big Bang (285 vs ~20, a clear anchor) from The
    # Herculoids (48 vs 319 — Aqua Teen is in fact the bigger show, so the key wins).
    if series_stats:
        _key = _squash(k[len("tvfran:"):] if k.startswith("tvfran:") else k)
        exact = exact_n = None
        prefix = prefix_n = None
        best, best_n = None, -1
        for s in ((entry or {}).get("shows") or []):
            tv = _coerce_int(s)
            if tv is None:
                continue
            meta = (series_stats or {}).get(tv) or {}
            name = titles.get(tv) or meta.get("title")
            if not name:
                continue
            n = _coerce_int(meta.get("episode_count")) or 0
            sq = _squash(name)
            # EXACT beats PREFIX. A spin-off's squashed title starts with the parent's key
            # ('ncislosangeles' startswith 'ncis'), so taking the first prefix hit let
            # NCIS: Los Angeles claim the anchor slot ahead of NCIS itself.
            if _key and sq == _key and exact is None:
                exact, exact_n = name, n
            elif _key and sq.startswith(_key) and prefix is None:
                prefix, prefix_n = name, n
            if n > best_n:
                best, best_n = name, n
        anchor, anchor_n = (exact, exact_n) if exact else (prefix, prefix_n)
        if anchor and best and best is not anchor and \
                best_n >= max(1, anchor_n or 0) * _DOMINANT_MEMBER_RATIO:
            return str(best)
        if anchor:
            return str(anchor)
        if best and best_n > 0:
            return str(best)
    if titles:
        for s in ((entry or {}).get("shows") or []):      # else the entry's own order
            tv = _coerce_int(s)
            if tv is not None and tv in titles:
                return str(titles[tv])
        return str(next(iter(titles.values())))
    if k.startswith("tvfran:"):
        k = k[len("tvfran:"):]
    return k.replace("_", " ").title()


def build_progress_artifact(users, universes, series_stats, *, watched_by_user,
                            recency_by_user=None, owned_tvdbs=None, title_to_tvdb=None,
                            movie_stats=None, watched_tmdbs_by_user=None,
                            runtime_by_identity=None, canonical_name=None,
                            generated_at=None) -> dict:
    """The full web-ready artifact.

    ``{generated_at, users: {name: {rate: {...}, universes: [...]}}}`` — every value a plain
    scalar or list, so persisting is ``json.dumps`` and a web layer needs no glidearr
    imports to render it. One entry per tracked user.

    ``watched_tmdbs_by_user`` is optional: when a per-user FILM watch-set is unavailable the
    caller may pass the HOUSEHOLD set under every user, which over-reports an individual's
    film progress but never under-reports the remainder.
    """
    out: dict = {"generated_at": generated_at, "users": {}}
    for name in users or []:
        wk = (watched_by_user or {}).get(name) or set()
        rec = (recency_by_user or {}).get(name) or {}
        rate = viewing_rate(rec, runtime_lookup=runtime_by_identity)
        counts = watched_counts_by_tvdb(wk, title_to_tvdb)
        wt = (watched_tmdbs_by_user or {}).get(name) or set()
        out["users"][name] = {
            "rate": rate,
            "universes": universe_progress(
                universes, series_stats, counts, rate=rate, owned_tvdbs=owned_tvdbs,
                movie_stats=movie_stats, watched_tmdbs=wt,
                canonical_name=canonical_name),
        }
    return out

"""habits.py — WHEN does this profile watch this show, and what fits tomorrow?

Per USER, per WEEKDAY. "Raina watches Rick and Morty on Tuesdays" is the claim,
and the output is a list built for ONE profile for ONE upcoming day.

WHY NOT HOUSEHOLD. An earlier version tried to infer co-viewing from temporal
regularity and pool the household. It was dropped deliberately: a Tautulli
session is logged against ONE profile no matter how many people are in the room,
so co-viewing is not observable and the inference could never be validated.
Attributing a play to more than one person needs an explicit UI where somebody
says who was watching. That is tabled, not attempted, and nothing here guesses.

JITTER IS OFF, DELIBERATELY. A play counts only toward the day it happened on
(``DEFAULT_JITTER_SIGMA_DAYS = 0.0``). The machinery for a smoothing kernel is
still here and still correct - set the sigma and a play spreads over neighbouring
days on a circular Gaussian - but it is not used, and the reason is worth
keeping:

Spreading a Tuesday play onto Wednesday makes the show appear on WEDNESDAY'S
list too. Attribution in ``outcomes.py`` credits a scheduled family only when the
play lands on the exact day it was built for, so that Wednesday appearance could
only ever record a miss. The model would have been manufacturing its own
failures. Measured before switching it off: the Tuesday list was IDENTICAL with
the kernel on or off, and the shares got sharper (a clean Tuesday show 0.499 ->
1.000). It cost nothing and removed the bleed.

The kernel is CIRCULAR when enabled: Sunday is adjacent to Monday, and a linear
distance would split a Sunday/Monday habit into two.

RECENCY DECAY on top: a show binged solid last spring and untouched since is not
what belongs on tomorrow's list. Half-life in days, default 45.

PURE. No I/O, no manager, no config. Rows in, numbers out.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

WEEK = 7

#: Kernel width in days. ZERO: a play counts only toward the day it happened on.
#:
#: It was 0.8 (a +/- 1 day slip counting 0.46 toward the target). Turned off to
#: match the strict attribution in outcomes.py, and measurement said it costs
#: nothing: the Tuesday list was IDENTICAL either way, and the shares got
#: sharper (a clean Tuesday show went 0.499 -> 1.000 rather than smearing).
#:
#: What it removes is BLEED. With the kernel on, a Tuesday show also appeared on
#: Wednesday's list - where, under strict same-day scoring, it could only ever
#: record missed_day. The model was manufacturing its own misses. A show now
#: appears on a day only if it is actually watched on that day.
#:
#: Raise it again ONLY alongside re-introducing grace in outcomes.py. The two
#: are one decision: a model that spreads across days must be scored across
#: days, or it is penalised for doing what it was told.
DEFAULT_JITTER_SIGMA_DAYS = 0.0

#: Recency half-life in days for weighting a play.
DEFAULT_HALFLIFE_DAYS = 45.0

#: Plays below which a series has no trustworthy weekday shape. Two plays can
#: look like any pattern; this is the floor before a show is eligible at all.
DEFAULT_MIN_PLAYS = 3

#: Share of a series' OWN weekly weight that must fall on the target day. A show
#: watched uniformly scores 1/7 = 0.143 every day, so the threshold must sit
#: meaningfully above that or "watched whenever" qualifies for all seven days.
DEFAULT_DAY_THRESHOLD = 0.28


def _circular_distance(a: int, b: int, period: int = WEEK) -> int:
    d = abs(int(a) - int(b)) % period
    return min(d, period - d)


def _kernel(distance: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if distance == 0 else 0.0
    return math.exp(-0.5 * (distance / sigma) ** 2)


def _decay(ts, now_ts, halflife_days: float) -> float:
    """1.0 for a play right now, halving every ``halflife_days``."""
    try:
        age_days = max(0.0, (float(now_ts) - float(ts)) / 86400.0)
    except (TypeError, ValueError):
        return 0.0
    if halflife_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / float(halflife_days))


def local_weekday(ts, tz_offset_hours: float = 0.0):
    """Weekday 0=Mon..6=Sun in LOCAL time, or None if unparseable.

    The offset matters: "Tuesday night" in a US household is Wednesday morning
    in UTC, so without it every late-evening habit lands on the wrong day.
    """
    try:
        dt = datetime.fromtimestamp(float(ts) + tz_offset_hours * 3600.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt.weekday()


def _resolve_key(row, key):
    """The grouping handle for one row. ``key`` is a field name OR a callable.

    Callable support is what lets movies participate at all: a TV row groups on
    its series, but a MOVIE has no repeating identity - nobody watches Alien
    every Saturday - so it has to group one level up (franchise, genre, or just
    the medium). See :func:`derive_key`.
    """
    if callable(key):
        return key(row)
    return row.get(key)


#: Grouping ladder for :func:`derive_key`, most specific first.
#:
#: DETECTABILITY FALLS OFF FAST DOWN THIS LIST, and that is the honest caveat.
#: A series accumulates a dozen plays in a season, so its weekday shape is solid.
#: A franchise might get three Saturday plays in a quarter - barely over
#: ``min_plays`` and easily noise. The MEDIUM level ("Saturday is movie night")
#: is the most reliable movie signal there is, because every movie play feeds it.
#:
#: So the ladder is not just a fallback for missing data, it is a CONFIDENCE
#: ordering: prefer the specific claim when the evidence supports it, fall back
#: to the broad one that always will.
DEFAULT_LEVELS = ("series", "franchise", "genre", "medium")


def derive_key(row, *, franchise_by_rk=None, genre_by_rk=None,
               levels=DEFAULT_LEVELS, rk_field="rating_key",
               series_field="grandparent_rating_key",
               medium_field="media_type"):
    """A namespaced grouping handle: ``series:47889``, ``franchise:mcu``,
    ``genre:horror``, ``medium:movie``. None when nothing on the ladder resolves.

    NAMESPACED on purpose - an unprefixed franchise called "3" would collide
    with a series whose ratingKey is "3", and the two would silently merge into
    one bogus habit.

    ``franchise_by_rk`` / ``genre_by_rk`` are ``{plex_rating_key: label}`` maps
    the CALLER supplies. Tautulli rows carry neither, so that join has to come
    from glidearr's own data (the movie inventory resolves a ratingKey to tmdb,
    and the universe/franchise maps go from there).
    """
    rk = str(row.get(rk_field) or "")
    for level in levels:
        if level == "series":
            v = row.get(series_field)
            if v not in (None, ""):
                return f"series:{v}"
        elif level == "franchise":
            v = (franchise_by_rk or {}).get(rk)
            if v:
                return f"franchise:{str(v).strip().lower()}"
        elif level == "genre":
            v = (genre_by_rk or {}).get(rk)
            if v:
                return f"genre:{str(v).strip().lower()}"
        elif level == "medium":
            v = row.get(medium_field)
            if v not in (None, ""):
                return f"medium:{str(v).strip().lower()}"
    return None


def weekday_profile(rows, *, now_ts, key="grandparent_rating_key",
                    tz_offset_hours: float = 0.0,
                    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
                    jitter_sigma_days: float = DEFAULT_JITTER_SIGMA_DAYS,
                    min_plays: int = DEFAULT_MIN_PLAYS) -> dict:
    """``{series: {"weights":[w0..w6], "share":[s0..s6], "plays":n, "peak_day":d}}``.

    ``weights`` are decayed, jitter-smeared mass per weekday. ``share`` is the
    same normalised to sum 1, and it is what a threshold should test - raw
    weight scales with how much a show is watched, so a heavily-watched show
    would clear any absolute bar on every day of the week.

    A show below ``min_plays`` is ABSENT rather than scored zero: too little
    evidence is not evidence of no habit.
    """
    weights: dict = {}
    plays: dict = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        ts, series = row.get("date"), _resolve_key(row, key)
        if ts is None or series in (None, ""):
            continue
        weekday = local_weekday(ts, tz_offset_hours)
        if weekday is None:
            continue
        recency = _decay(ts, now_ts, halflife_days)
        if recency <= 0:
            continue
        series = str(series)
        acc = weights.setdefault(series, [0.0] * WEEK)
        plays[series] = plays.get(series, 0) + 1
        for day in range(WEEK):
            acc[day] += recency * _kernel(_circular_distance(day, weekday),
                                          jitter_sigma_days)

    out: dict = {}
    for series, acc in weights.items():
        if plays.get(series, 0) < min_plays:
            continue
        total = sum(acc)
        if total <= 0:
            continue
        share = [w / total for w in acc]
        out[series] = {
            "weights": acc,
            "share": share,
            "plays": plays[series],
            "peak_day": max(range(WEEK), key=lambda d: (share[d], -d)),
        }
    return out


def shows_for_day(profile: dict, weekday: int, *, limit: int,
                  threshold: float = DEFAULT_DAY_THRESHOLD) -> list:
    """Series that belong to ``weekday``, strongest first, capped at ``limit``.

    Filters on the SHARE of a show's own weekly weight landing on the target
    day, so the question is "is this a Tuesday show" and not "is this show
    popular". A uniformly-watched show sits at 1/7 on every day and is excluded
    by any threshold above that.

    Ties break on plays then key, so two runs on identical history produce an
    identical list - required, or the outcome measurement compares different
    lists to each other and the hit rate means nothing.
    """
    day = int(weekday) % WEEK
    eligible = [(v["share"][day], v["plays"], k) for k, v in (profile or {}).items()
                if v["share"][day] >= threshold]
    eligible.sort(key=lambda t: (-t[0], -t[1], str(t[2])))
    return [k for _s, _p, k in eligible[:max(0, int(limit))]]


def one_per_show(candidates_by_show: dict, ranked_shows) -> list:
    """Exactly ONE item per show, in rank order - the anti-binge rule.

    Five to ten different shows, one episode each. If they want the next episode
    Plex autoplays it; a list offering six episodes of one series would be a
    binge queue wearing a different name. A show with no candidate is skipped.
    """
    picked = []
    for show in ranked_shows:
        items = candidates_by_show.get(str(show)) or []
        if items:
            picked.append(items[0])
    return picked


# ── session length: which days does THIS profile have time for a long one? ────
# "Cap movies on weeknights, allow them at the weekend" encodes an assumption
# about somebody else's week. Measured against this household's own history it is
# wrong for two profiles in three:
#
#     8592385   Sat 138 (highest)                  - conventional
#     569473003 Wed 37, Mon 33, Fri 33 | Sat 22    - midweek person
#     795458226 Mon 16, Sun 15 | Fri 2             - Monday person
#
# Someone with Monday and Tuesday off has a "weekend" the calendar does not know
# about. So the cap is LEARNED per profile, starting from the convention and
# moving as evidence accumulates - the only version that is right on day one AND
# right in three months.

#: Runtime at or above which a play counts as a LONG session. 75 rather than 60
#: so a 65-minute drama episode does not read as a film-length commitment.
DEFAULT_LONG_MINUTES = 75

#: The PRIOR, Mon..Sun: the conventional week, used until the profile's own
#: history outweighs it. Not uniform, because "no data" is not the same as "no
#: idea" - the convention is a better guess than a coin flip.
DEFAULT_LONG_PRIOR = (0.15, 0.15, 0.15, 0.15, 0.25, 0.60, 0.55)

#: Decayed plays on a weekday before observation and prior carry equal weight.
#: At 8, one evening barely moves the number and a month of Mondays moves it
#: decisively - which is the intended rate of belief change.
DEFAULT_PRIOR_STRENGTH = 8.0

#: Shrunk long-rate at or above which a day is treated as a LONG-session day.
DEFAULT_LONG_DAY_THRESHOLD = 0.35


def session_profile(rows, *, runtime_of, now_ts, tz_offset_hours: float = 0.0,
                    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
                    long_minutes: float = DEFAULT_LONG_MINUTES,
                    prior=DEFAULT_LONG_PRIOR,
                    prior_strength: float = DEFAULT_PRIOR_STRENGTH) -> dict:
    """``{weekday: {"long_rate", "observed", "plays", "prior", "confidence"}}``.

    ``runtime_of`` is ``row -> minutes or None``, supplied by the caller from the
    Radarr/Sonarr parquets. A play whose runtime is UNKNOWN is skipped entirely
    rather than assumed short - it is evidence of nothing, and counting it either
    way would bias the rate (P-C).

    ``long_rate`` is the decayed share of that weekday's viewing that ran long,
    SHRUNK toward the prior by ``n / (n + prior_strength)``. So:

      * with no history it IS the prior - the conventional week;
      * a handful of plays nudge it;
      * a season of Mondays overrides it completely.

    ``confidence`` is that same weight, exposed so a caller or a log can say how
    much of the answer is evidence and how much is still assumption.
    """
    hits: dict = {}
    total: dict = {}
    raw: dict = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        ts = row.get("date")
        if ts is None:
            continue
        day = local_weekday(ts, tz_offset_hours)
        if day is None:
            continue
        minutes = runtime_of(row)
        if minutes is None:
            continue                      # unknown length is not short
        w = _decay(ts, now_ts, halflife_days)
        if w <= 0:
            continue
        total[day] = total.get(day, 0.0) + w
        raw[day] = raw.get(day, 0) + 1
        if float(minutes) >= float(long_minutes):
            hits[day] = hits.get(day, 0.0) + w

    out: dict = {}
    for day in range(WEEK):
        n = total.get(day, 0.0)
        p = float(prior[day % len(prior)])
        observed = (hits.get(day, 0.0) / n) if n > 0 else None
        conf = n / (n + float(prior_strength)) if prior_strength > 0 else 1.0
        blended = p if observed is None else (conf * observed + (1.0 - conf) * p)
        out[day] = {"long_rate": blended, "observed": observed,
                    "plays": raw.get(day, 0), "prior": p, "confidence": conf}
    return out


def runtime_cap_for(profile: dict, weekday: int, *,
                    short_cap_minutes: float = 60.0,
                    threshold: float = DEFAULT_LONG_DAY_THRESHOLD):
    """Minutes cap for ``weekday``, or None when the day takes a long one.

    None rather than a large number on purpose: "no cap" and "a very high cap"
    read the same in the output but differently in intent, and a caller forced to
    invent a ceiling will pick a different one each time.
    """
    entry = (profile or {}).get(int(weekday) % WEEK) or {}
    rate = entry.get("long_rate")
    if rate is None:
        return short_cap_minutes
    return None if float(rate) >= float(threshold) else short_cap_minutes


def build_day_list(rows, *, now_ts, weekday, candidates_by_show, limit,
                   key="grandparent_rating_key", tz_offset_hours: float = 0.0,
                   threshold: float = DEFAULT_DAY_THRESHOLD, **profile_kw) -> dict:
    """One profile's list for one upcoming day: ``{"items", "shows", "profile"}``.

    The whole pipeline in one call, so a caller cannot rank on one profile's
    history and then pick candidates from another's.
    """
    prof = weekday_profile(rows, now_ts=now_ts, key=key,
                           tz_offset_hours=tz_offset_hours, **profile_kw)
    shows = shows_for_day(prof, weekday, limit=limit, threshold=threshold)
    return {"items": one_per_show(candidates_by_show, shows),
            "shows": shows, "profile": prof}

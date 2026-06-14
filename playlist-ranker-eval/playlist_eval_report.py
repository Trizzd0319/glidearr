#!/usr/bin/env python3
"""
playlist_eval_report.py — standalone, anonymized Plex + Tautulli playlist-ranker benchmark.
================================================================================
A SINGLE self-contained file with NO third-party dependencies. It pulls your library
(genres + ratings) from Plex and your watch history from Tautulli, runs a temporal-holdout
evaluation of a personalized "Up Next" ranker across all your users, and writes one
PRIVACY-SAFE report. Pooling those reports across many households lets us pick ranker
settings that work for everyone instead of guessing from one library.

REQUIREMENTS: Python 3.9+ (standard library only — nothing to pip install).

USAGE
  # interactive — prompts for the two connections:
  python playlist_eval_report.py
  # non-interactive — flags or env vars:
  python playlist_eval_report.py --tautulli-url http://host:8181 --tautulli-apikey KEY \
                                 --plex-url http://host:32400 --plex-token TOKEN
  #   env: TAUTULLI_URL TAUTULLI_APIKEY PLEX_URL PLEX_TOKEN
  # drill into ONE user with a verbose table instead of the report:
  python playlist_eval_report.py --user 12345 --cutoff-days 90 --aff-w 0.3 --hh-w 0.7

WHAT THE REPORT CONTAINS (all aggregate / non-identifying):
  • per-user anonymous id = sha256(user_id)[:12]  (a hash of a number; not a name)
  • watch COUNTS, affinity BREADTH, library SIZE, the affinity GENRE distribution (generic
    names like "Action"/"Drama"), and the metrics matrix (meanPct / recall@K / MRR).
WHAT IT NEVER CONTAINS:
  • NO usernames, emails, server/host, IPs, paths
  • NO watched TITLES, ratingKeys, tmdb/imdb ids, or absolute timestamps
  • NO API keys or tokens (used only to fetch, never written anywhere)
Open the JSON before sharing — it is small and readable by design. Network access is only
to YOUR Plex + Tautulli; the report is written locally and shared by you, manually.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import sys
from pathlib import Path

try:  # UTF-8 console so glyphs never crash on a Windows cp1252 terminal
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCHEMA = "plex_playlist_eval.v1"
TOOL_VERSION = "2.0"

# ── easy-to-change knobs (the whole point: tweak, re-run, compare) ───────────────
AFFINITY_WEIGHTS = [0.1, 0.3, 0.5, 0.7, 0.9]   # household weight = 1 - affinity weight
HOLDOUT_FRAC = 0.25                            # hide the most-recent 25% of each user's watches
MIN_POSITIVES = 5                              # below this a user is flagged low_power (kept, not trusted)
GENRE_MATCH_MODES = ("precision", "soft", "coverage", "blend")
_NOW = dt.datetime.now(tz=dt.timezone.utc)


# ── the ranking math under test ──────────────────────────────────────────────────
# priority_score = affinity_weight·A + jit_weight·J + household_weight·h
#   A = genre_match(genres, affinity, mode)   J = active-watching boost   h = title quality/popularity
def _clamp(x):
    return max(0.0, min(1.0, float(x)))


def genre_match(series_genres, user_genre_affinity, *, mode="precision",
                soft_lambda=0.5, blend_weight=0.85):
    """How well a title's genres fit ONE user's taste, in [0, 1] (None if no signal).
    ``mode``: precision (mean over the title's genres) | soft (off-taste genres discounted) |
    coverage (affinity-weighted recall) | blend (weighted mix of coverage + precision)."""
    if not user_genre_affinity or not series_genres:
        return None
    aff = {str(k).strip().lower(): float(v) for k, v in user_genre_affinity.items()}
    mx = max(aff.values(), default=0.0) or 1.0
    ws = [aff.get(str(g).strip().lower(), 0.0) / mx for g in series_genres if g is not None]
    if not ws:
        return None
    mode = (mode or "precision").strip().lower()
    precision = sum(ws) / len(ws)
    if mode == "precision":
        return _clamp(precision)
    if mode == "soft":
        pos = sum(1 for w in ws if w > 0)
        denom = pos + max(0.0, float(soft_lambda)) * (len(ws) - pos)
        return _clamp(sum(ws) / denom) if denom > 0 else 0.0
    total = sum(aff.values())
    gset = {str(g).strip().lower() for g in series_genres if g is not None}
    coverage = (sum(w for g, w in aff.items() if g in gset) / total) if total > 0 else 0.0
    if mode == "coverage":
        return _clamp(min(coverage, 1.0 - 1e-3) + 1e-3 * precision)
    if mode == "blend":
        w = _clamp(blend_weight)
        return _clamp(w * coverage + (1.0 - w) * precision)
    return _clamp(precision)


def priority_score(household_norm, affinity_match, *, is_jit=False,
                   affinity_weight=0.9, jit_weight=0.5, household_weight=0.1):
    a = 0.0 if affinity_match is None else max(0.0, min(1.0, float(affinity_match)))
    h = max(0.0, min(1.0, float(household_norm or 0.0)))
    return (float(affinity_weight) * a + float(jit_weight) * (1.0 if is_jit else 0.0)
            + float(household_weight) * h)


# ── small helpers ────────────────────────────────────────────────────────────────
def _norm(title: str) -> str:
    t = re.sub(r"\(\d{4}\)", "", str(title or "")).lower()
    return re.sub(r"[^a-z0-9]", "", t)


def _finite(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# Agent token in a guid string → normalized id namespace.
_GUID_AGENTS = (("thetvdb", "tvdb"), ("themoviedb", "tmdb"),
                ("tvdb", "tvdb"), ("tmdb", "tmdb"), ("imdb", "imdb"))


def _parse_guids(*sources) -> set:
    """Normalized external ids ('tvdb:121361','tmdb:1399','imdb:tt0944947') parsed from any mix of:
    a primary guid string ('com.plexapp.agents.thetvdb://121361?lang=en' or 'tvdb://121361'),
    a Plex `Guid` array ([{'id': 'tvdb://121361'}, …]), or a Tautulli `guids` list. The new Plex
    agent's opaque 'plex://…' guids yield nothing — that's why we also read the Guid array. These
    ids are used ONLY to join; they are never written to the report."""
    out: set = set()

    def _one(s):
        s = str(s or "").lower()
        for agent, kind in _GUID_AGENTS:
            tok = agent + "://"
            if tok in s:
                val = s.split(tok, 1)[1].split("?")[0].split("/")[0].strip()
                if val:
                    out.add(f"{kind}:{val}")
                return
    for src in sources:
        if not src:
            continue
        if isinstance(src, (list, tuple)):
            for it in src:
                _one(it.get("id") if isinstance(it, dict) else it)
        else:
            _one(src)
    return out


# ── live fetch: Plex (library) + Tautulli (history) ──────────────────────────────
def _http_json(url, headers=None, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 (your own LAN URL)
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_tautulli_history(base, apikey, *, page=5000, max_rows=500000):
    """Every watch row from Tautulli `get_history` (paginated). Exits with a clear message."""
    import urllib.error
    import urllib.parse
    base = base.rstrip("/")
    out, start = [], 0
    while True:
        q = urllib.parse.urlencode({"apikey": apikey, "cmd": "get_history",
                                    "length": page, "start": start})
        try:
            data = _http_json(f"{base}/api/v2?{q}")
        except urllib.error.HTTPError as e:
            sys.exit(f"Tautulli HTTP {e.code} — check the URL and API key.")
        except Exception as e:
            sys.exit(f"Could not reach Tautulli at {base}: {e}")
        resp = (data or {}).get("response", {}) or {}
        if resp.get("result") != "success":
            sys.exit(f"Tautulli API error: {resp.get('message') or resp}")
        d = resp.get("data", {}) or {}
        rows = d.get("data", []) or []
        out.extend(rows)
        total = int(d.get("recordsFiltered") or d.get("recordsTotal") or len(out))
        start += len(rows)
        if not rows or start >= total or len(out) >= max_rows:
            break
    return out


def parse_tautulli_rows(rows):
    """Tautulli history → {user_id: [rows]}. Episodes join to the Plex library on the SHOW
    title (grandparent_title); movies on the movie title. Title is used (not ratingKey) because
    ratingKeys go stale across Plex library rescans/rebuilds, while titles from the same server
    match almost exactly."""
    users: dict = {}
    for r in rows:
        uid, mt, date = str(r.get("user_id") or ""), r.get("media_type"), r.get("date")
        if not uid or not date:
            continue
        if mt == "episode" and r.get("grandparent_title"):
            row = {"media_type": "episode", "kind": "tv", "date": int(date), "user_id": uid,
                   "user": r.get("friendly_name") or r.get("user"),
                   "grandparent_title": str(r.get("grandparent_title")),
                   "title": str(r.get("title") or ""),
                   "meta_rk": str(r.get("grandparent_rating_key") or "")}    # show ratingKey → get_metadata
        elif mt == "movie" and r.get("title"):
            row = {"media_type": "movie", "kind": "movie", "date": int(date), "user_id": uid,
                   "user": r.get("friendly_name") or r.get("user"),
                   "grandparent_title": "", "title": str(r.get("title")),
                   "meta_rk": str(r.get("rating_key") or "")}
        else:
            continue
        users.setdefault(uid, []).append(row)
    return users


def fetch_plex_library(base, token):
    """(tv, mv, idx). ``tv``/``mv`` are keyed by normalised TITLE → [display, genres, h];
    ``idx`` maps (kind, external_id) → that title key (from each item's Plex `Guid` array,
    requested with ?includeGuids=1). The id index gives a rescan-proof, rename-proof join;
    title is the fallback. ``h`` = audienceRating/10."""
    import urllib.error
    base = base.rstrip("/")
    headers = {"Accept": "application/json", "X-Plex-Token": token}
    try:
        secs = _http_json(f"{base}/library/sections", headers)
    except urllib.error.HTTPError as e:
        sys.exit(f"Plex HTTP {e.code} — check the URL and token.")
    except Exception as e:
        sys.exit(f"Could not reach Plex at {base}: {e}")
    dirs = ((secs or {}).get("MediaContainer", {}) or {}).get("Directory", []) or []
    tv, mv, idx = {}, {}, {}
    for s in dirs:
        st, key = s.get("type"), s.get("key")
        if st not in ("show", "movie") or not key:
            continue
        try:
            items = _http_json(f"{base}/library/sections/{key}/all?includeGuids=1", headers)
        except Exception:
            continue
        meta = ((items or {}).get("MediaContainer", {}) or {}).get("Metadata", []) or []
        target, kind = (tv, "tv") if st == "show" else (mv, "movie")
        for it in meta:
            title = it.get("title")
            genres = [g.get("tag") for g in (it.get("Genre") or []) if g.get("tag")]
            tk = _norm(str(title)) if title else ""
            if not tk or not genres:
                continue
            rating = _finite(it.get("audienceRating") if it.get("audienceRating") is not None
                             else it.get("rating"))
            hh = (rating / 10.0) if rating is not None else None
            target.setdefault(tk, [title, genres, hh])
            for eid in _parse_guids(it.get("guid"), it.get("Guid")):
                idx.setdefault((kind, eid), tk)
    return tv, mv, idx


def fetch_tautulli_guids(base, apikey, rating_keys):
    """{rating_key: set(external_ids)} via Tautulli get_metadata — one call per DISTINCT watched
    title (show/movie ratingKey). Best-effort: a failed/empty lookup is just skipped."""
    import urllib.parse
    base = base.rstrip("/")
    out: dict = {}
    keys = [k for k in rating_keys if k]
    for i, rk in enumerate(keys, 1):
        q = urllib.parse.urlencode({"apikey": apikey, "cmd": "get_metadata", "rating_key": rk})
        try:
            data = _http_json(f"{base}/api/v2?{q}")
        except Exception:
            continue
        md = (((data or {}).get("response") or {}).get("data")) or {}
        ids = _parse_guids(md.get("guid"), md.get("guids"))
        if ids:
            out[rk] = ids
        if i % 250 == 0:
            print(f"      …resolved {i}/{len(keys)} titles")
    return out


def _resolve_watches(users, tv, mv, idx, guid_map):
    """Annotate each watch with ``_ck`` (the matched library title-key) + ``_method``
    (``guid`` | ``title`` | ``none``), preferring the rescan-proof GUID match, then title.
    Returns distinct-title counts per method (for the report — counts only, never ids)."""
    lib = {"tv": tv, "movie": mv}
    counts = {"guid": 0, "title": 0, "none": 0}
    seen: set = set()
    for rows in users.values():
        for r in rows:
            kind = r["kind"]
            ck, method = None, "none"
            for eid in guid_map.get(r["meta_rk"], ()):            # GUID first (most robust)
                if (kind, eid) in idx:
                    ck, method = idx[(kind, eid)], "guid"
                    break
            if ck is None:                                         # fall back to title
                nt = _norm(r["grandparent_title"] if kind == "tv" else r["title"])
                ck = nt
                if nt in lib[kind]:
                    method = "title"
            r["_ck"], r["_method"] = ck, method
            ident = (kind, ck if method != "none" else (r["meta_rk"] or ck))
            if ident not in seen:
                seen.add(ident)
                counts[method] += 1
    return counts


def gather_live(taut_url, taut_key, plex_url, plex_token, *, use_guids=True):
    """(tv, mv, {user_id: rows}, household_source) fetched live from Plex + Tautulli, with each
    watch resolved to a library title (GUID-first, title-fallback)."""
    print("  · fetching Plex library (genres + ratings + external ids) …")
    tv, mv, idx = fetch_plex_library(plex_url, plex_token)
    print(f"    {len(tv)} TV + {len(mv)} movie titles, {len(idx)} external ids")
    print("  · fetching Tautulli watch history …")
    users = parse_tautulli_rows(fetch_tautulli_history(taut_url, taut_key))
    print(f"    {sum(len(v) for v in users.values())} watches across {len(users)} users")
    guid_map = {}
    if use_guids and idx:
        rks = sorted({r["meta_rk"] for rows in users.values() for r in rows if r.get("meta_rk")})
        print(f"  · resolving external ids for {len(rks)} distinct titles (Tautulli get_metadata) …")
        guid_map = fetch_tautulli_guids(taut_url, taut_key, rks)
    counts = _resolve_watches(users, tv, mv, idx, guid_map)
    print(f"    matched {counts['guid'] + counts['title']} titles "
          f"(guid {counts['guid']}, title {counts['title']}); "
          f"{counts['none']} no longer in Plex — excluded")
    return tv, mv, users, "plex_audience_rating"


def _prompt(argval, env, label, *, default=None, secret=False):
    """CLI flag → $ENV → interactive prompt (getpass for secrets). Never echoes secrets."""
    if argval:
        return argval
    v = os.environ.get(env)
    if v:
        return v
    if not sys.stdin.isatty():
        sys.exit(f"Missing {label}: pass the flag or set ${env} (no terminal to prompt on).")
    if secret:
        import getpass
        return getpass.getpass(f"  {label}: ").strip()
    return input(f"  {label}{f' [{default}]' if default else ''}: ").strip() or (default or "")


# ── scoring + metrics ────────────────────────────────────────────────────────────
def _watch_key(row: dict):
    ck = row.get("_ck")                       # resolved by _resolve_watches (guid-first, title-fallback)
    if ck is not None:
        return (row.get("kind") or ("tv" if row.get("media_type") == "episode" else "movie"), ck)
    if row.get("media_type") == "episode":    # fallback for un-resolved rows (e.g. direct tests)
        gt = row.get("grandparent_title")
        return ("tv", gt) if gt else None
    if row.get("media_type") == "movie":
        t = row.get("title")
        return ("movie", t) if t else None
    return None


def _affinity(rows: list, tv: dict, mv: dict) -> dict:
    counts: dict = {}
    for r in rows:
        wk = _watch_key(r)
        if not wk:
            continue
        hit = (tv if wk[0] == "tv" else mv).get(_norm(wk[1]))
        if not hit:
            continue
        for g in hit[1]:
            counts[g] = counts.get(g, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _rank_stats(score: float, asc: list):
    n = len(asc)
    hi = bisect.bisect_right(asc, score)
    lo = bisect.bisect_left(asc, score)
    midrank = (n - hi) + (hi - lo + 1) / 2.0
    return midrank, (1.0 - (midrank - 1) / (n - 1) if n > 1 else 1.0)


def _metrics(positives, all_scores):
    if not positives:
        return None
    asc = sorted(all_scores)
    rs = [_rank_stats(s, asc) for s in positives]
    ranks = [r for r, _ in rs]
    pcts = [p for _, p in rs]
    rec = lambda k: sum(1 for r in ranks if r <= k) / len(ranks)
    return {"n": len(positives), "mean_pct": round(sum(pcts) / len(pcts), 4),
            "r10": round(rec(10), 4), "r20": round(rec(20), 4), "r50": round(rec(50), 4),
            "mrr": round(sum(1.0 / r for r in ranks) / len(ranks), 4)}


def split_history(history: list, cutoff_ts: float):
    pre = [r for r in history if (r.get("date") or 0) < cutoff_ts]
    post = [r for r in history if (r.get("date") or 0) >= cutoff_ts]
    return pre, post


def evaluate_user(pre, post, tv, mv, *, weight_configs, modes=GENRE_MATCH_MODES,
                  jit_days=0, soft_lambda=0.5, blend_weight=0.75, cutoff=None):
    """Holdout for one user over (aff_w, hh_w, jit_w) × modes. Returns a PII-free dict."""
    aff = _affinity(pre, tv, mv)
    if not aff:
        return None
    tv_max = max((c[2] for c in tv.values() if c[2] is not None), default=0.0) or 1.0
    mv_max = max((c[2] for c in mv.values() if c[2] is not None), default=0.0) or 1.0
    cands: dict = {}
    for k, (_d, genres, sc) in tv.items():
        cands[("tv", k)] = [genres, (sc / tv_max) if sc is not None else 0.0]
    for k, (_d, genres, sc) in mv.items():
        cands[("movie", k)] = [genres, (sc / mv_max) if sc is not None else 0.0]

    pre_keys = {(wk[0], _norm(wk[1])) for r in pre if (wk := _watch_key(r))}
    post_keys = {(wk[0], _norm(wk[1])) for r in post if (wk := _watch_key(r))}
    pos_all = [k for k in post_keys if k in cands]
    pos_new = [k for k in pos_all if k not in pre_keys]

    jit: set = set()
    if jit_days > 0 and cutoff is not None:
        lo = cutoff - jit_days * 86400
        for r in pre:
            if lo <= (r.get("date") or 0) < cutoff and (wk := _watch_key(r)):
                jit.add((wk[0], _norm(wk[1])))

    def score_rows(score_of):
        scored = {k: s for k in cands if (s := score_of(k)) is not None}
        allv = list(scored.values())
        return (_metrics([scored[k] for k in pos_all if k in scored], allv),
                _metrics([scored[k] for k in pos_new if k in scored], allv))

    hh_all, hh_new = score_rows(lambda k: cands[k][1])
    rows = []
    kw = dict(soft_lambda=soft_lambda, blend_weight=blend_weight)
    for (aff_w, hh_w, jit_w) in weight_configs:
        for mode in modes:
            def _s(k, aff_w=aff_w, hh_w=hh_w, jit_w=jit_w, mode=mode):
                gm = genre_match(cands[k][0], aff, mode=mode, **kw)
                return priority_score(cands[k][1], gm, is_jit=(k in jit),
                                      affinity_weight=aff_w, jit_weight=jit_w, household_weight=hh_w)
            ma, mn = score_rows(_s)
            rows.append({"aff_w": aff_w, "hh_w": hh_w, "jit_w": jit_w, "mode": mode,
                         "all": ma, "new": mn})

    return {
        "meta": {"n_pre": len(pre), "n_post": len(post), "n_pos_all": len(pos_all),
                 "n_pos_new": len(pos_new), "n_jit_active": len(jit & set(pos_all)) if jit else 0,
                 "library_size": len(cands), "affinity_breadth": len(aff),
                 "affinity_top": dict(list(aff.items())[:15]),
                 "jit_days": jit_days, "soft_lambda": soft_lambda, "blend_weight": blend_weight},
        "household_baseline": {"all": hh_all, "new": hh_new},
        "rows": rows,
    }


# ── report (multi-user, anonymized) ──────────────────────────────────────────────
def _anon(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


def _quantile_cutoff(history: list, holdout_frac: float):
    dates = sorted(r.get("date") for r in history if r.get("date"))
    if len(dates) < 4:
        return None
    idx = max(1, min(len(dates) - 1, int(round(len(dates) * (1.0 - holdout_frac)))))
    return float(dates[idx])


def build_report(tv, mv, users, *, household_source, holdout_frac=HOLDOUT_FRAC,
                 affinity_weights=AFFINITY_WEIGHTS, jit_days=0, soft_lambda=0.5,
                 blend_weight=0.75, min_positives=MIN_POSITIVES) -> dict:
    """``users`` = {user_id: history_rows}; ``tv``/``mv`` = {norm_title: [display, genres, h]}."""
    rated = sum(1 for m in (tv, mv) for v in m.values() if v[2] is not None)
    rating_coverage = round(rated / (len(tv) + len(mv)), 3) if (tv or mv) else 0.0
    join = {"by_guid": 0, "by_title": 0, "not_in_plex": 0}   # how distinct watched titles resolved
    seen: set = set()
    for rows in users.values():
        for r in rows:
            mth = r.get("_method", "none")
            ident = (r.get("kind"), r.get("meta_rk") if mth == "none" else r.get("_ck"))
            if ident in seen:
                continue
            seen.add(ident)
            join["by_guid" if mth == "guid" else "by_title" if mth == "title" else "not_in_plex"] += 1
    weight_configs = [(round(a, 3), round(1 - a, 3), 0.0) for a in affinity_weights]
    users_out = []
    for uid, history in users.items():
        # disregard watches whose title is no longer in Plex — they can never be recommended,
        # so they don't belong in the timeline split, the affinity, or the held-out set.
        history = [r for r in history if r.get("_method", "none") != "none"]
        cutoff = _quantile_cutoff(history, holdout_frac)
        if cutoff is None:
            continue
        pre, post = split_history(history, cutoff)
        res = evaluate_user(pre, post, tv, mv, cutoff=cutoff, jit_days=jit_days,
                            soft_lambda=soft_lambda, blend_weight=blend_weight,
                            weight_configs=weight_configs)
        if not res or res["meta"]["n_pos_all"] == 0:
            continue
        res["anon_id"] = _anon(uid)
        res["low_power"] = res["meta"]["n_pos_all"] < min_positives
        users_out.append(res)
    return {
        "schema": SCHEMA, "tool_version": TOOL_VERSION,
        "generated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds"),
        "household_source": household_source,
        "params": {"holdout_frac": holdout_frac, "affinity_weights": affinity_weights,
                   "modes": list(GENRE_MATCH_MODES), "jit_days": jit_days,
                   "soft_lambda": soft_lambda, "blend_weight": blend_weight,
                   "min_positives": min_positives},
        "library": {"tv_titles": len(tv), "movie_titles": len(mv),
                    "rating_coverage": rating_coverage},
        "join": join,
        "n_users": len(users_out),
        "n_users_high_power": sum(1 for u in users_out if not u["low_power"]),
        "users": users_out, "aggregate": _aggregate(users_out),
        "_privacy": {
            "contains": ["sha256(user_id)[:12]", "watch counts", "affinity genre distribution",
                         "metrics matrix"],
            "excludes": ["usernames", "emails", "server/host", "watched titles",
                         "ratingKeys / tmdb / imdb ids", "absolute timestamps", "file paths",
                         "API keys / tokens"],
        },
    }


def _aggregate(users: list) -> dict:
    hp = [u for u in users if not u["low_power"]]
    if not hp:
        return {"note": "no high-power users (need >= MIN_POSITIVES held-out watches)"}

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 4) if vals else None

    base = {m: mean([u["household_baseline"]["all"][m] for u in hp if u["household_baseline"]["all"]])
            for m in ("mean_pct", "r20", "r50", "mrr")}
    cells: dict = {}
    for u in hp:
        for r in u["rows"]:
            cells.setdefault(f"aff{r['aff_w']}|{r['mode']}", []).append(r["all"])
    grid = {k: {m: mean([x[m] for x in v if x]) for m in ("mean_pct", "r20", "r50", "mrr")}
            for k, v in cells.items()}
    best = max(grid.items(), key=lambda kv: (kv[1]["mean_pct"] or 0)) if grid else (None, None)
    return {"n_high_power": len(hp), "household_baseline": base, "by_config": grid,
            "best_by_mean_pct": {"config": best[0], **(best[1] or {})}}


def _md(report: dict) -> str:
    a = report["aggregate"]
    L = [f"# Playlist-ranker holdout report (anonymized)", "",
         f"- schema `{report['schema']}` · tool {report['tool_version']} · "
         f"household={report.get('household_source', '?')} · {report['generated_at']}",
         f"- users: {report['n_users']} ({report['n_users_high_power']} high-power) · "
         f"library {report['library']['tv_titles']} TV + {report['library']['movie_titles']} movies",
         f"- holdout: most-recent {report['params']['holdout_frac']:.0%} of each user's watches", "",
         "## Aggregate (mean over high-power users; higher = better; random ≈ 0.500)", ""]
    if "by_config" in a:
        b = a["household_baseline"]
        L += [f"**household-only baseline:** meanPct {b['mean_pct']} · rec@50 {b['r50']} · MRR {b['mrr']}",
              "", "| affinity_weight | mode | meanPct | rec@20 | rec@50 | MRR |", "|---|---|---|---|---|---|"]
        for k in sorted(a["by_config"]):
            aff, mode = k.replace("aff", "").split("|")
            c = a["by_config"][k]
            L.append(f"| {aff} | {mode} | {c['mean_pct']} | {c['r20']} | {c['r50']} | {c['mrr']} |")
        L += ["", f"**best by meanPct:** `{a['best_by_mean_pct']['config']}` "
              f"(meanPct {a['best_by_mean_pct']['mean_pct']})"]
    else:
        L.append(a.get("note", ""))
    L += ["", "_No titles, usernames, ids, or timestamps are included — see `_privacy` in the JSON._"]
    return "\n".join(L)


# ── single-user verbose drill ────────────────────────────────────────────────────
def _line(label, ma, mn):
    if not ma:
        return f"{label:12} | (no scorable positives)"
    nstr = (f"{mn['mean_pct']:>7.3f} {mn['r20']:>6.2f} {mn['mrr']:>5.3f}" if mn
            else f"{'n/a':>7} {'-':>6} {'-':>5}")
    return (f"{label:12} | {ma['mean_pct']:>7.3f} {ma['r10']:>6.2f} {ma['r20']:>6.2f} "
            f"{ma['r50']:>6.2f} {ma['mrr']:>5.3f}  ||  {'':4}{nstr}")


def run_single(tv, mv, users, user_id, cutoff_days, weights, jit_days, soft_lambda, blend_weight):
    history = users.get(str(user_id)) or users.get(user_id)
    if not history:
        sys.exit("No history for that user. Available user ids: " + ", ".join(map(str, users)))
    name = history[0].get("user", user_id)
    n_excluded = sum(1 for r in history if r.get("_method", "none") == "none")
    history = [r for r in history if r.get("_method", "none") != "none"]   # disregard not-in-Plex
    cutoff = (_NOW - dt.timedelta(days=cutoff_days)).timestamp()
    pre, post = split_history(history, cutoff)
    res = evaluate_user(pre, post, tv, mv, cutoff=cutoff, jit_days=jit_days,
                        soft_lambda=soft_lambda, blend_weight=blend_weight, weight_configs=[weights])
    if not res:
        sys.exit(f"{name}: affinity empty (no pre-cutoff watches resolved to genres).")
    m = res["meta"]
    print(f"\n{'='*92}\n{name} — cutoff {cutoff_days}d | pre={m['n_pre']} post={m['n_post']} | "
          f"weights aff={weights[0]} jit={weights[2]} hh={weights[1]} jit_days={jit_days}")
    print(f"(excluded {n_excluded} watch(es) no longer in Plex)")
    print("affinity (top): " + ", ".join(f"{g}={c}" for g, c in m["affinity_top"].items()))
    print(f"held-out positives in library: {m['n_pos_all']} total, {m['n_pos_new']} NEW")
    print(f"\n{'row':12} | {'meanPct':>7} {'rec@10':>6} {'rec@20':>6} {'rec@50':>6} {'MRR':>5}"
          f"  ||  NEW {'meanPct':>7} {'rec@20':>6} {'MRR':>5}")
    print("-" * 92)
    print(_line("household", res["household_baseline"]["all"], res["household_baseline"]["new"]))
    print("-" * 92)
    for r in res["rows"]:
        print(_line(r["mode"], r["all"], r["new"]))
    print(f"\n(random meanPct ≈ 0.500; candidates = {m['library_size']:,})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone anonymized Plex+Tautulli playlist-ranker eval.")
    # ── connections ──
    ap.add_argument("--tautulli-url"); ap.add_argument("--tautulli-apikey")
    ap.add_argument("--plex-url"); ap.add_argument("--plex-token")
    ap.add_argument("--no-guid", action="store_true",
                    help="skip the get_metadata GUID join (faster; titles-only matching)")
    # ── sweep / output ──
    ap.add_argument("--holdout-frac", type=float, default=HOLDOUT_FRAC)
    ap.add_argument("--jit-days", type=int, default=0)
    ap.add_argument("--soft-lambda", type=float, default=0.5)
    ap.add_argument("--blend-weight", type=float, default=0.75)
    ap.add_argument("--out", default=None, help="report .json path (a sibling .md is also written)")
    # ── single-user drill (verbose, no file written) ──
    ap.add_argument("--user", help="user_id — print a verbose single-user table instead of the report")
    ap.add_argument("--cutoff-days", type=int, default=90)
    ap.add_argument("--aff-w", type=float, default=0.9)
    ap.add_argument("--hh-w", type=float, default=0.1)
    ap.add_argument("--jit-w", type=float, default=0.65)
    args = ap.parse_args()

    print("Connecting to YOUR Plex + Tautulli. Credentials stay local; they are never written "
          "to the report.")
    t_url = _prompt(args.tautulli_url, "TAUTULLI_URL", "Tautulli URL", default="http://localhost:8181")
    t_key = _prompt(args.tautulli_apikey, "TAUTULLI_APIKEY", "Tautulli API key", secret=True)
    p_url = _prompt(args.plex_url, "PLEX_URL", "Plex URL", default="http://localhost:32400")
    p_tok = _prompt(args.plex_token, "PLEX_TOKEN", "Plex token", secret=True)
    tv, mv, users, hsrc = gather_live(t_url, t_key, p_url, p_tok, use_guids=not args.no_guid)

    if not users:
        sys.exit("No watch history returned by Tautulli.")

    if args.user:
        run_single(tv, mv, users, args.user, args.cutoff_days, (args.aff_w, args.hh_w, args.jit_w),
                   args.jit_days, args.soft_lambda, args.blend_weight)
        return

    report = build_report(tv, mv, users, household_source=hsrc, holdout_frac=args.holdout_frac,
                          jit_days=args.jit_days, soft_lambda=args.soft_lambda, blend_weight=args.blend_weight)
    out = Path(args.out) if args.out else (Path.cwd() / "playlist_eval_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(_md(report), encoding="utf-8")
    rc, j = report["library"]["rating_coverage"], report["join"]
    print(f"\nhousehold_source: {hsrc} | matched {j['by_guid'] + j['by_title']} titles "
          f"(guid {j['by_guid']}, title {j['by_title']}); {j['not_in_plex']} no longer in Plex (excluded) "
          f"| audienceRating coverage {rc:.0%}")
    if rc < 0.3:
        print("  ⚠ LOW audienceRating coverage — the household quality signal `h` is weak on this "
              "server, so the household baseline will look weaker than on rating-rich libraries.")
    print(f"wrote: {out}\n       {out.with_suffix('.md')}  "
          f"({report['n_users']} users, {report['n_users_high_power']} high-power)")
    print("\nReview both files (small + readable), then share the .json — NO titles, usernames, ids, or timestamps.")
    print(_md(report))


if __name__ == "__main__":
    main()

"""radarr_credits_sync — populate the movie people bucket from Radarr instead of Trakt.

WHY
---
Movie cast/crew was coming from Trakt's ``movies/{id}/people``, which costs a throttled
API call per title: ~24.8k movies against a 650-call/5-minute budget is the better part of
a day just for the people bucket, and people data is what gates ``people_affinity`` and the
co-occurrence proposer. Radarr already holds the same credits, sourced from TMDB, on a
LOCAL server with no rate limit.

It also fixes an id problem. The Trakt buckets store what ``normalise_people`` labels a
tmdb person id but which is demonstrably Trakt's own: Michael J. Fox came back as 521
(TMDB 3223), Eminem as 325 (TMDB 39123), and 8 Mile's billed cast as a sequential
325/326/327/328. That is self-consistent — affinity and candidates both read the same
buckets — but it is not the TMDB id space the column names claim, and it means credits
from any other source cannot be merged in without splitting each person in two.

Radarr becomes the SOLE people source for movies, so the movie half lives in one real TMDB
id space. Shows stay on Trakt in theirs; cross-medium person overlap is rare enough that
the split costs little.

HOW IT INTEGRATES
-----------------
This writes the same ``cache/trakt/movies/<tmdbId>.json.gz`` files the daemon writes, in
the same ``{"cast":[...], "crew":[...]}`` shape. So the daemon needs NO change: its
``is_cached()`` check sees a warm people bucket and skips that endpoint at zero cost, while
still fetching the other seven buckets from Trakt. Warming this bucket removes ~24.8k calls
from the backfill as a side effect.

USAGE
-----
    python scripts/support/tools/radarr_credits_sync.py --probe
    python scripts/support/tools/radarr_credits_sync.py --sync --limit 25
    python scripts/support/tools/radarr_credits_sync.py --sync
    python scripts/support/tools/radarr_credits_sync.py --verify

Run ``--probe`` FIRST. Radarr's credits payload field names are not assumed by this tool —
probe prints the raw keys of a real response so the mapping can be confirmed against your
Radarr version before 24.8k files get written.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.config.config_loader import ConfigLoader      # noqa: E402
from scripts.managers.factories.daemons.daemon_paths import (                 # noqa: E402
    CONFIG_PATH, MOVIE_BUCKETS,
)
from scripts.support.daemons.enrich_daemon import is_cached, write_cache      # noqa: E402

_TIMEOUT = (10, 60)
_PEOPLE_BUCKET = MOVIE_BUCKETS["people"]

# ONE pooled session for every request. Without this each call opened a fresh TCP
# connection and left it in TIME_WAIT; on Windows the default ephemeral port range plus a
# ~4-minute TIME_WAIT means a few thousand rapid requests exhaust the pool and throughput
# collapses. Observed live: 35/s for the first 1,000 titles decaying to 5/s by 13,000.
# Keep-alive reuses a handful of sockets instead of burning ~25k.
_SESSION = requests.Session()
_SESSION.mount("http://", requests.adapters.HTTPAdapter(
    pool_connections=16, pool_maxsize=32, max_retries=2))
_SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=16, pool_maxsize=32, max_retries=2))

# Progress state, so an interrupted --force pass resumes instead of redoing everything.
_STATE_PATH = _PEOPLE_BUCKET.parent / "radarr_credits_sync.state.json"

# Field-name candidates, most specific first. Radarr has renamed these across versions and
# this tool refuses to guess silently — --probe prints what your instance actually sends.
_PERSON_ID_KEYS = ("personTmdbId", "tmdbId", "person_tmdb_id")
_NAME_KEYS      = ("personName", "name")
_CHAR_KEYS      = ("character", "characterName")


def _radarr(cfg: dict) -> tuple[str, str]:
    inst = cfg.get("radarr_instances", {}) or {}
    name = (inst.get("default_instance") or {}).get("name", "standard")
    node = inst.get(name) or {}
    return str(node.get("base_url", "")).rstrip("/"), str(node.get("api", ""))


def _get(base: str, api: str, path: str):
    r = _SESSION.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": api}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _try(base: str, api: str, path: str):
    """(status_code, parsed_or_None) without raising — used to discover the API surface."""
    try:
        r = _SESSION.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": api}, timeout=_TIMEOUT)
    except Exception as e:
        return None, f"error: {e}"
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except Exception:
        return 200, None


# Radarr has moved credits around between versions and some builds don't expose them at
# all. Rather than assume, the probe walks these and reports what answers. ``{id}`` is the
# Radarr internal movie id, ``{tmdb}`` the TMDB id.
_CREDIT_ENDPOINTS = (
    "credit?movieId={id}",             # Radarr 6.3 — SINGULAR; the plural form 404s
    "credits?movieId={id}",
    "moviecredit?movieId={id}",
    "movie/{id}",                      # single-movie payload may embed credits
    "movie/{id}?extended=true",
    "moviecredits?movieId={id}",
)


def _discover_credit_endpoint(base: str, api: str, movies: list) -> str | None:
    """The first template in _CREDIT_ENDPOINTS that returns credit-shaped data.

    Discovered rather than hardcoded because the path moved between Radarr versions —
    6.3.0 serves ``/api/v3/credit`` and 404s on ``/api/v3/credits``. sync() resolves it the
    same way probe() does so the two can never disagree about which endpoint works.
    """
    sample = next((m for m in movies if m.get("id")), None)
    if not sample:
        return None
    for tmpl in _CREDIT_ENDPOINTS:
        _, payload = _try(base, api, tmpl.format(id=sample["id"], tmdb=sample.get("tmdbId")))
        rows, _where = _looks_like_credits(payload)
        if rows:
            return tmpl
    return None


def _pick(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _normalise(rows: list) -> dict:
    """Radarr credits list -> the daemon's {"cast": [...], "crew": [...]} bucket shape.

    Deliberately mirrors ``enrich_daemon.normalise_people`` field-for-field so every
    existing consumer (route_people, the people-matrix sidecar, person_affinity_score)
    reads it without knowing the source changed.
    """
    cast, crew = [], []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        pid = _pick(row, _PERSON_ID_KEYS)
        name = _pick(row, _NAME_KEYS) or ""
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        kind = str(row.get("type") or "").lower()
        if kind == "cast" or (not kind and row.get("order") is not None):
            cast.append({
                "name": name, "id": pid,
                "character": _pick(row, _CHAR_KEYS) or "",
                "order": row.get("order", len(cast)),
            })
        else:
            crew.append({
                "name": name, "id": pid,
                "job": row.get("job") or "",
                "department": row.get("department") or "",
            })
    cast.sort(key=lambda c: c.get("order", 9999))
    for i, c in enumerate(cast):
        c["order"] = i
    return {"cast": cast, "crew": crew}


def probe_bulk(base: str, api: str) -> int:
    """Time the per-movie endpoint against candidate BULK shapes.

    Per-movie is ~4s/request regardless of worker count, which points at Radarr rather
    than the client: Radarr is SQLite-backed, so concurrent reads serialise and adding
    workers buys nothing. One request returning many rows sidesteps that entirely — if any
    of these shapes works, 24,849 round-trips collapse into a handful.
    """
    movies = _get(base, api, "movie") or []
    sample = next((m for m in movies if m.get("id")), None)
    mid = sample["id"] if sample else 1
    ids = ",".join(str(m["id"]) for m in movies[:50] if m.get("id"))

    candidates = [
        ("per-movie baseline", f"credit?movieId={mid}"),
        ("all credits",        "credit"),
        ("paged",              "credit?page=1&pageSize=1000"),
        ("multi-id",           f"credit?movieIds={ids}"),
        ("by metadata id",     f"credit?movieMetadataId={mid}"),
    ]
    print(f"Library {len(movies):,} movies. Timing candidate shapes:\n")
    best = None
    for label, path in candidates:
        t0 = time.time()
        code, payload = _try(base, api, path)
        dt = time.time() - t0
        rows, where = _looks_like_credits(payload)
        n = len(rows) if rows else (len(payload) if isinstance(payload, list) else 0)
        distinct = 0
        if rows:
            distinct = len({r.get("movieMetadataId") for r in rows if isinstance(r, dict)})
        print(f"  [{str(code):>4}] {dt:6.2f}s  rows={n:<8,} distinct_movies={distinct:<6,} "
              f"{label}  (/api/v3/{path[:60]})")
        if rows and distinct > 1 and (best is None or distinct > best[1]):
            best = (path, distinct)
    print()
    if best:
        print(f"  BULK WORKS: /api/v3/{best[0]} returned {best[1]:,} distinct movies in one call.")
        print("  Tell me and I'll switch --sync to it.")
    else:
        print("  No bulk shape available — per-movie is the only option on this build.")
        print("  Use --workers 4; more just contends on Radarr's SQLite lock.")
    return 0


def _looks_like_credits(payload):
    """(rows, where) for anything credit-shaped in *payload*, else (None, None)."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        keys = set(payload[0])
        if keys & {"personName", "personTmdbId", "character", "job", "department"}:
            return payload, "list"
    if isinstance(payload, dict):
        cr = payload.get("credits")
        if isinstance(cr, list) and cr:
            return cr, "movie.credits[]"
        if isinstance(cr, dict):
            merged = (cr.get("cast") or []) + (cr.get("crew") or [])
            if merged:
                return merged, "movie.credits{cast,crew}"
    return None, None


def probe(base: str, api: str) -> int:
    code, status = _try(base, api, "system/status")
    if isinstance(status, dict):
        print(f"Radarr {status.get('version', '?')} ({status.get('appName', 'Radarr')})\n")
    movies = _get(base, api, "movie") or []
    print(f"Radarr library: {len(movies):,} movies")
    sample = next((m for m in movies if m.get("id") and m.get("tmdbId")), None)
    if not sample:
        print("No usable movie to probe with.")
        return 1
    mid, tmdb, title = sample["id"], sample["tmdbId"], sample.get("title")
    print(f"Probing with: {title} (radarr id {mid}, tmdb {tmdb})\n")

    found = None
    for tmpl in _CREDIT_ENDPOINTS:
        path = tmpl.format(id=mid, tmdb=tmdb)
        code, payload = _try(base, api, path)
        rows, where = _looks_like_credits(payload)
        mark = "CREDITS" if rows else ("ok" if code == 200 else "--")
        print(f"  [{str(code):>4}] {mark:>7}  /api/v3/{path}")
        if rows and not found:
            found = (path, rows, where)
    print()

    if not found:
        print("  No credits endpoint on this Radarr build.")
        print("  Movie people will have to keep coming from Trakt — do NOT run --sync.")
        print("  If your Radarr has a 'Credits' section in the movie detail UI, tell me")
        print("  which URL its web app calls and I'll add it to _CREDIT_ENDPOINTS.")
        return 1

    path, rows, where = found
    print(f"  Credits found at /api/v3/{path}  (shape: {where}, {len(rows)} rows)")
    print(f"    raw keys: {sorted(rows[0].keys())}")
    print(f"    sample  : {json.dumps(rows[0])[:320]}")
    norm = _normalise(rows)
    print(f"    parsed  : {len(norm['cast'])} cast / {len(norm['crew'])} crew")
    print(f"      cast[:3] = {[(c['name'], c['id']) for c in norm['cast'][:3]]}")
    print()
    print("  Confirm those ids are real TMDB person ids before running --sync.")
    return 0


def _load_state() -> set:
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            return {int(x) for x in (json.load(f) or {}).get("done", [])}
    except Exception:
        return set()


def _save_state(done: set) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"done": sorted(done)}, f, separators=(",", ":"))
    except Exception:
        pass


def sync(base: str, api: str, *, limit: int | None, dry_run: bool, force: bool,
         workers: int = 8, resume: bool = False) -> int:
    movies = _get(base, api, "movie") or []
    tmpl = _discover_credit_endpoint(base, api, movies)
    if not tmpl:
        print("No credits endpoint on this Radarr build — run --probe. Nothing written.")
        return 1
    print(f"Using /api/v3/{tmpl}")
    todo = [m for m in movies if m.get("id") and m.get("tmdbId")]
    if not force:
        todo = [m for m in todo if not is_cached(_PEOPLE_BUCKET, int(m["tmdbId"]))]
    done = _load_state() if resume else set()
    if done:
        before = len(todo)
        todo = [m for m in todo if int(m["tmdbId"]) not in done]
        print(f"Resuming: {before - len(todo):,} already done in a previous pass.")
    if limit:
        todo = todo[:limit]
    print(f"{len(todo):,} movie(s) to fetch credits for"
          f"{' (dry run)' if dry_run else ''}, {workers} worker(s).\n")

    lock = threading.Lock()
    counts = {"written": 0, "empty": 0, "failed": 0, "n": 0}
    t0 = time.time()

    def one(m: dict) -> None:
        tmdb = int(m["tmdbId"])
        try:
            payload = _get(base, api, tmpl.format(id=m["id"], tmdb=tmdb))
        except Exception as e:
            with lock:
                counts["failed"] += 1
                if counts["failed"] <= 5:
                    print(f"  ! {m.get('title')}: {e}")
            return
        rows, _where = _looks_like_credits(payload)
        norm = _normalise(rows or [])
        # Negative-cache empties, matching the daemon: an empty marker stops the title
        # being retried forever by both this tool and the daemon's people endpoint.
        payload_out = norm if (norm["cast"] or norm["crew"]) else {}
        if not dry_run:
            write_cache(_PEOPLE_BUCKET, tmdb, payload_out)
        with lock:
            done.add(tmdb)
            if payload_out:
                counts["written"] += 1
            else:
                counts["empty"] += 1
            counts["n"] += 1
            if counts["n"] % 500 == 0:
                rate = counts["n"] / max(1e-6, time.time() - t0)
                eta = (len(todo) - counts["n"]) / max(1e-6, rate)
                print(f"  {counts['n']:,}/{len(todo):,}  ({rate:.0f}/s, "
                      f"{counts['written']:,} written, ETA {eta/60:.1f}m)")
                _save_state(done)

    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(one, todo))
    except KeyboardInterrupt:
        print("\n  Interrupted — saving progress so --resume can pick up here.")
    finally:
        if not dry_run:
            _save_state(done)

    dt = time.time() - t0
    print(f"\n  written={counts['written']:,}  empty={counts['empty']:,}  "
          f"failed={counts['failed']:,}  in {dt:.0f}s "
          f"({counts['n']/max(1e-6, dt):.0f}/s)")
    if not dry_run and counts["written"]:
        print("  The daemon will now SKIP the people endpoint for these titles (is_cached),")
        print("  so the remaining Trakt backfill drops by roughly this many calls.")
    return 0


def prune_foreign(base: str, api: str, dry_run: bool) -> int:
    """Delete movie people buckets for titles Radarr does not hold.

    Radarr can only serve credits for movies in its library — ``/api/v3/credit`` is keyed
    on the internal movieId, and ``movie/lookup`` carries no credits. So a watchlisted
    title that was never added (271 of 845 here) can only have Trakt-sourced credits.

    Those are worth strictly LESS than nothing. The household affinity vector is built
    from watched titles, all of which are in Radarr, so it lives in TMDB id space — a
    Trakt person id on a candidate cannot match it under any circumstances. Keeping them
    buys no signal at all, while guaranteeing the same actor holds two ids and breaking
    the one check that can detect a mixed corpus. Deleting them costs nothing real:
    ``_weighted()`` drops absent signals, so those candidates simply score on genre,
    source, rating and popularity instead of taking a penalty.
    """
    movies = _get(base, api, "movie") or []
    rad = {int(m["tmdbId"]) for m in movies if m.get("tmdbId")}
    victims = []
    for f in _PEOPLE_BUCKET.glob("*.json.gz"):
        try:
            tmdb = int(f.stem)
        except ValueError:
            continue
        if tmdb not in rad:
            victims.append(f)
    print(f"  {len(victims):,} people bucket(s) for titles not in Radarr "
          f"{'would be' if dry_run else 'to be'} removed.")
    if not dry_run:
        for f in victims:
            f.unlink(missing_ok=True)
        print("  Removed. Radarr is now the only source of movie credits.")
    return 0


def verify() -> int:
    """Check the written buckets hold the right film AND one consistent id space.

    Deliberately does NOT assert hardcoded TMDB person ids. Radarr is the authority on
    those and I'd only be checking my own recollection against it — the first draft of
    this function claimed 3223 was Michael J. Fox when your library says it is Robert
    Downey Jr. So the checks are ones the data can answer for itself:

      1. right film — a known cast member appears under the expected tmdbId;
      2. one id space — a person appearing in several films carries the SAME id in each,
         which is exactly what breaks if Trakt-sourced and Radarr-sourced credits get
         mixed (Trakt's sequential per-title ids would differ per film).
    """
    import gzip
    from collections import defaultdict

    checks = [
        (105, "Back to the Future", "Michael J. Fox"),
        (65, "8 Mile", "Eminem"),
        (419430, "Get Out", "Daniel Kaluuya"),
        (7446, "Tropic Thunder", "Ben Stiller"),
    ]
    ok = True
    for tmdb, title, person in checks:
        p = _PEOPLE_BUCKET / f"{tmdb}.json.gz"
        if not p.exists():
            print(f"  skip  {title}: no bucket yet")
            continue
        d = json.loads(gzip.open(p, "rt", encoding="utf-8").read())
        hit = next((c for c in (d.get("cast") or [])
                    if person.lower() in (c.get("name") or "").lower()), None)
        if not hit:
            names = [c.get("name") for c in (d.get("cast") or [])[:3]]
            print(f"  FAIL  {title}: no {person} — got {names} — WRONG FILM")
            ok = False
            continue
        print(f"  PASS  {title}: {person} id={hit.get('id')}")

    # Id-space consistency across every bucket present.
    seen: dict[str, set] = defaultdict(set)
    n = 0
    for f in _PEOPLE_BUCKET.glob("*.json.gz"):
        try:
            d = json.loads(gzip.open(f, "rt", encoding="utf-8").read())
        except Exception:
            continue
        n += 1
        for c in (d.get("cast") or []):
            if c.get("name") and c.get("id") is not None:
                seen[c["name"]].add(c["id"])
    multi = {k: v for k, v in seen.items() if len(v) > 1}
    print(f"\n  scanned {n:,} bucket(s); {len(seen):,} distinct cast names")
    if multi:
        ok = False
        print(f"  FAIL  {len(multi)} name(s) carry MORE THAN ONE id — mixed id spaces:")
        for k, v in list(multi.items())[:5]:
            print(f"          {k}: {sorted(v)}")
    else:
        print("  PASS  every name maps to exactly one id — single id space")
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Populate the movie people bucket from Radarr credits")
    ap.add_argument("--probe", action="store_true", help="Print a real credits payload; assume nothing")
    ap.add_argument("--probe-bulk", action="store_true",
                    help="Time bulk endpoint shapes against the per-movie one")
    ap.add_argument("--sync", action="store_true", help="Fetch credits for the library")
    ap.add_argument("--verify", action="store_true", help="Check written buckets carry TMDB person ids")
    ap.add_argument("--limit", type=int, default=None, help="With --sync: stop after N movies")
    ap.add_argument("--dry-run", action="store_true", help="With --sync: fetch but write nothing")
    ap.add_argument("--force", action="store_true", help="With --sync: refetch even if cached")
    ap.add_argument("--prune-foreign", action="store_true",
                    help="Delete people buckets for titles Radarr doesn't hold (Trakt-sourced ids)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent requests (default 8)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip titles completed by an earlier interrupted pass")
    args = ap.parse_args()
    if not (args.probe or args.probe_bulk or args.sync or args.verify or args.prune_foreign):
        ap.print_help()
        return 2
    if args.verify and not (args.probe or args.sync or args.prune_foreign):
        return verify()

    cfg = ConfigLoader(CONFIG_PATH).load() or {}
    base, api = _radarr(cfg)
    if not base or not api:
        print("Radarr is not configured (no base_url / api key).")
        return 1
    if args.probe:
        return probe(base, api)
    if args.probe_bulk:
        return probe_bulk(base, api)
    rc = 0
    if args.sync:
        rc = sync(base, api, limit=args.limit, dry_run=args.dry_run, force=args.force,
                  workers=args.workers, resume=args.resume)
    if args.prune_foreign:
        rc = prune_foreign(base, api, args.dry_run) or rc
    if args.verify:
        rc = verify() or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

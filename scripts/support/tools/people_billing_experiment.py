"""people_billing_experiment.py — measure the billing decay + role weights against
================================================================================
THIS household's actual watch behaviour (GLD-PPL-01 / GLD-PPL-03 / GLD-PPL-04).

The people-affinity signal weights every credited person by
``role_weight x billing_weight(rank) x engagement`` — but the seven role weights
and the 0.25 billing decay are, per people_matrix/DESIGN.md, "asserted from
intuition, propagating everywhere, unmeasured". This tool measures them, offline
and read-only, by replaying the PRODUCTION aggregator
(:func:`aggregate_person_affinity` — role_weights / billing_decay / engagement are
already parameters) under swept constants and scoring each variant on how well the
resulting affinity separates titles the household went on to watch from library
titles it never touched.

Two stages, mirroring the DESIGN's own gate ("Q3 gates Q1 — coverage first"):

    python scripts/support/tools/people_billing_experiment.py            # INSPECT
    python scripts/support/tools/people_billing_experiment.py --sweep    # EXPERIMENT

INSPECT reports what is on disk: forward-map size, per-role id coverage (the
GLD-PPL-03 number that decides whether the sweep is over a biased sample), the
watched-set source found, date + engagement availability, and the overlap between
the two. Paste its output before trusting sweep numbers.

SWEEP runs, per scheme:
  - a TEMPORAL holdout when watch dates exist (train on the earlier --split
    fraction, test on the rest; falls back to a seeded random split and says so):
    AUC + lift of train-built affinity separating test-watched titles from
    never-watched forward-map titles (the forward map IS the negative universe —
    library titles with credits the household did not watch),
  - split-half STABILITY: Jaccard overlap of the top-25 people computed from two
    halves of the train window — a scheme that yields the same favourite people
    from either half is capturing signal, not noise,
  - and a per-role ablation (each role as the ONLY nonzero weight, current
    constants otherwise) — the first empirical test of the role-weight ORDERING
    (does director-affinity out-predict editor-affinity, as the table claims?).

The recommendation block at the end names the best decay, the cast-depth knee
(where another 5 billing ranks stops buying AUC), and the measured role ordering
next to the asserted one. Movie-only for now — the forward map is Radarr-only
(GLD-PPL-09). Nothing is written anywhere except --json, if you ask for it.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── repo bootstrap: walk up to the first dir containing scripts/ ─────────────────
_here = Path(__file__).resolve()
_REPO = next((p for p in _here.parents if (p / "scripts").is_dir()), _here.parents[-1])
sys.path.insert(0, str(_REPO))

try:
    from scripts.managers.machine_learning.people_matrix import (          # type: ignore
        BILLED_ROLES, PERSON_BILLING_DECAY, PERSON_ROLE_WEIGHTS,
        billing_weight, deserialize_forward,
    )
    from scripts.managers.machine_learning.affinity.genre_affinity import (  # type: ignore
        aggregate_person_affinity,
    )
except Exception as e:                                                     # pragma: no cover
    print(f"cannot import the production modules ({e}) — run from the repo root")
    raise SystemExit(1)


def _cache_base() -> Path:
    try:
        from scripts.managers.factories.cache.key_builder import CacheKeyBuilder  # type: ignore
        return Path(CacheKeyBuilder().base_dir)
    except Exception:
        return _REPO / "scripts" / "support" / "cache"


# ── loading: forward map + watched set, defensively ─────────────────────────────
def _unwrap(obj):
    """Global-cache values may sit inside a {value:..., ts:...}-style envelope."""
    if isinstance(obj, dict) and "value" in obj and len(obj) <= 4:
        return obj["value"]
    return obj


def _load_forward(base: Path):
    """The people_matrix forward map {(medium, ext_id): {role: [pids in billing order]}}."""
    pm_dir = base / "people_matrix"
    candidates = sorted(pm_dir.glob("*forward*")) if pm_dir.is_dir() else []
    for p in candidates:
        try:
            raw = _unwrap(json.loads(p.read_text(encoding="utf-8")))
            fwd = deserialize_forward(raw)
            if fwd:
                return fwd, p
        except Exception:
            continue
    return {}, None


_TMDB_COLS = ("tmdb_id", "tmdbId", "tmdb")
_WATCHED_COLS = ("is_watched", "watched")
_DATE_COLS = ("last_watched_at", "last_watched", "watched_at", "last_played_at")
_ENGAGE_COLS = ("watch_count", "play_count", "plays")


def _pick(colnames, options):
    for o in options:
        if o in colnames:
            return o
    return None


def _load_watched(base: Path, parquet_override: Path | None):
    """Dated watched movie keys from the radarr parquet: [(key, date|None, engagement)]."""
    import pandas as pd
    paths = [parquet_override] if parquet_override else \
        sorted(base.rglob("movie_files.parquet"))
    for p in paths:
        if p is None or not p.exists():
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        tc = _pick(df.columns, _TMDB_COLS)
        wc = _pick(df.columns, _WATCHED_COLS)
        if not tc or not wc:
            continue
        dc = _pick(df.columns, _DATE_COLS)
        ec = _pick(df.columns, _ENGAGE_COLS)
        rows = []
        sub = df[df[wc] == True]  # noqa: E712  (pandas bool column)
        for _, r in sub.iterrows():
            try:
                tmdb = int(r[tc])
            except (TypeError, ValueError):
                continue
            date = None
            if dc is not None and pd.notna(r.get(dc)):
                try:
                    date = pd.to_datetime(r[dc], utc=True).to_pydatetime()
                except Exception:
                    date = None
            eng = 1.0
            if ec is not None and pd.notna(r.get(ec)):
                try:
                    eng = 1.0 + math.log1p(max(0.0, float(r[ec]) - 1.0))
                except (TypeError, ValueError):
                    eng = 1.0
            rows.append((("movie", tmdb), date, eng))
        if rows:
            return rows, p, {"tmdb": tc, "watched": wc, "date": dc, "engagement": ec}
    return [], None, {}


def _load_collections(base: Path) -> dict:
    """{('movie', tmdb): collection_tmdbId} from the radarr whole-library snapshots
    (radarr.movies.<inst>.full.json) — the franchise-membership source for
    --decontaminate (GLD-PPL-12). Envelope-tolerant; standalone titles absent."""
    out: dict = {}
    for p in sorted(base.glob("radarr.movies.*.full.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("value") if isinstance(data, dict) and "value" in data else data
        if isinstance(items, dict):
            items = items.get("movies") or items.get("items") or list(items.values())
        for mv in items or []:
            if not isinstance(mv, dict):
                continue
            t = mv.get("tmdbId")
            col = (mv.get("collection") or {}).get("tmdbId")
            if t and col:
                try:
                    out[("movie", int(t))] = int(col)
                except (TypeError, ValueError):
                    continue
    return out


def _norm_keys(fwd: dict):
    """Forward-map keys may deserialize as tuples or 'movie:123' strings — normalize
    to ('movie', int) so the watched-set join is exact either way."""
    out = {}
    for k, v in fwd.items():
        if isinstance(k, tuple) and len(k) == 2:
            try:
                out[(str(k[0]), int(k[1]))] = v
                continue
            except (TypeError, ValueError):
                pass
        if isinstance(k, str) and ":" in k:
            m, _, i = k.partition(":")
            try:
                out[(m, int(i))] = v
                continue
            except ValueError:
                pass
        out[k] = v
    return out


# ── scoring one variant ─────────────────────────────────────────────────────────
def _title_score(roles: dict, aff: dict, role_weights: dict, decay: float,
                 cast_limit: int) -> float:
    """Candidate-side score under the SAME scheme as the affinity build — symmetric
    by design so a scheme is judged as a whole, not half-applied."""
    s = 0.0
    for role, pids in (roles or {}).items():
        rw = role_weights.get(role, 0.0)
        if rw <= 0 or not pids:
            continue
        billed = role in BILLED_ROLES
        for rank, pid in enumerate(pids[: cast_limit if billed else len(pids)]):
            w = billing_weight(rank, decay=decay) if billed else 1.0
            s += rw * w * aff.get(pid, 0.0)
    return s


def _auc(pos: list, neg: list) -> float:
    """Rank AUC, ties at 0.5 — exact O(P*N), fine at these sizes, stdlib only."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / (len(pos) * len(neg))


def _cap(fwd_roles: dict, cast_limit: int) -> dict:
    """Truncate cast to the swept depth BEFORE the production aggregator runs, so
    cast_limit is exercised on the affinity-build side too (the aggregator itself
    walks whatever list it is given)."""
    out = {}
    for role, pids in (fwd_roles or {}).items():
        out[role] = pids[:cast_limit] if role in BILLED_ROLES else pids
    return out


def _run_variant(name, train_keys, test_keys, neg_keys, fwd, *, role_weights,
                 decay, cast_limit, engagement):
    capped = {k: _cap(v, cast_limit) for k, v in fwd.items()}
    aff = aggregate_person_affinity(
        train_keys, capped, role_weights=role_weights,
        engagement=engagement, billing_decay=decay)
    pos = [_title_score(capped.get(k), aff, role_weights, decay, cast_limit)
           for k in test_keys]
    neg = [_title_score(capped.get(k), aff, role_weights, decay, cast_limit)
           for k in neg_keys]
    auc = _auc(pos, neg)
    mp, mn = (sum(pos) / len(pos)) if pos else 0.0, (sum(neg) / len(neg)) if neg else 0.0
    lift = (mp / mn) if mn > 0 else float("inf") if mp > 0 else 1.0
    # split-half stability of the top-25 people
    half = len(train_keys) // 2
    top = lambda keys: set(list(dict(sorted(aggregate_person_affinity(
        keys, capped, role_weights=role_weights, engagement=engagement,
        billing_decay=decay).items(), key=lambda kv: -kv[1])).keys())[:25])
    a, b = top(train_keys[:half]), top(train_keys[half:])
    stab = (len(a & b) / len(a | b)) if (a or b) else 0.0
    return {"scheme": name, "decay": decay, "cast_limit": cast_limit,
            "auc": round(auc, 4), "lift": round(lift, 2), "stability@25": round(stab, 3)}


# ── main ────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true", help="run the experiment (default: inspect only)")
    ap.add_argument("--cache-base", type=Path, default=None)
    ap.add_argument("--parquet", type=Path, default=None, help="explicit movie_files.parquet")
    ap.add_argument("--split", type=float, default=0.8, help="train fraction (temporal when dated)")
    ap.add_argument("--neg-ratio", type=int, default=3, help="negatives per positive")
    ap.add_argument("--decontaminate", action="store_true",
                    help="GLD-PPL-12: drop test titles AND negatives sharing a Radarr "
                         "collection with any train title — separates 'follows people' "
                         "from 'follows franchises'")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", type=Path, default=None, help="dump results as JSON")
    args = ap.parse_args()

    base = (args.cache_base or _cache_base()).resolve()
    print(f"cache base: {base}")

    fwd_raw, fwd_path = _load_forward(base)
    fwd = _norm_keys(fwd_raw)
    print(f"forward map: {len(fwd)} title(s)  ({fwd_path})" if fwd else
          "forward map: NOT FOUND under cache/people_matrix/ — run the people-matrix build first")

    watched, wp, cols = _load_watched(base, args.parquet)
    print(f"watched set: {len(watched)} watched movie(s)  ({wp})  columns={cols}" if watched else
          "watched set: NOT FOUND — no movie_files.parquet with a tmdb + watched column")
    if not fwd or not watched:
        return 1

    # ── join diagnostics + self-heal (first live run: 0/213 tuple overlap) ───────
    _fwd_sample = list(fwd.keys())[:3]
    _w_sample = [k for k, _, _ in watched[:3]]
    print(f"key samples — forward: {_fwd_sample}  watched: {_w_sample}")
    _fwd_ints = {k[1] for k in fwd if isinstance(k, tuple) and len(k) == 2}
    _w_ints = {k[1] for k, _, _ in watched}
    _int_overlap = len(_fwd_ints & _w_ints)
    if not any(k in fwd for k, _, _ in watched) and _int_overlap:
        # Id spaces agree but the medium token doesn't — adopt the forward map's
        # dominant medium so the join works, and say so out loud.
        from collections import Counter as _C
        _dom = _C(k[0] for k in fwd if isinstance(k, tuple)).most_common(1)[0][0]
        print(f"medium mismatch self-heal: id-space overlap {_int_overlap} but tuple join 0 — "
              f"remapping watched keys to medium '{_dom}'")
        watched = [((_dom, k[1]), d, e) for k, d, e in watched]
    elif _int_overlap == 0:
        print("id-space overlap is 0 — the watched tmdb ids and the forward map's ext ids do "
              "not intersect AT ALL; paste this block (samples above) for diagnosis")

    # ── INSPECT: the numbers that gate everything else ──────────────────────────
    role_titles, role_ids = defaultdict(int), defaultdict(int)
    for roles in fwd.values():
        for role, pids in (roles or {}).items():
            if pids:
                role_titles[role] += 1
                role_ids[role] += len(pids)
    print("\nper-role coverage (GLD-PPL-03 — the number that decides sample bias):")
    for role in sorted(role_titles, key=lambda r: -role_titles[r]):
        print(f"  {role:<16} {role_titles[role]:>5} title(s)   {role_ids[role]:>6} credit id(s)")

    wkeys = [k for k, _, _ in watched]
    overlap = [k for k in wkeys if k in fwd]
    dated_raw = sum(1 for _, d, _ in watched if d is not None)
    dated = [(k, d, e) for k, d, e in watched if d is not None and k in fwd]
    print(f"\nwatched ∩ forward map: {len(overlap)}/{len(wkeys)} "
          f"({(100 * len(overlap) / max(1, len(wkeys))):.0f}%)  |  dated: {dated_raw} raw, "
          f"{len(dated)} in-join")
    if not args.sweep:
        print("\ninspect only — re-run with --sweep once these numbers look sane.")
        return 0
    if not overlap:
        print("\nzero overlap — the sweep would measure nothing; fix the join first "
              "(see key samples above).")
        return 1
    if len(overlap) < 20:
        print(f"\nonly {len(overlap)} overlapping title(s) — too thin to measure anything; "
              f"a sweep at this size is coincidence theater. Fix the join / rebuild the "
              f"forward map first.")
        return 1

    # ── split ───────────────────────────────────────────────────────────────────
    rng = random.Random(args.seed)
    engagement = {k: e for k, _, e in watched if k in fwd}
    if len(dated) >= 40:
        dated.sort(key=lambda t: t[1])
        cut = int(len(dated) * args.split)
        train_keys = [k for k, _, _ in dated[:cut]]
        test_keys = [k for k, _, _ in dated[cut:]]
        mode = f"TEMPORAL split at {dated[cut][1].date()} " \
               f"({len(train_keys)} train / {len(test_keys)} test)"
    else:
        keys = list(overlap)
        rng.shuffle(keys)
        cut = int(len(keys) * args.split)
        train_keys, test_keys = keys[:cut], keys[cut:]
        mode = f"RANDOM split, seed {args.seed} ({len(train_keys)}/{len(test_keys)}) — " \
               f"dates unavailable, treat AUC as weaker evidence"
    neg_pool = [k for k in fwd if k not in set(wkeys)]
    if args.decontaminate:
        cols = _load_collections(base)
        if not cols:
            print("decontaminate: no collection data found in radarr.movies.*.full.json — "
                  "proceeding UNdecontaminated")
        else:
            train_cols = {c for k in train_keys if (c := cols.get(k)) is not None}
            t0, p0 = len(test_keys), len(neg_pool)
            test_keys = [k for k in test_keys if cols.get(k) not in train_cols]
            neg_pool = [k for k in neg_pool if cols.get(k) not in train_cols]
            print(f"decontaminated (GLD-PPL-12): train spans {len(train_cols)} collection(s); "
                  f"dropped {t0 - len(test_keys)}/{t0} test + {p0 - len(neg_pool)} negative "
                  f"franchise-continuation title(s); {len(test_keys)} clean positives remain")
            if len(test_keys) < 10:
                print("  ⚠️ fewer than 10 clean positives — treat every number below as "
                      "directional, not conclusive")
    rng.shuffle(neg_pool)
    neg_keys = neg_pool[: max(1, len(test_keys) * args.neg_ratio)]
    print(f"\n{mode}; effective positives after filters: {len(test_keys)}; "
          f"negatives: {len(neg_keys)} never-watched library titles\n")

    # ── the sweep ───────────────────────────────────────────────────────────────
    results = []
    print(f"{'scheme':<26}{'decay':>7}{'cast≤':>7}{'AUC':>8}{'lift':>8}{'stab@25':>9}")
    def emit(r):
        results.append(r)
        print(f"{r['scheme']:<26}{r['decay']:>7.2f}{r['cast_limit']:>7}"
              f"{r['auc']:>8.4f}{r['lift']:>8.2f}{r['stability@25']:>9.3f}")

    for decay in (0.0, 0.10, PERSON_BILLING_DECAY, 0.40, 0.60, 1.00):
        tag = " (current)" if abs(decay - PERSON_BILLING_DECAY) < 1e-9 else \
              " (flat)" if decay == 0.0 else ""
        emit(_run_variant(f"decay={decay:.2f}{tag}", train_keys, test_keys, neg_keys,
                          fwd, role_weights=PERSON_ROLE_WEIGHTS, decay=decay,
                          cast_limit=10, engagement=engagement))
    for lim in (3, 5, 10, 15, 20):
        emit(_run_variant(f"cast_limit={lim}", train_keys, test_keys, neg_keys,
                          fwd, role_weights=PERSON_ROLE_WEIGHTS,
                          decay=PERSON_BILLING_DECAY, cast_limit=lim,
                          engagement=engagement))
    print("\nper-role ablation (GLD-PPL-01 — each role alone; asserted order is "
          "cast=directors > writers > composers > producers=cinematographers > editors):")
    role_rows = []
    for role in sorted(PERSON_ROLE_WEIGHTS, key=lambda r: -PERSON_ROLE_WEIGHTS[r]):
        rw = {r: (1.0 if r == role else 0.0) for r in PERSON_ROLE_WEIGHTS}
        r = _run_variant(f"role={role}", train_keys, test_keys, neg_keys, fwd,
                         role_weights=rw, decay=PERSON_BILLING_DECAY,
                         cast_limit=10, engagement=engagement)
        role_rows.append(r)
        print(f"  {role:<18} AUC {r['auc']:.4f}   lift {r['lift']:.2f}   "
              f"(asserted weight {PERSON_ROLE_WEIGHTS[role]})")
    results += role_rows

    # ── recommendation ──────────────────────────────────────────────────────────
    decays = [r for r in results if r["scheme"].startswith("decay=")]
    best_d = max(decays, key=lambda r: (r["auc"], r["stability@25"]))
    lims = sorted((r for r in results if r["scheme"].startswith("cast_limit=")),
                  key=lambda r: r["cast_limit"])
    knee = lims[-1]["cast_limit"]
    for a, b in zip(lims, lims[1:]):
        if b["auc"] - a["auc"] < 0.005:
            knee = a["cast_limit"]
            break
    measured = [r["scheme"].split("=")[1] for r in
                sorted(role_rows, key=lambda r: -(r["auc"] if r["auc"] == r["auc"] else -1))]
    print(f"\nRECOMMENDATION (this household's data):")
    print(f"  billing decay : {best_d['decay']:.2f}  (current {PERSON_BILLING_DECAY}) — "
          f"AUC {best_d['auc']:.4f}, stability {best_d['stability@25']:.3f}")
    print(f"  cast depth    : {knee}  (current 10) — the knee where deeper billing "
          f"stops buying AUC")
    print(f"  role ordering : measured {' > '.join(measured)}")
    print(f"  NOTE: interpret against the coverage block above — thin-coverage roles "
          f"measure their coverage, not their signal.")

    if args.json:
        args.json.write_text(json.dumps(
            {"mode": mode, "coverage": dict(role_titles), "results": results},
            indent=2, default=str), encoding="utf-8")
        print(f"\nresults written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

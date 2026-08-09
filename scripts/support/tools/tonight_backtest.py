"""tonight_backtest.py — what would the Tonight thresholds have bought?

Operator tool. Replays the CACHED Tautulli history day by day and reports the
hit rate for every (day_threshold, min_plays) pair, so the numbers in
`habits.py` can be set from this household's evidence rather than from the
synthetic data they were guessed on.

    python -m scripts.support.tools.tonight_backtest
    python -m scripts.support.tools.tonight_backtest --tz -4 --warmup 45
    python -m scripts.support.tools.tonight_backtest --user 8592385

READS ONLY. It touches the history cache and writes nothing - no Plex calls, no
cache writes, no plan. Safe to run at any time, including mid-run.

ERROR-SAFE. A missing cache, an empty file, malformed rows or a history shorter
than the warm-up all produce a printed reason and exit 0. This is a diagnostic;
it must never be the thing that fails a run.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.managers.machine_learning.playlists import backtest as B  # noqa: E402
from scripts.managers.machine_learning.playlists import habits as H  # noqa: E402

CACHE = (Path(__file__).resolve().parents[1] / "cache" / "tautulli" / "history")


def load_rows(path: Path):
    """``(rows, reason)`` - never raises.

    The cache has been written in two shapes over time (a bare list, and a
    ``{"value": [...]}`` envelope), so both are accepted. An unknown shape is
    reported rather than guessed at, because silently reading zero rows would
    look identical to a household that has never watched anything.
    """
    if not path.is_file():
        return [], f"no history cache at {path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"cannot read {path.name}: {type(exc).__name__}: {exc}"
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("data", raw))
    if not isinstance(raw, list):
        return [], f"{path.name} is not a list of history rows (got {type(raw).__name__})"
    rows = [r for r in raw if isinstance(r, dict) and r.get("date") is not None]
    if not rows:
        return [], f"{path.name} holds no dated rows"
    return rows, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tz", type=float, default=None,
                    help="local UTC offset in hours (default: this host's)")
    ap.add_argument("--warmup", type=int, default=B.DEFAULT_WARMUP_DAYS)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--user", default=None, help="one Tautulli user_id (default: each)")
    ap.add_argument("--min-users-plays", type=int, default=30,
                    help="skip profiles with fewer plays than this")
    args = ap.parse_args(argv)

    if args.tz is None:
        from datetime import datetime
        off = datetime.now().astimezone().utcoffset()
        args.tz = (off.total_seconds() / 3600.0) if off else 0.0

    rows, reason = load_rows(CACHE / "all.json")
    if reason:
        print(f"[backtest] {reason}")
        print("[backtest] nothing to do - run the Tautulli history fetch first.")
        return 0

    by_user = collections.defaultdict(list)
    for r in rows:
        by_user[str(r.get("user_id"))].append(r)
    if args.user:
        by_user = {args.user: by_user.get(args.user, [])}

    print(f"[backtest] {len(rows):,} plays · tz {args.tz:+.0f}h · "
          f"{args.warmup}d warm-up · limit {args.limit}")

    ranked = sorted(by_user.items(), key=lambda kv: -len(kv[1]))
    for uid, urows in ranked:
        if len(urows) < args.min_users_plays:
            print(f"\n  user {uid}: {len(urows)} plays - below --min-users-plays, skipped")
            continue
        res = B.backtest(urows, tz_offset_hours=args.tz,
                         warmup_days=args.warmup, limit=args.limit)
        if res.get("reason"):
            print(f"\n  user {uid}: {res['reason']}")
            continue
        print(f"\n  user {uid} · {len(urows)} plays · {res['days_scored']} day(s) scored "
              f"({res['days_dormant']} dormant, excluded)")
        print(f"    {'thresh':>6} {'minpl':>5} {'hit':>6} {'prec':>6} {'recall':>6} {'silent':>6}")
        for (t, m), s in sorted(res["grid"].items()):
            p = "  -  " if s["precision"] is None else f"{s['precision']:.2f} "
            r = "  -  " if s["recall"] is None else f"{s['recall']:.2f} "
            print(f"    {t:6.2f} {m:5d} {s['hit_rate']:6.2f} {p:>6} {r:>6} "
                  f"{s['silent_rate']:6.2f}")
        best = B.best_cell(res)
        if best is None:
            print("    -> no cell speaks on at least half the days; the model is "
                  "too quiet at every setting on this profile.")
        else:
            (t, m), s = best
            print(f"    -> best usable: threshold {t:.2f}, min_plays {m} "
                  f"(hit {s['hit_rate']:.2f}, silent {s['silent_rate']:.2f})")
            print(f"       current defaults are threshold "
                  f"{H.DEFAULT_DAY_THRESHOLD:.2f}, min_plays {H.DEFAULT_MIN_PLAYS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

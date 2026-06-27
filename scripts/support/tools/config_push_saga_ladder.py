"""config_push_saga_ladder.py — push the recalibrated quality ladder + activate the saga credit in
the LIVE config.json.

Two coupled config changes that together make the symmetric web->bluray->remux ladder AND the
household cross-media saga caught-up/depth credit actually take effect on the running engine:

  1. RE-SPACED LADDER — sync the recalibrated keys to the canonical schema defaults
     (scripts/managers/factories/onboarding/schema.empty_config): ``radarr_quality_ladder`` plus the
     ``watch_likelihood`` cutoffs ``started_floor`` / ``affinity_cap`` / ``uhd_cutoff`` / ``fhd_cutoff``.
     These are read with a per-key fallback to the code default, so if the live config PINS the old
     values they override the new defaults and the new spacing is dead until this runs. Values are
     pulled from ``empty_config`` itself, so the tool can never drift from the code.

  2. ACTIVATE SAGA CREDIT — set ``scoring.saga_credit.enabled = true``. The whole saga/universe credit
     is opt-in (default OFF -> ``gather_saga_engagement`` returns {} -> byte-identical inert), so it
     does NOTHING live until this flag is set. The math knobs (cap 6.0, 90d grace at the 4-member
     reference, sqrt(ref/N) household scaling, 30d post-grace half-life) are correct code defaults and
     are deliberately NOT written here — override them under ``watch_likelihood`` only if you want
     different spacing.

DRY-RUN BY DEFAULT — prints a before->after table and writes NOTHING. Pass ``--confirm`` to apply,
which first writes a timestamped backup (``config.json.bak-YYYYMMDD-HHMMSS``) next to the config, then
rewrites it atomically. Idempotent: a no-op (no write, no backup) once the live config already matches.

    python -m scripts.support.tools.config_push_saga_ladder            # dry-run (no writes)
    python -m scripts.support.tools.config_push_saga_ladder --confirm   # APPLY (backs up first)
    python -m scripts.support.tools.config_push_saga_ladder --config /path/to/config.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.managers.factories.daemons.daemon_paths import CONFIG_PATH      # noqa: E402
from scripts.managers.factories.onboarding.schema import empty_config        # noqa: E402

_MISSING = object()

# The ladder recalibration touched exactly these watch_likelihood keys (net schema diff
# 4271750~1..25ef2db); hd_cutoff/uhd_res/etc. were unchanged so we leave them alone.
_WL_KEYS = ("started_floor", "affinity_cap", "uhd_cutoff", "fhd_cutoff")


def _targets() -> dict:
    """{key_path_tuple: desired_value}. Ladder + cutoffs come from the canonical empty_config so they
    track the code; the saga master switch is a literal (it has no schema default — it's opt-in)."""
    d = empty_config()
    wl = d.get("watch_likelihood", {}) or {}
    out = {("radarr_quality_ladder",): d.get("radarr_quality_ladder")}
    for k in _WL_KEYS:
        out[("watch_likelihood", k)] = wl.get(k)
    out[("scoring", "saga_credit", "enabled")] = True
    return out


def _get(cfg, path):
    cur = cfg
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _set(cfg, path, value) -> None:
    cur = cfg
    for p in path[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[path[-1]] = value


def _fmt(v) -> str:
    return "(absent)" if v is _MISSING else json.dumps(v)


def _print_saga_math_fyi() -> None:
    """Show the saga math defaults that go live once enabled — informational, never written."""
    try:
        from scripts.managers.machine_learning.likelihood.watch_likelihood import _DEFAULTS as wld
        print("saga credit math (code defaults, active once enabled):")
        print(f"  cap={wld['saga_credit_cap']}  engagement_full={wld['saga_engagement_full']}  "
              f"grace_days={wld['saga_grace_days']} @ ref {wld['saga_grace_ref_members']} members  "
              f"post_grace_halflife={wld['saga_postgrace_halflife_days']}d")
        print("  (override under watch_likelihood in config.json only if you want different spacing)")
        print()
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Push the re-spaced ladder + enable saga credit in the live config.json.")
    ap.add_argument("--config", default=str(CONFIG_PATH),
                    help="path to the live config.json (default: the engine's CONFIG_PATH)")
    ap.add_argument("--confirm", action="store_true",
                    help="apply the changes (writes a timestamped backup first)")
    args = ap.parse_args(argv)

    path = Path(args.config)
    if not path.is_file():
        print(f"ERROR: config not found: {path}")
        return 2
    raw = path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON: {e}")
        return 2
    if not isinstance(cfg, dict):
        print(f"ERROR: {path} is not a JSON object.")
        return 2

    print(f"config: {path}")
    print("push: re-spaced quality ladder + saga credit master switch")
    print()
    _print_saga_math_fyi()

    targets = _targets()
    pending = []
    for key_path, want in targets.items():
        have = _get(cfg, key_path)
        if have is _MISSING or have != want:
            pending.append((key_path, have, want))

    if not pending:
        print("Already current -- live config matches the re-spaced ladder and saga credit is on.")
        print("(no write, no backup)")
        return 0

    print(f"{'key':40} {'current':30} ->  new")
    print("-" * 90)
    for key_path, have, want in pending:
        key = ".".join(str(p) for p in key_path)
        print(f"{key:40} {_fmt(have)[:28]:30} ->  {_fmt(want)}")
    print()

    if not args.confirm:
        print(f"DRY-RUN -- {len(pending)} change(s) above. Re-run with --confirm to apply.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_text(raw, encoding="utf-8")

    for key_path, _have, want in pending:
        _set(cfg, key_path, want)
    out = json.dumps(cfg, indent=2, ensure_ascii=False)
    if raw.endswith("\n"):
        out += "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)

    print(f"Backed up original -> {backup.name}")
    print(f"Applied {len(pending)} change(s) to {path.name}.")
    print("Saga credit is now ACTIVE (scoring.saga_credit.enabled=true) and the ladder is re-spaced.")
    print("The next engine run picks it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

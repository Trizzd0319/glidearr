"""
acquire_preview.py — offline preview of the acquisition decision table.
================================================================================
Replays the REAL acquisition pipeline (CandidateGatherer normalization/dedup →
Resolver → AcquisitionScorer) against on-disk caches and a captured *arr
snapshot, and prints the same decision table AcquisitionManager renders — WITHOUT
adding anything to Sonarr/Radarr and without needing a throwaway library.

Why this exists (and why it is not just ``dry_run``):
  * ``dry_run`` still hits Trakt + the *arr lookup endpoints on every run — slow,
    rate-limited, and non-deterministic. This is deterministic and repeatable.
  * ``dry_run`` reaches the Adder and relies on a flag to not POST. This harness
    never imports Adder at all, and its gateway raises on any non-GET. There is
    no code path from here to a write.
  * It ignores ``acquisition.enabled`` on purpose, so you can preview decisions
    while the real pipeline stays switched off.

    # one-time (or whenever you want fresh *arr data) — live read-only GETs
    python scripts/support/tools/acquire_preview.py --capture

    # thereafter: fully offline, no network at all
    python scripts/support/tools/acquire_preview.py
    python scripts/support/tools/acquire_preview.py --verbose
    python scripts/support/tools/acquire_preview.py --min-score 40 --max-adds 25
    python scripts/support/tools/acquire_preview.py --sources trakt_watchlist
    python scripts/support/tools/acquire_preview.py --find dune

    # point a service at a specific instance (e.g. a disposable Radarr)
    python scripts/support/tools/acquire_preview.py --capture --radarr-instance test

Config is loaded through the app's own ConfigLoader, so API keys resolve from the
SecretStore (RECOMMENDARR_* env var → OS keyring) exactly as they do in main.py.
The ONLY thing this writes is its own snapshot under _acquire_preview_snapshots/
(and only with --capture, and only when the capture actually produced data).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # UTF-8 console so titles never crash on Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SCRIPTS = Path(__file__).resolve().parents[2]          # scripts/
if str(_SCRIPTS.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent))

from scripts.managers.factories.config.config_loader import ConfigLoader        # noqa: E402
from scripts.managers.services.acquisition.candidates import CandidateGatherer  # noqa: E402
from scripts.managers.services.acquisition.gateway import ArrGateway            # noqa: E402
from scripts.managers.services.acquisition.resolver import Resolver             # noqa: E402
from scripts.managers.services.acquisition.scorer import AcquisitionScorer      # noqa: E402

_CACHE = _SCRIPTS / "support" / "cache"
_CONFIG = _SCRIPTS / "support" / "config" / "config.json"
_SNAP_DIR = Path(__file__).parent / "_acquire_preview_snapshots"
_SNAP = _SNAP_DIR / "arr_snapshot.json"


# ── tiny shims ──────────────────────────────────────────────────────────────
class _Logger:
    """Permissive stand-in for the app logger; warnings surface, debug is opt-in."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def _emit(self, level: str, msg) -> None:
        if level == "debug" and not self.verbose:
            return
        print(f"  [{level}] {msg}")

    def __getattr__(self, name: str):
        level = name.replace("log_", "") or "info"
        return lambda msg, *a, **k: self._emit(level, msg)


class _Cache:
    """Stand-in for global_cache: serves the handful of keys the scorer reads."""

    def __init__(self):
        self._data = {}
        aff = _load_json(_CACHE / "tautulli" / "affinity.json")
        if aff is not None:
            self._data["tautulli/affinity"] = aff
        for key, rel in (
            ("people_matrix/forward", "people_matrix/forward.json"),
            ("people_matrix/affinity", "people_matrix/affinity.json"),
        ):
            val = _load_json(_CACHE / Path(rel))
            if val:
                self._data[key] = val

    def get(self, key, default=None):
        return self._data.get(key, default)


def _unwrap(obj):
    """Cache files are sometimes wrapped as {"value": ..., "timestamp": ...}."""
    if isinstance(obj, dict) and "value" in obj and len(obj) <= 3:
        return obj["value"]
    return obj


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return _unwrap(json.load(f))
    except (OSError, ValueError):
        return None


def _load_config(logger) -> dict:
    """Load config.json through the app's ConfigLoader so secret leaves are
    overlaid from the SecretStore (env → keyring). Reading the file directly
    yields blank api keys, because config.json is written with secrets stripped."""
    return ConfigLoader(_CONFIG, logger=logger).load() or {}


# ── candidate loading (real _norm / _dedup, cached payloads) ────────────────
def _trakt_user(config: dict) -> str:
    return str((config.get("trakt", {}) or {}).get("username") or "").strip()


def load_candidates(config: dict, sources: set) -> tuple[list, list]:
    """Returns (candidates, notes). Order mirrors the real gather(): watchlist
    before recommendations, so explicit intent wins de-duplication."""
    user = _trakt_user(config)
    base = _CACHE / "trakt" / user
    notes, raw = [], []
    if not user:
        return [], ["config.trakt.username is empty — no Trakt cache to read"]
    if not base.is_dir():
        return [], [f"no Trakt cache dir at {base}"]

    plan = [
        ("trakt_watchlist", "watchlist", "trakt_watchlist"),
        ("trakt_recommendations", "recommendations", "trakt_recommendations"),
    ]
    for source_key, folder, src_label in plan:
        if source_key not in sources:
            continue
        for kind, fname in (("show", "shows.json"), ("movie", "movies.json")):
            path = base / folder / fname
            items = _load_json(path)
            if items is None:
                notes.append(f"{source_key}: MISSING {folder}/{fname}")
                continue
            if not isinstance(items, list):
                notes.append(f"{source_key}: {folder}/{fname} is not a list — skipped")
                continue
            raw += [CandidateGatherer._norm(it, kind, src_label) for it in items]

    deduped = CandidateGatherer._dedup(raw)
    notes.append(f"loaded {len(raw)} raw → {len(deduped)} after de-dup")
    return deduped, notes


# ── gateway: read-only, snapshot-backed ─────────────────────────────────────
class SnapshotGateway(ArrGateway):
    """ArrGateway with the network swapped for a snapshot dict.

    Subclasses the real gateway so instance selection, memoisation, dedup and
    lookup all run the production code path. Writes are hard-removed: add/put/
    command raise, and _req refuses any non-GET.
    """

    def __init__(self, service, config, logger, snapshot: dict, live=None,
                 instance_override: str | None = None):
        super().__init__(service, None, config, logger)
        self.snapshot = snapshot
        self.live = live                # LiveReader when capturing, else None
        self.instance_override = instance_override
        self.misses: list = []

    @property
    def available(self) -> bool:
        return True

    def default_instance(self) -> str:
        return self.instance_override or super().default_instance()

    def categorized_instance(self, label: str = "1080p") -> str:
        if self.instance_override:
            return self.instance_override
        return super().categorized_instance(label)

    def _key(self, inst, endpoint) -> str:
        return f"{self.service}|{inst}|{endpoint}"

    def _req(self, inst, endpoint, method="GET", payload=None, fallback=None):
        if method != "GET":
            raise RuntimeError(
                f"acquire_preview is read-only; refused {method} {self.service}/{endpoint}"
            )
        key = self._key(inst, endpoint)
        if key in self.snapshot:
            return self.snapshot[key]
        if self.live is not None:
            val = self.live.get(self.service, inst, endpoint)
            if val is not None:
                self.snapshot[key] = val
                return val
        self.misses.append(key)
        return fallback

    # writes are not reachable from this harness
    def add(self, inst, payload):
        raise RuntimeError("acquire_preview never adds")

    def put(self, inst, endpoint, payload):
        raise RuntimeError("acquire_preview never writes")

    def command(self, inst, payload):
        raise RuntimeError("acquire_preview never issues commands")


class LiveReader:
    """Direct read-only GETs against the *arr instances (capture mode only).

    Credentials come from the already-overlaid config (ConfigLoader resolved them
    from the SecretStore); this does NOT touch keyring itself.
    """

    def __init__(self, config: dict, logger):
        import requests            # imported lazily so offline runs need no requests
        self._requests = requests
        self.config, self.logger = config, logger
        self._warned: set = set()

    def _warn_once(self, tag: str, msg: str) -> None:
        if tag not in self._warned:
            self._warned.add(tag)
            self.logger.log_warning(msg)

    def _endpoint_base(self, service, inst):
        insts = self.config.get(f"{service}_instances", {}) or {}
        node = insts.get(inst)
        if not isinstance(node, dict):
            return None, None
        base = node.get("base_url") or f"http://{node.get('url')}:{node.get('port')}"
        return base, node.get("api")

    def get(self, service, inst, endpoint):
        base, key = self._endpoint_base(service, inst)
        if not base or not key:
            missing = "url" if not base else "api key"
            self._warn_once(
                f"{service}/{inst}",
                f"capture: no {missing} for {service}/{inst} — is the SecretStore provisioned? "
                f"(`python scripts/support/setup/migrate_secrets.py`)",
            )
            return None
        try:
            r = self._requests.get(
                f"{base}/api/v3/{endpoint}",
                headers={"X-Api-Key": key},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self._warn_once(f"{service}/{inst}/err", f"capture: GET {service}/{inst} failed: {e}")
            return None


# ── rendering ───────────────────────────────────────────────────────────────
def _size_str(e: dict) -> str:
    gb = e.get("expected_size_gb")
    if not gb:
        return "-"
    return f"~{gb}GB" + ("/ep" if e.get("size_unit") == "per-episode" else "")


def _print_table(rows: list) -> None:
    if not rows:
        print("\n(no candidates survived resolution)")
        return
    hdr = (f"{'#':>3}  {'score':>5}  {'type':<5}  {'instance':<9}  "
           f"{'profile':<22}  {'~size':>10}  {'decision':<9}  title")
    print("\n" + hdr)
    print("-" * len(hdr))
    for i, (e, decision) in enumerate(rows, 1):
        prof = (e.get("quality_profile") or {}).get("name") or "-"
        title = e.get("title") or f"({e.get('id_field')}={e.get('ext_id')})"
        year = f" ({e['year']})" if e.get("year") else ""
        print(f"{i:>3}  {e.get('score', 0):>5}  {e.get('type', '?'):<5}  "
              f"{str(e.get('instance', '-')):<9}  {str(prof)[:22]:<22}  {_size_str(e):>10}  "
              f"{decision:<9}  {title}{year}")


def _print_matrix(rows: list) -> None:
    print("\nscore matrix (component → 0-100; blank = n/a, dropped from the average)")
    keys = ["genre_affinity", "source", "trakt_rating", "popularity", "recency", "people_affinity"]
    hdr = f"{'score':>5}  " + "  ".join(f"{k[:12]:>12}" for k in keys) + "   title"
    print(hdr)
    print("-" * len(hdr))
    for e, _ in rows:
        m = e.get("matrix") or {}
        cells = "  ".join(f"{(m.get(k) if m.get(k) is not None else ''):>12}" for k in keys)
        print(f"{e.get('score', 0):>5}  {cells}   {e.get('title')}")


# ── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="Offline preview of the acquisition decision table.")
    ap.add_argument("--capture", action="store_true",
                    help="perform live read-only GETs and refresh the snapshot")
    ap.add_argument("--sources", nargs="*",
                    default=["trakt_watchlist", "trakt_recommendations"],
                    help="which cached sources to gather from")
    ap.add_argument("--min-score", type=int, default=None, help="override acquisition.min_score")
    ap.add_argument("--max-adds", type=int, default=None, help="override acquisition.max_adds_per_run")
    ap.add_argument("--sonarr-instance", help="force the Sonarr instance name")
    ap.add_argument("--radarr-instance", help="force the Radarr instance name")
    ap.add_argument("--find", help="only show titles containing this substring")
    ap.add_argument("--verbose", action="store_true", help="show the per-component score matrix")
    args = ap.parse_args()

    logger = _Logger(verbose=args.verbose)
    config = _load_config(logger)
    if not config:
        sys.exit(f"could not read {_CONFIG}")
    acq = config.get("acquisition", {}) or {}
    min_score = args.min_score if args.min_score is not None else int(acq.get("min_score", 0))
    max_adds = args.max_adds if args.max_adds is not None else int(acq.get("max_adds_per_run", 10))

    snapshot = _load_json(_SNAP) or {}
    if not snapshot and not args.capture:
        sys.exit(f"No snapshot at {_SNAP}\nRun once with --capture to build it.")

    live = LiveReader(config, logger) if args.capture else None
    gateways = {
        "sonarr": SnapshotGateway("sonarr", config, logger, snapshot, live, args.sonarr_instance),
        "radarr": SnapshotGateway("radarr", config, logger, snapshot, live, args.radarr_instance),
    }

    print("=" * 78)
    print(f"ACQUISITION PREVIEW  ({'CAPTURE — live GETs' if args.capture else 'offline replay'})")
    print(f"  min_score={min_score}  max_adds_per_run={max_adds}  "
          f"config acquisition.enabled={acq.get('enabled')}")
    print(f"  sonarr={gateways['sonarr'].default_instance()}  "
          f"radarr={gateways['radarr'].default_instance()}  snapshot_keys={len(snapshot)}")
    print("=" * 78)

    candidates, notes = load_candidates(config, set(args.sources))
    for n in notes:
        print(f"  · {n}")
    if not candidates:
        sys.exit("\nNo candidates loaded — nothing to preview.")

    resolver = Resolver(gateways, config, logger)
    scorer = AcquisitionScorer(_Cache(), logger)

    enriched, skips = [], {}
    for cand in candidates:
        e = resolver.prepare(cand)
        if e.get("skip_reason"):
            skips[e["skip_reason"]] = skips.get(e["skip_reason"], 0) + 1
            continue
        scored = scorer.score(e)
        e["score"] = scored["total"]
        e["matrix"] = scored["matrix"]
        enriched.append(resolver.resolve_quality(e, scored["total"]))

    eligible = sorted((e for e in enriched if e.get("score", 0) >= min_score),
                      key=lambda x: x.get("score", 0), reverse=True)
    below = len(enriched) - len(eligible)
    selected = eligible[:max_adds] if max_adds > 0 else eligible

    rows = [(e, "would-add") for e in selected]
    rows += [(e, "over-cap") for e in eligible[len(selected):]]
    if args.find:
        needle = args.find.lower()
        rows = [r for r in rows if needle in str(r[0].get("title", "")).lower()]

    _print_table(rows)
    if args.verbose and rows:
        _print_matrix(rows)

    print(f"\nresolved={len(enriched)}  eligible={len(eligible)}  "
          f"would-add={len(selected)}  over-cap={len(eligible) - len(selected)}  "
          f"below-min-score={below}")
    if skips:
        print("skipped:")
        for reason, n in sorted(skips.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {reason}")

    misses = sorted({k for gw in gateways.values() for k in gw.misses})
    if misses:
        print(f"\n{len(misses)} snapshot miss(es) — served as empty. Re-run with --capture:")
        for k in misses[:15]:
            print(f"  · {k}")
        if len(misses) > 15:
            print(f"  … and {len(misses) - 15} more")

    if args.capture:
        if not snapshot:
            print("\ncapture produced 0 keys — refusing to write an empty snapshot "
                  "(existing snapshot, if any, left intact).")
        else:
            _SNAP_DIR.mkdir(parents=True, exist_ok=True)
            with open(_SNAP, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            print(f"\nsnapshot written: {_SNAP}  ({len(snapshot)} keys)")


if __name__ == "__main__":
    main()

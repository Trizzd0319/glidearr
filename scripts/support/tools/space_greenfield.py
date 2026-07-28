"""
space_greenfield.py — greenfield REBUILD simulator: "if the library were empty,
what would the system re-acquire, in what order, at what quality?"
================================================================================
Standalone CLI (never imported by main.py). **READ-ONLY**: it never talks to
Radarr/Sonarr, never deletes anything, and the ONLY path it writes is
``<cache>/ml/reports/greenfield_{date}.json``.

The question
------------
Wipe every byte. Keep everything the system *knows* (watch history, affinity,
the scores it already computed). Now hand it the empty disk and let it re-fill
from its own ranking until the space budget runs out. What comes back?

The answer is a direct validation of the scoring:

  * high overlap  ⇒ the scores agree with the household's own curation
  * the DROPPED list ⇒ the dead weight the scores would not pay for again
  * PROMOTED/DEMOTED ⇒ where today's quality tier disagrees with today's policy

What it does
------------
1. Loads the CURRENT state read-only: ``radarr/*/movie_files.parquet`` (every
   instance) + ``sonarr/*/episode_files.parquet``, the cached quality profiles
   (``radarr.quality.<inst>.json`` / ``sonarr/<inst>/profiles.json``), the
   Sonarr series library shards (runtime + episode counts + real sizeOnDisk),
   and the size-model calibration (``size_model/calibration.json``).
2. Simulates the empty library: every owned row becomes "not present". Scores
   are KEPT (see ASSUMPTIONS below).
3. Ranks every known title by the SHIPPED value ordering — the stored
   ``watchability_score`` column, descending. TV ranks at SERIES level (every
   episode row of a series carries the same broadcast series score, exactly as
   ``_build_show_score_map`` writes it; the tool asserts that and takes max).
4. Assigns a quality tier with the SHIPPED mapping:
       label     = scoring/_shared.score_to_profile(score)      → e.g. "Bluray 1080p"
       cap       = likelihood.resolution_cap_for_likelihood(watch_likelihood(row))
       tier      = min(resolution(label), cap)
   then picks the profile the live path would pick via
   ``_shared.select_profile_id(score, ranked_profiles, target_resolution=tier)``.
5. Estimates size through ``machine_learning/sizing/size_model`` (MiB/min ×
   runtime × items). Rate preference, highest first:
       a. the library's OWN measured MiB/min at that resolution tier
          (size_model resolution-order step 1 — "measured always wins"),
       b. the calibrated table for the chosen profile's top quality name,
       c. the size model's per-resolution default.
6. Fills to the budget: budget = capacity − reserve. Keep-policy pins are
   acquired FIRST (explicit user intent), then everything else by score desc.
   The walk stops at the first title that does not fit (a live acquisition run
   is sequential by rank, so it stalls rather than cherry-picking).
7. Prints the diff (KEPT / PROMOTED / DEMOTED / DROPPED / ADDED) and writes the
   whole thing to ``<cache>/ml/reports/greenfield_{date}.json``.

ASSUMPTIONS + HONESTY (also emitted into the report JSON, not just this docstring)
---------------------------------------------------------------------------------
* Sizes are MODEL ESTIMATES, not real release sizes. A real grab lands wherever
  the indexer's best release lands.
* This is a RE-ACQUISITION simulation, not an ideal-library search: it can only
  consider titles that already have an *arr record. It cannot invent titles the
  household never added.
* Scores derive substantially from Tautulli watch history + affinity, which
  survive a disk wipe — so this is "what would you rebuild given what you know
  now", NOT a true cold start.
* One exception to "scores survive": the scorer's Group-D terms (v2's
  D4_transcode_risk, or the legacy D1/D2/D3 trio) are computed FROM THE OWNED
  FILE's resolution, codec, bitrate, audio and subtitle tracks — none of which an
  unowned title has.
  We do NOT recompute the score (the instruction is to reuse the shipped
  column); instead we read those three terms out of ``watchability_breakdown``
  and report a SENSITIVITY: re-run the whole fill with them zeroed and count how
  many acquire/drop and tier decisions flip. Current-file resolution/codec are
  used ONLY to describe the title's TODAY tier — never to size the rebuild.
* Any title with a missing score is EXCLUDED and counted.

Usage
-----
    python scripts/support/tools/space_greenfield.py
    python scripts/support/tools/space_greenfield.py --service radarr --top 15
    python scripts/support/tools/space_greenfield.py --reserve-gb 4000 --capacity-gb 24000
    python scripts/support/tools/space_greenfield.py --cache-base /tmp/cache-copy
"""
from __future__ import annotations

import argparse
import glob as _glob
import gzip
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from scripts.managers.factories.cache.key_builder import CacheKeyBuilder            # noqa: E402
from scripts.managers.machine_learning.likelihood.watch_likelihood import (         # noqa: E402
    resolution_cap_for_likelihood,
    watch_likelihood,
)
from scripts.managers.machine_learning.scoring._shared import (                     # noqa: E402
    score_to_profile,
    select_profile_id,
)
from scripts.managers.machine_learning.sizing import size_model                     # noqa: E402

GIB = 1024 ** 3

# Keep-policy values that mean "the household said so" — acquired first, always.
# keep_forever / keep_movie / keep_universe are the movie-side pins; keep_series
# is the Sonarr analogue of keep_movie. The auto-derived "universe" policy is
# deliberately NOT here: it is inferred, not declared.
PINNED_POLICIES = frozenset({"keep_forever", "keep_movie", "keep_universe", "keep_series"})

# Scorer groups computed FROM the owned file — meaningless for an unowned title.
# D4_transcode_risk is the Group-D v2 term (a NEGATIVE transcode-risk penalty; the
# v1 D1/D2/D3 bonuses report 0.0 whenever it is active). It is the most file-derived
# term of the four — it reads the file's codec, bitrate, audio track, subtitle tracks
# and container — so leaving it out would understate the sensitivity, and would do so
# in the direction that flatters the simulation.
FILE_DERIVED_GROUPS = ("D1_device_capability", "D2_transcode_avoidance",
                       "D3_platform_ceiling", "D4_transcode_risk")

# Fallback quality name per resolution tier when no profile / no measurement.
_TIER_FALLBACK_QUALITY = {2160: "WEBDL-2160p", 1080: "WEBDL-1080p", 720: "Bluray-720p", 480: "DVD"}

# Minimum owned files at a resolution before its measured MiB/min is trusted.
# Mirrors SizeCalibrator.MIN_SAMPLES.
MIN_TIER_SAMPLES = 20

DEFAULT_TOP = 25

HONESTY: list[str] = [
    "Sizes are MODEL ESTIMATES (MiB/min x runtime x items), not real release sizes.",
    "RE-ACQUISITION simulation: only titles that already have an *arr record can be "
    "considered — it cannot invent titles you never added.",
    "Scores derive partly from watch history + affinity, which SURVIVE a wipe. This is "
    "'what would you rebuild knowing what you know now', not a true cold start.",
    "The scorer's Group-D terms (v2 D4_transcode_risk, or the legacy D1/D2/D3 trio) are "
    "computed FROM the owned file (resolution/codec/bitrate/audio/subtitles/container) and "
    "are not meaningful for an unowned title. The shipped score column is reused as-is; the "
    "report carries a sensitivity run with those groups zeroed.",
    "Titles with a missing watchability score are EXCLUDED and counted.",
    "READ-ONLY: no *arr call, no deletion, nothing written outside <cache>/ml/reports/.",
]


# ─────────────────────────────────────────────────────────────────────────────
# Title record
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Title:
    key: str
    service: str
    instance: str
    name: str
    score: float
    likelihood: float
    pinned: bool
    keep_policy: "str | None"
    owned: bool
    owned_gib: float
    owned_res: "int | None"
    runtime_min: float
    n_items: int
    tier_label: str
    tier_res: int
    profile_id: "int | None"
    profile_name: "str | None"
    quality_name: str
    rate_source: str
    est_gib: float
    file_signal_pts: float = 0.0
    extra: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {
            "key": self.key, "service": self.service, "instance": self.instance,
            "title": self.name, "score": self.score, "likelihood": round(self.likelihood, 2),
            "pinned": self.pinned, "keep_policy": self.keep_policy,
            "owned": self.owned, "owned_gib": round(self.owned_gib, 3),
            "owned_res": self.owned_res, "runtime_min": round(self.runtime_min, 1),
            "items": self.n_items, "tier_label": self.tier_label, "tier_res": self.tier_res,
            "profile_id": self.profile_id, "profile_name": self.profile_name,
            "quality_name": self.quality_name, "rate_source": self.rate_source,
            "est_gib": round(self.est_gib, 3), "file_signal_pts": round(self.file_signal_pts, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Pure logic (the testable core)
# ─────────────────────────────────────────────────────────────────────────────
_RES_IN_LABEL = re.compile(r"(\d{3,4})")


def label_resolution(label: str) -> int:
    """Pixel height implied by a ``score_to_profile`` label ('Remux 2160p' -> 2160).
    'SD' (and anything without a number) -> 480."""
    m = _RES_IN_LABEL.search(label or "")
    return int(m.group(1)) if m else 480


def assign_tier(score, row, config=None) -> "tuple[str, int, float]":
    """Shipped tier policy for a title: ``(profile_label, tier_resolution, likelihood)``.

    label = ``score_to_profile(score)`` (the scoring ladder), cap =
    ``resolution_cap_for_likelihood(watch_likelihood(row))`` (the watch_likelihood
    ladder — this is where ``uhd_cutoff`` binds). The tier is the intersection:
    the score ladder proposes, the likelihood cap disposes. Both halves are the
    real shipped functions, imported, not re-implemented.
    """
    try:
        s = int(round(float(score)))
    except (TypeError, ValueError):
        s = 0
    label = score_to_profile(s)
    likelihood = float(watch_likelihood(row, config=config))
    cap = int(resolution_cap_for_likelihood(likelihood, config=config))
    return label, min(label_resolution(label), cap), likelihood


def sort_key(t: Title):
    """Acquisition order: pins first, then score desc, then cheapest, then stable."""
    return (0 if t.pinned else 1, -t.score, t.est_gib, t.key)


@dataclass
class FillResult:
    acquired: list = field(default_factory=list)     # list[Title]
    skipped: list = field(default_factory=list)      # list[Title]
    used_gib: float = 0.0
    budget_gib: float = 0.0
    stopped_at_rank: "int | None" = None
    stopped_on: "str | None" = None


def fill_budget(titles: "list[Title]", budget_gib: float) -> FillResult:
    """Walk the ranked list accumulating estimated GiB; stop at the first title
    that does not fit. Pins are already first via :func:`sort_key`, so they are
    acquired ahead of higher-scored unpinned titles."""
    ordered = sorted(titles, key=sort_key)
    res = FillResult(budget_gib=float(budget_gib))
    stopped = False
    for rank, t in enumerate(ordered):
        if stopped:
            res.skipped.append(t)
            continue
        if res.used_gib + t.est_gib > budget_gib:
            stopped = True
            res.stopped_at_rank = rank
            res.stopped_on = t.key
            res.skipped.append(t)
            continue
        res.used_gib += t.est_gib
        res.acquired.append(t)
    return res


def diff_buckets(titles: "list[Title]", acquired_keys: "set[str]") -> dict:
    """Partition into the five disjoint buckets the report cares about.

    KEPT      owned, re-acquired at the SAME tier
    PROMOTED  owned, re-acquired at a HIGHER tier than owned today
    DEMOTED   owned, re-acquired at a LOWER tier than owned today
    DROPPED   owned, NOT re-acquired  (the dead-weight list)
    ADDED     not owned today, but the rebuild would acquire it
    """
    out: dict = {"kept": [], "promoted": [], "demoted": [], "dropped": [], "added": [],
                 "unowned_skipped": []}
    for t in titles:
        got = t.key in acquired_keys
        if not t.owned:
            out["added" if got else "unowned_skipped"].append(t)
            continue
        if not got:
            out["dropped"].append(t)
            continue
        cur = t.owned_res
        if cur is None or int(cur) == int(t.tier_res):
            out["kept"].append(t)
        elif int(t.tier_res) > int(cur):
            out["promoted"].append(t)
        else:
            out["demoted"].append(t)
    return out


def bucket_stats(items: "list[Title]") -> dict:
    """Both sides of every bucket: what it costs TODAY and what the rebuild
    ESTIMATES it would cost. Never conflate the two — one is measured, one is modelled."""
    return {"count": len(items),
            "today_gib": round(sum(t.owned_gib for t in items), 1),
            "rebuild_gib": round(sum(t.est_gib for t in items), 1)}


# ─────────────────────────────────────────────────────────────────────────────
# Cache readers (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_config(explicit: "str | None" = None) -> dict:
    path = Path(explicit) if explicit else (Path(__file__).resolve().parents[1] / "config" / "config.json")
    return _read_json(path) or {}


def install_calibration(base: Path) -> int:
    """Warm size_model's runtime overlay from ``<cache>/size_model/calibration.json``
    exactly the way SizeCalibrator.load_into_model() does. Returns tier count.

    Scoped to *base*: when that base has no calibration the overlay is CLEARED,
    so a ``--cache-base`` copy can never inherit another base's measurements."""
    payload = _read_json(base / "size_model" / "calibration.json")
    table = payload.get("table") if isinstance(payload, dict) else None
    if table:
        return size_model.set_calibration(table)
    size_model.clear_calibration()
    return 0


def discover_instances(base: Path, service: str, filename: str,
                       wanted: "list[str] | None") -> "list[str]":
    root = base / service
    found = sorted(p.parent.name for p in root.glob(f"*/{filename}")) if root.is_dir() else []
    if wanted:
        want = {w.strip() for w in wanted if w and w.strip()}
        found = [i for i in found if i in want]
    return found


def load_ranked_profiles(base: Path, service: str, instance: str) -> list:
    """Cached quality profiles for an instance, sorted ascending by max allowed
    resolution (the shape ``select_profile_id`` expects). [] when not cached."""
    candidates = [base / f"{service}.quality.{instance}.json",
                  base / service / instance / "profiles.json",
                  base / service / instance / "quality" / "profiles.json"]
    for path in candidates:
        data = _read_json(path)
        if isinstance(data, dict):
            data = data.get("data") or data.get("profiles")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return sorted([p for p in data if isinstance(p, dict)],
                          key=lambda p: size_model.profile_max_quality(p)[0])
    return []


def measured_tier_rates(df: pd.DataFrame, runtime_col: str, unit: str) -> dict:
    """{resolution: {"mib_per_min": r, "n": n}} measured from the library's OWN
    files — size_model's preferred rate source (resolution-order step 1)."""
    if df is None or df.empty or runtime_col not in df.columns:
        return {}
    need = {"size_bytes", "resolution", runtime_col}
    if not need.issubset(df.columns):
        return {}
    d = df[["size_bytes", "resolution", runtime_col]].copy()
    d["size_bytes"] = pd.to_numeric(d["size_bytes"], errors="coerce")
    d["resolution"] = pd.to_numeric(d["resolution"], errors="coerce")
    d[runtime_col] = pd.to_numeric(d[runtime_col], errors="coerce")
    d = d[(d["size_bytes"] > 0) & (d[runtime_col] > 0) & d["resolution"].notna()]
    if d.empty:
        return {}
    minutes = d[runtime_col] / 60.0 if unit == "seconds" else d[runtime_col]
    d["mib_min"] = d["size_bytes"] / (1024.0 * 1024.0) / minutes
    out: dict = {}
    for res, grp in d.groupby(d["resolution"].astype(int)):
        out[int(res)] = {"mib_per_min": round(float(grp["mib_min"].mean()), 2),
                         "n": int(len(grp))}
    return out


def rate_for_tier(tier_res: int, rates: dict, quality_name: str) -> "tuple[dict, str]":
    """Pick the MiB/min source for a tier. Measured (>= MIN_TIER_SAMPLES) wins;
    otherwise fall through to the calibrated table for ``quality_name``.
    Returns ``(measured_map_for_estimate_gb, source_label)``."""
    hit = rates.get(int(tier_res))
    if hit and hit["n"] >= MIN_TIER_SAMPLES and hit["mib_per_min"] > 0:
        return {quality_name: hit["mib_per_min"]}, f"measured@{tier_res}p(n={hit['n']})"
    return {}, "calibrated-table"


def _f(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _engagement(row: dict) -> dict:
    """The watch-history fields ``watch_likelihood`` reads. These SURVIVE a disk
    wipe (they come from Tautulli/Trakt history, not from the file), so the
    simulation keeps them — see ASSUMPTIONS."""
    keys = ("watch_count", "percent_complete", "is_watched", "universe_credit",
            "watchability_percentile")
    out = {}
    for k in keys:
        v = row.get(k)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        out[k] = bool(v) if k == "is_watched" else float(v)
    # ``last_watched_at`` is a STRING, so it can't go through the float() loop above —
    # but explain_likelihood needs it: since watch_count counts WATCHES rather than
    # plays, it is the only signal that keeps a sampled-but-never-finished title on the
    # ABANDONED branch instead of letting it fall through to UNTOUCHED (where it would
    # score higher for having been abandoned). Survives a disk wipe like the rest.
    lw = row.get("last_watched_at")
    if lw is not None and not (isinstance(lw, float) and math.isnan(lw)) and str(lw).strip():
        out["last_watched_at"] = str(lw)
    return out


def _file_signal_pts(breakdown) -> float:
    if not isinstance(breakdown, str) or not breakdown.strip():
        return 0.0
    try:
        d = json.loads(breakdown)
    except Exception:
        return 0.0
    return float(sum(_f(d.get(g)) for g in FILE_DERIVED_GROUPS))


def _tier_from(score, row, config, ranked, rates) -> tuple:
    """(label, tier_res, likelihood, profile_id, profile_name, quality_name, measured, src)"""
    label, tier_res, likelihood = assign_tier(score, row, config=config)
    pid = pname = None
    quality = _TIER_FALLBACK_QUALITY.get(tier_res, "WEBDL-1080p")
    if ranked:
        try:
            pid = select_profile_id(int(round(_f(score))), ranked, target_resolution=tier_res)
        except Exception:
            pid = None
        if pid is not None:
            prof = next((p for p in ranked if p.get("id") == pid), None)
            if prof:
                pname = prof.get("name")
                _, qname = size_model.profile_max_quality(prof)
                quality = qname or quality
    measured, src = rate_for_tier(tier_res, rates, quality)
    return label, tier_res, likelihood, pid, pname, quality, measured, src


# ─────────────────────────────────────────────────────────────────────────────
# Radarr (movies)
# ─────────────────────────────────────────────────────────────────────────────
def load_movie_titles(base: Path, instances: "list[str]", config: dict) -> tuple:
    """(titles, meta). One Title per (instance, movie). Movies with no score are
    excluded and counted."""
    titles: "list[Title]" = []
    meta = {"instances": [], "no_score": 0, "no_runtime": 0, "rows": 0,
            "owned_gib": 0.0, "rates": {}}
    frames = []
    for inst in instances:
        path = base / "radarr" / inst / "movie_files.parquet"
        try:
            df = pd.read_parquet(path)
        except Exception as exc:                                    # pragma: no cover
            meta.setdefault("errors", []).append(f"radarr/{inst}: {exc}")
            continue
        frames.append(df)
        meta["instances"].append({"instance": inst, "rows": int(len(df))})
    if not frames:
        return titles, meta
    rates = measured_tier_rates(pd.concat(frames, ignore_index=True), "runtime_minutes", "minutes")
    meta["rates"] = rates

    for inst, df in zip([m["instance"] for m in meta["instances"]], frames):
        ranked = load_ranked_profiles(base, "radarr", inst)
        for rec in df.to_dict("records"):
            meta["rows"] += 1
            score = rec.get("watchability_score")
            owned_gib = _f(rec.get("size_bytes")) / GIB
            meta["owned_gib"] += owned_gib
            if score is None or (isinstance(score, float) and math.isnan(score)):
                meta["no_score"] += 1
                continue
            runtime = _f(rec.get("runtime_minutes"))
            if runtime <= 0:
                # Unsizeable → EXCLUDED. Keeping it would let it into the fill at
                # zero estimated cost, which would silently inflate the rebuild.
                meta["no_runtime"] += 1
                continue
            (label, tier_res, likelihood, pid, pname,
             quality, measured, src) = _tier_from(score, rec, config, ranked, rates)
            est = size_model.estimate_gb(quality, runtime, 1, measured or None,
                                         resolution=tier_res)
            ident = rec.get("tmdb_id") or rec.get("movie_id") or rec.get("title")
            owned_res = rec.get("resolution")
            titles.append(Title(
                key=f"radarr:{inst}:{ident}", service="radarr", instance=inst,
                name=str(rec.get("title") or ident),
                score=_f(score), likelihood=likelihood,
                keep_policy=(str(rec["keep_policy"]) if rec.get("keep_policy") else None),
                pinned=str(rec.get("keep_policy") or "") in PINNED_POLICIES,
                owned=bool(rec.get("has_file", True)) and owned_gib > 0,
                owned_gib=owned_gib,
                owned_res=(int(owned_res) if _f(owned_res) > 0 else None),
                runtime_min=runtime, n_items=1,
                tier_label=label, tier_res=tier_res, profile_id=pid, profile_name=pname,
                quality_name=quality, rate_source=src, est_gib=est,
                file_signal_pts=_file_signal_pts(rec.get("watchability_breakdown")),
                extra={"year": rec.get("year"), "engagement": _engagement(rec)},
            ))
    meta["owned_gib"] = round(meta["owned_gib"], 1)
    return titles, meta


# ─────────────────────────────────────────────────────────────────────────────
# Sonarr (series)
# ─────────────────────────────────────────────────────────────────────────────
def load_series_library(base: Path, instance: str) -> dict:
    """{series_id: series_obj} from the sharded Sonarr library cache. {} if absent.
    Gives per-episode runtime, monitored/total episode counts and REAL sizeOnDisk."""
    out: dict = {}
    root = base / "sonarr" / instance / "library"
    if not root.is_dir():
        return out
    for shard in sorted(_glob.glob(str(root / "*.json.gz"))):
        try:
            data = json.loads(gzip.open(shard).read())
        except Exception:
            continue
        for s in (data if isinstance(data, list) else []):
            if isinstance(s, dict) and s.get("id") is not None:
                out[int(s["id"])] = s
    return out


def _episode_count(series_obj: dict, basis: str, owned_rows: int) -> int:
    st = (series_obj or {}).get("statistics") or {}
    if basis == "total":
        n = st.get("totalEpisodeCount")
    elif basis == "owned":
        n = st.get("episodeFileCount")
    else:                                    # "monitored" (default)
        n = st.get("episodeCount") or st.get("totalEpisodeCount")
    n = int(n or 0)
    return n if n > 0 else max(int(owned_rows), 1)


def load_series_titles(base: Path, instances: "list[str]", config: dict,
                       episode_basis: str = "monitored") -> tuple:
    """(titles, meta). One Title per SERIES — episode rows aggregate to the series
    the way ``_build_show_score_map`` broadcasts the score (every episode row of a
    series carries the same value; we take the max and record any disagreement)."""
    titles: "list[Title]" = []
    meta = {"instances": [], "no_score": 0, "no_runtime": 0, "series": 0,
            "score_disagreements": 0, "owned_gib": 0.0, "owned_gib_source": "parquet",
            "rates": {}, "episode_basis": episode_basis, "episode_count_source": {}}
    frames, libs = [], {}
    for inst in instances:
        path = base / "sonarr" / inst / "episode_files.parquet"
        try:
            df = pd.read_parquet(path)
        except Exception as exc:                                    # pragma: no cover
            meta.setdefault("errors", []).append(f"sonarr/{inst}: {exc}")
            continue
        frames.append((inst, df))
        libs[inst] = load_series_library(base, inst)
        meta["instances"].append({"instance": inst, "rows": int(len(df)),
                                  "library_series": len(libs[inst])})
    if not frames:
        return titles, meta
    rates = measured_tier_rates(pd.concat([d for _, d in frames], ignore_index=True),
                                "runtime_seconds", "seconds")
    meta["rates"] = rates

    for inst, df in frames:
        ranked = load_ranked_profiles(base, "sonarr", inst)
        lib = libs.get(inst, {})
        # Real on-disk bytes, when the library cache has them (the parquet holds
        # only the pilot-stub + active working set, so it UNDER-counts).
        lib_bytes = sum(_f(((s.get("statistics") or {}).get("sizeOnDisk"))) for s in lib.values())
        if lib_bytes > 0:
            meta["owned_gib"] += lib_bytes / GIB
            meta["owned_gib_source"] = "sonarr library sizeOnDisk"
        else:
            meta["owned_gib"] += _f(pd.to_numeric(df.get("size_bytes"), errors="coerce").sum()) / GIB

        key_col = "series_id" if "series_id" in df.columns else "series_title"
        for sid, grp in df.groupby(key_col, dropna=False):
            meta["series"] += 1
            scores = pd.to_numeric(grp.get("watchability_score"), errors="coerce").dropna()
            if scores.empty:
                meta["no_score"] += 1
                continue
            if scores.nunique() > 1:
                meta["score_disagreements"] += 1
            score = float(scores.max())

            sizes = pd.to_numeric(grp.get("size_bytes"), errors="coerce").fillna(0)
            owned_rows = int((sizes > 0).sum())
            sobj = lib.get(int(sid)) if isinstance(sid, (int, float)) and not pd.isna(sid) else None
            sobj = sobj or {}
            st = sobj.get("statistics") or {}

            file_count = int(st.get("episodeFileCount") or 0)
            owned = bool(file_count > 0 or owned_rows > 0)
            owned_gib = (_f(st.get("sizeOnDisk")) / GIB) if st.get("sizeOnDisk") else float(sizes.sum() / GIB)

            runtime = _f(sobj.get("runtime"))
            rt_src = "sonarr series runtime"
            if runtime <= 0:
                secs = pd.to_numeric(grp.get("runtime_seconds"), errors="coerce").dropna()
                secs = secs[secs > 0]
                runtime = float(secs.median() / 60.0) if len(secs) else 0.0
                rt_src = "episode file median" if runtime > 0 else "missing"
            if runtime <= 0:
                # Unsizeable → EXCLUDED (see the movie loader for why).
                meta["no_runtime"] += 1
                continue
            n_eps = _episode_count(sobj, episode_basis, owned_rows)
            src = "library-statistics" if st else "parquet-rows"
            meta["episode_count_source"][src] = meta["episode_count_source"].get(src, 0) + 1

            # Representative row for the likelihood: the engagement fields are
            # broadcast per series, so the max-watch-count row carries them.
            rrow = grp.sort_values("watch_count", ascending=False).iloc[0].to_dict() \
                if "watch_count" in grp.columns else grp.iloc[0].to_dict()
            (label, tier_res, likelihood, pid, pname,
             quality, measured, msrc) = _tier_from(score, rrow, config, ranked, rates)
            est = size_model.estimate_gb(quality, runtime, n_eps, measured or None,
                                         resolution=tier_res)

            res_vals = pd.to_numeric(grp.get("resolution"), errors="coerce").dropna()
            res_vals = res_vals[res_vals > 0]
            owned_res = int(res_vals.max()) if len(res_vals) and owned else None

            kp = grp.get("keep_policy")
            kp = kp.dropna() if kp is not None else pd.Series(dtype=object)
            keep_policy = str(kp.iloc[0]) if len(kp) else None

            titles.append(Title(
                key=f"sonarr:{inst}:{sid}", service="sonarr", instance=inst,
                name=str(grp["series_title"].iloc[0] if "series_title" in grp.columns else sid),
                score=score, likelihood=likelihood, keep_policy=keep_policy,
                pinned=(keep_policy in PINNED_POLICIES),
                owned=owned, owned_gib=owned_gib, owned_res=owned_res,
                runtime_min=runtime, n_items=n_eps,
                tier_label=label, tier_res=tier_res, profile_id=pid, profile_name=pname,
                quality_name=quality, rate_source=msrc, est_gib=est,
                file_signal_pts=_file_signal_pts(
                    grp["watchability_breakdown"].dropna().iloc[0]
                    if "watchability_breakdown" in grp.columns
                    and grp["watchability_breakdown"].notna().any() else None),
                extra={"runtime_source": rt_src, "episode_files": file_count,
                       "engagement": _engagement(rrow)},
            ))
    meta["owned_gib"] = round(meta["owned_gib"], 1)
    return titles, meta


# ─────────────────────────────────────────────────────────────────────────────
# Capacity
# ─────────────────────────────────────────────────────────────────────────────
def _diskspace_entries(payload) -> list:
    if isinstance(payload, dict):
        payload = payload.get("diskspace") or payload.get("data") or []
    return [d for d in payload if isinstance(d, dict)] if isinstance(payload, list) else []


def resolve_capacity(base: Path, current_gib: float, *, capacity_gb: "float | None") -> dict:
    """Capacity resolution, most-trusted first:
       1. --capacity-gb
       2. cached totalSpace (mount-deduped) from any */storage/space_estimates.json
       3. DERIVED: current library usage + the largest cached freeSpace
    Returns {"gib": float|None, "source": str, "detail": ...}."""
    if capacity_gb:
        return {"gib": float(capacity_gb), "source": "flag --capacity-gb"}

    totals: dict = {}
    frees: dict = {}
    for path in sorted(base.glob("*/*/storage/space_estimates.json")) + \
            sorted(base.glob("*/*/storage_space.json")):
        for d in _diskspace_entries(_read_json(path)):
            p = str(d.get("path") or "")
            if d.get("totalSpace"):
                totals[p] = _f(d.get("totalSpace"))
            if d.get("freeSpace"):
                frees[p] = _f(d.get("freeSpace"))
    if totals:
        # Dedupe by identical byte total → same mount reported under many roots.
        deduped = sum(set(totals.values()))
        return {"gib": deduped / GIB, "source": "cached diskspace totalSpace",
                "detail": {"roots": len(totals), "distinct_mounts": len(set(totals.values()))}}
    if frees:
        free_gib = max(frees.values()) / GIB
        return {"gib": current_gib + free_gib, "source": "DERIVED (current usage + cached free space)",
                "detail": {"free_gib": round(free_gib, 1), "roots": len(frees)}}
    return {"gib": None, "source": "unresolved — pass --capacity-gb"}


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def cross_instance_duplicates(titles: "list[Title]") -> dict:
    """Same title id, two instances, BYTE-IDENTICAL owned size — i.e. one physical
    file counted twice. A genuine dual-version (4K + HD) has two different sizes,
    so this isolates the stale/mirrored rows that inflate the current-library
    total. Reported, never silently corrected: the parquets are the source of
    truth and this tool does not rewrite them."""
    by_ident: dict = {}
    for t in titles:
        if not t.owned or t.owned_gib <= 0:
            continue
        ident = t.key.split(":", 2)[-1]
        by_ident.setdefault((t.service, ident), []).append(t)
    dup_titles, dup_gib = 0, 0.0
    examples: list = []
    for (_svc, ident), group in by_ident.items():
        if len(group) < 2:
            continue
        seen: dict = {}
        for t in group:
            k = round(t.owned_gib, 4)
            if k in seen:
                dup_titles += 1
                dup_gib += t.owned_gib
                if len(examples) < 10:
                    examples.append({"title": t.name, "gib": round(t.owned_gib, 2),
                                     "instances": [seen[k].instance, t.instance]})
            else:
                seen[k] = t
    return {"redundant_copies": dup_titles, "double_counted_gib": round(dup_gib, 1),
            "examples": examples}


def tier_histogram(titles: "list[Title]", attr: str) -> dict:
    out: dict = {}
    for t in titles:
        v = getattr(t, attr)
        k = str(int(v)) if v is not None else "unknown"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == "unknown", -int(kv[0] or 0) if kv[0] != "unknown" else 0)))


def sensitivity_without_file_signals(titles: "list[Title]", budget_gib: float,
                                     config: dict, rates_by_service: dict,
                                     baseline_acquired: "set[str]") -> dict:
    """Re-run the WHOLE fill with the file-derived score groups (Group D) removed
    and count how many decisions flip. This is the honest answer to "those signals
    are computed from the file, so they are not meaningful for an unowned title".

    Only titles whose tier actually moves are re-sized (using the same measured
    tier rate the main run used), so the comparison stays apples-to-apples.
    """
    shadow: "list[Title]" = []
    tier_changes = 0
    for t in titles:
        s2 = max(0.0, t.score - t.file_signal_pts)
        row = dict(t.extra.get("engagement") or {})
        row["watchability_score"] = s2
        label, res, _lk = assign_tier(s2, row, config=config)
        est = t.est_gib
        if res != t.tier_res:
            tier_changes += 1
            quality = _TIER_FALLBACK_QUALITY.get(res, "WEBDL-1080p")
            measured, _src = rate_for_tier(res, rates_by_service.get(t.service, {}), quality)
            est = size_model.estimate_gb(quality, t.runtime_min, t.n_items,
                                         measured or None, resolution=res)
        shadow.append(Title(**{**t.__dict__, "score": s2, "tier_label": label,
                               "tier_res": res, "est_gib": est}))
    fill = fill_budget(shadow, budget_gib)
    got = {t.key for t in fill.acquired}
    return {
        "mean_file_pts": round(sum(t.file_signal_pts for t in titles) / max(1, len(titles)), 2),
        "titles_with_file_pts": sum(1 for t in titles if abs(t.file_signal_pts) > 0),
        "acquired_titles": len(fill.acquired),
        "acquire_decisions_flipped": len(got.symmetric_difference(baseline_acquired)),
        "tier_changes": tier_changes,
    }


def build_report(*, base: Path, args, config: dict, titles: "list[Title]",
                 movie_meta: dict, series_meta: dict, capacity: dict,
                 reserve_gib: float, fill: FillResult) -> dict:
    acquired_keys = {t.key for t in fill.acquired}
    buckets = diff_buckets(titles, acquired_keys)
    owned = [t for t in titles if t.owned]
    reacq = buckets["kept"] + buckets["promoted"] + buckets["demoted"]
    owned_gib = sum(t.owned_gib for t in owned)
    reacq_gib = sum(t.owned_gib for t in reacq)

    current_actual_gib = round(_f(movie_meta.get("owned_gib")) + _f(series_meta.get("owned_gib")), 1)

    sens = sensitivity_without_file_signals(
        titles, fill.budget_gib, config,
        {"radarr": movie_meta.get("rates", {}), "sonarr": series_meta.get("rates", {})},
        acquired_keys)

    report = {
        "tool": "space_greenfield",
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "writes_only": "<cache>/ml/reports/greenfield_{date}.json",
        "honesty": HONESTY,
        "params": {
            "cache_base": str(base), "service": args.service,
            "instances": list(args.instance or []), "top": args.top,
            "reserve_gb": args.reserve_gb, "capacity_gb": args.capacity_gb,
            "tv_episode_basis": args.tv_episode_basis,
            "pinned_policies": sorted(PINNED_POLICIES),
        },
        "capacity": {
            "capacity_gib": round(_f(capacity.get("gib")), 1),
            "capacity_source": capacity.get("source"),
            "capacity_detail": capacity.get("detail"),
            "reserve_gib": round(reserve_gib, 1),
            "reserve_source": ("flag --reserve-gb" if args.reserve_gb is not None
                               else "config free_space_limit"),
            "budget_gib": round(fill.budget_gib, 1),
        },
        "current_library": {
            "titles": len(owned),
            "actual_gib": current_actual_gib,
            "movies": {"titles": sum(1 for t in owned if t.service == "radarr"),
                       "actual_gib": _f(movie_meta.get("owned_gib"))},
            "series": {"titles": sum(1 for t in owned if t.service == "sonarr"),
                       "actual_gib": _f(series_meta.get("owned_gib")),
                       "gib_source": series_meta.get("owned_gib_source")},
            "tiers": tier_histogram(owned, "owned_res"),
            "cross_instance_duplicates": cross_instance_duplicates(titles),
        },
        "rebuild": {
            "titles": len(fill.acquired),
            "estimated_gib": round(fill.used_gib, 1),
            "budget_gib": round(fill.budget_gib, 1),
            "headroom_gib": round(fill.budget_gib - fill.used_gib, 1),
            "stopped_at_rank": fill.stopped_at_rank,
            "stopped_on": fill.stopped_on,
            "movies": sum(1 for t in fill.acquired if t.service == "radarr"),
            "series": sum(1 for t in fill.acquired if t.service == "sonarr"),
            "pinned_acquired": sum(1 for t in fill.acquired if t.pinned),
            "tiers": tier_histogram(fill.acquired, "tier_res"),
        },
        "overlap": {
            "owned_titles": len(owned),
            "reacquired_titles": len(reacq),
            "pct_titles": round(100.0 * len(reacq) / max(1, len(owned)), 1),
            "owned_gib": round(owned_gib, 1),
            "reacquired_gib": round(reacq_gib, 1),
            "pct_bytes": round(100.0 * reacq_gib / max(1e-9, owned_gib), 1),
            "statement": None,   # filled below
        },
        "buckets": {k: bucket_stats(v) for k, v in buckets.items()},
        "excluded": {
            "movies_no_score": movie_meta.get("no_score", 0),
            "series_no_score": series_meta.get("no_score", 0),
            "movies_no_runtime": movie_meta.get("no_runtime", 0),
            "series_no_runtime": series_meta.get("no_runtime", 0),
            "series_score_disagreements": series_meta.get("score_disagreements", 0),
            "note": "no_score and no_runtime titles are EXCLUDED from the simulation "
                    "entirely — an unsizeable title must not be acquired for free.",
        },
        "size_model": {
            "measured_movie_tier_rates_mib_per_min": movie_meta.get("rates", {}),
            "measured_series_tier_rates_mib_per_min": series_meta.get("rates", {}),
            "min_tier_samples": MIN_TIER_SAMPLES,
            "episode_count_source": series_meta.get("episode_count_source", {}),
            "tv_episode_basis": series_meta.get("episode_basis"),
        },
        "file_signal_sensitivity": sens,
        "sources": {"radarr": movie_meta.get("instances", []),
                    "sonarr": series_meta.get("instances", [])},
        "top_lists": {},
        "titles": [t.row() for t in sorted(titles, key=sort_key)],
    }
    ov = report["overlap"]
    ov["statement"] = (
        f"the rebuild would re-acquire {ov['pct_titles']}% of what you own "
        f"({ov['reacquired_titles']} of {ov['owned_titles']} titles, "
        f"{ov['pct_bytes']}% of the bytes)")

    n = max(1, int(args.top))
    order = {
        "dropped": sorted(buckets["dropped"], key=lambda t: (t.score, -t.owned_gib)),
        "kept": sorted(buckets["kept"], key=lambda t: -t.score),
        "promoted": sorted(buckets["promoted"], key=lambda t: (-t.tier_res, -t.score)),
        "demoted": sorted(buckets["demoted"], key=lambda t: (t.tier_res, -t.owned_gib)),
        "added": sorted(buckets["added"], key=lambda t: -t.score),
    }
    for k, items in order.items():
        report["top_lists"][k] = [t.row() for t in items[:n]]
    return report


def _fmt(v, w=10) -> str:
    return f"{v:>{w},.1f}"


def print_report(report: dict, top: int) -> None:
    cap, cur, reb, ov, bk = (report["capacity"], report["current_library"],
                             report["rebuild"], report["overlap"], report["buckets"])
    W = 86
    print("=" * W)
    print("GREENFIELD REBUILD — if the library were empty, what would it re-acquire?")
    print("=" * W)
    print("READ-ONLY simulation. No *arr call, no deletion. Only write: ml/reports/.")
    print()
    print("SPACE")
    print(f"   capacity        {_fmt(cap['capacity_gib'])} GiB   ({cap['capacity_source']})")
    print(f"   reserve         {_fmt(cap['reserve_gib'])} GiB   ({cap['reserve_source']})")
    print(f"   budget          {_fmt(cap['budget_gib'])} GiB")
    print(f"   library today   {_fmt(cur['actual_gib'])} GiB   "
          f"({cur['titles']:,} titles · movies {cur['movies']['actual_gib']:,.0f} · "
          f"tv {cur['series']['actual_gib']:,.0f})")
    print(f"   rebuild (est.)  {_fmt(reb['estimated_gib'])} GiB   "
          f"({reb['titles']:,} titles fit · {reb['movies']:,} movies + {reb['series']:,} series · "
          f"headroom {reb['headroom_gib']:,.0f})")
    if reb.get("stopped_at_rank") is not None:
        print(f"   fill stopped at rank {reb['stopped_at_rank']:,} on {reb['stopped_on']} "
              f"(budget exhausted)")
    print()
    print("OVERLAP — the validation")
    print(f"   {ov['statement']}")
    print()
    print("THE DIFF        (today GiB = real bytes on disk · rebuild GiB = model ESTIMATE)")
    print(f"   {'bucket':<12}{'titles':>8}{'today GiB':>12}{'rebuild GiB':>13}   meaning")
    rows = [
        ("KEPT", bk["kept"], "owned + re-acquired at the SAME tier"),
        ("PROMOTED", bk["promoted"], "owned, rebuild picks a HIGHER tier"),
        ("DEMOTED", bk["demoted"], "owned, rebuild picks a LOWER tier"),
        ("DROPPED", bk["dropped"], "owned, NOT re-acquired  <- dead weight"),
        ("ADDED", bk["added"], "no file today, rebuild WOULD acquire"),
        ("(no budget)", bk["unowned_skipped"], "no file today, does not fit either"),
    ]
    for label, st, meaning in rows:
        print(f"   {label:<12}{st['count']:>8,}{st['today_gib']:>12,.1f}"
              f"{st['rebuild_gib']:>13,.1f}   {meaning}")
    print()
    print("TIERS   (titles per resolution)")
    keys = sorted({*cur["tiers"], *reb["tiers"]},
                  key=lambda k: -(int(k) if k.isdigit() else -1))
    print(f"   {'tier':<10}{'today':>10}{'rebuild':>10}")
    for k in keys:
        print(f"   {k + 'p' if k.isdigit() else k:<10}"
              f"{cur['tiers'].get(k, 0):>10,}{reb['tiers'].get(k, 0):>10,}")
    print()
    for name, title in (("dropped", "DROPPED — worst score first (the dead-weight list)"),
                        ("demoted", "DEMOTED — owned above what the rebuild would buy"),
                        ("promoted", "PROMOTED — owned below what the rebuild would buy"),
                        ("added", "ADDED — no file today, the rebuild would acquire"),
                        ("kept", "KEPT — highest-scored re-acquisitions")):
        items = report["top_lists"].get(name) or []
        if not items:
            continue
        print(f"{title}  (top {min(top, len(items))})")
        print(f"   {'score':>6} {'today':>7} {'->':^3} {'rebuild':>7} "
              f"{'today GiB':>10} {'est GiB':>9}  title")
        for r in items[:top]:
            cur_t = f"{r['owned_res']}p" if r.get("owned_res") else ("-" if not r["owned"] else "?")
            pin = " *" if r["pinned"] else ""
            print(f"   {r['score']:>6.0f} {cur_t:>7} {'->':^3} {str(r['tier_res']) + 'p':>7} "
                  f"{r['owned_gib']:>10,.2f} {r['est_gib']:>9,.2f}  "
                  f"{r['title'][:42]}{pin} [{r['service'][:3]}]")
        print()
    ex = report["excluded"]
    print("EXCLUSIONS / CAVEATS")
    print(f"   excluded for missing score: movies {ex['movies_no_score']:,} · "
          f"series {ex['series_no_score']:,}")
    print(f"   unsizeable (no runtime), excluded: movies {ex['movies_no_runtime']:,} · "
          f"series {ex['series_no_runtime']:,}")
    dup = cur.get("cross_instance_duplicates") or {}
    if dup.get("redundant_copies"):
        names = ", ".join(e["title"] for e in (dup.get("examples") or [])[:4])
        print(f"   {dup['redundant_copies']:,} byte-identical copies across instances "
              f"({dup['double_counted_gib']:,.1f} GiB double-counted in 'library today') "
              f"— e.g. {names}")
    sens = report["file_signal_sensitivity"]
    print(f"   file-derived score groups (Group D): mean {sens['mean_file_pts']} pts over "
          f"{sens['titles_with_file_pts']:,} titles; zeroing them moves "
          f"{sens['tier_changes']:,} tier(s), flips {sens['acquire_decisions_flipped']:,} "
          f"acquire/drop decision(s) and fits {sens['acquired_titles']:,} titles "
          f"(vs {reb['titles']:,}).")
    for line in report["honesty"]:
        print(f"   • {line}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", choices=("radarr", "sonarr", "both"), default="both")
    ap.add_argument("--reserve-gb", type=float, default=None,
                    help="free-space floor to keep (default: config free_space_limit)")
    ap.add_argument("--capacity-gb", type=float, default=None,
                    help="mount total (default: cached diskspace, else derived)")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--instance", action="append", default=None,
                    help="repeatable; restrict to these instance names")
    ap.add_argument("--cache-base", default=None)
    ap.add_argument("--config", default=None, help="path to config.json (default: repo config)")
    ap.add_argument("--tv-episode-basis", choices=("monitored", "owned", "total"),
                    default="monitored",
                    help="episodes to size a series at: monitored (statistics.episodeCount — "
                         "what Sonarr is actually trying to hold), owned (episodeFileCount), "
                         "or total (every episode ever aired)")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.cache_base).resolve() if args.cache_base else CacheKeyBuilder().base_dir
    config = load_config(args.config)
    n_cal = install_calibration(base)

    titles: "list[Title]" = []
    movie_meta: dict = {}
    series_meta: dict = {}

    if args.service in ("radarr", "both"):
        insts = discover_instances(base, "radarr", "movie_files.parquet", args.instance)
        mt, movie_meta = load_movie_titles(base, insts, config)
        titles.extend(mt)
    if args.service in ("sonarr", "both"):
        insts = discover_instances(base, "sonarr", "episode_files.parquet", args.instance)
        st, series_meta = load_series_titles(base, insts, config,
                                             episode_basis=args.tv_episode_basis)
        titles.extend(st)

    if not titles:
        print(f"No *arr parquet caches found under {base} for --service {args.service}.")
        return 1

    current_gib = _f(movie_meta.get("owned_gib")) + _f(series_meta.get("owned_gib"))
    capacity = resolve_capacity(base, current_gib, capacity_gb=args.capacity_gb)
    if capacity.get("gib") is None:
        print(f"Could not resolve mount capacity from {base}. Pass --capacity-gb.")
        return 2
    if args.service != "both" and str(capacity["source"]).startswith("DERIVED"):
        # The derived total is "current usage + free"; with a service filter the
        # other service's usage is invisible, so this UNDER-states the mount.
        capacity["source"] += f" — WARNING: --service {args.service} hides the other " \
                              "service's usage, so this UNDER-states the real mount total"
        capacity["understated"] = True
    reserve = float(args.reserve_gb) if args.reserve_gb is not None \
        else _f(config.get("free_space_limit"))
    budget = max(0.0, float(capacity["gib"]) - reserve)

    fill = fill_budget(titles, budget)
    report = build_report(base=base, args=args, config=config, titles=titles,
                          movie_meta=movie_meta, series_meta=series_meta,
                          capacity=capacity, reserve_gib=reserve, fill=fill)
    report["size_model"]["calibrated_tiers_loaded"] = n_cal

    print_report(report, args.top)

    if not args.no_write:
        out_dir = base / "ml" / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"greenfield_{datetime.now(tz=timezone.utc):%Y-%m-%d}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nReport written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

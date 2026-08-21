# sizing

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **sizing**

**Package** — `scripts.managers.machine_learning.sizing`
**Run position** — Warm-loaded at the top of `Main.run()`; consulted by every "how big will this be?" question thereafter; refreshed after Phase 2.
**One-liner** — The single source of truth for file-size estimation: a MiB/min model calibrated against 6,914 real files, its pure calibration math, size-based upgrade/downgrade classification, and storage forecasting.

---

## Purpose

From [`__init__.py`](./__init__.py):

> MiB/min estimation + the calibration math + free-space forecasting. **Leaf
> layer, highest reuse — migrated first (MIGRATION.md Step 1).**

Every size question in the application funnels through here — acquisition
`~size`, JIT upgrade space reservation, active-watcher upgrade anticipation,
quality file-size comparisons, ML storage forecasting — *"so the numbers can never
disagree again."*

```
size_GiB = (mb_per_min × runtime_minutes × n_items) / 1024
```

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`size_model.py`](./size_model.py) | The model. Calibration table, overlay state, resolution order, clamps. **Intentionally dependency-free** | ✅ Implemented |
| [`size_calibration.py`](./size_calibration.py) | The **pure** calibration math — `fold_stats`, `compute_calibration_table`, `movie_runtime_min`, `calibration_is_fresh` | ✅ Implemented |
| [`file_comparison.py`](./file_comparison.py) | `classify_file_size` → `upgrade` / `downgrade` / `keep` | ✅ Implemented |
| [`storage_estimator.py`](./storage_estimator.py) | `MLStorageForecaster` — episode-count and size forecasting | 🟡 Implemented, but see [`DESIGN.md`](./DESIGN.md) §3.4 |
| [`anomaly.py`](./anomaly.py) | Size-anomaly detection + remediation policy — `recommend_action` (rescan vs re-grab routing), the re-grab **attempt ledger** (`should_attempt` / `record_attempt` / `prune_attempts`), `size_bytes` on every anomaly row | ✅ Implemented |
| [`quality_caps.py`](./quality_caps.py) | **Grab-time size ceiling** — turns this package's measured MiB/min into *arr `qualitydefinition.maxSize` (MB/min) at the same `over_ratio` `anomaly.py` uses, plus the shared diff-table renderer and write path both services call. Caps only ever TIGHTEN | ✅ Planner done · 🔵 unwired (`GLD-RAD-34`, `GLD-SON-21`) |

## Test coverage

[`test_anomaly.py`](./test_anomaly.py) · [`test_size_model_codec.py`](./test_size_model_codec.py) · [`test_quality_caps.py`](./test_quality_caps.py)

---

## Resolution order

| # | Source | When |
|---|---|---|
| 1 | **Measured** | The library's own `size_bytes / runtime` average for that quality name. *"Always preferred when at least one real sample exists."* |
| 2 | **Calibrated fallback** | `CALIBRATED_MB_PER_MIN` — cold start only |
| 3 | **Resolution default** | Coarse per-resolution number when the quality name is unrecognised |
| 4 | `DEFAULT_MB_PER_MIN` | 25.0 — last resort |

Whatever the source, the result is **clamped** to `[0.5, 900.0]`.

---

## Anomaly remediation routing & the attempt ledger

Two policy pieces live beside the detector (`GLD-SON-13`/`-14`, §0.1 #57):

**Routing** — `recommend_action(verdict, quality_name)` decides *rescan* vs
*re-grab* for an oversized file. `_MISGRADE_WHEN_BLOATED` (renamed from
`_JUNK_OR_SD_GRADES`, back-compat alias kept) now includes the BROADCAST tiers
`HDTV-720p`/`HDTV-1080p`: broadcast bitrate is capped far below disc bitrate,
so an HDTV-graded file at several times its expected rate is a mis-graded disc
source — a re-grab cannot fix it (Sonarr only grabs *upgrades*, and a smaller
file never is one), a mediainfo rescan can. `HDTV-2160p` is deliberately
excluded — UHD broadcast is genuinely high-bitrate with no higher HDTV tier to
be mistaken for. Live effect: 39 fruitless searches → 1.

**Attempt ledger** — pure helpers bounding the re-grab retry loop (the measured
failure: the same 38 episodes searched on consecutive runs forever, no
detector). `should_attempt` → `first` / `changed` / `retry` / `cooling` /
`abandoned` under `max_regrab_attempts` (3) and `regrab_retry_days` (7); a size
CHANGE resets the budget (the file moved — its history no longer describes it);
`prune_attempts` drops entries for files no longer anomalous. **P-C at the
core**: an absent/unparseable size is UNKNOWN — never "changed" (would reset
the budget every run) and never `0` (would look like a change every time);
`_as_int` returns `None` for both. The service I/O lives in
[`sonarr/cache/`](../../services/sonarr/cache/README.md).

---

## The clamp exists because of a real bug

> a bad upstream value (e.g. reading a Radarr quality-definition `maxSize`
> ceiling of ~2000 MiB/min) can never again produce a **187 GB estimate for a
> 96-minute movie**.

The bounds are empirical, not arbitrary:

| Bound | Value | Basis |
|---|---|---|
| `MAX_MB_PER_MIN` | 900.0 | Heaviest real file measured is a 4K remux at **~540** MiB/min (p90 ~477 across 18 remuxes). A full UHD BR-DISK reaches ~850. *"Above that is not a real file — it is a units/field bug."* |
| `MIN_MB_PER_MIN` | 0.5 | Floor |

---

## The calibration table

Seeded from a full measured run — **Radarr `standard` 1,776 movie files +
Sonarr `720` 5,138 episode files = 6,914** — via
[`support/tools/calibrate_sizes.py`](../../../support/tools/calibrate_sizes.py).
A tier's value is the **mean** MiB/min of its files.

Documented cleanups against raw measurement:

| Tier | Adjustment | Reason |
|---|---|---|
| `DVD-R` | n=3 measured ~55 → pinned **12.0** | *"tiny-n artifact, not a rate"* |
| CAM / TELESYNC / junk | kept low | *"never a real grab target; tiny-n samples high"* |
| `WEBRip-1080p` | n=2 → conservative **40** | Thin sample |
| `Bluray-2160p` | n=6 measured ~112 → **135** | *"to stay ≥ WEBDL-2160p"* |
| `WEBRip-2160p` / `HDTV-2160p` | no samples → sane defaults | — |

Per-tier sample counts are recorded inline — `Bluray-720p 1170 · DVD 1176 ·
SDTV 1148 · … · WEBDL-2160p 4`.

Sonarr's `"Bluray-2160p Remux"` and Radarr's `"Remux-2160p"` **share a value**,
so either service resolves without normalisation.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Impure bridge:** [`machine_learning/size_calibration.py`](../) — `SizeCalibrator`, the I/O half
- **Consumers:** [`space/`](../space/README.md) planners · [`ledger/`](../ledger/README.md) · acquisition · JIT upgrades
- **Tool:** [`support/tools/calibrate_sizes.py`](../../../support/tools/calibrate_sizes.py)

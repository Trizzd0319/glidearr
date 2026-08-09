# thresholds

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **thresholds**

**Package** — `scripts.managers.machine_learning.thresholds`
**Run position** — `get_threshold()` at every consumer; the calibrator fits once at end-of-run inside `PlanSummary`.
**One-liner** — The single seam every decision cutoff reads through, with an empirical-Bayes path from hand-set literal to household-calibrated value — defaulting to report-only.

---

## Purpose

Consumers used to embed cutoffs as literals (`score >= 35`). Each literal is a
private opinion about what "35" means, so a calibrated replacement could never be
swapped in without touching every call site.

This package is that seam:

```python
from scripts.managers.machine_learning.thresholds.registry import get_threshold
threshold = get_threshold("movie_monitor", self.config, threshold)
```

`default` is the consumer's **own** fully-resolved value — its literal or its
config override. In the default mode, `get_threshold` returns that object
unchanged, identity-preserved, having read exactly one config key. Shadow and off
are **byte-identical** to the pre-registry code, and the tests assert it.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`registry.py`](./registry.py) | `get_threshold`, `THRESHOLD_SPECS` (the full inventory), mode/config accessors, the derived-value store | ✅ Implemented |
| [`derive.py`](./derive.py) | Calibrator fit, inversion, `blend_threshold` shrinkage | ✅ Implemented |
| [`shadow.py`](./shadow.py) | `compare` — flip counts per threshold; `render_rows` for the end-of-run grid | ✅ Implemented |
| [`report.py`](./report.py) | `run` — end-of-run derivation + audit JSON, hosted by `PlanSummary` | ✅ Implemented |

## Test coverage

[`test_derive.py`](./test_derive.py) · [`test_shadow.py`](./test_shadow.py) ·
[`test_delete_floor_anchor.py`](./test_delete_floor_anchor.py) ·
[`test_installed_config_axis.py`](./test_installed_config_axis.py)

---

## Modes — `ml.thresholds.mode`

| Mode | Consumers get | Report? |
|---|---|---|
| **`shadow`** *(default)* | Their literal, unchanged | ✅ Fits the calibrator, derives every cutoff, counts what **would** flip, writes the audit JSON |
| `derived` | The **effective** cutoff — calibrated value shrunk toward their own literal | ✅ |
| `off` | Their literal | ❌ No derivation, no cost |

An unrecognised mode reads as the default — *"a typo must never arm `derived`."*

### Shrinkage

```
effective = w · derived + (1 − w) · constant       w = n_pos / (n_pos + k)
```

`k` defaults to **150** — the midpoint of the "usable" (100) and "stable" (300)
milestones. A household with 38 positives moves a fifth of the way; one with none
does not move at all.

A threshold with no derived value falls back to its literal and logs **one**
warning naming the threshold and the reason. *"Never a silent switch, and never an
unshrunk one."*

### Where derived values come from

The calibrator is fit **once at end-of-run** (inside `PlanSummary`, the only place
with both parquets already open). Rather than refit per consumer, `derived` mode
reads the newest `<cache>/ml/reports/thresholds_*.json` — **the artifact the
previous run committed**.

> *"A derived cutoff is therefore always a value someone can open, diff and
> blame, and it can only change between runs, not during one."*

---

## ⚠️ Two watchability axes

The single most important thing in this package, flagged in-source with a banner:

> **THE DELETE FAMILY SPANS TWO DIFFERENT SCORE AXES. READ THIS BEFORE "FIXING"
> ANY NUMBER BELOW TO MATCH ANY OTHER NUMBER BELOW.**

| Axis | Produced by | Inputs |
|---|---|---|
| **V2** — "the persisted axis" | `refresh_scores` → `_build_score_map` → `_score_row` | Full set: platform usage, transcode stats, household `transcode_profile`, per-user affinity, kids/adult split, C3 related graph, C4 person matrix, real engagement |
| **LEGACY** — "the anomaly axis" | `repair/anomaly.py::_score_owned` on raw Radarr dicts | Almost none of it — no `transcode_profile`, no C3/C4, no per-user affinity, `completion_pct` hardcoded 0.0 |

Measured on 2,151 owned movies joined on `tmdb_id`:

| Axis | mean | sd | median | p95 | p99 | max |
|---|---|---|---|---|---|---|
| LEGACY | 7.43 | 5.18 | 7 | 16 | 21 | 36 |
| V2 | 9.30 | 8.16 | 8 | 24 | 34 | 58 |

**Pearson r = 0.659.** At the adopted values (LEGACY floor 20, V2 ceiling 17) the
two axes **disagree about delete-eligibility for 354 of 2,151 movies — 16.5 %**.

> *"That gap is structural (missing C3/C4/engagement), NOT a threshold-value
> problem, and no single number closes it."*

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Host:** [`ledger/plan_summary.py`](../ledger/README.md) runs `report.run`
- **Related:** [`scoring/`](../scoring/README.md) · [`likelihood/`](../likelihood/README.md) · [`labels/`](../labels/)

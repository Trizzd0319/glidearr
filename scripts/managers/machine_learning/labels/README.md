# labels

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **labels**

**Package** — `scripts.managers.machine_learning.labels`
**Run position** — Snapshots appended during the run; labeling and first-run backfill at end-of-run inside `PlanSummary`.
**One-liner** — ML Stage 1: capture what the system saw, join it against Tautulli ground truth, and reconstruct a fresh install's missing history once, automatically.

---

## Purpose

Every calibrated threshold and every eval metric needs **supervised evidence**:
"the system saw this, and then the household did/didn't watch it." Nothing else in
Glidearr produces that.

Three jobs:

| Job | Module |
|---|---|
| Capture feature state at decision time | [`snapshots.py`](./snapshots.py) |
| Join snapshots against watch history → labels | [`labeling.py`](./labeling.py) |
| Reconstruct a fresh install's missing labels, once | [`first_run.py`](./first_run.py) |

The cold-start problem is real and specific: the prospective pipeline starts
logging on installation day and produces its first *matured* label a horizon
later. For two weeks a new household has **zero** supervised evidence — while the
same install already owns months of Tautulli history. `first_run.py` closes that
gap without the operator knowing a CLI exists.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`snapshots.py`](./snapshots.py) | Snapshot store — append, load, `SOURCE_BACKFILL`, `SNAPSHOT_SUBDIR` | ✅ Implemented |
| [`labeling.py`](./labeling.py) | `build_labels` + `load_*` helpers. **Pure**; all I/O in the loaders | ✅ Implemented |
| [`first_run.py`](./first_run.py) | `maybe_backfill_on_first_run` — one-shot truncated replay. ⚠️ **Not pure** (§3.5) | ✅ Implemented |
| [`recommendations.py`](./recommendations.py) | Recommendation label handling | ✅ Implemented |

## Test coverage

[`test_first_run.py`](./test_first_run.py) ·
[`test_recommendations.py`](./test_recommendations.py) ·
[`test_snapshot_signal_columns.py`](./test_snapshot_signal_columns.py)

---

## The three labels

| Column | Meaning |
|---|---|
| `watched_within_h` | Watched within `horizon_days` **after** the snapshot timestamp |
| `label_mature` | `snapshot_ts + horizon` has fully elapsed — *"an immature row cannot yet be a trustworthy negative"* |
| `recommended_not_watched` | Was in the Up Next plan (`in_up_next`) and was **not** watched in the horizon — the implicit-negative signal |

`label_mature` is the one that keeps the dataset honest: without it, everything
recent looks like a negative.

---

## Cache shapes it joins

Documented in-source as inspected on disk, with fields explicitly listed as used
and not-used:

| Cache | Shape | Note |
|---|---|---|
| `tautulli/history/all.json` | list of events, **n = 931** | `date` is unix epoch **seconds** — *"the only timestamp field present; there is no `started`"* |
| `tautulli/group/household/tmdb_completions.json` | `{tmdb: {pct, threshold}}` | **No timestamps** — *"so it can NEVER time-scope a label on its own"* |
| `plex/movies/owned_inventory.json` | `{tmdb: {rating_key, …}}` | Inverted to `rating_key → tmdb`, the exact movie join |

Deliberately unused: `user`/`user_id` (labels are household-level), `platform`,
`transcode_decision`, `location`, `media_index`, `row_id`, `reference_id`, and
`grandparent_rating_key` — *"no rating_key→series id map exists on disk."*

---

## Matching rules

| Media | Join | Completion cut |
|---|---|---|
| **Movies** | `rating_key → tmdb` == snapshot `tmdb_id` | `pct ≥ 90` **OR** (tmdb completed per `tmdb_completions` **AND** `pct ≥ 50`) |
| **Shows** | normalised `grandparent_title` == normalised snapshot title | `pct ≥ 50` — *"continued engagement, not per-episode completion"* |

Event timestamp must fall in `(snapshot_ts, snapshot_ts + horizon]`.

The relaxed movie cut exists because *"grouped sessions split `percent_complete`
across events"* — a film watched in two sittings shows as two ~50 % events, and
neither clears 90 alone.

**Title matching is the only join available for shows** — history carries no
series id or tvdb. Normalisation lowercases and strips punctuation/whitespace.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`thresholds/`](../thresholds/README.md) calibrator · [`eval/`](../eval/README.md) · [`challenger/`](../challenger/)
- **Host:** [`ledger/plan_summary.py`](../ledger/README.md)
- **Tool:** [`support/tools/ml_backfill_snapshots.py`](../../../support/tools/ml_backfill_snapshots.py)

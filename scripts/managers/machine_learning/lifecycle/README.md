# lifecycle

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **lifecycle**

**Package** — `scripts.managers.machine_learning.lifecycle`
**Run position** — Called during Radarr/Sonarr cache and storage phases.
**One-liner** — What happens to a title over time: the single definition of "watched", grace-period marking, monitor/restore/prune policies, and per-viewer retention.

---

## Purpose

Acquisition decides what arrives. `space/` decides what goes when the disk is
full. **This package decides everything in between** — how long a watched file is
kept, what protects it, when a series stops being monitored, and what may be
pruned.

Its keystone is [`watched_definition.py`](./watched_definition.py), which holds
**the one definition of "watched"** for the whole system. Before it existed, the
movie path and the show path each inlined `watch_count += 1` and drifted.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`watched_definition.py`](./watched_definition.py) | **The ONE definition of "watched."** `play_is_watched`, `watched_by_tautulli`, `resolve_watched_percent` | ✅ Implemented |
| [`grace_policy.py`](./grace_policy.py) | Grace-window computation + per-row guard precedence for movies and episodes | ✅ Implemented |
| [`viewer_retention.py`](./viewer_retention.py) | Per-viewer protected interval around each account's position | ✅ Implemented |
| [`household_watch.py`](./household_watch.py) | Household-level watch aggregation | ✅ Implemented |
| [`saga_retention.py`](./saga_retention.py) | Franchise/saga-aware retention across series | ✅ Implemented |
| [`monitor_policy.py`](./monitor_policy.py) · [`series_monitor_policy.py`](./series_monitor_policy.py) | What to monitor | ✅ Implemented |
| [`restore_policy.py`](./restore_policy.py) | Re-acquiring previously removed items | ✅ Implemented |
| [`stale_prune_policy.py`](./stale_prune_policy.py) | Pruning long-untouched items | ✅ Implemented |
| [`auto_rater.py`](./auto_rater.py) | Inferred ratings from behaviour | ✅ Implemented |

## Test coverage

Nine test modules including [`test_watched_definition.py`](./test_watched_definition.py),
[`test_grace_policy.py`](./test_grace_policy.py),
[`test_viewer_retention.py`](./test_viewer_retention.py),
[`test_saga_retention.py`](./test_saga_retention.py).

---

## The definition of "watched"

Precedence, per play:

| # | Source | Rule |
|---|---|---|
| 1 | `watched_status` | **Tautulli's own verdict.** `1` watched · `0.5` partial · `0` unwatched. Only `1` counts. Preferred whenever present |
| 2 | `percent_complete >= threshold_pct` | Fallback for rows predating the field |
| 3 | Neither present | **`True`** — fail-open |

Rule 1 is the point: Tautulli's verdict already reflects whatever completion
threshold the operator configured (85 % out of the box, shared with Plex).
Honouring it means Glidearr agrees with what Plex shows the household rather than
inventing a second, disagreeing definition.

Rule 3 fails **open** because this predicate gates *delete guards* — a viewer's
protected interval, the grace clock — and a guard must never shrink on missing
data.

**Config:** `watched_threshold.percent` (default 85). The legacy key
`episode_retention.watched_percent` remains a back-compatible alias; the new key
wins. `resolve_retention_config` resolves through the same function, so the
retention rule and the global bar cannot disagree.

---

## What is deliberately *not* thresholded

`percent_complete` and `last_watched_at` stay **threshold-free**. They are raw
playback facts — *how far did the furthest play get*, *when was this last played*
— not verdicts.

If `percent_complete` were thresholded, a title abandoned at 15 % would fall
through to the UNTOUCHED branch in `watch_likelihood` (`untouched_base` 25 +
score), and **abandoning a show could raise its quality target** — the exact
inverse of the intent.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`services/radarr/cache/movie_files.py`](../../services/radarr/cache/movie_files.py) · [`services/sonarr/cache/episode_files.py`](../../services/sonarr/cache/episode_files.py)
- **Related:** [`space/`](../space/README.md) · [`likelihood/`](../likelihood/) · [`scoring/`](../scoring/README.md)

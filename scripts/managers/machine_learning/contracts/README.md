# contracts

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **contracts**

**Package** — `scripts.managers.machine_learning.contracts`
**Run position** — Not executed. Pure shape definitions, imported by both sides of the service↔brain boundary.
**One-liner** — The typed service↔brain boundary: frozen dataclasses flowing in both directions, with no logic whatsoever.

---

## Purpose

This package is the entire interface between the I/O layer and the decision
layer. Its docstring states the rule plainly:

> Plain, frozen dataclasses flowing in both directions: feature rows (service →
> ML) and plans/decisions (ML → service). **No logic lives here — only shapes.**

The load-bearing consequence is in [`feature_rows.py`](./feature_rows.py):

> A service builds one of these from a cached Parquet row + affinity context
> (**the ONLY place a column name / API JSON shape is known**)

That parenthetical is the architecture. Once a `MovieFeatureRow` exists, nothing
downstream knows that Radarr nests video codec at `movieFile.mediaInfo.videoCodec`,
or that Sonarr measures runtime in seconds while Radarr uses minutes. Vendor shape
stops at this boundary.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`feature_rows.py`](./feature_rows.py) | **service → brain**: `MovieFeatureRow`, `ShowFeatureRow`, `EpisodeFeatureRow` | ✅ Implemented |
| [`plans.py`](./plans.py) | **brain → service**: `QualityPlan`, `DeletePlan`, `DeleteCandidate`, `MonitorPlan`, `GracePlan`, `AcquirePlan` | ✅ Implemented |
| [`context.py`](./context.py) | **service → brain**: `AffinityContext`, `SpaceContext` — library-wide inputs beyond a single row | ✅ Implemented |
| [`__init__.py`](./__init__.py) | Package docstring. ⚠️ Claims to re-export common types; **contains no re-exports** | 🟡 Partial |

---

## The two directions

### Service → brain

| Type | Represents | Notes |
|---|---|---|
| `MovieFeatureRow` | One Radarr movie, fully resolved | ~45 fields spanning identity, playback facts, engagement, critic ratings, classification |
| `ShowFeatureRow` | One Sonarr series, aggregated from episode rows + Trakt/Tautulli | Modal for categoricals, **median** for bitrate, **max** for resolution |
| `EpisodeFeatureRow` | One Sonarr episode file | Carries the broadcast series score |
| `AffinityContext` | Household + per-user affinity from Tautulli | genres, actors, directors, writers, studios, per_user, kids/adult users, platform usage, transcode stats |
| `SpaceContext` | The space band for a pressure decision | `free_gb`, `floor_gb` (T), `target_gb` (U = T×(1+headroom)), `coordinator_owns_deletion` |

### Brain → service

| Plan | Effect | Space accounting |
|---|---|---|
| `QualityPlan` | Upgrade/downgrade a profile | `est_space_gb_signed` — **+freed** (downgrade) / **−consumed** (upgrade) |
| `DeletePlan` | Delete a specific file | `reclaim_gb` **+** (freed); carries `restore_key` |
| `DeleteCandidate` | A rankable pre-decision candidate for the cross-service pool | `tier` 0 = watched+grace-expired, 1 = unwatched-low; `score` lower = delete first; `critic` as secondary key |
| `MonitorPlan` | Set monitored on/off | — |
| `GracePlan` | Mark/clear grace-period deletion eligibility | Carries `available_until` |
| `AcquirePlan` | Monitor + search a not-yet-present item | — |

**A plan never executes anything.** The service adapter turns it into HTTP plus a
ledger stamp.

---

## Two properties worth knowing before writing a consumer

**1. Signed space.** `reclaim_gb` and `est_space_gb_signed` are signed so that a
dry-run ledger **sums to a true net** rather than to gross reclaim. An upgrade
consuming 8 GB and a downgrade freeing 3 GB net to −5 GB, not 11 GB of "activity."

**2. Partial rows are safe by construction.** Every optional field defaults to
`None`/`0`, explicitly so a partially-enriched row does not raise. What each
consumer *does* with a `None` is the consumer's decision — and the two live
strategies differ. See [`DESIGN.md`](./DESIGN.md) §3.3.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`scoring/`](../scoring/) · [`space/`](../space/) · [`lifecycle/`](../lifecycle/) · [`likelihood/`](../likelihood/)
- **Producers:** [`services/radarr/`](../../services/radarr/README.md) · [`services/sonarr/`](../../services/sonarr/README.md)

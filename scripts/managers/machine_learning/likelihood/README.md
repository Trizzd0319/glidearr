# likelihood

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **likelihood**

**Package** — `scripts.managers.machine_learning.likelihood`
**Run position** — Radarr universe pass, Radarr active-watcher upgrades, Sonarr JIT upgrade pass.
**One-liner** — `P(this will be watched)` on a 0–100 scale, mapped to a quality tier — the "earn your quality tier" rule, and the home of `uhd_cutoff`.

---

## Purpose

The watchability score says *how much the household values this*. Likelihood
answers a narrower, more actionable question: **will this actually get played,
and therefore what quality is it worth holding?**

The two are different axes and must not be confused (see
[`DESIGN.md`](./DESIGN.md) §3.1 — this distinction is the source of a live
documentation error elsewhere in the repo).

```
likelihood = max( engagement floor , affinity propensity )
```

Engagement **floors** it; affinity can only raise an *untouched* title, and is
capped below the 4K gate so taste alone can never buy 4K.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`watch_likelihood.py`](./watch_likelihood.py) | The engine. `watch_likelihood`, `explain_likelihood`, `profile_id_for_likelihood`, `ladder_rank`, `resolution_cap_for_likelihood`, `affinity_boost`, `radarr_ladder` | ✅ Implemented |
| [`quality_ladder.py`](./quality_ladder.py) | Ladder resolution | ✅ Implemented |
| [`survival.py`](./survival.py) | Survival modelling | ✅ Implemented |
| [`saga_engagement.py`](./saga_engagement.py) · [`saga_order.py`](./saga_order.py) · [`saga_progress.py`](./saga_progress.py) | Franchise-arc engagement, ordering and progress | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_watch_likelihood.py`](./test_watch_likelihood.py) | The engine |
| [`test_untouched_anchor.py`](./test_untouched_anchor.py) | **Pins every invariant of the `untouched_base` anchor** — see [`DESIGN.md`](./DESIGN.md) §3.3 |
| [`test_saga_engagement.py`](./test_saga_engagement.py) | Saga engagement |

---

## The likelihood scale

**This is not the watchability scale.** Both run 0–100; they measure different
things and their thresholds are not interchangeable.

| Constant | Value | Meaning |
|---|---|---|
| `uhd_cutoff` | **75** | 4K gate |
| `affinity_cap` | **74** | Ceiling on the affinity term — deliberately **one below** `uhd_cutoff` |
| `fhd_cutoff` | 45 | 1080p gate |
| `hd_cutoff` | 20 | (crossing has no effect — `hd_res` and `floor_res` are both 720) |
| `watched_floor` | 50 | One watch ⇒ ≈1080p |
| `rewatch_floor` | 90 | Ceiling from repeated rewatches |
| `started_floor` | 45 | Partial view, 20–90 % complete |
| `abandoned_ceiling` | 25 | Abandoned, <20 % |
| `untouched_base` | 25 | Anchor for the never-watched branch |

**`affinity_cap` (74) < `uhd_cutoff` (75) is the load-bearing relationship.**
Affinity alone reaches Remux-1080p and **never** 4K. 4K is reserved for content
the household actually rewatches — roughly 3+ watches.

---

## Engagement grading

```
watched once        → watched_floor 50            ≈ 1080p
each further watch  → + rewatch_step, up to 90    ≈ high-1080p
regular rewatch (≈3+)                             → clears uhd_cutoff 75 → 4K
20–90 % complete    → started_floor 45
< 20 % complete     → ≤ abandoned_ceiling 25
never touched       → untouched_base 25 + score   (affinity, capped at 74)
```

Since the global watched bar landed, a 30-second sample no longer reaches the
`ewc >= 1` branch and no longer buys `watched_floor` 50. Those plays now land on
the partial-view branches instead, which are evaluated **before** the untouched
branch — so a sub-threshold play can never fall through to `untouched_base +
score`, which would have meant **abandoning a title raises its quality target**.

Measured after the change: **0 titles moved from a floor branch to UNTOUCHED.**

---

## Two ladders

| Service | Mechanism |
|---|---|
| **Radarr** | Explicit profile-id ladder (`radarr_quality_ladder`) — ascending `[min_likelihood, profile_id]`. `ladder_rank()` gives the quality rank so callers only ever **upgrade** (target rank > current rank). Distinguishes sub-tiers sharing a resolution (low/high-1080p, low/high-4K) |
| **Sonarr** | `resolution_cap_for_likelihood()` returns a max resolution (2160/1080/720), since Sonarr profile ids differ from Radarr's |

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Depends on:** [`scoring/`](../scoring/README.md) (raw `watchability_score` at gain 1.0) · [`lifecycle/watched_definition.py`](../lifecycle/README.md)
- **Consumers:** Radarr universe + active-watcher passes · Sonarr JIT upgrades

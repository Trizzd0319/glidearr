# episode_files.py — verification, part 2 (class constants)

> Extends [`VERIFICATION_episode_files.md`](./VERIFICATION_episode_files.md).
> Covers lines ~120–205, session 44. `SCHEMA_COLUMNS` is **still truncated** —
> `GLD-CACHE-S02` remains open.

---

## 1. 🎯 The config-vs-constant rule — the register has been missing this

```
Its knobs are REAL CONFIG KEYS ("episode_retention"), unlike the three class
constants above, for two reasons. (1) They are household preferences, not physics:
GRACE_HOURS/RECENT_AIR_DAYS/PREFETCH_HOURS are "how long is a sensible buffer"
defaults nobody has ever needed to tune, while the backward buffer and the horizon
encode how a specific household watches TV — Robert named 2 and 14 as *his* numbers
and asked for them to be tunable. (2) They gate DELETION, so they must be auditable
from config.json and settable headlessly (.env / unraid template) without editing
source.
```

**A constant should become config when it is (a) a household preference rather
than physics, or (b) gates deletion and must be auditable and headless-settable.**

Four register items propose routing hand-set constants through
`thresholds/registry` — `GLD-DIS-09` (shelf floor), `GLD-ACQ-09` (demand
threshold), `GLD-ACQS-09` (`_GENRE_SATURATION`, source tiers), `GLD-WB-08`
(writeback watched bar) — and none of them states *why those and not others*.

Tested against this rule they separate cleanly:

| Item | Household preference? | Gates deletion? | Verdict |
|---|---|---|---|
| `GLD-WB-08` writeback watched bar | ✅ | — (gates a **third-party write**, arguably worse) | Route it |
| `GLD-DIS-09` shelf floor | ✅ *"how much of next season do I want?"* | ❌ | Route it |
| `GLD-ACQ-09` demand threshold 0.15 | ❌ — a signal-strength cut, not a taste | ❌ | **Leave as a constant** |
| `GLD-ACQS-09` `_GENRE_SATURATION` 0.80 | ❌ — chosen so multi-genre can outrank single | ❌ | **Leave as a constant** |

So two of the four should be dropped rather than done. `GLD-CACHE-S05` proposes
recording the rule in `DOCS_CONVENTIONS.md` and re-triaging the set against it.

---

## 2. 🎯 Synchronised-expiry herd control, with measured cost

```
Stubs created together (cold start) expire together, and re-checking one refreshes
its timestamp — so the whole cohort re-expires together FOREVER: a ~7.7k-series,
~77s serial API spike every 48h.
```

Two dampers, each with its choice justified:

| Damper | Mechanism | Why *this* way |
|---|---|---|
| `PILOT_STALE_TTL_JITTER_PCT` 0.25 | Effective TTL spread over `[TTL, TTL×1.25]` **by series id** | **Deterministic, not random** — *"so a series' due-time is stable across runs rather than re-rolled every pass"* |
| `PILOT_STALE_RECHECK_CAP` 2000 | Bounds re-checks per run, **oldest first** | Deferred stubs *"keep their old timestamp, so they stay due and are picked up next run — **FIFO, nothing starves**"* |

The self-reinforcing part is what makes it hard: re-checking a stub *refreshes its
timestamp*, so the cohort re-synchronises after every attempt to spread it. Random
jitter would fix the herd but make due-times unstable; deterministic-by-id fixes
both.

Same bounded-work-per-run shape as
[`space/`](../../../machine_learning/space/DESIGN.md)'s `DEFAULT_REGRAB_CAP` — but
for a different reason (API spike vs indexer storm), and with an explicit
anti-starvation guarantee `space/` does not state.

---

## 3. 🔴 Tautulli history is **volatile** — and this changes D32

```
The Tautulli history it is derived from is VOLATILE (tautulli/history/all is
overwritten hourly and Tautulli prunes its own DB), so the positions are merged
forward here.
```

This is a cross-cutting fact the sweep has not captured, and it bears on three
open items:

**D32 — the affinity half-life.** `affinity/genre_affinity` has decay built and
disabled, and I framed the choice as *"an aggressive half-life discards most of a
thin evidence base."* If Tautulli **prunes its own DB**, the evidence window is
already bounded by Tautulli's retention — so a long half-life may have nothing
older to weight, and the real question becomes *"what does Tautulli retain?"*

**`labels/`'s n = 931.** [`labels/DESIGN.md`](../../../machine_learning/labels/DESIGN.md)
records `tautulli/history/all.json` at **n = 931** events. That is not the
household's lifetime history — it is **what Tautulli currently retains**. Every
statistic derived from it (`foundation`'s ~2 % prevalence, the 9.8 %/36.2 % sample
rates, `next_watch`'s 258/259 solo) is a *window*, not a total.

**`eval/` replay.** Reconstructing state at cutoff `T` requires events from before
`T` to still exist. Pruning bounds how far back replay can reach, independently of
the snapshot-retention problem `GLD-PLX-01` found.

The module's own response is right — merge positions forward into a durable key
(`sonarr/{inst}/viewer_positions`) rather than re-deriving from volatile history.
That pattern is what the three items above need too. `GLD-CACHE-S06`.

---

## 4. 🔴 A fourth last-resort floor — three names, three values

```python
MIN_FREE_SPACE_GB = 50.0   # last-resort acquire/upgrade floor only (free_space_limit
                           # unset AND total drive unreadable)
```

| Site | Name | Value |
|---|---|---|
| `machine_learning/space/space_targets.py` | `PRESSURE_FALLBACK_GB` | **25.0** |
| `sonarr/series/space_pressure.py` | `PRESSURE_FALLBACK_GB` | **25.0** |
| `services/coordinator/space_coordinator.py` | `PRESSURE_FALLBACK_GB` | **1000.0** |
| **`sonarr/cache/episode_files.py`** | **`MIN_FREE_SPACE_GB`** | **50.0** |

All four are documented as the same thing — *"last resort when `free_space_limit`
is unset AND the total drive is unreadable."* Four sites, three names, three
values spanning **40×**.

`GLD-COORD-01` was scoped as "rename one of two constants." It is wider: the
fallback floor is a **family** with no single definition, and the same unreadable
drive produces 25, 50 or 1000 GB depending on which module asks.

---

## 5. ✅ Defence-in-depth without divergence

```python
_RETENTION_COLS = ("retention_hold", "retention_hold_by")
```

> The two parquet columns carry the per-run verdict so the **three duplicated guard
> layers** (grace marking, whole-file protected set, delete-time defence-in-depth)
> all read the **SAME decision** instead of each re-deriving it from history.

Three guard layers is the right amount of paranoia for a delete path. Three layers
each *re-deriving* the verdict is how they end up disagreeing — the shape §8
**P-E** tracks. Materialising the decision once into two columns keeps the
redundancy and removes the divergence.

---

## 6. `PILOT_MIN_WATCHABILITY = 20.0` — the `routed=False` spec, in situ

`thresholds/registry.py` carries `pilot_min_watchability` as `routed=False`
because it *"scores UNOWNED titles… the calibrator does not cover them."* Here is
the consumer, with a matching rationale — sample *"the shows the household is
plausibly interested in, not every empty series"* — and an important property:

> The row + score are **never gated**, so a held-back stub keeps being re-graded
> and returns once its affinity climbs back over the floor.

The gate suppresses the **API search**, not the **scoring**. So a stub excluded
today is not excluded permanently — exactly the reversibility `series/quality.py`'s
monitor band also builds in.

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CACHE-S05` | 🎯 **Record the config-vs-constant rule** in `DOCS_CONVENTIONS.md` and re-triage the four routing items against it — **two of them (`GLD-ACQ-09`, `GLD-ACQS-09`) should be dropped**, being signal-strength cuts rather than household preferences | S | `GLD-DIS-09`, `GLD-ACQ-09`, `GLD-ACQS-09`, `GLD-WB-08` |
| `GLD-CACHE-S06` | 🔴 **Establish Tautulli's retention window** — history is *"overwritten hourly and Tautulli prunes its own DB."* Every statistic derived from `n = 931` is a **window, not a total**, and D32's half-life question changes shape if the evidence is already truncated upstream | S | D32, `GLD-AFF-01`, `GLD-LAB-09`, `GLD-EVA-03` |
| `GLD-CACHE-S07` | 🎯 **Cite the jitter+cap damper as the synchronised-expiry pattern** — deterministic-by-id (stable due-times), oldest-first cap with timestamp preservation (FIFO, nothing starves). Measured: ~7.7k series, ~77 s spike, every 48 h | S | `GLD-CACHE-04` |
| `GLD-CACHE-S08` | 🔴 **Unify the last-resort floor family** — four sites, three names (`PRESSURE_FALLBACK_GB` ×3, `MIN_FREE_SPACE_GB`), three values (25 / 50 / 1000). **Widens `GLD-COORD-01`** from a rename to a family definition | S | `GLD-COORD-01` |

## 8. Open

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `SCHEMA_COLUMNS` cover `EpisodeFeatureRow`? **Still truncated** — the read reached only *Identity* and the start of *Signal flags* | `GLD-CACHE-S02` |
| Q2 | What is Tautulli's actual retention setting on this install? | `GLD-CACHE-S06` |
| Q3 | Should the four fallback floors be one constant, or are 25/50/1000 deliberately scoped to acquire vs pressure vs coordinator? | `GLD-CACHE-S08` |

**Q3 admits a defence** — an *acquire* floor (50) reasonably differs from a
*pressure* floor (25), since one gates spending and the other gates reclaiming.
But then they should not share a description, and the coordinator's 1000 still has
no such story.

## 9. Related

- [`VERIFICATION_episode_files.md`](./VERIFICATION_episode_files.md) — part 1, the import block
- [`machine_learning/affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) §3.2 — D32, reframed by §3
- [`machine_learning/labels/DESIGN.md`](../../../machine_learning/labels/DESIGN.md) — the n = 931 window
- [`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) §3.4 — `GLD-COORD-01`, widened by §4

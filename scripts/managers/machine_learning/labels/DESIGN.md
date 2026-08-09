# labels — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **labels**

**Package** — `scripts.managers.machine_learning.labels`
**Status** — ✅ Implemented · 🔴 Carries a third and fourth "watched" threshold · 🟡 `first_run.py` is deliberately impure
**Related** — [README.md](./README.md) · [`thresholds/DESIGN.md`](../thresholds/DESIGN.md) · [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md)

---

## 1. Problem statement

Supervised evidence is the scarcest thing in this system. Without it, the
threshold calibrator has nothing to fit, `eval/` has nothing to score against,
and the challenger has nothing to beat.

Three obstacles:

1. **Nothing records what the system saw.** A decision is made against feature
   state that is immediately overwritten by the next run. Without a snapshot,
   there is no "what did we know at the time."

2. **The join is lossy.** Tautulli history carries a Plex `rating_key` and a
   `grandparent_title`. Snapshots carry `tmdb_id` and a title. There is no
   `rating_key → series id` map on disk, so **shows can only be joined by
   normalised title string** — and movies only via an inverted owned-inventory
   map.

3. **A fresh install has no evidence at all, for two weeks.** The prospective
   pipeline starts logging on day one and matures its first label a horizon
   later. Meanwhile the same install owns months of Tautulli history — the labels
   exist, they simply have not been reconstructed.

Obstacle 3 has a subtle failure mode: the reconstruction tool exists
(`ml_backfill_snapshots.py`) but is *"a standalone CLI, run by hand, which is
exactly the step a new user will not know to take."*

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Capture feature state at decision time. |
| G2 | An immature row is never treated as a negative. |
| G3 | Cold start solves itself, once, without operator action. |
| G4 | The backfill is bounded by construction. |
| G5 | Nothing here can affect a score, a decision, or an `*arr` write. |
| G6 | Backfilled and prospective rows are distinguishable. |
| G7 | `labeling` stays pure; I/O lives in loaders so CLIs can inject fakes. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Per-user labels | Household-level by design; `user_id` explicitly unused. |
| N2 | Perfect joins | Title matching is the only option for shows (§3.3). |
| N3 | Continuous backfill | One shot, marker-guarded. |
| N4 | Owning the definition of "watched" | …except it does, twice. See §3.4. |

---

## 3. Architecture

### 3.1 Pipeline

```
during the run          snapshots.py    append feature state + in_up_next
                                             │
end of run (PlanSummary)                     ▼
                        first_run.py    5 preconditions → truncated replay (once)
                        labeling.py     join vs Tautulli → 3 label columns
                                             │
                                             ▼
                        thresholds/ calibrator · eval/ · challenger/
```

### 3.2 The maturity guard (G2)

`label_mature = (snapshot_ts + horizon) <= now`.

Without it every recent row reads as a negative — the household simply has not
had time to watch it yet — and the calibrator would fit a systematically
pessimistic map. It is one boolean doing a lot of work.

### 3.3 The join is the weak point

| Media | Join | Failure mode |
|---|---|---|
| Movies | `rating_key → tmdb` via `owned_inventory.json` | Unowned or un-inventoried titles yield `<NA>` and drop out |
| **Shows** | **normalised title string** | Two shows normalising alike collide; a retitled show breaks silently |

*"Title matching is the only join available for shows (history has no series
id/tvdb)"* — and `grandparent_rating_key` **is** present in the history but
unusable, because no `rating_key → series id` map exists on disk.

That is a fixable gap rather than an inherent one: the map could be built from
Plex's series inventory the same way `owned_inventory.json` is built for movies.

### 3.4 🔴 A third and fourth definition of "watched"

[`lifecycle/watched_definition.py`](../lifecycle/README.md) exists explicitly to
be *"the ONE definition of watched"*, created because two producers had drifted.
Counting what is actually in the brain:

| Consumer | Bar | Imports `watched_definition`? |
|---|---|---|
| Production producers | Tautulli `watched_status`, else `pct ≥ 85` | ✅ Yes |
| [`eval/replay.py`](../eval/README.md) · [`eval/forward.py`](../eval/README.md) | `0.9` | ❌ No |
| **`labeling.py` — movies** | **`pct ≥ 90`**, or `≥ 50` if completed overall | ❌ No |
| **`labeling.py` — episodes** | **`pct ≥ 50`** | ❌ No |

Four bars. The module built to prevent exactly this is imported by the two
producers and by **nothing else in the brain**.

**The labels bars are better justified than eval's.** The episode cut of 50 is
documented as measuring *"continued engagement, not per-episode completion"* —
genuinely a different question, and 50 is a defensible answer to it. The relaxed
movie cut of 50 exists because *"grouped sessions split `percent_complete` across
events."* Both are deliberate.

But the movie bar of 90 is documented as *"≈ the completions threshold 0.9"* —
i.e. it is trying to agree with production and is off by five points, and would
not track a household that changed `watched_threshold.percent`. That one looks
like drift rather than design.

This upgrades `GLD-EVA-01` from an eval-local question to a **systemic** one:
`GLD-LAB-01`.

### 3.5 🟡 `first_run.py` is deliberately impure

It:

- reads `tautulli/history/all.json` and probes `movie_files.parquet`,
- writes an atomic marker file,
- **dynamically loads and executes a CLI** from `scripts/support/tools/` via
  `importlib.util.spec_from_file_location`,
- captures the tool's stdout and collapses it into one log line.

That is I/O and subprocess-shaped work inside `machine_learning/`. It is
**correct** — the cold-start fix has to touch disk — but it is the same **P-F**
pattern found at the `machine_learning/` root: a deliberately impure module
living inside the brain.

**Consequence for `GLD-ML-02`.** That item proposes adding six subpackages —
including `labels` — to `_GUARDED_SUBPACKAGES`. Adding `labels` would either fail
or force an exemption, because `first_run.py` legitimately does I/O. The dynamic
`importlib` load also means an AST import-guard would **not** catch it, so the
subpackage would appear to pass while containing the most impure module in the
brain.

`GLD-ML-02` therefore cannot be executed as a flat list addition.

### 3.6 The five preconditions

Cheapest-first, and the *durability* distinction is the clever part:

| # | Check | Miss is durable? |
|---|---|---|
| 1 | `ml.snapshots.enabled` **and** `backfill_on_first_run` (both default **true**) | No |
| 2 | No marker file | No |
| 3 | Not already attempted in this process | No |
| 4 | Tautulli history non-empty **and** `movie_files.parquet` exists | **No** — *"neither is durable on a first boot, so a miss here writes NO marker: the next run tries again"* |
| 5 | Store carries no usable evidence — no backfill rows, no matured prospective row | **Yes** — marker written |

Precondition 5 is *not* "no rows". The check runs at end-of-run, by which point
today's prospective rows are already appended — *"Today's rows are not evidence;
they mature in a horizon."*

The marker is written on the first **attempt**, success or failure, *"so this can
never become a per-run cost — deleting it is the documented way to retry."*

### 3.7 Bounded by construction (G4)

Cost is `grid points × library size` rescores.

```
backfill_grid_days        7    →  --grid-days
backfill_max_grid_points  26   →  ~6-month, 26-point ceiling
start = max(first_event, end_cap − (points−1)·grid_days)
```

The first-event clamp *"stops us rescoring the library over grid dates that
predate the household's history (all-zero features, pure cost)."* `--end` is left
to the tool, which clamps to `now − horizon` **and** below the first prospective
snapshot — its never-mix guard.

### 3.8 Never fatal, never loud (G5)

Every path wrapped: an exception, a missing cache, a non-zero exit all become a
logged no-op returning a status dict. `SystemExit` from argparse is caught and
converted. *"Nothing here can affect a score, a decision, or an `*arr` write."*

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | `label_mature` as an explicit column | G2 — otherwise every recent row is a false negative | Filter by date at read time |
| D2 | Household-level labels | Per-user labels fragment already-scarce evidence | Label per user |
| D3 | Relaxed movie cut when completed overall | Grouped sessions split `percent_complete` | Single cut |
| D4 | Episode cut 50, not 90 | Measures continued engagement, a different question | Match movie cut |
| D5 | Title-normalised show join | The only join available (§3.3) | Skip show labels |
| D6 | `tmdb_completions` never time-scopes | It has no timestamps — it can only relax a cut | Use it as evidence |
| D7 | Auto-fire the backfill on first run | G3 — the CLI exists and a new user will not know to run it | Leave it manual |
| D8 | Marker on first *attempt*, not success | G4 — a failing backfill must not retry every run | Marker on success |
| D9 | Transient misses write no marker | Caches are not durable on first boot | Always mark |
| D10 | "Evidence" ≠ "rows" | Today's prospective rows are not evidence | Check emptiness |
| D11 | Grid capped at 26 points | Bounds a first run to seconds, not minutes | Replay all history |
| D12 | Clamp start to first event | Pre-history grid points are all-zero features, pure cost | Start at the cap |
| D13 | Load the tool by path | `support/tools` has no `__init__.py` — same approach as the tool's own test | Package it |
| D14 | stdout captured and collapsed | It is a CLI printing a full report | Let it print |
| D15 | `include_backfill` defaults **true** in `thresholds` | A fresh install has no other evidence | Match other entry points |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | An immature row is never a trustworthy negative. |
| I2 | The backfill fires at most once per install (marker) and once per process. |
| I3 | A transient precondition miss leaves no marker. |
| I4 | Backfilled rows are distinguishable via `source == SOURCE_BACKFILL`. |
| I5 | Backfill and prospective rows are never mixed in one grid window. |
| I6 | Nothing in this package affects a score, decision, or `*arr` write. |
| I7 | `labeling.py` is pure; all I/O is in `load_*` helpers. |
| I8 | `tmdb_completions` is never used to time-scope a label. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| **Show titles collide after normalisation** | **None** | Labels attributed to the wrong series | 🟡 Silently wrong evidence | ❌ **None** |
| **Show retitled between snapshot and history** | **None** | Join misses ⇒ false negative | 🟡 Same | ❌ **None** |
| Movie not in `owned_inventory` | `tmdb_id` `<NA>` | Row drops out | 🟡 Silent shrink | ❌ **None** |
| History cache missing | Precondition 4 | Skip, no marker, retry next run | Safe | 🟡 Debug |
| Backfill tool raises | `_invoke` → rc=1 | Marker written `failed`, one warning | Bounded | ✅ Warning |
| Backfill produces zero rows | Tool output | Marker written; summary line | 🟡 Looks like success | 🟡 Summary only |
| Grid cap truncates history | By design | Older evidence never reconstructed | Bounded, intended | ❌ **None** |
| Four watched bars disagree | **None** | Calibrator fits a different population than production acts on | 🔴 Systemic | ❌ **None** |

**Rows 1–3 all shrink the evidence set silently**, and the evidence set is
already small (n = 931 events). A join that quietly drops rows is
indistinguishable from a household that watched less.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `ml.snapshots.enabled` | `true` | Master gate |
| `ml.snapshots.backfill_on_first_run` | `true` | Auto-fire the reconstruction |
| `ml.snapshots.backfill_grid_days` | `7` | Grid spacing |
| `ml.snapshots.backfill_max_grid_points` | `26` | ~6-month ceiling; `0` = unbounded |
| `horizon_days` | `14` | Matches `ml.thresholds.horizon_days` |
| `movie_pct_min` / `relaxed_pct_min` / `episode_pct_min` | `90` / `50` / `50` | Completion cuts (§3.4) |

---

## 8. Implemented capabilities

- ✅ Snapshot store with `source` provenance
- ✅ Three label columns including the maturity guard
- ✅ Bisect-indexed event lookup per entity
- ✅ Movie join via inverted owned-inventory; relaxed cut for grouped sessions
- ✅ Show join via normalised title
- ✅ Implicit-negative signal from `in_up_next`
- ✅ One-shot auto-backfill with five cheapest-first preconditions
- ✅ Durable-vs-transient miss distinction
- ✅ Grid capped and clamped to first real event
- ✅ Never-mix guard between backfill and prospective rows
- ✅ Fully wrapped — never fatal, never loud
- ✅ Cache shapes documented with used/unused field lists

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-LAB-01` | 🔴 **Unify the watched bars** — four now exist (production 85/`watched_status`, eval 0.9, labels movie 90, labels episode 50). Route the ones that *mean* "watched" through `lifecycle.watched_definition`; keep and document the ones that deliberately mean something else | §3.4. The module built to prevent this is imported by nothing else in the brain | M | D25, `GLD-EVA-01` |
| `GLD-LAB-02` | 🔴 **Build a `rating_key → series id` map** from Plex series inventory, mirroring `owned_inventory.json` | Replaces the fragile title-string show join (§3.3); `grandparent_rating_key` is already in the history and unusable without it | M | Plex inventory |
| `GLD-LAB-03` | **Report join coverage** — snapshots labeled vs dropped, per media type | §6 rows 1–3 silently shrink an already-small evidence set | S | — |
| `GLD-LAB-04` | **Detect normalised-title collisions** and warn | §6 row 1 attributes labels to the wrong series | S | `GLD-LAB-02` |
| `GLD-LAB-05` | **Revisit `GLD-ML-02`** — `labels` cannot simply join `_GUARDED_SUBPACKAGES`; `first_run.py` is deliberately impure and its `importlib` load evades an AST guard anyway *(P-F)* | Prevents a guard change that would either fail or give false assurance | S | `GLD-ML-02`, `GLD-ML-17` |
| `GLD-LAB-06` | **Warn when the backfill produces zero rows** | §6 row 6 currently looks like success | S | — |
| `GLD-LAB-07` | **Report evidence growth** per run — total labels, matured, positives, by source | The calibrator's `n_pos` is shown; its trajectory is not | S | `GLD-THR-11` |
| `GLD-LAB-08` | **Allow a second backfill window** when the grid cap truncated real history | §6 row 7: older evidence is permanently unreachable after the one shot | M | `GLD-LAB-07` |
| `GLD-LAB-09` | **Use `user_id` for per-viewer labels** where the evidence supports it | Deliberately unused today; would feed per-user playlists and viewer retention | M | `GLD-TAUT-06` |
| `GLD-LAB-10` | **Re-verify the documented cache shapes** — they are dated in-source and drift silently | The join depends on field names that nothing asserts | S | `GLD-LAB-03` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should the labels bars route through `watched_definition`, or are they deliberately different questions? The episode cut clearly is; the movie 90 looks like drift from 85. *(= D28)* | `GLD-LAB-01` |
| Q2 | Is a title-string show join acceptable given n = 931, or does it need the rating-key map first? | `GLD-LAB-02` |
| Q3 | Should the backfill be re-runnable with a wider grid once the household has more history? | `GLD-LAB-08` |
| Q4 | Is 26 grid points the right ceiling against measured first-run cost? | — |
| Q5 | Should `labels` be purity-guarded with `first_run.py` exempted, or left unguarded? | `GLD-LAB-05` |

## 11. Related designs

- [`thresholds/DESIGN.md`](../thresholds/DESIGN.md) — the calibrator these labels feed; `include_backfill` defaults true there for the reason in §1
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) §3.1 — the definition §3.4 diverges from
- [`eval/DESIGN.md`](../eval/DESIGN.md) §3.6 — the other divergent bar
- [`ledger/DESIGN.md`](../ledger/DESIGN.md) §3.2 — `PlanSummary` hosts `first_run_backfill`
- [`support/tools/ml_backfill_snapshots.py`](../../../support/tools/ml_backfill_snapshots.py)

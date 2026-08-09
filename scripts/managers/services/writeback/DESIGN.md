# writeback — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **writeback**

**Manager** — `WritebackManager`
**Run position** — Phase 3, `main.py`'s final phase.
**Status** — ✅ Implemented · 🔴 A seventh "watched" bar with **different semantics** · 🔴 Zero tests · 🟡 Weakest `dry_run` resolution in the repo
**Existing docs** — [`README.md`](./README.md) (10.3 KB)

> **This document does not restate [`README.md`](./README.md).** It adds the
> 11-section frame and the cross-package findings.

---

## 1. Problem statement

This is the **only** package that writes to systems Glidearr does not own.
Everything else in the repo changes local state — a Parquet column, a Radarr
profile, a file on a mount. This pushes into a user's **permanent Trakt history**
and their **MAL list**.

That inverts the usual risk calculus:

| | Rest of the repo | `writeback/` |
|---|---|---|
| Wrong action | A file is deleted; the ledger records it; `restore_recovered_*` re-grabs it | 1,000 watch entries land in a third party's database |
| Undo | Built in, tracked, tested | **Manual, external, per-item** |
| Blast radius | Local | The user's account, visible to them and anyone they share it with |

Two sub-problems shape the design:

1. **The cached history is deliberately lossy.** The projection *"is
   PII-minimised and drops season/episode indices + the watched timestamp"* —
   exactly the fields an episode-level history push needs.
2. **A false positive is not symmetric with a false negative.** Failing to push a
   watch is invisible and recoverable next run. Pushing a watch that never
   happened writes a permanent, wrong record.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Every sub-sync independently gated by config. |
| G2 | `dry_run` logs "would …" and writes nothing. |
| G3 | A failed sync never breaks the run. |
| G4 | Unmapped entries are counted and reported. |
| G5 | Requests are chunked to survive large pushes. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Reading the cached history projection | It is PII-minimised past usefulness here (§3.2). |
| N2 | Being enabled by default | Both `trakt_writeback` and `mal_writeback` are opt-in. |
| N3 | Reconciling with what Trakt already has | Push-only; Trakt de-duplicates. |

---

## 3. Architecture

### 3.1 🔴 A seventh "watched" bar — and this one differs in *semantics*, not just value

```python
_WATCHED_PCT = 85  # default "counts as watched" completion threshold
...
if e.get("watched_status") != 1 and pct < threshold:
    continue
```

The value matches production's 85. **The logic does not.**

| | `lifecycle/watched_definition.py` | `writeback/trakt_history.py` |
|---|---|---|
| Rule | `watched_status` **decides when present**; `percent_complete` only as fallback | `watched_status == 1` **OR** `pct >= 85` |
| `watched_status = 0`, `pct = 90` | **Not watched** — the verdict wins | **Watched** — pushed to Trakt |
| `watched_status = 0.5` (partial), `pct = 90` | **Not watched** | **Watched** — pushed to Trakt |

Production is *"the operator's verdict is authoritative"*:

> `watched_status` is Tautulli's own per-row verdict and is **preferred whenever
> present**: it already reflects the operator's configured completion threshold…
> **no second, disagreeing definition of "watched."**

Writeback is *"either signal suffices."* A row Tautulli has explicitly marked
**unwatched** or **partial** is pushed to the user's permanent history if its
percentage happens to clear 85.

This is the **seventh** bar in the register and the most consequential, because it
is the only one whose output **leaves the system**. Every other divergence
mis-scores a title internally; this one writes a wrong fact into a third party's
database, where it then feeds back as evidence on the next run.

**And the fix is available.** §3.2 explains why writeback cannot read the cached
`is_watched` column — but it can do exactly what
[`labels/labeling.py`](../../machine_learning/labels/README.md) does: import
`play_is_watched` and apply it to the raw rows. `GLD-WB-01`.

### 3.2 Why it refetches raw history — a good reason, with a consequence

> The Tautulli watch-history projection cached for the rest of the app is
> **PII-minimised** and drops season/episode indices + the watched timestamp.
> Write-back therefore reads raw history **straight from the Tautulli API**.

Sound: an episode-level push needs `parent_media_index`, `media_index` and
`date`, and the cache deliberately discards them. Privacy minimisation working as
intended.

The consequence is that writeback operates on a **different view of history** from
the rest of the application — its own API calls, its own row shapes, its own
verdict. That is what makes §3.1 structural rather than careless: the module
genuinely cannot reuse the computed column. It can still reuse the *function*.

### 3.3 🔴 Zero tests

Four source files, ~17.7 KB of logic, and **no test file in the package.**

Against the risk profile in §1, that is the inversion worth naming: the module
with the least reversible output has the least verification. For comparison,
[`coordinator/`](../coordinator/DESIGN.md) — which deletes files, but tracks and
restores every one — has **61 KB of tests**.

Three things here are exactly what tests exist for:

- the §3.1 watched predicate, where a boolean slip pushes false history;
- `extract_id`'s prefix matching across `guids[]`, `guid` and `tmdb_id`;
- the chunk boundaries at 100 movies / 50 shows.

`GLD-WB-02`.

### 3.4 🟡 The weakest `dry_run` resolution in the repo

```python
self.dry_run = kwargs.get("dry_run",
                          getattr(parent, "dry_run", False) if parent else False)
```

Two levels, defaulting to **`False`** — which means **live writes**.

Compare [`coordinator/`](../coordinator/DESIGN.md) §3.6, in the manager that
deletes files:

```python
# dry_run resolution (kwargs → parent → Main); never silently default.
```

Three levels, an explicit principle, plus `effective_dry_run` from the backup
gate as an independent second path.

So the repo has two `dry_run` resolutions of markedly different rigour, and the
weaker one guards the **less reversible** operation. If `manager` is not passed
and `dry_run` is absent from kwargs, writeback silently assumes live.

That is `GLD-ORCH-01`'s distributed-invariant problem — *"every manager
individually remembers"* — with a concrete cost attached. `GLD-WB-03`.

### 3.5 Pagination is correct but silently capped

```python
def fetch_history(tau_api, logger, *, length=1000, max_pages=20) -> list:
    for _ in range(max_pages):
        ...
        if start >= int(total or 0):
            break
```

Terminates on an empty page **or** on `start >= recordsFiltered/recordsTotal` —
the grand total, not the page size. That is the correct form, and the same one
[`plex/`](../plex/DESIGN.md) §3.4 documents avoiding a bug over.

But the loop is bounded at **20 pages × 1000 = 20,000 entries**, and exhausting
the bound is indistinguishable from finishing: no warning, no flag on the return.
A household past 20,000 plays would silently push only the most recent slice, and
`history_max_pages` would need to be raised by someone who knew to look.

The `logger` parameter is accepted and never used, which is a small tell that a
warning was intended here. `GLD-WB-04`.

### 3.6 What it does well

**Unmapped entries are counted** (G4):

```
[writeback] history: {mapped} mapped, {unmapped} unmapped ({movies} movies, {shows} shows).
```

The same visible-degradation discipline [`plex/`](../plex/DESIGN.md) §3.5 shows
and eleven register items ask for elsewhere. A GUID that fails to resolve shrinks
the push *visibly*.

**Every sub-sync is independently wrapped** (G3) — a failed collection sync does
not prevent the history sync, and neither breaks the run.

**Chunked at 100 movies / 50 shows** (G5), with shows chunked smaller because each
carries a nested season/episode tree.

**dry_run reports what it would do**, with counts, before returning.

### 3.7 🟡 A private method across a module boundary

```python
api._make_request("sync/history", method="POST", data={"movies": batch})
```

`TraktHistorySync` calls `TraktAPI._make_request` — a leading-underscore method —
from another package. Minor, and pragmatic given there may be no public POST
wrapper, but it means the one write path in the repo depends on a private
interface. `GLD-WB-07`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Opt-in per sub-sync | G1 — three independent third-party writes | One master flag |
| D2 | Refetch raw history | The cache is PII-minimised past usefulness (§3.2) | Read the projection |
| D3 | Wrap each sync | G3 — a third-party outage must not fail the run | Let it propagate |
| D4 | Count unmapped | G4 — a shrinking push must be visible | Log the total only |
| D5 | Chunk 100 / 50 | Shows carry nested trees, so smaller batches | One request |
| D6 | `watched_status == 1 OR pct >= 85` | ❓ **Unstated** — the file gives no reason for diverging from production's precedence (§3.1) | Reuse `play_is_watched` |
| D7 | Cap at 20 pages | Bounds the fetch | Unbounded |

**D6 is the one without a recorded rationale.** Every other divergent watched bar
in this repo states its reason — `labels`' episode cut measures *"continued
engagement"*, `affinity`'s grace threshold models members who stop before the
credits. This one just differs.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Nothing is written unless the relevant `*_writeback.enabled` is true. |
| I2 | `dry_run` writes nothing and logs intended counts. |
| I3 | A failed sync logs a warning and never raises into the run. |
| I4 | Unmapped entries are counted and reported. |
| I5 | An entry with no resolvable external id is never pushed. |
| I6 | Requests are chunked. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Trakt or Tautulli unavailable | Guard | Skip, warn | Safe | ✅ |
| GUID unresolvable | `extract_id` → None | Counted as `unmapped` | Visible | ✅ |
| Sync raises | `try/except` | Warn, continue | Safe | ✅ Warning |
| **`watched_status = 0`, `pct ≥ 85`** | **None** | **Pushed to permanent Trakt history** | 🔴 §3.1 — wrong fact in a third party | ❌ **None** |
| **History exceeds 20,000 entries** | **None** | Silently truncated to the most recent slice | 🟡 §3.5 | ❌ **None** |
| **`dry_run` unresolvable** | **None** | Defaults `False` ⇒ **live writes** | 🔴 §3.4 | ❌ **None** |
| `_make_request` signature changes | `TypeError` | Sync fails, warns | Bounded | ✅ |
| Trakt rejects a batch | ❓ Unverified | Return value not checked in what I read | 🟡 | ❌ |

**Rows 4 and 6 are the ones to close**, and they compound: a divergent predicate
plus a permissive `dry_run` default means the failure mode is *"writes history
that never happened, live, by default, with no test covering it."*

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `trakt_writeback.enabled` | `false` | Master gate for both Trakt syncs |
| `trakt_writeback.collection` | `true` *(when enabled)* | Mirror the *arr library |
| `trakt_writeback.history` | `true` *(when enabled)* | Push episode-level history |
| `trakt_writeback.watched_threshold` | **85** | §3.1 |
| `trakt_writeback.history_max_pages` | **20** | ×1000 = 20,000-entry cap |
| `mal_writeback.enabled` | `false` | MAL list sync |

---

## 8. Implemented capabilities

- ✅ Three independently-gated third-party syncs
- ✅ Raw-history refetch with correct grand-total pagination
- ✅ External-id extraction across `tmdb_id`, `guids[]` and `guid`
- ✅ Episode-level season/episode tree assembly
- ✅ Mapped/unmapped counting
- ✅ Chunked POSTs
- ✅ Per-sync exception isolation
- ✅ `dry_run` reporting with counts

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-WB-01` | 🔴 **Route the watched predicate through `lifecycle.play_is_watched`** — writeback uses `watched_status == 1 **OR** pct ≥ 85`, production uses *verdict-wins*. A row Tautulli marked **unwatched** or **partial** is pushed to permanent Trakt history if its percentage clears 85 | §3.1. The **seventh** bar, and the only one whose output leaves the system. `labels/` already applies `play_is_watched` to raw rows, so the pattern exists | S | `GLD-LAB-01`, D28 |
| `GLD-WB-02` | 🔴 **Add tests** — the only package writing to third parties has **none**, while `coordinator/` (which deletes, but restores) has 61 KB | §3.3. Cover the watched predicate, `extract_id` across all three id shapes, and the chunk boundaries | M | — |
| `GLD-WB-03` | 🟡 **Strengthen `dry_run` resolution to kwargs → parent → Main** and refuse to default | §3.4 — two-level, defaults to **live writes**, in the least reversible module. `coordinator/` already has the stronger form to copy | S | `GLD-ORCH-01` |
| `GLD-WB-04` | **Warn when the page cap is hit** — `fetch_history` already accepts an unused `logger` | §3.5: 20,000 entries silently truncates | S | — |
| `GLD-WB-05` | **Check POST responses** — confirm a rejected batch is detected | §6 row 8 unverified; a silent 4xx would look like success | S | `GLD-WB-02` |
| `GLD-WB-06` | **Record what was pushed** — a per-run writeback ledger | Nothing records what left the system; an incorrect push cannot be found afterwards, let alone reversed | M | `GLD-LED-08` |
| `GLD-WB-07` | **Add a public POST wrapper to `TraktAPI`** — the one write path calls `_make_request` | §3.7 | S | — |
| `GLD-WB-08` | **Route `watched_threshold` through `thresholds/registry`** | A hand-set cutoff absent from `THRESHOLD_SPECS`, like `GLD-DIS-09`, `GLD-ACQ-09`, `GLD-ACQS-09` | S | `GLD-THR-01` |
| `GLD-WB-09` | **Document `mal_list.py` and `trakt_collection.py`** — 7.6 KB unread this pass | M | — |
| `GLD-WB-10` | **Dry-run diff before first live enable** — show exactly what would be pushed, once, at scale | The first live run of a push-only sync is the one that cannot be undone | M | `GLD-WB-05` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is writeback's OR-semantics deliberate, or drift from production's verdict-wins? *(= D47)* | `GLD-WB-01` |
| Q2 | Has `trakt_writeback.enabled` ever been true on this install? | `GLD-WB-06` |
| Q3 | Are POST failures detected? | `GLD-WB-05` |
| Q4 | Should writeback default `dry_run` to **True** rather than False? | `GLD-WB-03` |

**Q4 is worth considering on its merits.** Every other `dry_run` default in the
repo guards a *local, reversible, ledgered* action. This one guards a push to a
third party with no local record and no undo. The
[D36 rule](../../machine_learning/discovery/DESIGN.md) — *on unknown input, fail
toward the outcome that changes nothing* — argues for `True` here, and the cost of
being wrong in that direction is one skipped sync.

## 11. Related designs

- [`README.md`](./README.md) — the operational reference this complements
- [`machine_learning/lifecycle/DESIGN.md`](../../machine_learning/lifecycle/DESIGN.md) §3.1 — the definition §3.1 diverges from
- [`machine_learning/labels/DESIGN.md`](../../machine_learning/labels/DESIGN.md) §3.4 — `play_is_watched` applied to raw rows, the pattern `GLD-WB-01` follows
- [`coordinator/DESIGN.md`](../coordinator/DESIGN.md) §3.6 — the stronger `dry_run` resolution to copy
- [`plex/DESIGN.md`](../plex/DESIGN.md) §3.4–3.5 — correct pagination and visible degradation, both mirrored here

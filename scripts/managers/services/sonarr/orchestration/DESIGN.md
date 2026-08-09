# sonarr/orchestration — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **orchestration**

**Manager** — `SonarrOrchestrationManager`
**Status** — ✅ Loaded and run · 🎯 The best load-summary protocol in the repo · 🟡 `run()` reaches 2 of 11
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

---

## 1. What it is

The live Sonarr entrypoint. `SonarrManager.run()` reaches `orchestration`, which
constructs **eleven** sub-orchestrators and then runs the enrichment pipeline.

125 KB across 25 files. **One test file** — [`test_series_space_pressure_defer.py`](./test_series_space_pressure_defer.py) (2.0 KB).

---

## 2. 🟡 `run()` invokes two of eleven

```python
def run(self):
    self.run_full_enrichment()

def run_full_enrichment(self):
    if self.series:   self.series.run_full_series_enrichment()
    if self.episodes: self.episodes.run_full_episode_retrieval()
```

| Sub-orchestrator | Constructed | Reached by `run()` |
|---|---|---|
| `series` · `episodes` | ✅ | ✅ |
| `cache` · `episodes_retrieval` · `instance` · `monitoring` · **`quality`** · `repair` · `series_retrieval` · `series_sync` · `validator` | ✅ | ❌ |

The other nine are constructed and left on `self`. They are reachable only if
something calls them by attribute — which is clearly intended for at least one:
[`quality.py`](./quality.py) exposes four accessors precisely for that
(`get_adjustment_manager`, `get_custom_formats_manager`, `get_file_sizes_manager`,
`get_selector_manager`).

So "constructed" and "invoked" are two different questions here, and the second
is not answerable from this file.

### 2.1 Which sharpens `GLD-SIZ-04` — again

The chain for `filesizes` is now fully traced:

| Step | State |
|---|---|
| `SonarrQualityFileSizesManager` constructed by `orchestration/quality.py` | ✅ confirmed |
| `SonarrOrchestrationQualityManager` constructed by `orchestration/__init__.py` | ✅ confirmed |
| `orchestration.run()` invokes `quality` | ❌ **it does not** |
| Something calls `get_file_sizes_manager()` or `orchestration.quality.file_sizes` | ❓ **unknown** |

Session 36 said "dead code" (wrong — it is constructed). Session 37 reopened it as
"reachable" — which is true but **overstated**: reachable by construction, not
demonstrated to be invoked.

The honest position is the fourth row. `GLD-SQ-03` is now the precise question,
and the same applies to `GLD-SQ-04`'s selector.

**Third correction on this one item.** The pattern in my own errors is consistent:
I keep answering *"can this be reached?"* when the useful question is *"is this
called?"* — and each layer of construction looks like an answer without being one.

---

## 3. 🎯 The soft-disable protocol — three states, not two

```python
instance = cls(**init_kwargs)
if not getattr(instance, "active", True):
    reason = getattr(instance, "_inactive_reason", "dependency unavailable")
    self.logger.log_debug(f"⏭️ Orchestration sub-component '{name}' inactive: {reason}")
    setattr(self, name, None)
    self.load_summary[name] = f"⏭️ Inactive: {reason}"
else:
    self.load_summary[name] = "✅ Loaded"
except Exception as e:
    self.load_summary[name] = f"⚠️ Skipped: {e}"
```

| State | Meaning |
|---|---|
| ✅ **Loaded** | Working |
| ⏭️ **Inactive** | *"Sub-manager soft-disabled itself (missing dependency, **not an error**)"* — **with a stated reason** |
| ⚠️ **Skipped** | Raised, with the exception recorded |

**This is the best `load_summary` in the repo**, and it solves exactly what
`GLD-MIX-01` is about: distinguishing *absent* from *failed*. Sonarr's parent
`prepare()` ([`sonarr/DESIGN.md`](../DESIGN.md) §12.5) fixes the false-`❌` for
eagerly-built components; this goes further by giving a component the ability to
**declare itself inactive with a reason**, which no other manager offers.

The `active` / `_inactive_reason` convention is a genuine protocol. It should be
named and propagated. `GLD-ORCH-S02`.

---

## 4. 🎯 An honest denominator

```python
loaded_count = sum(1 for v in self.load_summary.values() if v.startswith("✅"))
f"{loaded_count}/{len(orchestrator_map)} sub-orchestrators loaded."
```

The denominator is **`len(orchestrator_map)` = 11** — the *full* map. Two inactive
components report **9/11**, and the gap is visible.

Contrast the parent, [`sonarr/DESIGN.md`](../DESIGN.md) §12.3:

```python
names = list(self.component_dependencies.keys())   # 8 — the FILTERED set
```

which reports **8/8** and is structurally incapable of naming `quality` or `sync`.

**The fix `GLD-SON-13` asks for already exists — in the child.** The parent counts
against what it decided to load; the child counts against what it knows about.
That is a one-line difference with a large consequence, and it is worth citing as
the precedent when `GLD-SON-13` is worked.

---

## 5. 🟡 The `series_init` fallback silently substitutes the wrong manager

```python
series_manager = getattr(self.manager, "series", None) if self.manager else None
series_init = {**base_init, "manager": series_manager} if series_manager else sonarr_init
```

`series_retrieval` and `series_sync` are documented as operating against
`SonarrSeriesManager`. If it is absent, they receive `sonarr_init` — the **top-level
`SonarrManager`** — instead.

For `series_sync` that produces a specific, traceable failure:

```python
self.series_sync = getattr(self.manager, "sync", None)
if not self.series_sync:
    raise ValueError("❌ SonarrSeriesSyncManager is not initialized in parent manager.")
```

With `manager = SonarrManager`, `getattr(manager, "sync")` returns **`None`** — the
class-attribute default, because `sync` is filtered out of
`all_component_classes`. So it raises, is caught, and records `⚠️ Skipped`.

**It does not fire today**: `component_dependencies` declares
`"orchestration": ["series", "episodes", "storage"]`, so `series` is loaded first.
The fallback is a latent path guarded by a dependency declaration rather than by
its own check.

Worth noting the near-miss: two unrelated decisions — the dependency ordering, and
`sync` being filtered out — combine such that the fallback would produce a
*confusing* error (`SonarrSeriesSyncManager is not initialized`) about a
*different* manager than the one actually missing. `GLD-ORCH-S04`.

---

## 6. Smaller observations

| Observation | Note |
|---|---|
| `getattr(kwargs.get("manager") or {}, "sonarr_cache", None)` | `{}` as a null-object stand-in — works, but `getattr` on a dict is unusual |
| `run_full_enrichment` wraps each stage individually | A series failure does not prevent episode retrieval |
| 11 sub-orchestrators, 1 test file (2.0 KB / 125 KB) | Another instance of coverage not tracking risk |
| `parent_name = "SonarrManager"` class attr, not overwritten here | Unlike `quality/` and `sync/`, which overwrite it with `__class__.__name__` |

---

---

## 6.5 `series.py` — one of the two modules `run()` reaches

Read: [`series.py`](./series.py) head (~100 of 15.9 KB).

### 6.5.1 🟡 It raises where its own parent offers soft-disable

```python
self.series_manager = getattr(self.manager, "series", None)
if not self.series_manager:
    raise ValueError("❌ Missing SonarrSeriesManager reference in orchestration layer.")

self.retrieval = getattr(self.series_manager, "retrieval", None)
self.sync = getattr(self.series_manager, "sync", None)
if not self.retrieval or not self.sync:
    raise ValueError("❌ Retrieval or Sync submanagers not found in SonarrSeriesManager.")
```

Both are **missing-dependency** conditions — precisely what §3's
`active` / `_inactive_reason` protocol exists for. Instead they raise, so the
parent's `except` records `⚠️ Skipped: …` rather than `⏭️ Inactive: …`.

The consequence compounds with §2: `series` is **one of only two** sub-orchestrators
`run()` invokes. If it is skipped, `run_full_enrichment` hits `if self.series:` →
`None` → **silently does nothing**, and the only trace is a constructor-time
warning. The enrichment pipeline would report success having enriched nothing.

So the protocol exists, and the module where it matters most does not use it.
`GLD-ORCH-S08`.

### 6.5.2 🟡 A second live caller of the migration shim

```python
from scripts.support.utilities.space_targets import coordinator_owns_deletion
```

[`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) §3.5 flagged
`space_coordinator.py` importing through the `support/utilities` re-export shim
— *"deleted at MIGRATION.md Step 10"* — and noted **nothing lists who the
importers are**.

Here is the second. Two live callers found by accident, in unrelated reads,
neither discoverable without opening the file. `GLD-COORD-02` is not a one-line
repoint; it needs an actual import search first.

### 6.5.3 ✅ A well-reasoned API-call saving

> When `refresh_all_series` reports that the result came from the disk cache (no
> live API call), the count-validation step is **skipped** — the freshness
> timestamp already proves the cache was successfully synced within the last
> 24 h, so a redundant live API call to re-count series would be wasted work.

and, on the live path:

> Reuse the live list `refresh_all_series` just fetched — the count-drift check
> needs a live count, but there's no reason to pull all **~8k series** from Sonarr
> a second time.

Two redundant full-library fetches removed, each justified by what the prior step
already established. The measured library size (~8k series) is what makes the
saving worth stating.

### 6.5.4 The shape guard degrades toward doing more work

```python
from_cache = False  # default: assume a sync happened
result = self.retrieval.fetch.refresh_all_series(instance=instance)
if isinstance(result, tuple) and len(result) == 2:
    live_series, from_cache = result
```

If the return shape ever changes, `from_cache` stays `False` and `live_series`
stays `None` — so validation **runs**, and runs without the reuse optimisation.
The degradation costs an extra fetch rather than skipping a check.

That is the [D36 direction](../../../machine_learning/discovery/DESIGN.md) applied
to a performance optimisation: on unknown input, fall back to the *safe, slower*
path. The comment `# default: assume a sync happened` states it.

### 6.5.5 `self.sync` is series sync, again

`getattr(self.series_manager, "sync", None)` — a `SonarrSeriesManager` child, not
the config-push `SonarrSyncManager`. Third module in which the name collision has
had to be disambiguated by hand. `GLD-SON-15`.

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-ORCH-S08` | 🟡 **Make `series.py` use `active` / `_inactive_reason` instead of raising** — both its `raise ValueError`s are missing-dependency cases, and it is **one of only two** sub-orchestrators `run()` invokes. Skipped, the pipeline silently enriches nothing | §6.5.1 — the protocol exists in the parent and the module that matters most does not use it | S | `GLD-ORCH-S02` |
| `GLD-ORCH-S01` | 🟡 **Establish which of the nine unrun sub-orchestrators have callers** — `run()` reaches only `series` and `episodes`. `quality.py`'s four `get_*_manager()` accessors imply external callers were intended | §2. Until answered, "constructed" keeps being mistaken for "runs" — including by me, three times on `GLD-SIZ-04` | M | `GLD-SQ-03`, `GLD-SQ-04` |
| `GLD-ORCH-S02` | 🎯 **Name and propagate the `active` / `_inactive_reason` protocol** — three-state load summary distinguishing *soft-disabled with a reason* from *raised*. **The best `load_summary` in the repo** | §3. Directly serves `GLD-MIX-01`; goes further than Sonarr's parent-level fix | S | `GLD-MIX-01`, `GLD-SON-04` |
| `GLD-ORCH-S03` | 🎯 **Cite this module's denominator as the fix for `GLD-SON-13`** — it counts against the **full** map (11) while the parent counts against the **filtered** set (8), which is why the parent can never surface `quality`/`sync` | §4. A one-line difference, already solved one level down | S | `GLD-SON-13` |
| `GLD-ORCH-S04` | 🟡 **Make the `series_init` fallback explicit** — falling back to `sonarr_init` substitutes the wrong manager and yields an error naming the wrong component | §5. Latent, guarded only by a dependency declaration | S | — |
| `GLD-ORCH-S05` | **Report the inactive/skipped rows in the run summary**, not just at debug | The three-state protocol's value is lost if only `log_debug` sees it | S | `GLD-ORCH-S02` |
| `GLD-ORCH-S06` | **Add tests** — 125 KB, 25 files, one 2.0 KB test | `series.py` alone is 15.9 KB and is one of the two things `run()` actually invokes | M | — |
| `GLD-ORCH-S07` | **Document the remaining modules** — `series.py` (15.9 KB), `episodes.py`, `cache.py`, `monitoring.py`, `repair.py`, `instance.py`, `validator.py`, the two retrieval modules | Each has a `.md`; none has a `DESIGN.md` | L | — |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What calls the nine unrun sub-orchestrators — and are any genuinely orphaned? | `GLD-ORCH-S01` |
| Q2 | Is `get_file_sizes_manager()` called anywhere? *(the precise form of `GLD-SIZ-04`)* | `GLD-SQ-03` |
| Q3 | Is `get_selector_manager()` called, and does it reach `choose_codec_profile`? *(D34's Sonarr half)* | `GLD-SQ-04` |
| Q4 | Which managers implement `active` / `_inactive_reason` today? | `GLD-ORCH-S02` |
| Q5 | How many modules import through the `support/utilities` migration shims? Two found so far, both by accident | `GLD-COORD-02` |

**Q1 is the one that matters structurally.** Nine constructed sub-orchestrators
with no invocation from their own parent is either a large accessor-driven API or
a second `GLD-SON-01`-shaped finding — and the two look identical from inside this
file. It is also the question my three successive corrections on `GLD-SIZ-04` kept
circling without landing on.

## 9. Related designs

- [`sonarr/DESIGN.md`](../DESIGN.md) §12 — the parent's filtering and its 8/8 summary
- [`quality/DESIGN.md`](../quality/DESIGN.md) §2.2 — the dead copy this module supersedes
- [`sync/DESIGN.md`](../sync/DESIGN.md) §4 — `GLD-SYNC-01`, closed by reading `series_sync.py`
- [`factories/mixins/DESIGN.md`](../../../factories/mixins/DESIGN.md) — `GLD-MIX-01`, which §3 solves better

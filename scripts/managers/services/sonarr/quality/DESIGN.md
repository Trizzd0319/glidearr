# sonarr/quality — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **quality**

**Manager** — `SonarrQualityManager`
**Status** — 🔴 **Never constructed — and its construction loop is broken**
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

> Companion to [`sync/DESIGN.md`](../sync/DESIGN.md). Together they answer Q1 and
> Q6 of [`sonarr/DESIGN.md`](../DESIGN.md) §10.

---

## 1. What this package is for

| Module | Size | Role |
|---|---|---|
| [`selector.py`](./selector.py) | 13.5 KB | `SonarrQualitySelectorManager` — profile selection |
| [`filesizes.py`](./filesizes.py) | 12.5 KB | `SonarrQualityFileSizesManager` — size comparison |
| [`custom_formats.py`](./custom_formats.py) | 8.7 KB | Custom-format scoring |
| [`adjustment.py`](./adjustment.py) | 5.3 KB | Quality adjustment |

No test files.

---

## 2. 🔴 The construction loop cannot work

`split_components` returns **plain classes**:

```python
# support/utilities/managers/component_splitter.py
critical = {k: v for k, v in all_components.items() if k in critical_keys}
...
noncritical[name] = cls
return critical, noncritical          # → (dict[str, type], dict[str, type])
```

[`sync/__init__.py`](../sync/__init__.py) uses that correctly:

```python
for name, cls in critical_components.items():
    instance = cls(**init_args)                                    # ✅
```

[`quality/__init__.py`](./__init__.py) does not:

```python
for name, cls in critical_components.items():
    instance = cls(**critical_components[name]["init_kwargs"])     # ❌
```

`critical_components[name]` **is a class**. Subscripting it raises:

```
TypeError: 'type' object is not subscriptable
```

For **all four** components. Each is caught by the surrounding `try/except`,
recorded as `❌ Failed: …` in `load_summary`, and `all_critical_loaded` is set
`False`. The manager would finish constructing with **`adjustments`,
`custom_formats`, `file_sizes` and `selector` all unset**, and
`registry.set_flag("sonarr.quality_manager_initialized", False)`.

Note the expression is self-referential — `cls` and `critical_components[name]`
are the same object, so it reduces to `value(**value["init_kwargs"])`, requiring
the value be **both callable and subscriptable**. Nothing `split_components`
returns is both.

### 2.1 Why this has never been seen

Because [`sonarr/DESIGN.md`](../DESIGN.md) §12.1 established the manager **never
loads**. The bug sits behind a filter, in a `try/except`, in a manager that is
never constructed. Three layers, any one of which would hide it.

### 2.2 This answers Q1: **superseded by a working duplicate**

> ⚠️ **Revised session 37.** Session 36 concluded *"regressed, not superseded"*
> on the strength of the construction bug alone. Reading
> [`orchestration/quality.py`](../orchestration/quality.py) shows the opposite,
> and better.

`SonarrOrchestrationQualityManager` constructs **the same four component
classes**, with the same `critical_keys`, the same registry-flag pattern and the
same summary call — and it **is** loaded, because `orchestration` is in
`component_dependencies`.

The only material difference is the loop:

```python
# orchestration/quality.py  — LIVE
component_init_kwargs = { ... }                       # hoisted to a variable
instance = cls(**component_init_kwargs)               # ✅

# quality/__init__.py  — DEAD
split_components(..., init_kwargs={ ...inline... })
instance = cls(**critical_components[name]["init_kwargs"])   # ❌
```

Plus four `get_*_manager()` accessors on the orchestration version, and a
distinct flag namespace (`sonarr.orchestration.quality.*` vs `sonarr.quality.*`).

**So the four quality subcomponents — `adjustment`, `custom_formats`,
`filesizes`, `selector` — DO load and run.** They are reached through
orchestration, not through `SonarrQualityManager`.

`SonarrQualityManager` is therefore a **superseded dead copy**, and the remedy is
**delete**, not fix. The construction bug is a symptom of the copy being left
behind, not the cause of its disconnection.

This also makes the pair a genuine §8 **P-E** instance: two implementations of
one manager, one live and correct, one dead and broken.

---

## 3. 🟡 A latent hazard in `split_components` itself

```python
for name, cls in all_components.items():
    if name in critical_keys:
        continue
    try:
        temp_instance = cls(**init_kwargs)        # ← constructed purely to read one attribute
        if getattr(temp_instance, "parent_name", "") == parent_name_match:
            noncritical[name] = cls
```

To classify a component as noncritical, the splitter **instantiates it** and reads
`parent_name`. The caller then instantiates it **again** for real.

So every noncritical component is constructed **twice**, and the first instance is
a throwaway whose constructor side effects are real. These managers call
`self.register()` in `__init__`, so a throwaway would register itself into the
registry before being discarded.

**It does not fire in Sonarr today** — both `sync/` and `quality/` put *every*
component in `critical_keys`, so the `continue` skips the loop body entirely. But
any manager with genuinely noncritical components pays it, and the cost scales
with whatever that constructor does.

`GLD-SPLIT-01`.

---

## 4. Two register items — one corrected, one retracted

### 4.1 ⚠️ `GLD-SIZ-04` — my session-36 resolution was WRONG

Session 36 concluded the Sonarr `filesizes.py` copy was **dead code** because
`quality/` never loads. **That was incorrect.**

`SonarrQualityFileSizesManager` is constructed by
[`orchestration/quality.py`](../orchestration/quality.py), which **does** load. So
the Sonarr copy of the 0.6/1.4 acceptance band **is reachable**, and MIGRATION
Step 1d has a live caller to migrate after all.

The error was inferring reachability from **one** construction path without
checking for others. `GLD-SIZ-04` returns to open.

What remains genuinely open: whether `compare_file_sizes` is *called*, not merely
loaded. `GLD-SQ-03`.

### 4.2 ⚠️ `GLD-SON-10` — retracted

I logged that `cache/` and `quality/` *"each carry `test_size_anomaly_remediate.py`
+ `test_size_anomaly_report.py`."* **There are no test files in `quality/` at
all** — ten files, none of them tests.

That claim came from a session-4 reading of a directory listing and was never
re-checked. §8 **P-G**, mine, fifth instance.

---

## 5. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SQ-01` | 🔴 **Delete `SonarrQualityManager`** — it is a **superseded dead copy** of `SonarrOrchestrationQualityManager`, which is live and correct. Its construction loop is also broken (§2), but that is a symptom, not the reason to remove it *(P-E)* | S | `GLD-SON-01` |
| `GLD-SQ-03` | **Check whether `compare_file_sizes` is actually called** from the live `orchestration/quality.py` path — loaded ≠ invoked. Then complete or drop Step 1d | S | `GLD-SIZ-04` |
| `GLD-SQ-04` | ❓ **Check whether `selector.py` reaches `choose_codec_profile`** — `SonarrQualitySelectorManager` **is** loaded via orchestration, so D34's Sonarr half is *reachable*. Whether it calls the brain selector is the remaining question | S | `GLD-QAN-01`, D34 |
| `GLD-SPLIT-01` | 🟡 **Stop double-constructing noncritical components** — `split_components` instantiates each one purely to read `parent_name`, then the caller instantiates it again. Constructor side effects (incl. `self.register()`) fire twice. Dormant in Sonarr (all components critical); live anywhere they are not | S | — |
| `GLD-SQ-05` | **Assert `split_components`' return shape** at the call site, or type it | §2 is exactly the bug a type annotation would have caught | S | `GLD-SPLIT-01` |
| `GLD-SQ-06` | **Add tests, or delete** — 79.8 KB of source and documentation, zero tests | Same posture as `GLD-SYNC-06` | M | `GLD-SQ-02` |

## 6. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ✅ **Answered s37** — `SonarrQualityManager` is **superseded** by `SonarrOrchestrationQualityManager`, a live and correct duplicate. The four subcomponents **do** run. Remedy: **delete the dead copy** | `GLD-SQ-01` |
| Q5 | ⚠️ **Lesson from §4.1** — I inferred "dead code" from **one** construction path without checking for others, and was wrong within a session. **Reachability requires searching for all constructors, not verifying one is absent** | — |
| Q2 | Is `selector.py` the Sonarr consumer D34 is looking for? | `GLD-SQ-04` |
| Q3 | Which other managers have genuinely noncritical components, and therefore pay §3's double construction? | `GLD-SPLIT-01` |
| Q4 | Did `split_components` once return `{name: {"class":…, "init_kwargs":…}}`? That would explain the divergence as a helper refactor that missed one caller | `GLD-SQ-01` |

**Q4 is the interesting archaeology.** `quality/`'s expression is not a random
mistake — it is *precisely* what you would write against a helper returning
`{name: {"class": …, "init_kwargs": …}}`. The most economical explanation is that
`split_components` once had that shape, was simplified to `{name: class}`, and
`sync/` was updated while `quality/` — already disconnected — was not.

If so, this is a **P-D instance of the purest kind**: a refactor that broke a
caller, with no detector, because the caller was already unreachable.

## 7. Related designs

- [`sonarr/DESIGN.md`](../DESIGN.md) §12 — the filtering that hides this
- [`sync/DESIGN.md`](../sync/DESIGN.md) — the sibling, correctly written and equally uncalled
- [`machine_learning/sizing/DESIGN.md`](../../../machine_learning/sizing/DESIGN.md) §3.6 — `GLD-SIZ-04`, resolved in §4.1
- [`machine_learning/quality_analytics/DESIGN.md`](../../../machine_learning/quality_analytics/DESIGN.md) — D34's selector, whose Sonarr consumer may be here
- `support/utilities/managers/component_splitter.py` — §3

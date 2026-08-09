# sonarr/episodes — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **episodes**

**Manager** — `SonarrEpisodesManager`
**Status** — ✅ Live — the second of the two paths `orchestration.run()` reaches
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

---

## 1. Position

```
orchestration.run_full_enrichment()
  ├─ series.run_full_series_enrichment()
  └─ episodes.run_full_episode_retrieval()   ← this subtree
```

Five components: `retrieval` (critical), `file`, `history`, `monitoring`,
`sharding`.

---

## 2. 🔴 `retrieval.py` is unreachable — Python shadows it

Both of these exist:

```
episodes/retrieval.py          3.1 KB   ← a stub
episodes/retrieval/            package, has __init__.py (3.7 KB)
    cache.py  enrich.py  fetch.py  sync.py  tvdb.py  validate.py
```

Python's `FileFinder` checks for a **regular package** (a directory containing
`__init__`) *before* it checks for a module of the same name. So:

```python
from scripts.managers.services.sonarr.episodes.retrieval import SonarrEpisodesRetrievalManager
```

resolves to **`retrieval/__init__.py`**, and `retrieval.py` is never imported.

The file's contents confirm which is which. `retrieval.py`'s only method is a
placeholder:

```python
def run_episode_data_pull(self, instance_name):
    self.logger.log_info(f"… 🧪 Would pull episode data for: {instance_name}")
    return True
```

…plus three commented-out debug lines. The package beside it has six real modules
with their own README and per-module `.md` files.

So the history is legible: `retrieval.py` was the original stub, the package
replaced it, and **the stub was never deleted**. Python silently shadows it, and
nothing — not an import error, not a linter, not the load summary — says so.

### 2.1 ✅ Confirmed, session 47 — and it is a shadowed **class**, not just a file

`episodes/retrieval/__init__.py` defines:

```python
class SonarrEpisodesRetrievalManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"
    ...
    self.components = self.load_components(component_map={
        "fetch", "enrich", "tvdb", "sync", "validate", "episode_cache",
    }, ...)
```

**The same class name as the shadowed `retrieval.py`.** So the two are not merely
a file and a package sharing a path segment — they are two different classes
called `SonarrEpisodesRetrievalManager`, one with six loaded subcomponents and one
whose sole method returns `True` after logging *"Would pull episode data"*.

The import in `episodes/__init__.py` succeeds and gets the real one. But anything
reaching the stub by file path — the way
[`labels/first_run.py`](../../../machine_learning/labels/DESIGN.md) §3.5 loads a
tool — would get a same-named class that does nothing, and the substitution would
be invisible at the call site.

That removes the last doubt from `GLD-EPI-01`: the package defines the symbol, so
the import resolves, so `retrieval.py` is definitively dead.

The live manager also uses `load_components` and has **no component summary** in
`prepare()`/`run()` — the same gap as `GLD-SER-01`, one level deeper.

This is §8 **P-E** with a failure mode not yet seen: not *"two implementations
that might drift"* but **"two implementations where one is unreachable and nothing
reports it."** `GLD-EPI-01`.

There are also two retrieval docs — `episodes/retrieval.md` (4.3 KB, documenting
the dead stub) and `episodes/retrieval/README.md` (5.5 KB, documenting the live
package).

---

## 3. 🔴 `split_components` silently drops components — documented in a workaround

```python
# sharding has parent_name="SonarrEpisodes" which doesn't match the
# split_components parent_name_match="SonarrEpisodesManager", so it is
# silently dropped from both dicts.  Load it explicitly here.
if not getattr(self, "sharding", None):
    try:
        self.sharding = SonarrEpisodesShardingManager(**init_args)
        self.logger.log_debug("🧩 SonarrEpisodesShardingManager loaded explicitly.")
```

`split_components` classifies non-critical components by matching each one's
`parent_name` against `parent_name_match`. **A mismatch drops the component from
both returned dicts** — no exception, no warning, no summary row.

Three things follow.

**It is a real bug, worked around rather than fixed.** The comment names the
cause precisely and then hand-loads the component. The next component with a
mismatched `parent_name` gets dropped silently until someone notices it missing.

**Nobody knows how many others are affected.** `critical_keys` here is
`{"retrieval"}`, so `file`, `history`, `monitoring` and `sharding` all pass
through the `parent_name` filter. Only `sharding` is named — implying the other
three match, but nothing verifies it. And in `sonarr/sync/`, `sonarr/quality/`
and `orchestration/quality.py`, `critical_keys` covers **every** component, so the
filter never runs there and the bug is invisible by construction.

**The `parent_name` convention it depends on is already inconsistent.**
[`series/DESIGN.md`](../series/DESIGN.md) §6 recorded `SonarrSeriesManager`
declaring `parent_name = "SonarrSeries"` as a class attribute and then overwriting
it with `self.__class__.__name__`. Components that set it one way and managers
that match the other are exactly what this filter mis-handles.

### 3.1 🎯 Session 47 — the convention is *conditionally* applied, which makes the filter arbitrary

Three managers now show the same two-step:

```python
class X(BaseManager, ...):
    parent_name = "SonarrSeries"              # class attribute: name WITHOUT "Manager"
    def __init__(...):
        self.parent_name = self.__class__.__name__   # instance: name WITH "Manager"
```

| Manager | Class attr | Instance value |
|---|---|---|
| `SonarrSeriesManager` | `"SonarrSeries"` | `"SonarrSeriesManager"` |
| `SonarrEpisodesRetrievalManager` | `"SonarrEpisodesRetrieval"` | `"SonarrEpisodesRetrievalManager"` |
| **`SonarrEpisodesShardingManager`** | `"SonarrEpisodes"` | **not overwritten** — per the workaround comment |

`split_components` constructs a temp instance and reads `temp_instance.parent_name`
(→ `GLD-SPLIT-01`), so it sees the **post-`__init__`** value. A component that
overwrites the attribute matches its manager; one that does not keeps the bare
class attribute and is **dropped**.

So which components survive the filter depends on **whether each one happens to
overwrite `parent_name` in its constructor** — an inconsistently-applied
convention that nothing enforces and no test covers.

That is a sharper statement of `GLD-SPLIT-02` than §3: the filter is not merely
silent, it matches on an attribute whose value is **accidental**. `sharding` is
not an unlucky special case; it is the one instance somebody noticed.

This joins `GLD-SPLIT-01` (double construction of non-criticals) as a second
defect in the same helper. `GLD-SPLIT-02`.

---

## 4. 🟡 The summary cannot show the component it hand-loads

```python
self.log_filtered_component_summary(
    critical_components=critical_components.keys(),      # {"retrieval"}
    noncritical_components=noncritical_components.keys(),# sharding is NOT here
    all_critical_loaded=self.all_components_loaded,
)
```

`sharding` was dropped from both dicts (§3) and then loaded explicitly — so it
appears in **neither** collection the summary reports on. The component that
required a documented workaround is the one the summary is structurally unable to
mention.

Same shape as [`GLD-SON-13`](../DESIGN.md) — a denominator built from a filtered
set rather than the declared one — now found at a third level of the tree.

---

## 5. 🟡 `all_components_loaded` counts one component in five

```python
self.all_components_loaded = len(critical_components) == len(critical_instances)
self.registry.set_flag("sonarr.episodes_manager_initialized", self.all_components_loaded)
```

`critical_keys = {"retrieval"}`, so this evaluates `1 == 1`. Every non-critical
failure — `file`, `history`, `monitoring`, `sharding` — leaves the flag `True`.

The non-critical loop does log a warning per failure, so it is not invisible. But
`sonarr.episodes_manager_initialized` asserts more than it checks, and anything
gating on that flag is reading a 1-in-5 sample.

---

## 6. What it gets right

**Criticals propagate.** No `try/except` around the critical construction, with
the reason stated: *"errors propagate (these are required)."* A missing
`retrieval` fails loudly rather than degrading into a silently empty pass —
correct for the component the whole subtree depends on.

**Non-criticals are isolated and named.** Each failure logs
`⚠️ Non-critical episode component '{name}' failed to initialize: {e}` and
continues.

**The `sharding` workaround is honest.** It states the cause, the mechanism and
the remedy in three lines. The bug is worked around, but it is not hidden — which
is how it was findable at all.

---

## 7. Smaller observations

| Observation | Note |
|---|---|
| `deletion.md` (6.4 KB) exists with **no `deletion.py`** | Not in `episodes/` or `episodes/retrieval/`. Either the module was removed, or the doc describes logic now living in `file.py` |
| `getattr(kwargs.get("manager", {}), "sonarr_cache", None)` | Fourth use of `{}` as a null-object stand-in |
| No test files | 70 KB in `episodes/` + 69 KB in `episodes/retrieval/`, zero tests — on one of the two live paths |
| `parent_name = "SonarrEpisodesManager"` class attr, then `self.parent_name = self.__class__.__name__` | Same value both ways here, unlike `series/` |

---

## 8. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-EPI-01` | 🔴 **Delete `episodes/retrieval.py`** — a stub whose only method logs *"Would pull episode data"*, **shadowed and unimportable** because `episodes/retrieval/` is a regular package. Delete `retrieval.md` with it *(P-E, silent-shadowing variant)* | S | — |
| `GLD-SPLIT-02` | 🔴 **`split_components` silently drops a component whose `parent_name` does not match** — no exception, no warning, absent from both returned dicts. Documented in an `episodes/__init__.py` workaround and hand-patched there; **nothing checks whether other components are affected**. Second defect in this helper after `GLD-SPLIT-01` | S | `GLD-SPLIT-01`, `GLD-SER-06` |
| `GLD-EPI-02` | **Resolve `deletion.md`** — 6.4 KB documenting a module that does not exist | S | — |
| `GLD-EPI-03` | 🟡 **The summary cannot report `sharding`** — dropped from both dicts, then hand-loaded, so it is in neither collection the summary reads. Third instance of a filtered denominator | S | `GLD-SON-13`, `GLD-ORCH-S03` |
| `GLD-EPI-04` | 🟡 **`sonarr.episodes_manager_initialized` reflects 1 component of 5** — `critical_keys = {"retrieval"}`, so the flag is `1 == 1` regardless of the other four | S | `GLD-EPI-03` |
| `GLD-EPI-05` | **Add tests** — ~140 KB across `episodes/` and `episodes/retrieval/`, zero test files, on one of the two paths `run()` invokes | M | — |
| `GLD-EPI-06` | **Audit every `parent_name` against its manager's `parent_name_match`** — the convention `GLD-SPLIT-02` depends on is applied **inconsistently**: some managers overwrite the class attribute in `__init__`, some don't, and `split_components` reads whichever value results. One pass covers every caller of the helper | S | `GLD-SPLIT-02` |
| `GLD-EPI-08` | 🟡 **`SonarrEpisodesRetrievalManager` has no component summary either** — `prepare()`/`run()` iterate `self.components` and log per-component with no roll-up. Second instance of `GLD-SER-01`'s gap, one level deeper, on the live path | S | `GLD-SER-01` |
| `GLD-EPI-07` | **Document `episodes/retrieval/`** — six modules (cache, enrich, fetch, sync, tvdb, validate), each with a `.md`, none with a `DESIGN.md` | M | — |

## 9. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Are `file`, `history` and `monitoring` surviving the `parent_name` filter, or are more components silently dropped? | `GLD-SPLIT-02` |
| Q2 | Does `deletion.md` describe logic now in `file.py`, or a removed module? | `GLD-EPI-02` |
| Q3 | Does anything read `sonarr.episodes_manager_initialized`? | `GLD-EPI-04` |
| Q4 | Are there other shadowed modules — a `.py` beside a package of the same name? | `GLD-EPI-01` |

**Q1 is the one to run first.** The `sharding` comment proves the filter drops
silently; it does not prove `sharding` was the only casualty. A single check —
does each component's `parent_name` match its manager's `parent_name_match`? —
covers every `split_components` caller in the repo, and `GLD-SPLIT-02` is
unfixable without it.

**Q4 generalises `GLD-EPI-01`.** One shadowed module was found by noticing a
directory listing; a `.py` and a package sharing a name is mechanically
detectable everywhere.

## 10. Related designs

- [`orchestration/DESIGN.md`](../orchestration/DESIGN.md) §2 — the caller
- [`series/DESIGN.md`](../series/DESIGN.md) §2, §6 — the third loading mechanism, and the `parent_name` inconsistency §3 depends on
- [`quality/DESIGN.md`](../quality/DESIGN.md) §3 — `GLD-SPLIT-01`, the first defect in the same helper
- [`sonarr/DESIGN.md`](../DESIGN.md) §12.3 — the filtered-denominator pattern §4 repeats

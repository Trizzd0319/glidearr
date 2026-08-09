# sonarr/sync — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [sonarr](../README.md) › **sync**

**Manager** — `SonarrSyncManager`
**Status** — 🔴 **Never constructed, and has no entrypoint if it were**
**Existing docs** — [`README.md`](./README.md) + a `.md` per module (`custom_formats`, `folders`, `media`, `naming`, `tags`)

> This document exists to record a single finding: **Sonarr configuration sync
> does not run.** The package is otherwise well documented — see the six existing
> `.md` files for what each module *would* do.

---

## 1. What this package is for

Push Glidearr's intended Sonarr configuration into every Sonarr instance:

| Module | Pushes |
|---|---|
| [`custom_formats.py`](./custom_formats.py) | Custom format definitions + scores |
| [`naming.py`](./naming.py) | `standardEpisodeFormat`, `dailyEpisodeFormat`, `animeEpisodeFormat` |
| [`folders.py`](./folders.py) | Root folder configuration |
| [`media.py`](./media.py) | Media-management settings |
| [`tags.py`](./tags.py) | Tag definitions |

Radarr has an equivalent, and **Radarr's runs.**

---

## 2. 🔴 The finding: two independent reasons it cannot run

### 2.1 It is never constructed

Established in [`sonarr/DESIGN.md`](../DESIGN.md) §12.1: `"sync"` is present in
`full_components` but absent from `component_dependencies`, so the
`all_component_classes` comprehension drops it. `SonarrManager` never builds it,
and `SonarrSyncManager.register()` — called only from its own `__init__` — never
runs, so a registry lookup by name also returns nothing.

### 2.2 It has no `run()` and no `prepare()`

Reading [`__init__.py`](./__init__.py) in full: `SonarrSyncManager` defines
**`__init__` and nothing else.** No `run`, no `prepare`, no `sync_all`.

This matters because `SonarrManager.run()` guards on capability:

```python
if component and hasattr(component, "run"):
```

So **adding `"sync"` to `component_dependencies` would not be sufficient.** It
would be constructed, its five children would be constructed, and then nothing
further would happen.

### 2.3 Construction alone does no work

The obvious counter-hypothesis is that the children sync during `__init__`.
[`naming.py`](./naming.py) settles it — `__init__` only wires dependencies and
logs:

```python
self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")
```

The actual work lives in a method that **must be called with a config argument**:

```python
def sync_naming_settings(self, naming_config):
    ...
    self.sonarr_api._make_request(instance, "config/naming", method="PUT", payload=config)
```

So the full chain is broken at three points: the manager is not built; if built it
exposes no entrypoint; and its children require an explicit call carrying the
source configuration.

---

## 3. What this changes about `GLD-SON-01`

The register framed the remedy as *"superseded (delete) or regressed (re-wire)."*
**Re-wiring is not a one-line change.** Reactivation requires:

1. adding `"sync"` to `component_dependencies`;
2. giving `SonarrSyncManager` a `run()` (or `prepare()`);
3. that `run()` obtaining the **source configuration** each child needs —
   `sync_naming_settings` takes a `naming_config`, so something must decide what
   the intended naming config *is*.

Point 3 is the substantive one. The children are appliers; nothing in this package
produces the configuration to apply. Radarr's equivalent must solve that, and
whatever solves it there is the model.

---

## 4. Residual uncertainty

**Not yet read:** [`sonarr/orchestration/`](../orchestration/). Q6 hypothesised
that orchestration — which *is* loaded — might invoke sync directly.

Two things make that unlikely but not impossible:

- `getattr(sonarr_manager, "sync")` returns the class-attribute default `None`,
  since `_load_component` can never build it.
- A registry lookup by name returns nothing, since `register()` never runs.

The remaining path is orchestration **importing a sync child directly and
constructing it**. That is unusual, and it is the one check that would overturn
this finding. `GLD-SYNC-01`.

---

## 5. Consequence if confirmed

Sonarr's custom formats, naming schemes, root folders, media-management settings
and tags are **not being maintained from Glidearr's configuration**, while
Radarr's are. Any drift — a format edited in the Sonarr UI, a score changed, a
naming token adjusted — persists indefinitely and silently.

Nothing detects it: [`sonarr/DESIGN.md`](../DESIGN.md) §12.3 shows the prepare
summary counts `8/8` and is structurally incapable of naming `sync`.

This is the largest single instance of §8 **P-A** in the register — not a computed
signal with no consumer, but **five appliers with no caller**.

---

## 6. Smaller observations

| Observation | Note |
|---|---|
| `parent_name = "SonarrStorage"` then immediately overwritten | Dead assignment in every sync child |
| `self.sonarr_api._make_request(...)` | Private method called across a package boundary — same as [`writeback/`](../../writeback/DESIGN.md) §3.7 |
| `for instance in instances:` | Multi-instance-ready, on a service documented as single-instance |
| `if self.dry_run: log "[DRY-RUN] Would apply…"` | Correctly gated — the sync path would honour dry-run if it ever ran |
| No tests in this package | 65.7 KB of source and documentation, zero test files |

The `dry_run` handling is worth noting: the code is *ready* to run safely. This is
not abandoned code, it is finished code that is not called.

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SYNC-01` | 🔴 **Check whether `orchestration/` constructs a sync child directly** — the one path that would overturn §2 | Settles Q6 definitively. Everything else here follows from the answer | S | — |
| `GLD-SYNC-02` | 🔴 **Decide: delete or reactivate** — and note that reactivation needs a `run()` **and** a source of the configuration to apply, not just a `component_dependencies` entry (§3) | Corrects `GLD-SON-01`'s stated remedy | M | `GLD-SYNC-01`, Q6 |
| `GLD-SYNC-03` | **Compare against Radarr's sync** — it runs, so whatever supplies its source configuration is the model for §3 point 3 | The design question is already solved once in this repo | S | `GLD-SYNC-02` |
| `GLD-SYNC-04` | **Report Sonarr config drift** — diff intended vs live custom formats / naming / tags, read-only | Would make §5 visible **without** deciding §2 first, and is useful even if sync stays off | M | `GLD-SYNC-01` |
| `GLD-SYNC-05` | **Remove the dead `parent_name = "SonarrStorage"` assignment** | Present in every child; misleading | S | — |
| `GLD-SYNC-06` | **Add tests, or delete** — 65.7 KB with zero test files | Whichever way `GLD-SYNC-02` goes, the current state is untenable | M | `GLD-SYNC-02` |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `orchestration/` construct a sync child directly? *(= Q6 from `sonarr/DESIGN.md`)* | `GLD-SYNC-01` |
| Q2 | Where does Radarr's sync get its source configuration? | `GLD-SYNC-03` |
| Q3 | Has Sonarr's configuration drifted from intent, and by how much? | `GLD-SYNC-04` |

**Q3 is answerable without resolving any of the above.** A read-only diff of
intended vs live Sonarr custom formats would say immediately whether §5 is a
theoretical concern or a live one — and it is the cheapest way to find out how
long this has been the case.

## 9. Related designs

- [`sonarr/DESIGN.md`](../DESIGN.md) §12 — the source verification this builds on
- [`README.md`](./README.md) and the five per-module `.md` files — what each applier does
- [`radarr/DESIGN.md`](../../radarr/DESIGN.md) — the equivalent that runs
- [`ENHANCEMENTS.md`](../../../../ENHANCEMENTS.md) §8 P-A — signal computed, never consumed

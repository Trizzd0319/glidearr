# ManagerAttributionMixin

- **File** — `scripts/managers/factories/mixins/mixins.py`
- **One-liner** — A mixin that back-fills a submanager's `parent_name` and `manager` links by inferring them from the class's module path on disk and looking the parent up in the registry.

> Note on the file: `mixins.py` also defines two helper mixins whose names do **not** end in `Manager` — `SonarrInstanceResolverMixin` (a `resolve_instance` passthrough requiring `self.instance_manager`) and `ProjectPathMixin` (project-root-relative `build_cache_path` / `build_log_path` / `build_support_path` helpers). They are out of scope for this manager-class documentation and are mentioned here only so the file isn't mistaken for empty. Only `ManagerAttributionMixin` is documented below.

## What it does (for a senior Python engineer)

`ManagerAttributionMixin` is a tiny stateless mixin (no `__init__`) that provides a single non-public helper, `_verify_parent_and_manager(self)`. Its job is to make sure a submanager knows two things about its place in the tree: a `parent_name` string and a live `manager` object reference. It exists as a fallback for cases where those attributes were not set during construction (e.g. a submanager constructed outside the normal `ComponentManagerMixin.load_components` injection, or before its parent was registered).

It performs none of FETCH / CACHE / APPLY, touches no external API, reads no config keys, and reads/writes no `global_cache` or Parquet keys. Its only effects are setting `self.parent_name` / `self.manager` and emitting warnings.

Key method:

- **`_verify_parent_and_manager(self)`** — Inference + lookup + validation in three steps:
  1. **Infer the parent name from the file path.** It calls `inspect.getfile(self.__class__)` and inspects the path parts. If `"services"` appears in the path, it builds the parent name from the two path segments after it: `<Service><Module>` (each `.capitalize()`-ed) — e.g. `.../services/sonarr/cache.py` → `"SonarrCache"`, or `.../services/sonarr/...` with only one trailing segment → `"Sonarr"`. If `"services"` is not in the path (or anything raises), it falls back to the class name with `"Manager"` stripped (e.g. `FooManager` → `"Foo"`).
  2. **Set `parent_name`** only if it is currently missing/falsy.
  3. **Set `manager`** only if currently missing/`None`, by looking the parent up in the registry: `self.registry.get("manager", self.parent_name)` (guarded by `hasattr(self, "registry")`).
  4. **Validate & warn.** If a logger is present, it logs `⚠️ <ClassName> missing 'manager' link.` and/or `⚠️ <ClassName> missing 'parent_name' attribute.` when either is still unset.

Concurrency/threading: none. No locks, no singleton logic (that lives in `BaseManager`). The method is idempotent — it never overwrites a `parent_name`/`manager` that is already set.

## How it functions

There is no lifecycle of its own. A host submanager calls `self._verify_parent_and_manager()` (typically late in its own init, after `logger`/`registry` are available) to repair its parent linkage. The control flow is purely synchronous: infer-from-path → conditionally set `parent_name` → conditionally resolve `manager` from the registry → warn on anything still missing.

The path-based inference is the only nontrivial helper logic, and it is deliberately defensive: the whole inference is wrapped in `try/except`, and any failure (e.g. `inspect.getfile` not resolving, an index out of range) collapses to the class-name-minus-`Manager` fallback rather than raising.

This mixin delegates no decision to any `machine_learning` brain module — it is structural plumbing only.

## Criteria & examples

- **Service-path inference, two trailing segments:** a class defined in `.../services/sonarr/cache.py` yields `parent_name = "SonarrCache"` (`sonarr`.capitalize() + `cache`.capitalize()).
- **Service-path inference, one trailing segment:** a class whose path ends `.../services/radarr/<file>` with no module segment after the service yields `parent_name = "Radarr"`.
- **Non-service fallback:** a class `TrendingScoreManager` outside any `services/` path yields `parent_name = "TrendingScore"` (the class name with `"Manager"` removed).
- **Idempotence:** if the submanager was already constructed with `parent_name = "SonarrCache"` and a non-`None` `manager`, calling `_verify_parent_and_manager()` changes nothing and logs nothing.
- **Failed registry lookup:** if `parent_name` infers to `"SonarrCache"` but `self.registry.get("manager", "SonarrCache")` returns `None` (parent not yet registered), `self.manager` stays unset and the method logs `⚠️ <ClassName> missing 'manager' link.`

## In plain English

Imagine a new crew member arrives on a film set without a name badge or a note saying which department they belong to. Rather than leave them adrift, this helper reads the door they walked in through — "ah, you came from the *Sonarr → cache* room, so you report to the SonarrCache department" — writes that on their badge, and points them to their supervisor on the staff list. If it still can't find a supervisor for them, it doesn't crash the production; it just flags "this person has no manager yet" so someone can sort it out. It only fills in blanks — if you already had a badge, it leaves it alone.

## Interactions

- **Hosts:** Sonarr/Radarr (and similar) submanagers that mix it in to self-repair their `parent_name`/`manager` links — complementary to, and a fallback for, the injection done by `ComponentManagerMixin.load_components` and the auto-link in `BaseManager`.
- **Registry (`RegistryManager`):** read-only via `registry.get("manager", parent_name)` to resolve the live parent object.
- **Logger (`LoggerManager`):** emits the two "missing link" warnings.
- **Sibling mixins in this file:** `SonarrInstanceResolverMixin` and `ProjectPathMixin` (not manager classes; noted above).
- **Brain modules:** none — no `machine_learning/` delegation.

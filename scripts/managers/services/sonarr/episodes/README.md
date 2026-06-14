# SonarrEpisodesManager

- **File** — `scripts/managers/services/sonarr/episodes/__init__.py`
- **One-liner** — The top-level orchestrator for everything episode-level under Sonarr; it loads and owns the six episode sub-managers (retrieval, file, history, monitoring, sharding, deletion) and exposes them as attributes on itself.

## What it does (for a senior Python engineer)

`SonarrEpisodesManager(BaseManager, ComponentManagerMixin)` is a coordinator with no business logic of its own — it constructs the episode sub-tree and wires shared dependencies into each child.

It defines no public "do work" methods. Its entire job happens in `__init__`:

- Calls `super().__init__(...)` then `self.register()` (self-registers in the registry "manager" category).
- Reads `dry_run` from `kwargs` (default `False`).
- Sets up a **dual-cache** pattern: keeps the injected `global_cache`, and resolves a Sonarr-specific cache as `self.sonarr_cache` from `kwargs["cache_manager"]` or the parent manager's `sonarr_cache`.
- Builds a single `init_args` dict (logger, config, global_cache, cache_manager, validator, registry, `manager=self`, `sonarr_api`, `instance_manager`, `dry_run`) and passes it identically to every child.

It does NOT use the standard `ComponentManagerMixin.load_components` path. Instead it builds a class map and splits it via `split_components` (from `scripts/support/utilities/managers/component_splitter.py`):

- `all_component_classes` = `{retrieval, file, history, monitoring, sharding, deletion}`.
- `critical_keys = {"retrieval"}` — retrieval is mandatory.
- `split_components(..., parent_name_match="SonarrEpisodesManager", ...)` partitions the map into critical vs non-critical.
- **Critical** components are instantiated directly; any exception propagates (hard failure).
- **Non-critical** components are instantiated in a try/except; a failure is logged as a warning and skipped (graceful degradation).

Each successfully-built child is attached as an attribute (`self.retrieval`, `self.file`, etc.).

**Known quirk handled explicitly:** `SonarrEpisodesShardingManager` declares `parent_name = "SonarrEpisodes"`, which does NOT match the `parent_name_match="SonarrEpisodesManager"` passed to `split_components`, so sharding is silently dropped from both partitions. The code compensates with a fallback block: if `self.sharding` is falsy after the split, it instantiates `SonarrEpisodesShardingManager(**init_args)` explicitly (logging a debug line), and sets `self.sharding = None` on failure. (Note: `file`, `history`, and `deletion` also declare `parent_name = "SonarrEpisodes"`, so the same name-mismatch likely affects them in the split — only `sharding` has an explicit fallback.)

**Completion flag:** `self.all_components_loaded = len(critical_components) == len(critical_instances)` (i.e. all criticals built). It writes this to the registry flag `sonarr.episodes_manager_initialized` and emits one filtered component-summary log line via `log_filtered_component_summary` (service `"Sonarr"`).

- Position in the tree: parent is the Sonarr service manager (`manager=self` is injected into children, so children's parent is this manager). Children: the six episode sub-managers below.
- FETCH / CACHE / APPLY: **none directly** — it is pure wiring; the verbs live in its children.
- API endpoints: none directly.
- Config keys: none read directly.
- global_cache / Parquet keys: none read/written directly.
- dry_run: stored and forwarded to all children via `init_args`; this manager mutates nothing itself.
- Singleton / concurrency: as a `BaseManager`, it is a process-wide singleton keyed by `(class, singleton_key)`. No threading here.

## How it functions

Lifecycle: `Main` (or the Sonarr service manager) constructs this manager → `__init__` resolves caches/deps → builds `init_args` → `split_components` partitions criticals vs non-criticals → criticals instantiated (errors fatal) → non-criticals instantiated (errors logged + skipped) → sharding fallback → completion flag + registry flag + summary log. After construction, callers reach episode functionality through the attached child attributes (e.g. `episodes.deletion.delete_old_episodes(...)`).

No machine_learning brain module is invoked here — this is structural wiring only.

## Criteria & examples

- **Critical vs non-critical rule:** only `retrieval` is critical. Example: if `SonarrEpisodesFileManager` raises during init, the warning `⚠️ Non-critical episode component 'file' failed to initialize: ...` is logged and the manager still finishes; but if `SonarrEpisodesRetrievalManager` raises, the exception propagates and construction fails.
- **`all_components_loaded` example:** with `critical_keys = {"retrieval"}`, if retrieval builds successfully then `len(critical_components) == len(critical_instances) == 1` → `all_components_loaded = True` and `sonarr.episodes_manager_initialized` is set `True`.

## In plain English

Think of this manager as the manager of a TV-archive department. It doesn't personally pull, sort, or delete any episodes — it just hires and equips six specialist teams (one fetches episode data, one inspects files, one reads history, one decides what to keep watching, one splits big jobs into batches, one throws old files out) and hands each team the same set of office keys (logger, config, caches, API access). One team — the "fetch the data" team — is essential; if they fail to show up, the whole department shuts down. The other five are nice-to-haves: if one can't start, it's noted and the rest carry on.

## Interactions

- **Parent:** the Sonarr service manager (passed `manager=self` into children).
- **Children (sibling submanagers to each other):** `SonarrEpisodesRetrievalManager` (critical), `SonarrEpisodesFileManager`, `SonarrEpisodesHistoryManager`, `SonarrEpisodesMonitoringManager`, `SonarrEpisodesShardingManager`, `SonarrEpisodesDeletionManager`.
- **Helpers:** `split_components` (component_splitter), `LoggerManager`, `BaseManager`/`ComponentManagerMixin`.
- **Brain modules:** none directly.

# SonarrEpisodesRetrievalManager

- **File** — `scripts/managers/services/sonarr/episodes/retrieval.py`
- **One-liner** — A thin, currently-stubbed retrieval manager that validates Sonarr instance API access at init and exposes a single placeholder "pull episode data" entry point.

> Note: there is a separate, fully-built `retrieval/` **subdirectory** (with `__init__.py`, `tvdb.py`, `sync.py`, `fetch.py`, `enrich.py`, `validate.py`, `cache.py`) that also defines a `SonarrEpisodesRetrievalManager`. That subdirectory is a separate work item and is NOT documented here. This `retrieval.py` flat file is the one actually imported by `episodes/__init__.py` (`from scripts.managers.services.sonarr.episodes.retrieval import SonarrEpisodesRetrievalManager`).

## What it does (for a senior Python engineer)

`SonarrEpisodesRetrievalManager(BaseManager, ComponentManagerMixin)` is the one **critical** child of `SonarrEpisodesManager`. In its current state it is mostly a connectivity check plus a stub.

`__init__`:
- Derives `self.parent_name` from `kwargs["parent_name"]` or `class_name.replace("Manager", "")` (i.e. `"SonarrEpisodesRetrieval"`).
- Calls `super().__init__(...)`.
- Resolves context: `self.logger` (or `manager.logger`), `self.manager`, `self.dry_run`, `self.cache_manager` (or `manager.cache_manager`), `self.instance_manager` (or `manager.instance_manager`).
- Raises `ValueError` if no logger is available — logger is mandatory.
- Logs parent-manager and `manager.episodes.instance_manager` debug detail when present.
- **Instance-aware API validation:** if `self.instance_manager` is missing, logs a warning; otherwise calls `self.instance_manager.get_all_sonarr_apis()` and logs the count and keys of the exposed Sonarr instances (or warns on failure).

Public method:
- `run_episode_data_pull(self, instance_name)` — currently a **stub**. It logs `🧪 Would pull episode data for: {instance_name}` and returns `True`. It does not actually fetch or cache anything yet.

- Position in the tree: parent is `SonarrEpisodesManager`; it loads no submanagers of its own.
- FETCH / CACHE / APPLY: none performed yet (the real pull is unimplemented in this file). It only *reads* the instance list via `instance_manager.get_all_sonarr_apis()` at init for validation.
- API endpoints: none called directly in this file.
- Config keys: none read directly.
- global_cache / Parquet keys: none read/written.
- dry_run: stored as `self.dry_run` but not yet used (the only method is a stub that logs regardless).
- Singleton / concurrency: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: constructed by `SonarrEpisodesManager` as a critical component → `__init__` resolves deps and validates that the instance manager can enumerate Sonarr APIs → ready. The sole entry method `run_episode_data_pull` is a no-op placeholder that logs intent and returns `True`.

No machine_learning brain module is invoked.

## Criteria & examples

- **Logger guard:** if neither an explicit `logger` nor `manager.logger` is available, `__init__` raises `ValueError("❌ Logger is required for SonarrEpisodeRetrievalManager")`.
- **Instance validation example:** with two configured Sonarr instances `{"sonarr_4k", "sonarr_hd"}`, init logs `✅ API exposes 2 Sonarr instances: ['sonarr_4k', 'sonarr_hd']`. If `instance_manager` is `None`, it instead logs `❌ Instance manager not provided.`
- **Stub behavior:** `run_episode_data_pull("sonarr_hd")` logs `🧪 Would pull episode data for: sonarr_hd` and returns `True` — no data is fetched.

## In plain English

This is the "go fetch the episode list" desk — except right now it's a desk with a sign on it that says "we would fetch the episodes here." When the department opens, this desk does one real thing: it phones around to confirm it can reach every Sonarr server (and announces how many it found). The actual fetching is wired up but not switched on in this file; the heavy lifting lives in the dedicated retrieval sub-package.

## Interactions

- **Parent:** `SonarrEpisodesManager` (this is its critical child).
- **Siblings:** the other five episode submanagers (file, history, monitoring, sharding, deletion).
- **Talks to:** `instance_manager` (`get_all_sonarr_apis()`) for the connectivity self-check.
- **Brain modules:** none.

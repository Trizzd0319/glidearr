# RadarrRepairManager

- **File** — `scripts/managers/services/radarr/repair/manager.py`
- **One-liner** — A thin helper manager that resolves which Radarr instance a movie *should* live in based on resolution, and (placeholder) relabels movies between instances.

## What it does (for a senior Python engineer)

`RadarrRepairManager` is a small `BaseManager` + `ComponentManagerMixin` helper. It is loaded by `RadarrRepairWrapperManager` under the key `manager`, and is also separately constructed inside `RadarrRepairInterfaceManager.__init__` as `self.repair`. Its `parent_name` is hard-coded to `"RadarrRepairWrapperManager"`.

It holds the shared deps resolved from kwargs-or-parent: `radarr_api`, `instance_manager`, `manager` (the parent), and `dry_run`.

- **FETCH / CACHE / APPLY.** None of the three are truly performed. The two public methods are explicitly documented in-code as placeholders — they only call `instance_manager.resolve_instance(...)` and log; no HTTP, no cache, no PUT/DELETE/POST. `relabel_series_instance` logs the intended move but the comment states "the actual logic to move or update the movies would go" here.
- **External API endpoints.** None.
- **Config keys.** None read directly.
- **global_cache / Parquet keys.** None.
- **dry_run.** Stored on `self.dry_run` but not branched on (the methods never mutate anything anyway).
- **Singleton / concurrency.** `BaseManager` singleton. Note `RadarrRepairInterfaceManager` constructs a *separate* `RadarrRepairManager` instance directly; whether that resolves to the same singleton depends on `BaseManager`'s singleton keying.

Public methods:

- `determine_correct_instance(file_path, resolution, genres) -> resolved instance` — Picks a target instance purely from the resolution string: contains `"2160"`/`"4k"` → the `"4k"` instance; `"1080"` → `"1080"`; `"720"` → `"720"`; otherwise logs a warning and defaults to the `"1080"` instance. `file_path` and `genres` are accepted but currently unused in the routing decision (placeholder). Returns whatever `instance_manager.resolve_instance(tier)` yields.
- `relabel_series_instance(current_instance, correct_instance, tvdb_id)` — Resolves both instance names and logs an intent to move the movie with the given (TVDB) id from one instance to the other. No actual move is performed yet (placeholder). Returns nothing.
- `_resolve_instance(instance)` (internal) — Standard resolution helper: prefers `instance_manager.resolve_instance`, falls back to `radarr_api.resolve_instance`, else `instance or "default"`.

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. There is no `load_components` call (it loads no children) and no `run()` method, so `RadarrRepairWrapperManager.run()` does not invoke it — it is used on demand by `RadarrRepairInterfaceManager`.

Internally it is two stub decision helpers plus the instance resolver. No `machine_learning` delegation.

Note: the docstrings/log strings mention "series"/"TVDB" (copy-paste residue from a Sonarr analogue); the actual subject is Radarr *movies*, and the methods only manipulate instance routing, not series.

## Criteria & examples

Resolution routing rule (string match on the resolution argument):

- resolution `"2160p"` (matches `"2160"`) → returns the resolved `"4k"` instance.
- resolution `"1080p"` → returns the resolved `"1080"` instance.
- resolution `"720p"` → returns the resolved `"720"` instance.
- resolution `"480p"` (matches none) → warns and returns the resolved `"1080"` instance as the default.

Example: `determine_correct_instance("/movies/Dune (2021)/Dune.2160p.mkv", "2160p", ["Sci-Fi"])` → the `4k` instance, regardless of file path or genres.

## In plain English

Imagine you keep your DVDs on two shelves: a "regular" shelf and a "4K Blu-ray" shelf. This helper looks at a disc's picture quality label and says "this one belongs on the 4K shelf." Right now it only *decides* the correct shelf and announces the move out loud — the part that actually carries the disc across the room hasn't been built yet (it's a placeholder). So today it's the brains of a librarian who points, but doesn't yet do the lifting.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `manager`); also directly instantiated by `RadarrRepairInterfaceManager`.
- **Sibling submanagers** — Used by `RadarrRepairInterfaceManager.repair_mismatched_instance` for its decision logic.
- **Brain modules** — None.
- **Other services** — `instance_manager` (`resolve_instance`); `radarr_api` only as a fallback resolver.

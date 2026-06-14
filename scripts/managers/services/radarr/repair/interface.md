# RadarrRepairInterfaceManager

- **File** — `scripts/managers/services/radarr/repair/interface.py`
- **One-liner** — Adapter that takes a media item's metadata, decides whether it sits in the wrong Radarr instance (by resolution/path/genre), and asks `RadarrRepairManager` to relabel it.

## What it does (for a senior Python engineer)

`RadarrRepairInterfaceManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `interface`. Its `parent_name` is hard-coded to `"RadarrRepairWrapperManager"`.

At init it resolves the usual deps (`radarr_api`, `instance_manager`, `manager`, `dry_run`) plus two optional ones (`metadata_manager`, `radarr_manager`) from kwargs-or-parent. It then **directly instantiates a `RadarrRepairManager`** and stores it as `self.repair` (passing logger, global_cache, config, radarr_api, instance_manager, and `manager=self.manager`).

- **FETCH / CACHE / APPLY.** No direct HTTP. It consumes a `metadata` dict handed in by a caller and routes a relabel decision into `self.repair` (which is itself a placeholder). So in practice no live API call, cache write, or mutation occurs from this class today.
- **External API endpoints.** None directly.
- **Config keys.** None read directly. It does call `self.repair.config.get_instance_by_path(file_path)` — i.e. it relies on the config object exposing a path→instance lookup.
- **global_cache / Parquet keys.** None.
- **dry_run.** Stored but not branched on (no mutation path exists).
- **Singleton / concurrency.** `BaseManager` singleton; no threads.

Public method:

- `repair_mismatched_instance(rating_key, metadata)` — The single entry point. It:
  1. Extracts `file_path`, `resolution` (lower-cased), and `genres` from the supplied `metadata` (drilling into `metadata["media_info"][0]`).
  2. Calls `self.repair.determine_correct_instance(...)` to get the *expected* instance.
  3. Calls `self.repair.config.get_instance_by_path(file_path)` to get the *actual* instance.
  4. Resolves both via `instance_manager.resolve_instance`.
  5. If both resolve and differ, logs a mismatch warning, extracts a `tvdb_id` (trying `tvdb_id`, `tvdbid`, then `tvdb.id`), and if present calls `self.repair.relabel_series_instance(current, correct, tvdb_id)`. If no id, logs that it can't resolve one.
  6. Otherwise logs that the file is correctly aligned.
  Returns nothing.

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log → construct `self.repair`. There is no `run()` method, so `RadarrRepairWrapperManager.run()` does not call it. It is invoked externally (driven by a metadata source, with `rating_key` suggesting a Plex/Tautulli-style trigger).

The mismatch detection is its only logic; the actual decision (expected instance) and action (relabel) are both delegated to `self.repair` (`RadarrRepairManager`), which is currently a placeholder. No `machine_learning` delegation.

Note: like its delegate, the in-code language ("series", "TVDB ID") is Sonarr-flavoured copy; the real subject is Radarr movies, identified here by a TVDB id pulled from the metadata.

## Criteria & examples

Guard: a relabel is attempted only when `resolved_actual` and `resolved_expected` are both truthy AND differ. Then a TVDB id must also be resolvable.

Example: metadata describes a 4K file (`video_full_resolution = "2160p"`) whose `file_path` `get_instance_by_path` maps to the `1080` instance. Expected instance resolves to `4k`, actual to `1080` → mismatch logged → if a `tvdb_id` is present, `relabel_series_instance(current="1080", correct="4k", tvdb_id=...)` is called. If the file path already maps to `4k`, the method logs "correctly aligned" and does nothing.

## In plain English

This is the quality-control inspector standing at the shelf. You hand it a movie's details ("4K picture, currently filed on the regular shelf"). It checks the picture quality against where the movie actually lives, and if they don't match it flags it and tells the librarian (`RadarrRepairManager`) "move The Matrix to the 4K shelf." If the movie is already in the right place, it just nods and moves on.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `interface`).
- **Sibling submanagers** — Constructs and uses its own `RadarrRepairManager` (`self.repair`) for the decision + relabel; optionally references `metadata_manager` and `radarr_manager` from the parent.
- **Brain modules** — None.
- **Other services** — `instance_manager` for instance resolution; the `config` object for `get_instance_by_path`.

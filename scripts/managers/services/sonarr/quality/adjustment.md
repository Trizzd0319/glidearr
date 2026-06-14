# SonarrQualityAdjustmentManager

- **File** — `scripts/managers/services/sonarr/quality/adjustment.py`
- **One-liner** — Reads/applies/removes Sonarr "quality adjustments" and, in a separate data-pull, computes a per-instance set of disk-pressure-aware quality "boosts/overrides" and caches them.

## What it does (for a senior Python engineer)

`SonarrQualityAdjustmentManager(BaseManager, ComponentManagerMixin)` is a small FETCH + APPLY + CACHE leaf submanager. It has two distinct concerns: (1) thin CRUD over a Sonarr `quality/adjustments` endpoint, and (2) a heuristic data-pull that synthesizes adjustment recommendations from disk free-space and caches them.

Key public methods:
- `list_adjustments()` → FETCH: `_make_request("quality/adjustments")`, returns the list (or `[]`), logs the count.
- `apply_adjustment(adjustment_id, value)` → APPLY: `PUT quality/adjustments/{adjustment_id}` with `{"value": value}`.
- `remove_adjustment(adjustment_id)` → APPLY: `DELETE quality/adjustments/{adjustment_id}`.
- `refresh_adjustment_cache()` → CACHE: calls `list_adjustments()` and `global_cache.set("sonarr.quality.adjustments", adjustments)`.
- `run_adjustment_data_pull()` → CACHE: for every Sonarr instance, reads mount-deduped disk free/total bytes, computes `percent_free`, builds heuristic `customFormatBoosts` / `cutoffOverrides` / `conditions`, and writes to global_cache.

Position in the tree: child of **SonarrQualityManager** (`self.parent_name` derived by stripping `"Manager"` → `"SonarrQualityAdjustment"`, then registry-resolves its parent). Loads no submanagers.

FETCH / CACHE / APPLY:
- FETCH: `_make_request("quality/adjustments")`; `sonarr_api.disk_free_bytes` / `disk_total_bytes`; `get_all_sonarr_apis`.
- CACHE: `refresh_adjustment_cache` and `run_adjustment_data_pull` write.
- APPLY: `apply_adjustment` (`PUT`), `remove_adjustment` (`DELETE`).

External API endpoints touched (Sonarr REST via `_make_request`):
- `quality/adjustments` (GET), `quality/adjustments/{id}` (`PUT`, `DELETE`). NOTE: these calls pass only the path with no instance argument, so they hit `sonarr_api`'s default instance.

Config keys read: none directly in this file. (`run_adjustment_data_pull` iterates instances via `sonarr_api.get_all_sonarr_apis()` rather than reading `sonarr_instances` from config.)

global_cache keys written:
- `refresh_adjustment_cache`: literal key `"sonarr.quality.adjustments"` via `global_cache.set`.
- `run_adjustment_data_pull`: `key_builder.format_cache_key(CacheKeyPaths.sonarr.ADJUSTMENTS, instance=instance_name)` via `set_with_pretty_output`. **WARNING — latent bug:** `CacheKeyPaths.sonarr` has **no** `ADJUSTMENTS` attribute (the class defines `QUALITY_PROFILES`, `CUSTOM_FORMATS`, etc., but not `ADJUSTMENTS`), so `run_adjustment_data_pull` would raise `AttributeError` at the cache-key step if invoked. Documenting as-is.

dry_run: `self.dry_run` is captured but **not consulted** in `apply_adjustment` / `remove_adjustment` — both mutate unconditionally. Flagging this as a dry_run gap (no "would ..." path).

Singleton / concurrency: standard `BaseManager` singleton; no threading.

## How it functions

Init mirrors the sibling submanagers: `parent_name` derivation, `BaseManager.__init__`, `register()`, then resolve `sonarr_api`, `logger`, `manager`, `dry_run`, `key_builder` from kwargs or the registry-resolved parent; raise if no logger; debug "Initialized" line.

There is no single `run()`. The two flows are independent:
- CRUD flow: `list_adjustments` → optionally `apply_adjustment` / `remove_adjustment` → `refresh_adjustment_cache` to re-snapshot.
- Heuristic flow: `run_adjustment_data_pull` loops `sonarr_api.get_all_sonarr_apis().items()`, computes `percent_free` (mount-deduped; unreadable disk → treated as `100.0`, i.e. "no space pressure"), derives the boost/override/condition dict, and caches it per instance.

No `machine_learning` brain module is invoked. The boost/override values here are hard-coded heuristics in this file, not a brain decision — note that this duplicates space/quality logic that elsewhere in the project is centralized (size model / space gating), but this file makes its own local determination.

## Criteria & examples

`run_adjustment_data_pull` thresholds (all hard-coded):
- `percent_free` = `round(free/total*100, 2)`, except `total in (0, inf)` or `free == inf` → `percent_free = 100.0`.
- `customFormatBoosts`:
  - `x265`: `15` if `percent_free < 10` else `0`.
  - `4K`: `-10` if `percent_free < 10` else `0`.
  - `HDR`: `5` if `percent_free > 20` else `-5`.
- `cutoffOverrides`: fixed `{"anime": "WebDL-1080p", "documentary": "HDTV-720p"}`.
- `conditions`: `{"spaceFreePercent": percent_free, "preferredCodec": "x265", "preferredContainer": "mkv"}`.

Worked example: an instance with 200 GB free of 4 TB total → `percent_free ≈ 4.88` (< 10). Boosts become `{x265: +15, 4K: -10, HDR: -5}` (HDR is −5 because 4.88 is not > 20) — i.e. under heavy disk pressure it nudges toward space-efficient x265 and away from bulky 4K/HDR. An instance with 2 TB free of 4 TB → `percent_free = 50.0` → `{x265: 0, 4K: 0, HDR: +5}`.

## In plain English

This component is like a thermostat for video quality based on how full your hard drive is. When the drive is getting full (less than 10% free), it leans toward space-saving choices — it rewards the efficient x265 video format and discourages giant 4K files. When there's plenty of room (more than 20% free), it's happy to allow fancier HDR. It also keeps a couple of fixed rules, like "anime should top out at 1080p web releases." It just writes down these recommendations (and can read/apply/remove Sonarr's own adjustment entries); think of it as leaving sticky notes about quality preferences, sized to how crowded the shelf is.

## Interactions

- **Parent manager:** `SonarrQualityManager`.
- **Sibling submanagers:** `SonarrQualityCustomFormatsManager`, `SonarrQualityFileSizesManager`, `SonarrQualitySelectorManager`.
- **Services it talks to:** the Sonarr API adapter (`sonarr_api`) for the `quality/adjustments` CRUD and for `disk_free_bytes`/`disk_total_bytes`/`get_all_sonarr_apis`; `global_cache` + `key_builder` for the two cache writes.
- **Brain modules:** none (the boost/override heuristics are local to this file).

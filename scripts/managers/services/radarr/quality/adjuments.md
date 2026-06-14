# RadarrQualityAdjustmentManager

**File** — `scripts/managers/services/radarr/quality/adjuments.py` (note: filename is misspelled "adjuments")
**One-liner** — Thin adapter over Radarr's `qualitydefinition` endpoint that lists, applies, and caches per-quality file-size boundaries (min/max MB-per-minute).

## What it does (for a senior Python engineer)

`RadarrQualityAdjustmentManager(BaseManager, ComponentManagerMixin)` is a thin service adapter for Radarr "quality definitions" — the table that sets the minimum and maximum allowed size (in MB/min) per quality level. It performs FETCH (GET qualitydefinition), APPLY (PUT a single definition), and CACHE (store the list in `global_cache`).

Public methods:
- `list_adjustments(instance) -> list` — FETCH. GETs `qualitydefinition` for the resolved instance; returns the raw list (fallback `[]`). Logs the count.
- `apply_adjustment(instance, quality_id, min_size, max_size) -> bool` — APPLY. PUTs `qualitydefinition/{quality_id}` with payload `{"id", "minSize", "maxSize"}`. Returns `bool(result)`. **Note:** this method does NOT short-circuit on `dry_run` — it issues the PUT regardless. (The other quality managers gate writes on dry_run; this one does not.)
- `refresh_adjustment_cache(instance) -> list` — FETCH + CACHE. Calls `list_adjustments`, stores the result at `global_cache` key `radarr.quality.adjustments.{resolved}`, returns it.
- `get_quality_adjustments(instance) -> list` — CACHE-first read. Returns the cached list if present; otherwise calls `refresh_adjustment_cache`.

Internal helper:
- `_resolve_instance(instance)` — delegates to `instance_manager.resolve_instance` or `radarr_api.resolve_instance`, else returns `instance or "default"`. (Identical pattern across all quality submanagers.)

- **Parent manager**: `RadarrQualityManager` (sets `parent_name = "RadarrQualityManager"`).
- **Submanagers loaded**: none (no `load_components` call).
- **External API endpoints**: `GET qualitydefinition`, `PUT qualitydefinition/{quality_id}`.
- **config keys read**: none.
- **global_cache keys**: writes/reads `radarr.quality.adjustments.{resolved}`.
- **dry_run**: resolved into `self.dry_run` from kwargs/parent, but NOT consulted by `apply_adjustment` — a notable inconsistency.
- **Singleton / concurrency**: standard `BaseManager` singleton; no threading.

## How it functions

`__init__` does the standard wiring (super, register, resolve radarr_api/instance_manager/dry_run from kwargs or parent) and logs a debug line. There is no `load_components` and no top-level `run()`. The methods are called ad hoc by whatever orchestrates quality-definition tuning. No `machine_learning` brain module is involved — these are raw read/write operations against Radarr's size table.

## Criteria & examples

The only "rule" is the cache-or-refresh fallback in `get_quality_adjustments`: if `global_cache.get("radarr.quality.adjustments.default")` is `None`, it fetches live and stores it; otherwise it returns the cached list as-is (no staleness check).

Worked example: calling `apply_adjustment("4k", quality_id=7, min_size=8.0, max_size=200.0)` resolves the "4k" instance, PUTs `qualitydefinition/7` with `{"id":7,"minSize":8.0,"maxSize":200.0}`, logs "Applied quality definition 7 → min=8.0 max=200.0", and returns `True` if Radarr accepted it.

## In plain English

Radarr has a built-in "rulebook" that says, for each picture quality, how big a one-minute chunk of video is allowed to be — like a librarian's rule that a 1080p film should be between X and Y megabytes per minute. This manager just reads that rulebook, can rewrite one rule, and keeps a photocopy (cache) so it doesn't have to ask Radarr every time. For example, it can tell Radarr "for 4K, accept files up to 200 MB per minute" so a 90-minute 4K Princess Bride is allowed to be large and detailed.

## Interactions

- **Parent**: `RadarrQualityManager`.
- **Siblings**: `RadarrCustomFormatsManager`, `RadarrFileSizesManager`, `RadarrQualitySelectorManager`, `RadarrSpacePressureManager`, `RadarrQualityUniverseManager`.
- **Services**: `radarr_api` (HTTP), `instance_manager` / `radarr_api.resolve_instance` (instance name resolution), `global_cache` (caching).
- **Brain modules**: none.

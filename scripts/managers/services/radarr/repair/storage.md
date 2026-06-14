# RadarrRepairStorageManager

- **File** — `scripts/managers/services/radarr/repair/storage.py`
- **One-liner** — Storage health checks for Radarr: classifies each root folder's free space (ok/warn/critical), recommends deletion candidates when space runs low, and lists oversized movies for manual review.

## What it does (for a senior Python engineer)

`RadarrRepairStorageManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `storage`. `parent_name` derives from the class name (`RadarrRepairStorage`). Deps `radarr_api`, `instance_manager` from kwargs-or-parent.

Module fallback constants (used only when `free_space_limit` is unconfigured): `DEFAULT_WARN_GB = 50.0`, `DEFAULT_CRIT_GB = 20.0`, `DEFAULT_LARGE_GB = 30.0`. Per-instance overrides come from kwargs: `warn_threshold_gb`, `crit_threshold_gb`, `large_movie_gb`.

- **dry_run resolution is unusually strict.** It tries `kwargs["dry_run"]` → `parent.dry_run` → registry `RadarrManager.dry_run` → registry `Main.dry_run`, and **raises `ValueError` if all are None**, refusing to initialize without an explicit value — a guard against accidental destructive operations.
- **FETCH / CACHE / APPLY.**
  - FETCH: `GET rootfolder` (free/total bytes), `GET movie` (sizes, ratings, monitored).
  - APPLY: **none** — this manager only *recommends*; it never deletes. (Actual destructive deletion lives in the anomaly manager / space-pressure path.)
  - No CACHE.
- **External API endpoints:** `rootfolder` (GET), `movie` (GET).
- **Config keys.** `free_space_limit` (read in `run()` to set the deletion target, and inside `space_targets`); `large_file_gb` (read in `find_large_movies` as the size threshold). Also consumes `space_targets(self.config, …)`.
- **global_cache / Parquet keys.** None.
- **dry_run.** Stored (and required), but since there is no APPLY path it does not gate any mutation here.
- **Singleton / concurrency.** `BaseManager` singleton; no threads.

Public methods:

- `check_free_space(instance) -> list[dict]` — FETCH. For each root folder returns `{path, free_space_gb, total_space_gb, status}`. Status is computed against `space_targets`: critical below the floor `T`, warn inside the pressure band `[T, U)`, ok at/above `U`. `T = free_space_limit` or 25%-of-total (via `instance_manager.disk_total_gb`) when unset; the legacy `DEFAULT_CRIT/WARN_GB` survive only as last resort when both are unreadable. If the band collapses (`warn <= crit`), it widens warn up to `warn_threshold_gb` to keep three tiers.
- `recommend_deletions(instance, target_free_gb=100.0, limit=20) -> list[dict]` — Computes `needed_gb = max(0, target_free_gb - lowest_free)` across folders; if ≤ 0 returns `[]`. Otherwise builds candidates from movies with files and sorts by: (1) unmonitored first, (2) imdb_rating ascending, (3) size descending. Returns the top `limit` as `{movie_id, title, year, size_gb, imdb_rating, monitored, reason}` where `reason` is `"unmonitored"` or `"low_rating"`. Recommendation only — no deletion.
- `find_large_movies(instance, threshold_gb=None) -> list[dict]` — Returns movies with files at/above the threshold (`threshold_gb` arg, else config `large_file_gb`, else `large_movie_gb`), sorted size-descending: `{movie_id, title, year, size_gb, path}`.
- `run(instance) -> dict` — The pass invoked by the wrapper. Reads `free_space_limit` as the deletion `target_free_gb` (falling back to 100 GB only when unset/≤0). Returns `{"free_space": [...], "deletion_candidates": [...], "large_movies": [...]}`. Purely diagnostic — recommends, never deletes.

Internal helpers: `_resolve_instance`, `_fmt_bytes` (human-readable byte formatter, NaN-safe).

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → strict dry_run resolution (raise if unresolved) → set thresholds → debug log. No children loaded.

`run()` ties free-space classification to the configured floor: it honours `free_space_limit` as the target, then surfaces deletion *candidates* and the largest movies. The actual space-pressure deletion is a different code path (the anomaly manager's `demote_stale_monitored` and the cross-service coordinator); this manager intentionally stops at advice. The free-space tiering delegates to `support/utilities/space_targets.space_targets` (a utility, not a `machine_learning` brain module). No `machine_learning` delegation.

There is a companion test file `test_storage_floor.py` (out of scope here) exercising the floor logic.

## Criteria & examples

- **Free-space tiering with `free_space_limit=200 GB` (so T≈200, U≈220):** a folder with 150 GB free → `critical` (< T); 210 GB free → `warn` (in `[T, U)`); 300 GB free → `ok` (≥ U).
- **Fallback tiering (no `free_space_limit`, total unknown):** falls to `DEFAULT_CRIT_GB=20`, warn widened to `DEFAULT_WARN_GB=50` — 15 GB free → critical, 40 GB → warn, 80 GB → ok.
- **Deletion need:** `target_free_gb=100`, lowest folder free `60 GB` → `needed_gb=40` → candidates returned. With `120 GB` free → `needed_gb=0` → empty list.
- **Deletion ranking:** an unmonitored movie sorts before any monitored one regardless of rating; among monitored, a film with imdb 5.1 ranks ahead of one at 7.8; ties by size descending (delete the bigger one first).
- **Large-movie flag with `large_file_gb=40`:** a 52 GB remux → listed; a 12 GB encode → ignored.

## In plain English

This is the shelf-space watchdog. It checks how much room is left on each drive and colour-codes it green / yellow / red. If you're running low, it draws up a "consider removing these" list — putting the movies you've stopped tracking first, then your lowest-rated films, and among those the biggest space hogs at the top (so deleting one fat 4K copy of a movie nobody rates frees the most room). Importantly, it only *suggests* — it never actually deletes anything; that decision belongs to other parts of the system. It also keeps a separate "these files are huge" list for you to eyeball.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `storage`); falls back to `RadarrManager`/`Main` in the registry to resolve `dry_run`.
- **Sibling submanagers** — Provides advice that overlaps with the actual deletion done by `RadarrRepairAnomalyManager` (space-pressure prune).
- **Brain modules** — None. (Uses the `space_targets` utility, which is shared infrastructure, not the ML brain.)
- **Other services** — `radarr_api` (rootfolder, movie); `instance_manager` (`resolve_instance`, `disk_total_gb`); `config` (`free_space_limit`, `large_file_gb`).

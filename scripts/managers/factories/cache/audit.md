# CacheAuditManager

- **File** — `scripts/managers/factories/cache/audit.py`
- **One-liner** — A disk-cache inspector/janitor held by `GlobalCacheManager`; enumerates and reports on cache files, and (with explicit confirmation) wipes them all.

## What it does (for a senior Python engineer)

`CacheAuditManager` is a plain helper (not a `BaseManager`, not a singleton) owned by `GlobalCacheManager` as `self.audit`. It operates over the resolved cache root (`base_dir`, defaulting to `support/cache`) and walks it recursively.

### Key public methods

- `summarize(extensions=(".json", ".json.gz", ".parquet", ".csv", ".last_updated"))` — `rglob("*")` the base dir, and for each file whose suffix is in `extensions`, log a line with its relative path, size in KB, and ISO last-modified time. Logs running totals (file count and total bytes/KB) at the end, or "no matching files."
  - Caveat: the filter uses `path.suffix in extensions`, and `Path.suffix` for `foo.json.gz` is `".gz"`, so `.json.gz` files are not actually matched by the default tuple. Flagging this rather than asserting it works.
- `delete_all(confirm=False)` — a guarded full wipe. If `confirm` is not `True`, it logs an abort warning and does nothing. With `confirm=True`, it `rglob`s and `unlink`s **every file** under `base_dir` (note: *all* files, not just the audited extensions), counting successes and warning per failure, then logs the deleted count.

### FETCH / CACHE / APPLY

CACHE-tier maintenance. No HTTP, no external API, no config keys. The only mutating operation is `delete_all`, which is local-filesystem destruction gated behind the explicit `confirm` flag.

- **dry_run:** there is no `dry_run` hook here — the guard is the `confirm` argument, not the global `dry_run` config. A caller wanting a no-op should simply not pass `confirm=True`.
- **Concurrency:** no locking; a sequential walk/unlink.

## How it functions

No lifecycle beyond `__init__` (logger + resolved `base_dir`). Both methods short-circuit safely if the base directory is missing (`summarize`) or confirmation is absent (`delete_all`). It delegates no decision to any `machine_learning` brain module — what counts as "stale enough to delete" is entirely the caller's call; this class only reports and, on demand, deletes everything.

## Criteria & examples

- **Confirmation guard.** `delete_all()` (default `confirm=False`) logs "Deletion aborted — confirmation flag not set" and leaves the cache untouched. Only `delete_all(confirm=True)` actually unlinks files. This is the single safety guard in the file.
- **Audit report.** Over a cache holding `radarr/main/library.json` (50 KB) and `radarr/main/library_movies_enriched.parquet` (1,200 KB), `summarize()` logs one line per file plus "Found 2 cache files totaling 1250.0 KB." A `.json.gz` artifact in the same tree would be silently omitted from the count due to the suffix-matching caveat above.

## In plain English

This is the person who takes inventory of the fridge — "here's everything in here, how big each item is, and when it was last touched" — and, only if you explicitly say "yes, throw it all out," empties the entire fridge. It will never wipe things just because it was asked to look; the dramatic clean-out needs a deliberate confirmation, like having to type "DELETE" before a website erases your account.

## Interactions

- **Parent:** `GlobalCacheManager` (`self.audit`).
- **Collaborator:** `LoggerManager`, the filesystem under `base_dir`.
- **Brain modules:** none.

# CacheParquetManager

- **File** — `scripts/managers/factories/cache/parquet_handler.py`
- **One-liner** — The DataFrame persistence worker behind `GlobalCacheManager`; saves/loads pandas DataFrames as Parquet, falling back to CSV when the Parquet engine is missing or fails.

## What it does (for a senior Python engineer)

`CacheParquetManager` is a plain helper (not a `BaseManager`, not a singleton) held by `GlobalCacheManager` as `self.parquet_handler`. It is the storage backend for the enriched library DataFrames (`*_series_enriched.parquet`, `*_movies_enriched.parquet`, etc.) that the scoring/ML layer consumes.

It reads its engine settings from `FallbackSettings` in `constants.py`: `default_engine = "pyarrow"` and `supported_engines = ["pyarrow", "fastparquet"]`.

### Key public methods

- `save_dataframe(path, df, fallback_to_csv=True, index=False, engine=None)` — make the parent dir, then `df.to_parquet(path, index=index, engine=engine or "pyarrow")`. On `ImportError` (engine not installed) or any other exception, if `fallback_to_csv` it writes `path.with_suffix(".csv")` via `_save_as_csv`; otherwise returns `False`. Returns `True`/`False`.
- `load_dataframe(path, fallback_csv=True, engine=None)` — `pd.read_parquet`; on `FileNotFoundError` or any other error, optionally tries `_load_fallback_csv(path.with_suffix(".csv"))`. Returns the DataFrame or `None`.

Internal helpers `_save_as_csv` / `_load_fallback_csv` mirror the above with `to_csv`/`read_csv` and their own error handling.

### FETCH / CACHE / APPLY

Pure **CACHE**. No HTTP, no external API, no config keys (only the `FallbackSettings` constants). Paths are supplied by the caller (`GlobalCacheManager` builds them via `key_builder.build_parquet_path`).

- **Parquet keys/paths:** the caller passes a fully resolved `Path`; in practice these are `support/cache/{service}/{instance}/library<EnrichedSuffix>.parquet`.
- **dry_run:** not applicable — local writes always occur.
- **Concurrency:** no locking.

## How it functions

No lifecycle beyond `__init__` (logger + engine constants). Both public methods are defensive: the happy path is Parquet, and every failure mode (engine absent, write error, file missing, read error) is caught, logged at warning level, and either retried as CSV or surfaced as a `False`/`None` rather than an exception. It delegates no decision to a `machine_learning` brain module — it is dumb storage.

## Criteria & examples

- **Missing engine → CSV.** On a host where neither `pyarrow` nor `fastparquet` is installed, `save_dataframe(Path(".../library_series_enriched.parquet"), df)` hits `ImportError`, logs a warning, and instead writes `.../library_series_enriched.csv`, returning `True`. A later `load_dataframe(same .parquet path)` will fail the Parquet read, then `_load_fallback_csv` picks up the `.csv` sibling.
- **Clean save.** With pyarrow present, the same call writes the `.parquet` file (no index column, since `index=False`) and returns `True`.
- **Hard miss.** `load_dataframe(path)` for a path with neither a `.parquet` nor a `.csv` returns `None`; the caller (e.g. `get_delta_diff`) then treats all rows as new.

## In plain English

Spreadsheets — the big tables describing every show and movie with all their computed scores — are stored in a compact binary format (Parquet) that loads fast. If the machine is missing the tool that reads that format, this worker doesn't give up; it writes a plain CSV instead, the same data in a clunkier box, so nothing is lost. Think of saving a film in 4K when you can, but dropping to DVD quality automatically on a player that can't do 4K, rather than showing a blank screen.

## Interactions

- **Parent:** `GlobalCacheManager` (`self.parquet_handler`); called by `save_enriched_dataframe` / `load_enriched_dataframe` / `get_delta_diff`.
- **Collaborator:** `FallbackSettings` (`constants.py`), pandas.
- **Brain modules:** none directly — the enriched DataFrames it stores are the *input* the `machine_learning/` scoring layer reads, but this class makes no judgements.

# SonarrRepairMetadataManager

**File** — `scripts/managers/services/sonarr/repair/metadata.py`
**One-liner** — Scans a provided list of series for incomplete core metadata (tvdbId, title, year) and computes placeholder-patched values, returning the proposed fixes (it does not write them back).

## What it does (for a senior Python engineer)

`SonarrRepairMetadataManager(BaseManager, ComponentManagerMixin)` is the smallest leaf repair sub-manager under `SonarrRepairManager`. It is a **pure compute/report** helper: it neither FETCHes from the API nor APPLYs — it processes an in-memory `series_list` argument and returns proposed patches.

- **Parent:** `self.parent_name = "SonarrRepair"`. Constructed by `SonarrRepairManager` (non-critical).
- **Deps:** only the standard injected ones (logger/config/global_cache/validator/registry). It does **not** capture a `sonarr_api`, `instance_manager`, or `dry_run` — it relies entirely on its method argument.
- **Loads submanagers:** none.

Public method:

- **`repair_missing_metadata(series_list)`** — iterates the provided list of series dicts. For any series missing `tvdbId`, `title`, or `year`, it builds a `fixed` dict using fallbacks (`tvdbId` → `999999`, `title` → `"Unknown Title"`, `year` → `2000`) and appends `(series_id, fixed)` to `repaired`. Complete series are logged as intact. Returns the `repaired` list of `(id, patch)` tuples.

- API endpoints touched: none.
- Config keys read: none. global_cache keys: none.
- FETCH / CACHE / APPLY: none of the three — it only computes proposed patches in memory.
- dry_run: not present; the method is inherently non-mutating (the caller decides whether to apply the returned patches).
- Singleton/threading: standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` sets `parent_name`, calls `super().__init__`, `self.register()`, logs an init line — that's all. `repair_missing_metadata` is a single loop that classifies each series as intact or in need of patching and accumulates the patch proposals. The placeholder values are hard-coded defaults, not derived from any external lookup, and the patches are returned rather than persisted — so a caller is responsible for sending them to Sonarr. No `machine_learning` brain module is involved.

## Criteria & examples

- **Patch trigger:** `not series.get("tvdbId") or not series.get("title") or not series.get("year")`. Example: `{"id": 77, "tvdbId": null, "title": "Chernobyl", "year": 2019}` → patched to `{"tvdbId": 999999, "title": "Chernobyl", "year": 2019}` and appended as `(77, {...})`. The real title/year are preserved; only the missing field gets the placeholder.
- **Intact:** `{"id": 78, "tvdbId": 81189, "title": "Breaking Bad", "year": 2008}` is left alone (logged as intact, not added to `repaired`).

## In plain English

Think of each show as a library index card that should list its catalog number, its name, and its year. This specialist flips through a stack of cards you hand it and, for any card with a blank where one of those three should be, fills the blank with a stand-in ("Unknown Title", year 2000, a dummy catalog number). It doesn't refile the cards itself — it just hands back the list of "here's what I'd write in" so someone else can decide to commit it. It's a quick form-filler, not a researcher: it doesn't go look up the real missing values.

## Interactions

- **Parent manager:** `SonarrRepairManager`.
- **Siblings:** the other `SonarrRepair*Manager` specialists (a caller would supply the series list and decide whether to push the returned patches via the Sonarr API).
- **Services:** none directly.
- **Brain modules:** none.

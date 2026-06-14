# SonarrStorageDeletionManager

**File** — `scripts/managers/services/sonarr/storage/deletion.py`
**One-liner** — The destructive specialist: deletes Sonarr episode files (expired, duplicate, or by id) via the Sonarr API, with hard pilot protection and a strict dry-run safety gate.

## What it does (for a senior Python engineer)

`SonarrStorageDeletionManager(BaseManager, ComponentManagerMixin)` is the only storage child that issues `DELETE`s against Sonarr. It is the APPLY surface for episode-file removal.

Key public methods:
- `delete_episodes_older_than(instance, days=30, episode_ids=None, season_episode_pairs=None)` — fetches `episodefile` for the resolved instance, computes `cutoff = now - days`, and for each file parses `dateAdded` (ISO, `Z` stripped). Optional `episode_ids` / `season_episode_pairs` filters restrict the candidate set. Files added before the cutoff are deleted (`DELETE episodefile/<id>`) unless dry_run, in which case it logs a "[DRY-RUN] Would delete" line. Pilots (S01E01) are always skipped. Progress shown via `tqdm` to stderr. APPLY (DELETE).
- `delete_duplicate_episodes(instance)` — fetches `episodefile`, groups by `(seriesId, seasonNumber, episodeNumber)`, and for any group with >1 file sorts by `quality.quality.id` descending and deletes all but the best. Pilots are skipped. APPLY (DELETE).
- `delete_episode_by_id(instance, episode_id)` — direct `DELETE episodefile/<episode_id>` (or dry-run log). The low-level primitive used by relocation/library sweeps to drop a file from a wrong instance. APPLY (DELETE).

There is a commented-out stub `# def get_deletion_threshold_for_series(...)` — not implemented.

Position in tree: child of `SonarrStorageManager` (registry parent name `"SonarrStorage"`). Loads no submanagers.

FETCH / CACHE / APPLY: FETCH (`episodefile` lists) + **APPLY** (`DELETE episodefile/<id>`). No caching of its own (it has both `global_cache` and `sonarr_cache` handles but uses neither for reads/writes in the current methods).

Config keys read: none directly (instance resolution is delegated to `self.manager.resolve_instance`).
Cache keys: none read/written here.
API endpoints: Sonarr `GET episodefile`, `DELETE episodefile/<id>`.
dry_run: **hardened** — `__init__` resolves `dry_run` from `kwargs`, then the parent manager, then registry `SonarrManager`, then registry `Main`; if it is still `None` after all four it **raises** `ValueError` and refuses to initialize, explicitly to avoid accidental destructive operations with an unknown flag. When `dry_run` is true, every delete becomes a "would delete" log line and nothing is removed.
Singleton/concurrency: standard BaseManager singleton.

## How it functions

`__init__` derives `parent_name` from the class name, calls `super().__init__` + `register()`, looks up the parent, and back-fills `sonarr_api`, `logger`, `manager`, both caches, and — most importantly — the `dry_run` flag through the four-tier fallback chain described above. It also raises if no logger is available.

Control flow per operation is a fetch-filter-delete loop:
1. Resolve the instance name.
2. `GET episodefile` for the whole instance.
3. Filter by age / duplication / explicit id / pilot guard.
4. For each survivor: if `dry_run` → log; else `DELETE episodefile/<id>` and increment the counter.
5. Log a completion summary with the deleted count.

No `machine_learning` brain module is invoked from this file. (In the wider ML migration, "what should be deleted" is intended to be decided in `machine_learning/`; this class is the mechanical executor — note that callers like the library manager's floor-enforcement pass it hard-coded thresholds such as `days=90`.)

## Criteria & examples

- Age threshold (default 30 days): an episode file with `dateAdded` 31 days ago, not a pilot, with no id/pair filters → deleted (or "would delete" in dry-run). One added 29 days ago is kept.
- Pilot guard: S01E01 is **never** deleted by `delete_episodes_older_than` or `delete_duplicate_episodes`, regardless of age or duplication — the code logs "Skipping pilot episode (S01E01)". (Worked example: a duplicate S01E01 group of two files is left untouched; a duplicate S02E05 group of two files keeps the higher `quality.id` and deletes the other.)
- Duplicate resolution: group `[{quality.id: 7}, {quality.id: 4}]` for S03E02 → sorted desc → keeps `id 7`, deletes `id 4`.
- dry_run gate: if config dry_run is true, `delete_episode_by_id(instance, 512)` logs `[DRY-RUN] Would delete episode ID 512` and issues no HTTP DELETE.
- Init refusal: constructing this manager with no resolvable `dry_run` anywhere (kwargs, parent, SonarrManager, Main) raises `ValueError` rather than guessing `False`.

## In plain English

This is the shredder operator with a very strict supervisor. It's the one employee actually allowed to throw episodes away — old ones gathering dust (default: anything older than a month), or duplicate copies where it keeps the nicest-quality version and bins the rest. But two rules are absolute: it will *never* shred a show's very first episode (the pilot — imagine refusing to throw out Episode 1 of *Friends* even if everything else is gone), and if nobody has clearly told it "this is for real" versus "we're just rehearsing" (dry-run), it downs tools and refuses to start rather than risk shredding the wrong thing.

## Interactions

- **Parent:** `SonarrStorageManager`.
- **Siblings:** invoked by `SonarrStorageLibraryManager.enforce_free_space_floor` (calls `delete_episodes_older_than(days=90)` and `delete_duplicate_episodes`) and by both library/relocation relocation sweeps (`delete_episode_by_id` to drop a wrong-instance copy).
- **Services touched:** Sonarr HTTP API (`episodefile` GET/DELETE).
- **Brain modules:** none directly (it is the executor; deletion *policy* is intended to live in `machine_learning/`).

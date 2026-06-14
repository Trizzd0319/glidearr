# RadarrStorageRelocationManager

- **File** — `scripts/managers/services/radarr/storage/relocation.py`
- **One-liner** — Decides which Radarr instance a movie *should* live on (by resolution + free space) and lists mis-placed movies — but the actual cross-instance file move is a logged stub, not implemented.

## What it does (for a senior Python engineer)

`RadarrStorageRelocationManager(BaseManager, ComponentManagerMixin)` is the `relocation` submanager under `RadarrStorageManager`. The module docstring is explicit that real cross-instance moves (coordinating two Radarr APIs, filesystem access, re-import sequencing) are NOT done here — `relocate_movie` only logs intent.

Key PUBLIC methods:
- `relocate_movie(movie_id, from_instance, to_instance) -> dict`. Resolves both instance names and logs the intended move. If `self.dry_run` it returns `{... "status": "dry_run"}`. Otherwise (non-dry-run) it STILL performs no filesystem op and returns `{... "status": "stub_not_implemented"}`. The docstring documents the intended 4-step real flow (fetch record from source, `POST /movie` to dest, trigger disk scan/import on dest, remove from source) but none of it runs.
- `determine_target_instance(movie, free_space_map) -> str | None`. The real decision logic. Reads the movie's nested quality resolution (`movie["movieFile"]["quality"]["quality"]["resolution"]`, coerced to int, default 0) and maps `>=2160 → "4k"`, `>=1080 → "1080"`, `>=720 → "720"`, else `None`. Resolves the preferred instance name; if that instance has `>= MIN_FREE_GB (10.0)` free in `free_space_map`, returns it. Otherwise falls back to the instance with the most free space **if** that also clears 10 GB. Returns `None` (with a warning) if nothing qualifies or the map is empty.
- `get_relocation_candidates(instance) -> list[dict]`. FETCH `radarr_api._make_request(instance, "movie", fallback=[])`; for each movie with `hasFile`, calls `determine_target_instance(movie, free_space_map)` and, when the suggestion differs from the current instance, appends `{movie_id, title, year, resolution, current_instance, suggested_instance}`.
- `_resolve_instance(instance)` — `instance_manager` → `radarr_api` → literal/`"default"`.

FETCH/CACHE/APPLY: **FETCH** only (`GET movie` in `get_relocation_candidates`). No CACHE. APPLY is intentionally a **stub** (`relocate_movie` logs but never mutates). dry_run is honored in `relocate_movie` (returns a `"dry_run"` status), though since the non-dry path is also a no-op the practical effect is the same either way.

> **Accuracy note:** in `get_relocation_candidates`, `free_space_map` is initialized to `{}` and never populated before being passed to `determine_target_instance`. With an empty map, the preferred-instance check (`free_space_map.get(...) >= 10.0`) always fails and the fallback `max(...)` branch is skipped, so `determine_target_instance` returns `None` for every movie → the method effectively always returns `[]`. This appears to be incomplete wiring, consistent with the file's "stub" framing.

- External API endpoints: `GET movie`. (Stub docstring references `POST /movie` and a disk-scan/import command, none of which are invoked.)
- Config keys: none read directly.
- global_cache / Parquet keys: none.
- Singleton/concurrency: BaseManager singleton; self-registers; auto-links parent. Note `parent_name` is overwritten in `__init__` to `self.__class__.__name__.replace("Manager","")` = `"RadarrStorageRelocation"` (the class attribute `parent_name = "RadarrStorageManager"` is the pre-init default).

## How it functions

`__init__`: set `parent_name`, `super().__init__`, `register()`, pull `radarr_api` / `instance_manager` / `dry_run` from kwargs/parent. No `load_components` (leaf). Control flow when exercised: a caller would run `get_relocation_candidates(instance)` to see what *should* move, then (notionally) call `relocate_movie(...)` per candidate — but the move itself terminates in a logged stub.

The routing decision (`determine_target_instance`) is hard-coded here (resolution tiers + a flat 10 GB guard), not delegated to a `machine_learning` brain module.

## Criteria & examples

- **Resolution → instance**: a movie with `resolution = 2160` maps to preferred `"4k"`; `1080` → `"1080"`; `720` → `"720"`; `480` (or missing) → `None`.
- **10 GB guard**: a 1080p movie with `free_space_map = {"1080": 4.0, "4k": 80.0}` — preferred `"1080"` has only 4.0 GB (< 10) so it's rejected; fallback picks `"4k"` (80.0 ≥ 10) and returns `"4k"`.
- **All-tight**: `free_space_map = {"1080": 6.0}` for a 1080p movie → preferred fails (6 < 10), fallback `max` is `1080` but 6 < 10 → returns `None` (warning logged).
- **dry_run move**: `relocate_movie(42, "1080", "4k")` with `dry_run=True` returns `{"movie_id": 42, "from_instance": "1080", "to_instance": "4k", "status": "dry_run", "error": None}` and mutates nothing.

## In plain English

This is the "wrong-shelf detector." It can spot that a 4K movie was accidentally filed on the 1080p shelf and recommend moving it — and it checks the destination shelf has at least 10 GB of breathing room before suggesting it. But the actual heavy lifting of physically carrying the box to the other shelf isn't built yet; right now it just writes a note saying "this would move from here to there." (And because the free-space list it consults is currently left blank, in practice it tends to report "nothing to move.")

## Interactions

- **Parent**: `RadarrStorageManager`.
- **Siblings**: conceptually pairs with `RadarrStorageSpaceManager` (which produces the `free_space_map` it expects) — but that wiring is not present in this stub.
- **Services**: `radarr_api` (`GET movie`), `instance_manager` (resolution). No brain-module delegation; no cache/Parquet use.

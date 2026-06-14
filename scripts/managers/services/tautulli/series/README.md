# TautulliSeriesManager

- **File** — `scripts/managers/services/tautulli/series/__init__.py`
- **One-liner** — A thin Tautulli submanager that turns a list of raw watch-history entries into per-show watched/incomplete episode counts by delegating the math to a machine-learning feature module.

## What it does (for a senior Python engineer)

`TautulliSeriesManager(BaseManager)` is a leaf service submanager under the Tautulli service tree. It has no `__init__` logic of its own beyond calling `super().__init__(...)`, so it inherits the shared logger / config / global_cache / validator / registry from `BaseManager` and the auto-linked parent (`TautulliManager`).

Key public method:
- `get_series_completion_stats(history_entries: list) -> dict` — Accepts an already-fetched list of Tautulli history entries and returns a dict mapping each show to its watched / incomplete episode counts (a `series_map`). It does NOT fetch the history itself; the caller supplies `history_entries`. The actual aggregation is delegated to the brain function `series_completion_stats(history_entries)`. After computing, it logs one summary line — `[TautulliSeries] <N> shows tracked.` — and returns the dict.

Position in the manager tree:
- Parent: `TautulliManager` (`scripts/managers/services/tautulli/__init__.py`), which registers this class under the component key `"series"` in `all_component_classes` and treats `series` as a `critical_keys` component. The instance is reachable as `tautulli.series`.
- Submanagers: none. It loads no components via `load_components`.

FETCH / CACHE / APPLY:
- FETCH — none directly. It is a pure transform over data the parent already fetched (the parent passes `all_entries`, the cached full watch history).
- CACHE — none directly. This manager does not write to `global_cache`. The parent computes `series_stats = self.series.get_series_completion_stats(all_entries)` as a derived stat; persistence (if any) of that aggregate is the parent's concern, not this manager's.
- APPLY — none. No PUT/DELETE/POST; nothing mutates Tautulli or any other service.

External API endpoints touched: none (it receives pre-fetched entries).

Config keys read: none.

global_cache / Parquet keys read or written: none by this manager. (The history list it consumes originates from the parent's cached watch history, e.g. `tautulli/history/all`, fetched and passed in by `TautulliManager`.)

dry_run behavior: not applicable — there is no APPLY step to suppress. The method is read-only computation in every mode.

Singleton / concurrency / threading notes: inherits the `BaseManager` process-wide singleton behavior (cached in `_instances` keyed by class + singleton_key) and the shared-dependency auto-linking. No locks, threads, or shared mutable state of its own; the method is stateless apart from logging.

## How it functions

Lifecycle:
1. Construction — `__init__` simply forwards to `BaseManager.__init__`, so the instance self-registers under the registry "manager" category and auto-links to its parent `TautulliManager`, inheriting that parent's logger/config/cache/validator.
2. No `load_components` call — there are no submanagers, so there is no component-load summary line for this manager.
3. Entry method — `get_series_completion_stats(history_entries)` is invoked by the parent during its derived-stats phase (`series_stats = self.series.get_series_completion_stats(all_entries)`), after the parent has already pulled and cached the watch history.

Control flow inside the entry method is two lines: call the brain function, log the count, return the result. There are no internal helpers.

Delegated decision / computation: the per-show watched-vs-incomplete tally is computed by the machine-learning feature module `features.completion_stats.series_completion_stats` (imported as `series_completion_stats`). Per scope rules, that module is named here but NOT documented; this manager only keeps the raw-history hand-off and the summary log around it.

## Criteria & examples

This manager applies no thresholds, guards, or selection rules of its own — those (if any) live inside the delegated brain function. The only observable manager-level behavior is the summary log keyed off `len(series_map)`.

Worked example: suppose the parent passes `all_entries` containing history rows for three shows — say *The Mandalorian*, *Bluey*, and *The Office*. The brain returns a `series_map` with one entry per show (e.g. *The Mandalorian* watched 6 / incomplete 2). Because `len(series_map) == 3`, this manager logs exactly `[TautulliSeries] 3 shows tracked.` and returns the three-key dict unchanged.

## In plain English

Think of your Plex watch history as a giant pile of "you watched episode X of show Y" receipts. This little helper hands that whole pile to a counting specialist (the brain module) and gets back a tidy scorecard: for each show, how many episodes you've finished and how many you still haven't. For *Bluey*, it might say "watched 40, still 12 to go." The helper itself doesn't do the counting and doesn't change anything — it just passes the receipts along, then jots a one-line note ("3 shows tracked") so anyone reading the logs knows how many shows ended up on the scorecard. That scorecard later helps the app decide which shows you're actually engaged with versus the ones gathering dust.

## Interactions

- Parent manager: `TautulliManager` (constructs it as the `"series"` critical component and calls `get_series_completion_stats(all_entries)` during its derived-stats step).
- Sibling submanagers (under the same `TautulliManager`): `TautulliDevicesManager`, `TautulliEpisodesManager`, `TautulliInstanceManager`, `TautulliMetadataManager`, `TautulliTranscodeManager`, `TautulliUsersManager`, `TautulliWatchHistoryManager`, `TautulliValidatorManager`. It does not call any of them directly; it consumes the shared `all_entries` the parent assembles (notably from `TautulliWatchHistoryManager`).
- Brain modules: `machine_learning/features/completion_stats.py` (`series_completion_stats`) — delegated computation only, named not documented.
- Downstream consumers: the resulting `series_map` is used elsewhere in the app; e.g. `sonarr/series/sync` resolves `tautulli.series.get_series_completion_stats(...)` (via the registry) to obtain watched titles when seeding from Tautulli history.

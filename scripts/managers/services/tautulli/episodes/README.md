# TautulliEpisodesManager

- **File** — `scripts/managers/services/tautulli/episodes/__init__.py`
- **One-liner** — A thin Tautulli submanager that turns a list of raw watch-history entries into a household-wide "how many episodes were finished vs. abandoned" tally, delegating the actual counting to a machine_learning brain function.

## What it does (for a senior Python engineer)

`TautulliEpisodesManager` is a leaf service submanager under the Tautulli service hub. It subclasses `BaseManager` and adds nothing to `__init__` beyond the standard `super().__init__(logger, config, global_cache, validator, registry, **kwargs)` call, so it inherits the shared logger/config/global_cache/validator/registry and the parent auto-link machinery from `BaseManager`.

It exposes a single public method:

- `get_episode_completion_stats(history_entries: list) -> dict` — Takes an already-fetched list of Tautulli history entries and returns a summary dict `{"completed": int, "incomplete": int}`. The method itself does not compute anything; it calls the brain function `episode_completion_stats(history_entries)` (imported from `scripts.managers.machine_learning.features.completion_stats`), then logs one summary line of the form `[TautulliEpisodes] <N> complete / <M> incomplete.` and returns the dict unchanged.

Place in the manager tree:
- **Parent:** the Tautulli service hub (`scripts/managers/services/tautulli/__init__.py`), which registers this class under the component key `"episodes"` in `all_component_classes` and treats it as a **critical** component (`"episodes"` is in `critical_keys`). Its `component_dependencies["episodes"]` is `[]`, so it has no load-order prerequisites among the Tautulli submanagers.
- **Submanagers:** none. It does not call `load_components`; it is a flat leaf.

FETCH / CACHE / APPLY: this manager performs **none of the three** directly. It does no HTTP itself (it receives `history_entries` from a caller — the raw FETCH is done elsewhere, e.g. the Tautulli watch-history manager), writes nothing to `global_cache`/Parquet, and issues no PUT/DELETE/POST. It is a pure summarization helper that produces a derived dict for callers.

- **External API endpoints touched:** none.
- **Config keys read:** none (beyond whatever `BaseManager` injects).
- **global_cache / Parquet keys read or written:** none.
- **dry_run behavior:** not applicable — there are no APPLY/mutation steps to guard.
- **Singleton / concurrency / threading notes:** standard `BaseManager` singleton semantics (cached in `_instances` keyed by class + singleton_key). No locks, threads, or shared mutable state of its own.

## How it functions

Lifecycle is minimal:
1. The Tautulli hub instantiates it as the `"episodes"` component with the shared dependency set.
2. `__init__` only forwards to `BaseManager.__init__`; there is no `load_components` step and no internal state to set up.
3. At call time, `get_episode_completion_stats` is the entry point: it hands the supplied `history_entries` straight to the brain and logs the result.

The decision/computation — i.e. which entries count as "completed" vs "incomplete" — is delegated to the machine_learning brain module **`features.completion_stats.episode_completion_stats`**. Per the project's brain/service split, the manager keeps the FETCH/summary-log concern and the brain owns the value judgement. (The brain module itself is out of scope and is not documented here.)

## Criteria & examples

This manager applies no thresholds or guards of its own; the only rule is the one the brain enforces, which the manager exposes via its summary log:

- An entry is only counted if its `media_type` is `"episode"` (movies and other media types are ignored).
- An episode entry counts as **completed** when its `percent_complete` is `>= 90`, otherwise it counts as **incomplete**.

Worked example: given `history_entries` containing one episode at `percent_complete = 95` (completed), one episode at `percent_complete = 40` (incomplete, abandoned partway), and one movie entry (ignored entirely), the brain returns `{"completed": 1, "incomplete": 1}` and the manager logs `[TautulliEpisodes] 1 complete / 1 incomplete.` An episode watched to exactly `90%` counts as completed (the boundary is inclusive); one at `89%` counts as incomplete.

## In plain English

Think of your household binge-watching a show like Avatar: The Last Airbender. Tautulli quietly logs how far you got into each episode you started. This little helper goes through that log and tallies two numbers: how many episodes everyone actually finished (watched to at least 90%) versus how many got switched off partway through. It throws out anything that isn't an episode (so a movie night doesn't muddy the count). The result is a quick scoreboard — "you finished 1, bailed on 1" — that the rest of the app can use to judge how much a show is really being watched, which later feeds decisions like whether a series is worth keeping. The manager doesn't do the judging itself; it just hands the log to the "brain" that knows the 90% rule and then writes the headline number to the log.

## Interactions

- **Parent manager:** Tautulli service hub (`scripts/managers/services/tautulli/__init__.py`), where it is the critical `"episodes"` component.
- **Sibling submanagers:** other Tautulli components — `devices` (`TautulliDevicesManager`), `instance`, `metadata`, `series`, `transcode`, `users`, `watch_history` (`TautulliWatchHistoryManager`, the likely source of the `history_entries` it consumes), and `validator_manager`. There is no direct code-level call between them; they are siblings under the same hub.
- **Brain modules:** delegates the completion tally to `machine_learning/features/completion_stats.py::episode_completion_stats` (named only; not documented here).
- **Other services:** none directly. It operates on pre-fetched data passed in by its caller.

# RadarrMonitoringHistoryManager

**File** — `scripts/managers/services/radarr/monitoring/history.py`
**One-liner** — Reads Radarr acquisition history (grabs, imports, failures) and derives lifecycle signals: stuck grabs, repeated failures, and recent imports.

## What it does (for a senior Python engineer)

`RadarrMonitoringHistoryManager(BaseManager, ComponentManagerMixin)` is a child of `RadarrMonitoringManager` (declares `parent_name = "RadarrMonitoringManager"`). It is a read-only FETCH + CACHE adapter over the Radarr history endpoint plus light analysis. It loads no submanagers and performs no APPLY.

**Shared deps:** `radarr_api`, `instance_manager`, `dry_run` from `kwargs` or parent (`kwargs["manager"]`). Instances pass through `_resolve_instance` (instance_manager → radarr_api → literal/`"default"`).

**Key public methods:**

- `get_history(instance, page_size=100) -> list` — FETCH + CACHE. Returns the cached `radarr.history.{resolved}` records if present; otherwise `GET history?pageSize={page_size}&sortKey=date&sortDir=desc`, extracts the `records` list, writes it to `global_cache` under `radarr.history.{resolved}`, logs the count, and returns it.
- `get_movie_history(instance, movie_id) -> list` — FETCH only (no cache). `GET history/movie?movieId={id}`, fallback `[]`.
- `find_stuck_grabs(instance, stale_hours=6) -> list` — analysis over `get_history`. For each movie id, records the first `grabbed` timestamp and the set of imported ids (events `downloadFolderImported`/`movieFileImported`). A movie is "stuck" if it was grabbed, never imported, and its grab time is older than `now - stale_hours`. Returns `[{movie_id, title, grabbed_at}]`, fetching the title via `GET movie/{id}`.
- `find_repeated_failures(instance, failure_threshold=3) -> list` — counts events in `(downloadFailed, importFailed, grabbed)` per movie and returns `[{movie_id, title, failure_count}]` for those at or above the threshold. (Note: `grabbed` is counted toward "failures" here, so a movie repeatedly re-grabbed inflates the count.)
- `get_recent_imports(instance, limit=50) -> list` — filters `get_history` to import events and returns the first `limit` (history is sorted date-desc, so these are the most recent).

**FETCH / CACHE / APPLY:** FETCH (`history`, `history/movie`, `movie/{id}`) and CACHE (`global_cache` write of `radarr.history.{instance}`). No APPLY — purely diagnostic.
**API endpoints:** `history?pageSize=...&sortKey=date&sortDir=desc`, `history/movie?movieId=...`, `movie/{id}`.
**Config keys:** none.
**global_cache keys:** reads/writes `radarr.history.{resolved}`.
**dry_run:** captured but irrelevant — no writes to Radarr.
**Singleton/concurrency:** standard `BaseManager` singleton; no threading.

## How it functions

Lifecycle: `__init__` → `super().__init__` → `self.register()` → resolve deps → debug log. No `load_components`.

The analysis methods all funnel through `get_history`, which is cache-first: the first call populates `radarr.history.{resolved}`; subsequent calls within the cache's lifetime reuse it (so all three analyzers see a consistent snapshot). Timestamps are parsed from ISO strings with `Z` normalized to `+00:00`, compared against a UTC `now - timedelta(hours=stale_hours)` cutoff.

No decision is delegated to a `machine_learning` brain module; the outputs here are diagnostic lists that lifecycle/monitoring callers may consume. (Per project memory, the lifecycle "brain" lives in `machine_learning/` and would consume such signals — this manager only supplies the raw evidence.)

## Criteria & examples

- **Stuck grab** — with `stale_hours=6` and `now = 12:00 UTC`: a movie `grabbed` at `05:00 UTC` with no `downloadFolderImported`/`movieFileImported` event is stuck (05:00 < 06:00 cutoff). The same movie grabbed at `07:30 UTC` is not yet stuck (07:30 ≥ 06:00). A movie grabbed at 05:00 *and* imported at 05:20 is not stuck (it appears in `imported_ids`).
- **Repeated failures** — with `failure_threshold=3`: a movie with events `[grabbed, downloadFailed, grabbed, importFailed]` has count 4 ≥ 3 → reported. A movie with `[grabbed, importFailed]` has count 2 < 3 → not reported.
- **Recent imports** — `get_recent_imports(limit=10)` returns up to the 10 newest import events (history is date-desc).

## In plain English

This is the librarian's logbook reader. Every time Radarr tries to fetch a movie, succeeds, or fails, it gets written in a log. This manager reads that log and answers three practical questions: "Which movies did we *start* downloading hours ago that never actually showed up?" (a download for, say, Toy Story that got stuck), "Which movies keep failing over and over?" (so you can stop wasting bandwidth on a release that never works), and "What just finished arriving?" It never changes anything — it just reports what the logbook says so other parts of the system can decide what to do.

## Interactions

- **Parent:** `RadarrMonitoringManager`.
- **Siblings:** `RadarrMonitoringMoviesManager`, `RadarrMonitoringRulesManager`, `RadarrMonitoringSchedulerManager`.
- **Services:** Radarr API (`history`, `history/movie`, `movie/{id}`), instance manager (`resolve_instance`), `global_cache` (history snapshot).
- **Brain modules:** none directly; supplies evidence the lifecycle brain (`machine_learning/`) may later consume.

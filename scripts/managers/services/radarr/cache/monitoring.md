# RadarrMonitoringCacheManager

- **File** — `scripts/managers/services/radarr/cache/monitoring.py`
- **One-liner** — Caches the list of monitored movies per Radarr instance and provides helpers to patch a movie's monitored state (including enforcing "keep" tags).

## What it does (for a senior Python engineer)

`RadarrMonitoringCacheManager(BaseManager, ComponentManagerMixin)` is a thin monitoring adapter. It performs FETCH (GET `movie`, `config/ui`), CACHE (monitored list + monitoring rules), and APPLY (PUT `movie/<id>` to change a movie's `monitored` flag).

Where it sits in the tree:
- **Parent**: `RadarrCacheManager` (`parent_name = "RadarrCacheManager"`).
- **Submanagers**: none.

Public methods:
- `refresh_monitored_movies(instance)` — FETCH `GET movie`, filter to `m["monitored"]`, CACHE the filtered list under `radarr.<instance>.monitoring.monitored` (not compressed). Logs the monitored count.
- `get_monitored_movies(instance)` — reads `radarr.<instance>.monitoring.monitored` (default `[]`).
- `enforce_keep_tags(movie_list)` — for each movie whose `tags` contains `"keep"` and that is NOT currently monitored, APPLY `PUT movie/<id>` with the payload merged with `{"monitored": True}` (targets the hard-coded instance string `"default"`). Logs each enforced movie.
- `patch_movie_monitoring_state(instance, movie_id, movie_payload, desired_state)` — APPLY `PUT movie/<movie_id>` with `{**movie_payload, "monitored": desired_state}`. Logs the patch.
- `refresh_monitoring_rules(instance)` — FETCH `GET config/ui`; CACHE under `radarr.monitoring.rules.<instance>` (`compressed=True`).

External API endpoints: `GET movie`, `PUT movie/<id>`, `GET config/ui`.
Config keys read: none.
Global_cache keys written: `radarr.<instance>.monitoring.monitored`, `radarr.monitoring.rules.<instance>`. Read: `radarr.<instance>.monitoring.monitored`.

`dry_run`: captured into `self.dry_run` but NOT consulted — both `enforce_keep_tags` and `patch_movie_monitoring_state` issue `PUT`s unconditionally. This diverges from the FETCH/CACHE/APPLY dry-run convention (the APPLY paths are not gated).

Notes/caveats (document-only):
- `enforce_keep_tags` checks `"keep" in movie.get("tags", [])`, i.e. it expects tag LABELS in `tags`; raw Radarr movie payloads carry tag IDs (ints), so this matches only if the caller passes label-bearing data.
- It also calls the API against the literal instance `"default"` rather than a passed-in instance.

Singleton/concurrency: standard `BaseManager` singleton; no threading.

## How it functions

`__init__` does BaseManager wiring, `self.register()`, then resolves `radarr_api`, `instance_manager`, `manager`, and `dry_run` from kwargs-or-parent. No `run()` and no `load_components`; callers invoke the helpers. No machine_learning delegation.

## Criteria & examples

- Keep-tag enforcement guard: only fires when a movie is currently NOT monitored AND its `tags` contains `"keep"`. Example: a movie tagged `["keep"]` with `monitored=False` triggers `PUT movie/<id>` flipping it to monitored; the same movie already `monitored=True` is left alone.
- Monitored filter: `refresh_monitored_movies` keeps only truthy `monitored`. Given 500 movies of which 120 are monitored, the cache stores 120 and logs `(120 monitored)`.

## In plain English

In Radarr, a "monitored" movie is one the system actively watches over (e.g. to grab upgrades). This manager keeps a list of which movies are being watched over on each server, and can flip an individual movie's watched-over switch on or off. It also enforces a rule: if you've slapped a "keep" label on a film like The Princess Bride, it makes sure that film stays monitored.

## Interactions

- **Parent**: `RadarrCacheManager`.
- **Siblings**: relates to `RadarrTagCacheManager` (the "keep" concept) and to the owned-movie monitor policy that lives elsewhere in the Radarr tree.
- **Services**: `radarr_api`.
- **Brain modules**: none.

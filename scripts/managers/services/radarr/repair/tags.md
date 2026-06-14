# RadarrRepairTagsManager

- **File** — `scripts/managers/services/radarr/repair/tags.py`
- **One-liner** — Enforces tag consistency in Radarr: finds movies with conflicting "keep" tags or no tags at all, applies genre/resolution-based tag rules, and resolves keep-tag conflicts to the strongest policy.

## What it does (for a senior Python engineer)

`RadarrRepairTagsManager` is a `BaseManager` + `ComponentManagerMixin` loaded by `RadarrRepairWrapperManager` under the key `tags`. Its `parent_name` is derived from its class name (`RadarrRepairTags`). Deps (`radarr_api`, `instance_manager`, `dry_run`) come from kwargs-or-parent.

Module constant `KEEP_TAG_LABELS = {"keep", "keep_forever", "keep_movie"}` defines the lifecycle "keep" labels it recognises.

- **FETCH / CACHE / APPLY.** All three on the Radarr API directly (no global_cache use):
  - FETCH: `GET tag`, `GET movie`, `GET movie/{id}`.
  - APPLY: `POST tag` (create a label), `PUT movie/{id}` (write a movie record with updated `tags`).
  - No CACHE — it reads live from Radarr each call.
- **External API endpoints** (all via `radarr_api._make_request`): `tag` (GET, POST), `movie` (GET), `movie/{id}` (GET, PUT).
- **Config keys.** None read.
- **global_cache / Parquet keys.** None.
- **dry_run.** In `apply_tag_rule` and `fix_inconsistent_keep_tags`, when true it logs a "would …" line and increments the success counter without issuing the PUT.
- **Singleton / concurrency.** `BaseManager` singleton; no threads.

Public methods:

- `find_inconsistent_keep_tags(instance) -> list[dict]` — FETCH-only. Builds a `{tag_id: label}` map, fetches all movies, and flags any movie whose label set intersects `KEEP_TAG_LABELS` in more than one label. Returns `{movie_id, title, year, tag_ids, conflicting_labels}` per offender.
- `find_untagged_movies(instance) -> list[dict]` — FETCH-only. Returns `{movie_id, title, year}` for every movie with no tags.
- `apply_tag_rule(instance, genre=None, resolution=None, tag_label="") -> stats` — APPLY. Resolves (or creates) the tag id for `tag_label`, then iterates movies; tags those matching the optional genre (case-insensitive membership) and/or exact `resolution` (read from `movieFile.quality.quality.resolution`, only when `hasFile`). Skips already-tagged movies. Returns `{checked, tagged, already_tagged, failed}`. No-ops (empty stats) if `tag_label` is blank or `radarr_api` is None.
- `fix_inconsistent_keep_tags(instance) -> stats` — APPLY. For each conflict from `find_inconsistent_keep_tags`, keeps only the strongest keep label by priority `keep_forever > keep_movie > keep`, removing the weaker keep tags via `PUT movie/{id}` (fetching the full record first). Returns `{checked, fixed, failed}`.
- `run(instance) -> dict` — The scan invoked by the wrapper. Returns `{"inconsistent_keep_tags": [...], "untagged_movies": [...]}`. NOTE: `run` is read-only — it does not call the `fix_*`/`apply_*` mutators.

Internal helpers: `_resolve_instance`, `_get_tag_label_map` (builds `{id: label}` from `GET tag`), `_get_or_create_tag` (returns existing id or `POST tag` to create, updating the map in place).

## How it functions

Lifecycle: `__init__` → `register()` → resolve deps → debug log. No children loaded.

`run()` only diagnoses (the two `find_*` scans). The mutating methods (`apply_tag_rule`, `fix_inconsistent_keep_tags`) are not wired into `run()`, so they execute only when called directly by another caller. All policy is local (the `policy_order` list and the `KEEP_TAG_LABELS` set); no `machine_learning` delegation.

## Criteria & examples

- **Conflict detection:** a movie tagged both `keep_forever` and `keep_movie` has a 2-element intersection with `KEEP_TAG_LABELS` (> 1) → flagged. A movie with only `keep` is fine.
- **Conflict fix priority `keep_forever > keep_movie > keep`:** a movie carrying both `keep` and `keep_forever` → strongest is `keep_forever`; the `keep` tag id is removed, `keep_forever` retained, then `PUT movie/{id}` with the trimmed `tags`. In dry_run it logs `"Would fix keep tags … keeping 'keep_forever'"` and counts it as fixed.
- **apply_tag_rule resolution match is exact:** with `resolution=1080`, a movie whose `movieFile.quality.quality.resolution` is `2160` is skipped; one at `1080` is tagged (unless already carrying that tag id, in which case `already_tagged` increments).
- **apply_tag_rule genre match is case-insensitive membership:** `genre="horror"` tags a movie whose `genres` include `"Horror"`.

## In plain English

This is the person who keeps your movie collection's sticky notes tidy. Some movies have a "never delete" sticky (a "keep" tag). Sometimes a movie ends up with two contradictory stickies — like both "keep forever" and "keep this one" — so this manager picks the strongest one and peels off the rest. It can also flag movies with no stickies at all, or slap a label like "kids" on every animated film. With "preview mode" on, it just tells you what it *would* relabel without touching anything.

## Interactions

- **Parent manager** — `RadarrRepairWrapperManager` (loads it as `tags`).
- **Sibling submanagers** — None invoked; it shares the `KEEP_TAG_LABELS` concept with the lifecycle/keep-policy logic used by `anomaly`.
- **Brain modules** — None.
- **Other services** — `radarr_api` (tag and movie endpoints); `instance_manager` for resolution.

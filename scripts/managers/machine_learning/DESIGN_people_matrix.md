# DESIGN — person↔media co-occurrence matrix for watchability

Goal (user): make cast/crew **searchable** — "find other movies with Scarlett Johansson
and Robert Downey Jr." — and build a full person↔media matrix so people-overlap drives
watchability (next-watch ranking + acquisition candidates), not just the display "Why" tables.

Derived from a 10-agent mapping+design workflow (2026-06-13). Three design angles → one
synthesis; the load-bearing facts below were re-verified against live code after the workflow.

## The decisive (verified) fact: ids already reach the scorer

`flatten_trakt_people` (bucket_merge.py) collapses credits to **pipe-joined name strings** and
drops the person id — but that only feeds the **display** columns. The **scorer** never reads
those columns: `score_movie(credits=…)` / `score_show` receive the **live daemon-normalized
credits dict** (`people_manager.get_people(tmdb_id)` → `{cast:[{name, id, character, order}],
crew:[{name, id, job, department}]}`), and read only `m["name"]` (movie_scorer.py:259-277),
ignoring `m["id"]`. The daemon captures `id = ids.get("tmdb")` per person (normalise_people).

**Consequence:** a people-affinity term reads `m.get("id")` from the *same dict the scorer
already gets* — **no flatten change, no parquet schema change** for scoring. The co-occurrence
*index* reads the daemon people buckets directly (also ids intact). Flatten/parquet id-columns
are an **optional P7**, only if a parquet-only consumer ever needs ids.

## Matrix artifact — pure `machine_learning/people_matrix/`

New PURE sibling package (add `"people_matrix"` to `brain_purity._GUARDED_SUBPACKAGES`). The
**manager** does all I/O (reads buckets via Trakt{Movie,Show}CacheManager, passes decoded dicts
in); the builder is stdlib-only. All keyed on `tmdb_person_id: int`.

```
build_index(media_people) -> (person_index, media_people_fwd)
  person_index:     dict[int, set[tuple[str,int]]]   # pid -> {(medium, ext_id)}  inverted index
                                                     # medium in {"movie","show"}; ext_id = tmdb/tvdb
  media_people_fwd: dict[tuple[str,int], dict[str,list[int]]]  # title -> {cast/directors/writers/composers/producers: [pid]}
co_occurring(person_index, pids) -> dict[tuple[str,int], int]  # DERIVED on demand (set-intersection),
                                                     # NOT materialized — N×N at ~15k people is 100-400M cells
```
- `(medium, ext_id)` tuple namespaces movie-tmdb vs show-tvdb so id-spaces never collide (C3 pattern).
- Cache **two artifacts separately** (per critique): `person_index` is library-derived/stable;
  `person_weights` is watched-set-derived/volatile. Conflating them forces a full rebuild on every
  watched-set change. Add `PEOPLE_MATRIX_PATH` to `daemon_paths.py`; manager writes gz atomically,
  dry_run-safe, rebuilt per run from buckets already on disk.
- Scale (UNVERIFIED — no real parquet to count): ~50-150k edges, ~10-20k persons, <15MB dict-of-sets.
  Cap people-per-title (`cast_limit=10` + directors/writers) before edge-building. P8 escape hatch =
  persist `people_matrix.parquet`; validate against a real `movie_files.parquet` first.

## Scoring — NEW Group-C4 term (do NOT overload Group-B)

- Pure `scoring/_shared.py::person_affinity_score(media_people_ids, person_weights, role_caps)` —
  per role, top-3-mean of `person_weights[pid]` scaled to role cap, keyed on **int ids**.
  **Must be a separate fn** — `affinity_topk` does `aff_map.get(n.lower())` which raises on int keys.
- `score_movie`/`score_show` gain `person_ids`, `person_weights`, `person_affinity_cap=0.0`;
  `breakdown["C4_person_affinity"]`. Cap default `0.0` ⇒ **plan_summary oracle byte-identical**
  until a caller opts in (exact `related_graph_cap` discipline). `person_ids` extracted from the
  credits dict the scorer already receives.
- C4 (id-keyed) is immune to "Scarlett Johansson" vs alias name-drift. Group-B (name) stays untouched.
- **Why not Group-B `max(name_b, id_b)` fusion:** a `max()` has no `cap=0.0` knob — any non-empty
  `person_weights` raises the score, so it moves the oracle even "off." C4-with-cap is the safe path.
- `person_weights` from pure `affinity/genre_affinity.py::aggregate_person_affinity(watched_media_keys,
  media_people_fwd, half_life_days=None)` — defaultdict tally + role weights (cast 1.0 / director 1.0 /
  writer 0.6 / composer 0.4) + optional recency decay, mirroring `aggregate_affinity`.

## Candidate generation — co-cast proposer + acquisition signal (default-off)

- `acquisition/candidates.py::_people()` gated by a `people_cooccurrence` source flag (default-off):
  for the household's top-K most-watched persons, union `person_index[pid]` minus owned → candidates
  `source="people_cooccurrence"`; add to `_SOURCE_SCORE`. Cap top-K persons + top-N proposals (a
  prolific actor's list is huge) and respect existing dedup + space-pressure gate.
- `AcquisitionScorer.score`: `matrix["people_affinity"]` signal + `_WEIGHTS["people_affinity"]=0.0`.
  Byte-identical: `_weighted` drops `None` signals and `0.0` weight adds nothing to num/den.
  Rebalance to positive weight in a later behavior-change PR. (A non-zero "tidy" would shift every score.)

## Personalization — global-first, per-user later

- **Household (v1, shippable):** `aggregate_person_affinity` over the existing household watched-set
  (`watched_tmdb_ids` movies / `watched_tvdb_ids` shows, already built for C3). Weights ScarJo high
  because the household watched her films — exactly how Group-B household affinity works today.
- **Per-user (P6, BLOCKED not just deferred):** needs (a) `movie_files` to gain a `per_user` column
  (only `episode_files` has one today) and (b) a per-user `rating_key→tmdb` resolution (Tautulli
  history is rating_key/title-keyed, never tmdb-resolved). Tautulli/Plex-only (Trakt PIN is
  account-scoped). Degrades to household when a profile lacks a Plex↔Tautulli crosswalk.

## Graceful degradation (every layer no-ops to today's behavior)

Daemon not warm / no ids → empty index → `person_affinity_score`=0.0, `co_occurring`={}, source=[].
With `person_affinity_cap=0.0` default + source flag off + weight 0.0, the whole feature is inert and
byte-identical. MDbList absence is irrelevant (matrix uses Trakt people buckets + watched-set only).
Assert with an explicit empty-buckets test.

## Phased plan (each = own gated PR; brain_purity + secret_scan + plan_summary oracle)

| PR | Scope | Gate |
|---|---|---|
| **P0** | `people_matrix/{__init__,build}.py` (`build_index`, `co_occurring`) + `_GUARDED_SUBPACKAGES`. Unit tests incl. hand-verified ScarJo∩RDJ, empty→empty, movie/show id-space separation. | unit + brain_purity; no runtime wiring → oracle untouched |
| **P1** | `daemon_paths.PEOPLE_MATRIX_PATH` + `services/trakt/people_matrix.py` (reads buckets, calls pure builder, writes gz atomically, caches index); wire build after enrich in main.py, dry_run-safe. **Emit coverage telemetry: % titles with ≥1 person id.** | smoke: gz exists w/ expected counts |
| **P2** | `aggregate_person_affinity` + `person_affinity_score`, pure + unit-tested (top-3-mean parity, zero-on-empty); household `person_weights`. | unit; no scorer default-on |
| **P3** | Thread `person_ids/person_weights/person_affinity_cap=0.0` into `score_movie`+`score_show` + `breakdown["C4_person_affinity"]`. | **plan_summary byte-identical at cap=0.0** across golden set |
| **P4** | Flip `person_affinity_cap` on **movies first** (behavior-change PR, 3-lens + ledger diff). Gate enabling cap>0 on a min coverage threshold. Shows deferred. | golden ledger diff reviewed; coverage sane |
| **P5** | Acquisition: `_people` (gated, default-off) + `_SOURCE_SCORE` + `_WEIGHTS["people_affinity"]=0.0` + signal. | acquisition byte-identical at weight 0 / flag off; ScarJo+RDJ E2E surfaces titles when on |
| **P6** | Per-user `people_matrix/users/{user}/person_affinity` (**BLOCKED** on the two joins above). | per-user golden; household fallback |
| **P7** (opt) | `bucket_merge.person_id_cols` sibling + additive nullable `*_ids` parquet cols (only if a parquet-only consumer needs ids); `_route_crew` parity keeps `flatten_trakt_people` byte-identical. | name fn + test_bucket_merge byte-identical |
| **P8** (opt) | Persist `people_matrix.parquet` if rebuild is costly; retire the never-read `relational.py` people graph. | **verify relational `person_tmdb_id` is actually populated** (one build path sets it None); size-validate first |

## STATUS — P0–P5 shipped (2026-06-13), all gated default-off

Built per the user's "full default-off feature" choice. 60+ tests, brain_purity + secret_scan green.

- **P0** `machine_learning/people_matrix/{build,__init__}.py` — pure `build_index` / `co_occurring` /
  `films_with_all` (the "ScarJo ∩ RDJ" query) / `invert_forward` / `serialize_forward` /
  `route_people` (+ `PERSON_ROLE_WEIGHTS`). Registered in `brain_purity._GUARDED_SUBPACKAGES`.
- **P1** `services/trakt/people_matrix.py::TraktPeopleMatrixManager` — reads the daemon people
  buckets via the existing cache managers, builds the forward map, caches it (gz + global_cache
  `people_matrix/forward`), logs coverage. `PEOPLE_MATRIX_PATH`/`PEOPLE_AFFINITY_PATH` in
  `daemon_paths`. Wired into `main.py` after Radarr, gated `people_matrix.enabled` (config block
  added, **default false**).
- **P2** pure `aggregate_person_affinity` (genre_affinity.py) + `person_affinity_score` (_shared.py,
  id-keyed, separate from `affinity_topk`). Manager `build()` also computes the household weights
  from the C3 watched-set (Trakt history + Tautulli completions) → `people_matrix/affinity`.
- **P3** Group-**C4** term in `score_movie`/`score_show` (`person_weights`, `person_affinity_cap=0.0`);
  reads the title's ids from the `credits` dict already passed. **cap=0.0 default → plan_summary
  byte-identical** (verified: existing golden scores unchanged; C4 integration test asserts it).
- **P5** acquisition `people_affinity` signal (scorer, `_WEIGHTS` 0.0 → byte-identical total) + co-cast
  candidate source (`candidates._people`, gated `acquisition.sources.people_cooccurrence` default-off,
  `_SOURCE_SCORE` 60).

**To enable, in order:** (1) `people_matrix.enabled=true` → builds the searchable index + household
weights every run (the `films_with_all` query works; the acquisition `people_affinity` signal becomes
visible in the matrix at weight 0). (2) `acquisition.sources.people_cooccurrence=true` → co-cast
proposals. (3) **P4 (future behavior-change PR)** — a scorer caller passes `person_weights` +
`person_affinity_cap>0` to activate C4 next-watch ranking (gate on a coverage floor); and rebalance
`_WEIGHTS["people_affinity"]>0` for acquisition. These move the oracle → own ledger-diff-reviewed PR.

## Blocked / verify-before-relying (from the adversarial critique)

- **TV/cross-medium is genuinely blocked:** `episode_files.parquet` doesn't broadly carry cast columns
  and is pruned (no per-episode tvdb). **Movies-first**; shows degrade to zero until a show-enrichment
  broadcast PR lands. P3's show kwargs must not imply TV parity.
- **Per-user (P6) blocked** on the `movie_files` per_user column + Tautulli→tmdb resolution.
- **The "global propose / local rank" re-ranker home is unbuilt** (DESIGN_recommendation_enhancement.md
  Phase-2). The additive 0.0-weight acquisition signal is a documented **stopgap**, not the designed re-ranker.
- **Verify before P8:** `relational.py` `person_tmdb_id` may be unpopulated in one build path (set None).
- **Scale unverified** against a real library parquet.

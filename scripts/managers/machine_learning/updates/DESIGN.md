# updates — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **updates**

**Package** — `scripts.managers.machine_learning.updates` *(no `__init__.py`)*
**Status** — 🔴 Legacy — recommended for deletion after a caller check
**Related** — [README.md](./README.md) · [`affinity/DESIGN.md`](../affinity/DESIGN.md) · [`labels/DESIGN.md`](../labels/DESIGN.md)

---

## 1. Problem statement

These two scripts solved a real problem, once: *turn Tautulli's history and
metadata caches into something a model could train on.* They predate the ML
migration, the `contracts/` boundary, the purity guard, and every package that
now does the job properly.

The problem they solve is now solved three times over, better, by
[`labels/`](../labels/README.md), [`affinity/`](../affinity/README.md) and
[`people_matrix/`](../people_matrix/README.md). What remains is not a design to
document but a **deletion to justify** — and, unexpectedly, a useful piece of
evidence for three findings already in the register.

---

## 2. What each script did

### `dataset_builder.py`

Joins `watch_history` × `metadata_libraries` on `rating_key`, emitting a flat
record per play: user, title, genres, actors, directors, composers, producers,
studios, labels, collections, codecs, `play_duration`, `watched_status`,
timestamp. Writes JSON, prints a count.

**Superseded by** [`labels/snapshots.py`](../labels/README.md) (captures feature
state at decision time) plus [`labels/labeling.py`](../labels/README.md) (joins
against ground truth with a maturity guard). The successor does what this does and
adds the two things that make the output usable: a **cutoff** and a **label**.

### `feature_aggregator.py`

Per-user counters — `genre_X`, `actor_Y`, `director_Z`, `studio_…`,
`collection_…`, `video_codec_…`, `audio_codec_…`, `audio_lang_…` — pivoted into a
user×feature CSV.

**Superseded by** [`affinity/genre_affinity.aggregate_affinity`](../affinity/README.md)
(same categories, plus optional temporal decay, plus the library-genre backstop
that stops a sparse profile collapsing to the household default) and
[`people_matrix/`](../people_matrix/README.md) (role weights and billing decay,
id-keyed).

---

## 3. Why this is stale rather than merely superseded

Superseded code can still be correct. This cannot run.

### 3.1 It reads a field that does not exist

```python
"timestamp": entry.get("started") or entry.get("date")
```

[`labels/labeling.py`](../labels/README.md), documenting the same cache as
inspected on disk:

> `"date"` unix epoch SECONDS of the play event (**the only timestamp field
> present; there is no `"started"`**)

The `or` fallback saves it, but the first branch is documentation of a schema
that changed and a reader who was not told.

### 3.2 Its paths point at a cache layout that no longer exists

| Hardcoded | Live |
|---|---|
| `cache/tautulli/watch_history/watch_history_default.json` | `tautulli/history/all.json` |
| `cache/tautulli/metadata_libraries/metadata_libraries_default.json` | no equivalent |

Both are relative to the working directory, so even the shape of the invocation
assumes a repo-root `python scripts/...` that the current tooling does not use.

### 3.3 Its methods are ones the brain has since explicitly rejected

| This folder | The brain's stated position |
|---|---|
| `f"actor_{name}"` — name-keyed people | *"a name-keyed graph **could not feed** [C4] at all"* — [`people_matrix`](../people_matrix/DESIGN.md) §3.2 |
| Raw `+= 1` counts | [`affinity/`](../affinity/DESIGN.md) §3.2 has `exp(-age/half_life)`; [`people_matrix`](../people_matrix/DESIGN.md) §3.3 has billing decay — both because undecayed counts flood the vector *(§8 P-H)* |
| `watched_status` captured raw | [`lifecycle/watched_definition.py`](../lifecycle/DESIGN.md) exists because untresholded plays counted **9.8 % of episode and 36.2 % of movie plays** as watches |

Each of the three is not a difference of taste — it is a documented lesson the
codebase learned after these scripts were written.

---

## 4. 🎯 What this folder proves

The genuinely useful thing here is that `updates/` is a **worked counter-example**
for three items already in the register.

### 4.1 It confirms `GLD-ML-03` — import-guarding is insufficient

Both scripts do raw disk I/O:

```python
with open(self.output_path, 'w', ...) as out: json.dump(...)
df.to_csv(output_file)
os.makedirs(...)
```

`brain_purity.py` *"guards **imports only**"* (`GLD-ML-03`). These import `json`,
`os`, `pathlib` and `pandas` — none forbidden. **An import-only guard would pass
two modules that write CSV and JSON to disk from inside the brain.**

That is the clearest possible demonstration that the guard's name promises more
than its scope delivers. `GLD-UPD-02`.

### 4.2 It confirms `GLD-ML-02` cannot be a flat list addition

`updates` is one of the six subpackages absent from `_GUARDED_SUBPACKAGES`. Adding
it would either fail or require an exemption — the same conclusion
[`labels/DESIGN.md`](../labels/DESIGN.md) §3.5 reached about `first_run.py`, but
for the opposite reason: `labels`' impurity is **load-bearing and justified**;
this is **legacy and unjustifiable**.

Two of six candidates now have a reason not to be added, and they are different
reasons. `GLD-ML-02` needs a per-package decision, not a list edit.

### 4.3 It violates the `ml → contracts` import rule

```python
from scripts.managers.factories.cache import make_json_safe
```

[`ARCHITECTURE.md`](../ARCHITECTURE.md) rule 3: *"Import direction (enforced
one-way): `services → contracts` and `services → ml.*`; **`ml → contracts`
only**."*

A brain module importing from `factories/` breaches that — and passes the guard,
because `brain_purity` forbids `services.*`, HTTP clients and `*_api`, not
`factories.*`.

Third confirmation that the enforced rule is narrower than the stated one.

### 4.4 The structural tells were sufficient

Worth noting for the remaining sweep: **no `__init__.py`, no docstring, no test**
identified this folder as an outlier before a line of logic was read. Every other
brain package has all three. That triple absence is a cheap, reliable signal of
pre-migration code.

---

## 5. Invariants

None. Nothing imports these modules as a library; both are `__main__` scripts.

---

## 6. Failure modes

| Failure | Behaviour |
|---|---|
| Run as-is | `FileNotFoundError` — the hardcoded paths do not exist |
| Run against remapped paths | Produces a name-keyed, undecayed, unthresholded matrix superseded three times over |
| Imported by something | ❓ **Unverified** — no caller check performed |

---

## 7. Configuration surface

None. Paths are constructor arguments with hardcoded `__main__` defaults.

---

## 8. Implemented capabilities

- ✅ Tautulli history × metadata join on `rating_key`
- ✅ Per-user categorical feature counting across 9 dimensions
- ✅ Pandas pivot to a user×feature CSV

All three are implemented better elsewhere.

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-UPD-01` | 🔴 **Delete `updates/` after a caller check** — grep for `TautulliMLDatasetBuilder`, `TautulliFeatureAggregator` and `machine_learning.updates`; if unreferenced, remove the folder | Two dead scripts inside the brain that read a nonexistent field, target a defunct cache layout, and use three approaches the codebase has since documented as wrong | S | D43 |
| `GLD-UPD-02` | 🎯 **Cite `updates/` as the worked example in `GLD-ML-03`** — an import-only guard passes two modules doing `open()`, `json.dump` and `df.to_csv` inside the brain | Turns an abstract gap into a demonstrated one, which makes the case for extending the guard | S | `GLD-ML-03` |
| `GLD-UPD-03` | **Add the structural tell to the sweep checklist** — *no `__init__.py` + no docstring + no test* reliably marks pre-migration code | §4.4: it identified this folder before any logic was read | S | `GLD-DIS-04` |
| `GLD-UPD-04` | **Extend `brain_purity` to forbid `factories.*` imports** — or amend `ARCHITECTURE.md` rule 3 to permit them | §4.3: the third instance of the enforced rule being narrower than the stated one | S | `GLD-ROU-06`, `GLD-SIZ-06` |
| `GLD-UPD-05` | **Record in `MIGRATION.md` that `updates/` was superseded** and by what, so the deletion has a paper trail | Six re-export shims are documented with their deletion step; this has none | S | `GLD-UPD-01` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does anything import these two classes? *(= D43)* | `GLD-UPD-01` |
| Q2 | Was `metadata_libraries` ever a real cache, or is the path aspirational? | `GLD-UPD-01` |
| Q3 | Should `brain_purity` forbid `factories.*`, or should `ARCHITECTURE.md` be relaxed? | `GLD-UPD-04` |

**Q1 is the only thing between this folder and deletion.** Everything else is
settled: the successors exist, the methods are documented as wrong, and the paths
are dead. One grep.

## 11. Related designs

- [`labels/DESIGN.md`](../labels/DESIGN.md) — the successor to `dataset_builder`, and §3.5's parallel purity finding
- [`affinity/DESIGN.md`](../affinity/DESIGN.md) — the successor to `feature_aggregator`
- [`people_matrix/DESIGN.md`](../people_matrix/DESIGN.md) §3.2 — why name-keying cannot work
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) — the untresholded-`watched_status` bug, measured
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) rule 3 — the import direction §4.3 breaches
- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-B — the guard-narrower-than-it-appears pattern this confirms three ways

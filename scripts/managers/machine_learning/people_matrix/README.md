# people_matrix

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **people_matrix**

**Package** — `scripts.managers.machine_learning.people_matrix`
**Run position** — Built from the enrich-daemon's people buckets; read by the scorer (Group B, C4) and the candidate layers.
**One-liner** — The person↔media co-occurrence graph: an inverted person→titles index and a role-segmented forward map, id-keyed and deliberately **not** a matrix.

---

## Purpose

From [`__init__.py`](./__init__.py):

> **THINKS only** (brain layer): a service manager reads the daemon people buckets
> and passes decoded credits dicts to `build_index`; the scorer / candidate
> layers read the resulting inverted index + forward map.

The query it exists to answer:

> *"find every film with Scarlett Johansson AND Robert Downey Jr."* is a **set
> intersection** over the inverted index — **NO N×N matrix is ever materialised**
> (at ~10–20k people that is **100–400M cells**; the intersection is
> `O(min(list))`).

Despite the package name, there is no matrix. See [`DESIGN.md`](./DESIGN.md) §3.1.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`build.py`](./build.py) | 16.7 KB | The whole graph — constants, `build_index`, `co_occurring`, `films_with_all`, `route_people`, serialisation | ✅ 9.0 KB |
| [`__init__.py`](./__init__.py) | 1.3 KB | Re-exports (18 symbols) | — |

---

## The seven roles

```python
ROLES = ("cast", "directors", "writers", "composers",
         "producers", "cinematographers", "editors")
```

`cinematographers` and `editors` *"have no counterpart in `flatten_trakt_people`
(the daemon's display columns stop at composers)"* — but both are first-class
`role_type` values in the Radarr relational credits table, *"so the graph carries
every credited role the library actually records, not a subset."*

### Role weights

| Role | Weight | Standing |
|---|---|---|
| cast | **1.0** | anchor — the signal this table expresses |
| directors | 0.7 | auteur signal kept; franchise-director pattern trimmed |
| composers | 0.4 | — |
| writers | **0.375** | measured compromise — see below |
| cinematographers | 0.3 | — |
| producers | 0.15 | deliberately low — topped the raw ablation via studio-stable continuity + density, which saga/universe + the studio term already pay |
| editors | 0.2 | — |

> **SINGLE source** for both the aggregation
> (`affinity.genre_affinity.aggregate_person_affinity`) and the scoring term
> (`scoring._shared.person_affinity_score`), **so the two never drift**.

**Set and measured 2026-08-07** (`GLD-PPL-01/04/12`, full record in
[`DESIGN.md`](./DESIGN.md) §9). The operator's ruling — *"more Robert Downey Jr in
Tropic Thunder, less Russo brothers for another superhero movie"* — makes this a
**people-following** table: franchise/studio continuation is intentionally not
chased here because other machinery already monetises it. Writers at 0.375 splits
the difference after the franchise-decontaminated ablation found writers and cast
**statistically tied** (AUC 0.766 vs 0.747, ≪1σ at 30 clean positives) — the
"do we actually follow storytellers?" question re-measures for free at ≥60 clean
positives:

```
python scripts/support/tools/people_billing_experiment.py --sweep --decontaminate
```

> ⚠️ The weights currently apply at **both** aggregation and candidate scoring —
> cross-role ratios are effectively **squared** end-to-end (writers ≈ 0.14 vs cast
> 1.0). Intentional concentration vs double-count is the one open question:
> `GLD-PPL-13`.

---

## Billing decay — cast only

```
weight = 1 / (1 + 0.25 × rank)

rank 0 → 1.00   rank 1 → 0.80   rank 2 → 0.67   …   rank 9 → 0.31
```

> Without it the **tenth-billed bit-part actor of a beloved film counts exactly as
> much as its star**, which floods the affinity vector with people the household
> has no opinion about.

Crew roles are exempt — *"a film has one director, and 'second credited writer'
carries no billing semantics."* `BILLED_ROLES = {"cast"}`.

**Validated 2026-08-07** against a temporal holdout of the household's own watch
history: flat (decay 0) is measurably worse; every decay ≥ 0.25 sits on a
plateau; cast depth 10 confirmed (deeper is flat, and ensemble tentpoles need no
special handling — their members are leads elsewhere and the consumers read a
top-3 mean anyway).

---

## Sources, and why ids

**The daemon people buckets are AUTHORITATIVE** (`trakt/movies/` +
`trakt/shows/` gz blobs, movie credits filled by `radarr_credits_sync`); the
Radarr relational parquet (`radarr/<inst>/relational/movie_person_relations.parquet`)
is an optional supplement for titles the buckets have not reached — it is a
**derivative** of the buckets, one hop further from the truth. On deployments
where the parquet has never been produced, the buckets alone are the movie half
(`GLD-PPL-11`: a missing-parquet early-return once silenced the entire movie
graph and zeroed the C4 signal system-wide — fixed 2026-08-07, 16,155 movies
restored in one rebuild).

Both sources carry stable `tmdb person ids` **and** billing order:

> **C4 is id-keyed on purpose** (immune to "Scarlett Johansson" vs alias drift);
> a name-keyed graph **could not feed it at all**.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md) · [`DESIGN_people_matrix.md`](../DESIGN_people_matrix.md)
- **Mirrors:** [`factories/daemons/bucket_merge.flatten_trakt_people`](../../factories/daemons/README.md)
- **Consumers:** [`scoring/`](../scoring/README.md) Group B + C4 · [`affinity/`](../affinity/README.md) · [`next_watch/`](../next_watch/README.md) (`people_cooccurrence` feed)

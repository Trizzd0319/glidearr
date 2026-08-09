# people_matrix — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **people_matrix**

**Package** — `scripts.managers.machine_learning.people_matrix`
**Status** — ✅ Implemented · 🟢 Strong identity discipline · ✅ Weights measured + finalised 2026-08-07 (`GLD-PPL-13` the sole open item)
**Related** — [README.md](./README.md) · [`DESIGN_people_matrix.md`](../DESIGN_people_matrix.md) · [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md)

---

## 1. Problem statement

*"The household likes Denis Villeneuve"* is a stronger signal than *"the household
likes sci-fi"* — it is specific, it survives genre drift, and it predicts
re-watching. But turning credits into a usable signal has three traps.

**Scale.** The naive structure for "which films share people?" is a person×person
co-occurrence matrix. At this library's ~10–20k credited people that is
**100–400 million cells**, almost all zero, rebuilt every run.

**Identity.** Names are not identifiers. *"Scarlett Johansson"* appears with and
without punctuation, with alternate transliterations, and shares a surname with
unrelated people. A name-keyed graph accumulates duplicates and false merges in
proportion to library size.

**Flooding.** A film's credits list one director and forty actors. Counting every
credit equally means the affinity vector is dominated by tenth-billed bit-parts
and below-the-line crew — people the household has no opinion about — and the
signal for the star is diluted by the noise around them.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Answer multi-person queries without materialising a matrix. |
| G2 | Key on stable ids, never names. |
| G3 | Weight roles by predictive value, not by credit count. |
| G4 | One definition of role weight, shared by aggregation and scoring. |
| G5 | Classify people into the same buckets the display columns use. |
| G6 | Pure — stdlib only, no I/O, no service imports. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Fetching credits | Service reads the daemon buckets and passes decoded dicts. |
| N2 | Caching the result | *"the returned structures are what the manager caches."* |
| N3 | Name resolution | Ids only; names live in the display columns. |
| N4 | A co-occurrence matrix | §3.1 — the whole point is not building one. |

---

## 3. Architecture

### 3.1 🎯 The package is not a matrix, and that is the design

```
inverted index   person_id → {titles}
forward map      title → {role: [person_id]}

"films with A AND B"  =  index[A] ∩ index[B]        O(min(|A|, |B|))
```

> NO N×N matrix is ever materialised (at ~10–20k people that is **100–400M
> cells**; the intersection is `O(min(list))`).

The alternative is quantified rather than dismissed, which is what makes the
argument checkable. A 20k×20k float matrix is ~3.2 GB dense; the inverted index is
proportional to actual credits.

The name is a leftover from the concept, not the implementation. Worth noting
because a reader looking for a matrix will not find one and may conclude the
package is unfinished — the same misreading `quality_analytics`'s stale
`__init__` docstring invites (§8 P-G). `GLD-PPL-06`.

### 3.2 🎯 Id-keyed, and the reason is stated as a hard constraint

> **C4 is id-keyed on purpose** (immune to "Scarlett Johansson" vs alias drift);
> **a name-keyed graph could not feed it at all.**

Not a preference — a precondition. And the source choice follows from it:

> the relational table … is the **ONLY** library source that carries a stable
> `person_tmdb_id` **AND** a `billing_order` for all seven credited roles, which is
> why it — **not** the pipe-joined `*_names` columns on `movie_files` — is what the
> matrix is built from

This is the **third** package to make identity discipline explicit, and the
contrast across the repo is now sharp:

| Package | Keys on | Stated reason |
|---|---|---|
| `discovery/occupancy` | ownership id | *"never a title (remake/same-name collisions)"* |
| `people_matrix` | `person_tmdb_id` | *"immune to alias drift; a name-keyed graph could not feed it at all"* |
| **`labels/labeling`** | **normalised title string** | *"history has no series id/tvdb"* — no id available |

Two packages had an id and used it; one did not and said so. That is not
inconsistency — it is the same principle meeting different data. But it does mean
`GLD-LAB-02` (build the `rating_key → series id` map) is the outstanding piece
that would let the third join the first two.

### 3.3 Billing decay against affinity flooding (G3)

```
billing_weight(rank) = 1 / (1 + 0.25 · rank)
```

> a **gentle hyperbolic falloff rather than a cliff**, so a third lead still counts
> substantially while a tenth-billed cameo does not dominate

> Without it the tenth-billed bit-part actor of a beloved film counts exactly as
> much as its star, which **floods the affinity vector with people the household
> has no opinion about**.

Applied to cast only, and the exemption is reasoned rather than assumed: *"a film
has one director, and 'second credited writer' carries no billing semantics."*
`BILLED_ROLES` is a frozenset rather than a hardcoded check, so adding a billed
role later is a one-line change.

This is the **third** appearance of the same anti-saturation argument:

| Package | Flooding avoided |
|---|---|
| `likelihood` | *"titles pinned at the cap are indistinguishable — a constant"* |
| `next_watch` | Solo at cap ⇒ the member term is invisible on a 258/259 solo distribution |
| `people_matrix` | Undecayed cast ⇒ the vector fills with people the household has no opinion about |

Three different subsystems, one recurring insight: **a signal that saturates
carries no information.** Worth recording as a design principle rather than
rediscovering it a fourth time. `GLD-PPL-05`.

### 3.4 One weight table, two consumers (G4)

> **SINGLE source** for both the aggregation
> (`affinity.genre_affinity.aggregate_person_affinity`) and the scoring term
> (`scoring._shared.person_affinity_score`), so the two never drift.

Same anti-drift instinct as [`foundation/`](../foundation/DESIGN.md)'s delegation
rule and [`discovery/`](../discovery/DESIGN.md)'s injected scorer — and here it is
achieved by a shared constant, which is the cheapest form and holds because both
consumers are inside the brain.

Contrast [`next_watch/DESIGN.md`](../next_watch/DESIGN.md) §3.5, where the same
intent fails because the other consumer is in `services/` and the layering forbids
the import.

### 3.5 🟡 The weights encode a falsifiable claim nobody measures

> **The ordering is the claim being made**: a household re-watches a DIRECTOR's or
> a LEAD's work far more reliably than an EDITOR's. Editors sit lowest because
> editing credits are both the most numerous per title and the least predictive of
> a re-watch; a cinematographer's look is more identifiable, so DPs sit a rung
> above.

This is refreshingly honest — the docstring says outright that the numbers are an
assertion, not a measurement. But that makes the gap explicit: **nothing tests
it.**

And the machinery to test it exists. [`labels/`](../labels/README.md) produces
`watched_within_h`; [`eval/`](../eval/README.md) computes AP; the `sig_*` columns
persist per-group contributions. Measuring whether director-affinity outperforms
editor-affinity as a re-watch predictor is a query against data already on disk.

Until then, seven hand-set weights sit on the same footing as the `≥ 70` figure
that turned out to be stale (D22) — a plausible number nobody has checked.
`GLD-PPL-01`.

### 3.6 🟡 A partial mirror

> The role routing here **MIRRORS** `factories/daemons/bucket_merge.flatten_trakt_people`
> so the matrix classifies a person into the same bucket the display columns do —
> only the captured field differs (`id` here vs `name` there).

Except it mirrors **five of seven** roles: `cinematographers` and `editors` have no
counterpart, *"the daemon's display columns stop at composers."*

So the relationship is *"identical where both exist, plus two"* — correct, and
declared. But it is still a mirror with no enforcement: reclassify a crew role in
`bucket_merge` and this silently disagrees, with the id-graph and the name-columns
bucketing the same person differently.

Milder than [`next_watch`](../next_watch/DESIGN.md) §3.5's duplicate — both sides
are in-repo and could share a constant — which is exactly why it is worth fixing.
`GLD-PPL-02`.

### 3.7 People without an id are dropped

```python
if isinstance(pid, int) and not isinstance(pid, bool) and pid not in out:
```

> a member whose source lacked a tmdb person id **simply doesn't enter the graph**
> — it still appears in the name-based display columns

The consequence is stated, and the fallback is real. But the graph's coverage is
therefore a function of enrichment completeness, and nothing reports what fraction
of credits made it in. A title whose credits carry no ids contributes **nothing**
to C4 while looking fully enriched in the display columns.

That is §8 **P-C**'s shape once more — though correctly handled at the point of
decision (dropped, not zero-valued). What is missing is the *count*.
`GLD-PPL-03`.

The `not isinstance(pid, bool)` guard is a nice detail: Python's `True == 1`, so
without it a malformed credit could enter the graph as person id 1.

### 3.8 Coverage note

I read `build.py`'s first ~105 lines — the module docstring, all five constants,
`billing_weight` and `_ordered_unique_ids`. The remaining ~13 KB —
`build_index`, `co_occurring`, `films_with_all`, `invert_forward`,
`forward_from_relations`, `merge_forward`, `route_people`, `route_people_names`
and the four serialisation helpers — was **not read**. Nothing here asserts their
behaviour.

Test coverage is 9.0 KB against 16.7 KB of source — proportionate, and better than
[`challenger/`](../challenger/DESIGN.md) (zero) or
[`playlists/`](../playlists/DESIGN.md) (spoiler invariant untested).

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Inverted index, no matrix | G1 — 100–400M cells avoided; intersection is `O(min(list))` | Person×person matrix |
| D2 | Key on `person_tmdb_id` | G2 — *"a name-keyed graph could not feed it at all"* | Name keys |
| D3 | Build from the relational table | The only source with both stable ids and billing order for all seven roles | `*_names` columns |
| D4 | Seven roles, not five | Carries *"every credited role the library actually records, not a subset"* | Mirror the daemon exactly |
| D5 | Hyperbolic billing decay, cast only | G3 — a cliff would drop third leads; crew has no billing semantics | Linear decay, or none |
| D6 | Decay 0.25 | rank 1 → 0.80, rank 9 → 0.31 — gentle enough to keep supporting leads | Steeper |
| D7 | One shared weight table | G4 — aggregation and scoring cannot drift | Per-consumer weights |
| D8 | `BILLED_ROLES` as a frozenset | Adding a billed role is a one-line change | Hardcoded `== "cast"` |
| D9 | Drop id-less members | They still appear in display columns; a name in an id-graph is worse than an absence | Synthesise ids |
| D10 | Explicit `bool` exclusion | `True == 1` would enter as person id 1 | `isinstance(pid, int)` alone |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No N×N structure is materialised. |
| I2 | Every graph node is a `person_tmdb_id`; no names key anything. |
| I3 | Role weights have one definition, read by aggregation and scoring. |
| I4 | Billing decay applies only to `BILLED_ROLES`. |
| I5 | `billing_weight(0) == 1.0`; the function is monotone decreasing in rank. |
| I6 | A member without an integer id never enters the graph. |
| I7 | Booleans are never accepted as person ids. |
| I8 | This package performs no I/O. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Credit lacks a tmdb person id | Type guard | Dropped from the graph; name survives in display columns | 🟡 Silent coverage gap | ❌ **None** |
| Enrichment incomplete for a title | — | Title contributes nothing to C4 while looking enriched | 🟡 Same shape as `GLD-ML-04` | ❌ **None** |
| **`bucket_merge` reclassifies a role** | **None** | Id-graph and name-columns bucket the same person differently | 🟡 §3.6 | ❌ **None** |
| Relational table missing for an instance | Service-side | No graph for that instance | 🟡 | ❌ **None** |
| Malformed credit with `id: True` | `bool` guard | Rejected | Safe | ✅ By construction |
| Negative or non-int billing rank | Guard | `1.0` — treated as unbilled | Safe | ❌ None |
| **Role weights are wrong** | **Nothing measures them** | Affinity mis-weighted library-wide | 🟡 §3.5 | ❌ **None** |

**Row 7 is the one with reach.** These weights feed Group B affinity and C4, which
feed the watchability score, which feeds every delete, upgrade and acquisition
decision. Seven numbers asserted from intuition, propagating everywhere,
unmeasured — and the data to check them is already on disk.

---

## 7. Configuration surface

None. Everything is a module constant:

| Constant | Value |
|---|---|
| `ROLES` | 7 role buckets |
| `PERSON_ROLE_WEIGHTS` | cast 1.0 · directors 0.7 · composers 0.4 · **writers 0.375** · cinematographers 0.3 · producers 0.15 · editors 0.2 — **final, set + measured 2026-08-07** (people-following table; producers deliberately low; writers = split-the-difference on a measured tie; ⚠️ applied at both stages ⇒ ratios squared, `GLD-PPL-13`) |
| `PERSON_BILLING_DECAY` | 0.25 — **validated 2026-08-07** (flat measurably worse; ≥0.25 a plateau) |
| `BILLED_ROLES` | `{"cast"}` |
| `RELATION_ROLE_TYPES` | 7 `role_type` → bucket mappings |

---

## 8. Implemented capabilities

- ✅ Inverted person→titles index with set-intersection multi-person queries
- ✅ Role-segmented forward map across all seven credited roles
- ✅ Id-keyed throughout, immune to alias drift
- ✅ Built from the only source carrying both stable ids and billing order
- ✅ Hyperbolic billing decay applied to cast only
- ✅ Single role-weight table shared by aggregation and scoring
- ✅ Boolean-rejecting id validation
- ✅ Serialisation / deserialisation for both index and name maps
- ✅ Forward-map merging and relational-table construction
- ✅ 9.0 KB of tests against 16.7 KB of source

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-PPL-01` | 🟡→✅ **MEASURED 2026-08-07** (billing experiment, temporal split 2026-06-14, 165 train / 42 test / 126 negatives): per-role standalone AUC — producers 0.8549 · writers 0.8388 · cast 0.7851 · directors 0.7303 · composers 0.7209 · cinematographers 0.7206 · editors 0.6831. **The asserted ordering does NOT hold: the 0.3-weighted producers out-predict everything; the 1.0-weighted cast/directors sit mid-pack.** Confounds before acting: producers carry ~5.7 ids/title (densest graph — discrimination partly from connectivity), and sequels/franchise entries in test share producers/writers with train (continuation leakage — writers' lift 32.9 vs AUC 0.84 is the tell). The inversion is real data but plausibly measures "household follows FRANCHISES" as much as "follows people" — which the saga machinery already exploits. Weights NOT flipped on one 42-positive test; decontaminated re-measure filed as `GLD-PPL-12` | §3.5 answered with numbers; interpretation gated on `GLD-PPL-12` | M | `GLD-PPL-12` |
| `GLD-PPL-02` | 🟡 **Share the role classification with `bucket_merge`** rather than mirroring it | §3.6: reclassify a crew role there and the id-graph silently disagrees with the name columns *(P-E)* | S | `GLD-NXW-01` |
| `GLD-PPL-03` | **Report graph coverage** — credits with ids vs without, per title and library-wide | §3.7: a title with no id-bearing credits contributes nothing to C4 while looking fully enriched | S | `GLD-ML-16` |
| `GLD-PPL-04` | ✅ **ANSWERED 2026-08-07: 0.25 is validated — keep it.** Decay sweep at cast≤10: flat 0.00 → AUC 0.8849; 0.25 → 0.8930; 0.60 → 0.8934; 1.00 → 0.8934. Flat is measurably worse (−0.008); everything ≥ 0.25 sits on a plateau (±0.0004 ≈ 2 pair-flips in 5,292 pairs = noise — the tool's max-pick of 0.60 is a tiebreak on that noise, disregarded). Lift keeps rising with decay (10.2 → 13.8) but that is mean-concentration on leads, not better ranking. Cast depth: 10 stands — the tool's knee=3 was a detector artifact of a noisy dip at depth 5 (0.8829), and depths 15/20 are UNTESTABLE because the builders truncate cast to 10 at build time (stored lists can't be deepened by the sweep) | D6 measured: intuition confirmed | M | — |
| `GLD-PPL-05` | 🎯 **Record the anti-saturation principle** in `DOCS_CONVENTIONS.md` — *"a signal that saturates carries no information"* — now independently rediscovered in `likelihood`, `next_watch` and here | Three subsystems reached the same insight separately; a fourth will too | S | `GLD-DIS-04` |
| `GLD-PPL-06` | **Rename the package, or note in-source that no matrix exists** | §3.1: a reader looking for a matrix finds none and may read the package as unfinished *(P-G shape)* | S | `GLD-QAN-03` |
| `GLD-PPL-07` | **Read and document the remaining ~13 KB** — `build_index`, `co_occurring`, `films_with_all`, `route_people`, serialisation | §3.8 | M | — |
| `GLD-PPL-08` | **Surface co-occurrence queries** — *"films with A and B"* is built and has no interface | The package's headline capability is unreachable by an operator | M | `GLD-WEB-04` |
| `GLD-PPL-09` | **Extend the graph to Sonarr** — the relational table cited is Radarr-only | Series credits carry the same signal and are absent | M | `GLD-PPL-07` |
| `GLD-PPL-10` | **Validate `people_cooccurrence`'s 0.60 intent weight** jointly with `GLD-NXW-10` | This graph is the feed; its strength is graded like a third-party recommendation | M | `GLD-NXW-10` |
| `GLD-PPL-11` | 🔴→✅ **THE PUBLISHED PEOPLE GRAPH HAD NO MOVIES AND THE HOUSEHOLD AFFINITY WAS `{}` — the C4 `people_affinity` signal has been dead system-wide.** Discovered via the billing experiment's join (`watched ∩ forward = 0/213`; forward's 4,152 keys ALL `('show', …)`): `_movie_forward`'s missing-parquet check `if not paths: return {}` EARLY-RETURNED past the bucket supplement the docstring declares AUTHORITATIVE ("buckets WIN") — textbook P-B, guard broader than it appears — and `relational/movie_person_relations.parquet` has never existed on this deployment (only `studios.parquet`), so every build published shows-only. Cascade: `aggregate_person_affinity(watched × shows-only-forward)` → `{}` → scorer's `if fwd_raw and aff_raw` gate → `({}, {})` → C4 absent for EVERY title, co-occurrence proposer blind — while `people_matrix.movies.json.gz` (1.5MB, fresh) and `trakt/movies/` blobs (radarr_credits_sync: thousands of tmdb done, incl. 389/12219/76203) sat ready one call away. Fixed: no-parquet ⇒ `return self._movie_forward_from_buckets()` (parquet path unchanged when present). Rebuild required: fingerprint reuse would republish the stale artifact — delete `cache/trakt/people_matrix.state.json` before the next run. Verify in the run log: `[PeopleMatrix] N movies from daemon credits … override the relational tables` + `household affinity over W watched → P people` with P > 0. ✅ **VERIFIED 2026-08-07 post-rebuild: 16,155 movies from daemon credits; 20,282/20,308 titles (100% of enriched), 170,103 distinct people; household affinity over 350 watched → 4,851 people — C4 live for the first time on this deployment.** Cosmetic follow-through: the `src` label said `relational(16,155)` for bucket-sourced movies — relabelled `movie graph(…)` | ✅ Fixed + verified | M | — |
| `GLD-PPL-12` | 🎯→✅ **RESOLVED 2026-08-07 — Franchise-decontaminated role ablation, MODE IMPLEMENTED + RUN + RULED.** (`--decontaminate` in the billing experiment): drops test titles AND negatives sharing a Radarr `collection.tmdbId` with any train title (source: `radarr.movies.*.full.json`, envelope-tolerant loader, unit-checked incl. filter semantics; <10 clean positives prints a directional-only warning). Motivation sharpened by the operator's follow-up question — "should we actually favor writers over actors, but naturally don't as actors are front and center?" — which this run answers with household data: if writers still out-predict cast on unrelated titles, the people-following table should favor them; if it collapses, franchise continuation was the whole effect. 📊 **RESULTS 2026-08-07 (30 clean positives / 90 negatives, ±~0.05 SE): the ordering SURVIVED — producers 0.814 > writers 0.766 > cast 0.747 — but writers-vs-cast is a STATISTICAL TIE (Δ0.019 ≪ 1σ) and producers' lead (~1.3σ) carries a residual confound decontamination cannot reach: TMDB collections miss STUDIO-STABLE continuity (collection-less Pixar-style films sharing producer names = studio taste, already paid via B5 + universe) plus the 5.7-ids/title density effect. Operator verdict: table STANDS — ties go to parsimony; **FINAL RESOLUTION: writers nudged 0.3 → 0.375 ("split the difference") and the thread CLOSED by operator ruling; re-measure at ≥60 clean positives**; DEFINITIVE re-run free at ≥60 clean positives as watch history grows (σ halves). Decay 0.25 + depth 10 reconfirmed on clean data (flat clearly worst, 0.844). Stability-metric mystery resolved: flat scored 0.042 vs 0.087 elsewhere — coarse, not degenerate.** Two tool notes ride along: the split-half stability printed an identical 0.087 for all 11 schemes — verify the metric isn't degenerate at n=165; and depths >10 need a build-side `cast_limit` raise to be measurable at all — ⛔ **operator ruled 2026-08-07: NOT pursued.** Ensemble tentpoles don't need deeper caps: affinity is person-centric library-wide (ensemble members are leads elsewhere), `affinity_topk` consumes only a top-3 mean, decay prices rank-11 at 0.27×, and the depth sweep was flat 3→10 — raising the cap would trade nothing for graph-wide bit-part noise | Decides whether `PERSON_ROLE_WEIGHTS` gets rewritten or the asserted order survives | M | `GLD-PPL-01` |
| `GLD-PPL-13` | 🟡 **Role weights apply TWICE end-to-end — cross-role ratios are effectively SQUARED.** `aggregate_person_affinity` applies `role_weight` building the household vector; `person_affinity_score` applies it AGAIN candidate-side (`(w/max) × rw`). A pure-writer person's end-to-end factor vs cast is `rw²`: 0.09 under the 2026-08-07 table (0.36 before it) — far steeper than the table reads. Predates today; "single shared table so the two never drift" shared the CONSTANT but not the awareness of double application. Decide after `GLD-PPL-12`: if intentional concentration → document at both sites; if double-count → apply role weight at ONE stage only (aggregation is the natural home) and re-check C4 magnitudes | The table's numbers don't mean what they appear to mean | S | `GLD-PPL-12` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ~~Does the role-weight ordering hold empirically?~~ ✅ **Answered 2026-08-07: NO as asserted** — producers topped both raw and decontaminated ablations (studio-stable + density confounds); writers ≈ cast (statistical tie). Table finalised on the ruling + measurement; re-measure at ≥60 clean positives | — |
| Q2 | ~~Is 0.25 the right billing decay for this household?~~ ✅ **Answered 2026-08-07: yes** (plateau from 0.25; flat worse) | — |
| Q3 | What fraction of credits carry a tmdb person id? 🟡 Partially answered: 100% of enriched titles carry ids (20,282/20,308), per-role coverage printed by the experiment's inspect; the per-CREDIT fraction remains unmeasured | `GLD-PPL-03` |
| Q4 | Should the graph cover series as well as movies? — shows are IN the graph via the daemon buckets (4,153); the RELATIONAL leg remains movie-only | `GLD-PPL-09` |
| Q5 | Is the double role-weight application intentional? | `GLD-PPL-13` |

**Q3 gates Q1.** Measuring whether directors out-predict editors is only
meaningful once the graph's coverage is known — if a third of credits lack ids,
the measurement is over a biased sample, and the bias likely correlates with
title obscurity. Coverage first, then the claim.

## 11. Related designs

- [`DESIGN_people_matrix.md`](../DESIGN_people_matrix.md) — the package's own design note
- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — Group B and C4, the consumers
- [`affinity/DESIGN.md`](../affinity/DESIGN.md) — `aggregate_person_affinity`, the other weight consumer
- [`next_watch/DESIGN.md`](../next_watch/DESIGN.md) — the `people_cooccurrence` feed, and the duplicate the layering forces
- [`factories/daemons/README.md`](../../factories/daemons/README.md) — `flatten_trakt_people`, the partial mirror
- [`labels/DESIGN.md`](../labels/DESIGN.md) §3.3 — the title-join this package's discipline argues against

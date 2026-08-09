# contracts — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **contracts**

**Package** — `scripts.managers.machine_learning.contracts`
**Status** — ✅ Implemented · 🟡 `__init__.py` promises re-exports it does not provide
**Related** — [README.md](./README.md) · [`machine_learning/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

The brain must score a Radarr movie and a Sonarr series with one model. Those two
systems agree on almost nothing:

| Fact | Radarr | Sonarr |
|---|---|---|
| Runtime unit | minutes | seconds |
| Video codec location | `movieFile.mediaInfo.videoCodec` (nested) | episode-file row |
| Identity | `tmdb_id` | `tvdb_id` |
| Granularity | one file | many files per series |
| Container | only in `relative_path` extension | only in `relative_path` extension |

If every scorer handled both shapes, each would re-implement the same joins,
unit conversions and null-handling — and they would diverge. The scoring model
would fragment by service, which is exactly what project goal G1 (one model)
exists to prevent.

`contracts/` is the single translation point. After it, no brain module knows
which `*arr` a row came from.

A second problem it solves: **partial data is the normal case, not the exception.**
Enrichment is incremental, some fields are absent from 45% of rows, and a pilot
stub has almost nothing. The contract must make a partial row *safe to construct*
without deciding what a missing value means — because that decision differs per
consumer.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Vendor shape stops here. No API JSON or Parquet column name leaks past this boundary. |
| G2 | A partially-enriched row is always constructible and never raises. |
| G3 | Immutable — a plan or row cannot be mutated after construction. |
| G4 | Space accounting is signed, so a dry-run ledger nets correctly. |
| G5 | Adding a field is backward-compatible by default. |
| G6 | Zero logic. Shapes only. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Validation | A frozen dataclass with defaults is the validation. Rejecting partial rows would break G2. |
| N2 | Serialisation | Parquet/JSON I/O belongs to the service layer. |
| N3 | Defaulting policy | The contract says a field may be `None`; what `None` *means* is the consumer's call (§3.3). |
| N4 | Runtime type enforcement | Dataclasses annotate; nothing checks at runtime. |

---

## 3. Architecture

### 3.1 The boundary

```
Radarr / Sonarr / Plex / Tautulli / Trakt
        │  vendor JSON, Parquet columns, per-service units
        ▼
   service layer ─── the ONLY place a column name is known
        │
        ▼
┌───────────────────────────────────────────┐
│  contracts/                               │
│    feature_rows.py   service → brain      │
│    context.py        service → brain      │
│    plans.py          brain  → service     │
│  frozen dataclasses · no logic · no I/O   │
└───────────────────────────────────────────┘
        │                          ▲
        ▼                          │
   brain: scoring, likelihood,     │  QualityPlan / DeletePlan /
   space, lifecycle, playlists ────┘  DeleteCandidate / MonitorPlan /
                                      GracePlan / AcquirePlan
```

### 3.2 Signed space accounting

`QualityPlan.est_space_gb_signed` and `DeletePlan.reclaim_gb` are signed:

```
+  frees space   (downgrade, delete)
−  consumes it   (upgrade)
```

The source states the reason: *"so the dry-run ledger sums to a true net."* A
gross-reclaim ledger would report a run that upgrades 8 GB and downgrades 3 GB as
11 GB of reclaim, which is not merely imprecise — it is the wrong sign for
capacity planning.

### 3.3 Missing data: two live strategies

This is the most important thing to understand before consuming a contract.
The contract guarantees a `None` is *constructible*, not what it *means*. Two
different strategies are in use, and the difference is deliberate.

**Strategy A — renormalise the axis out.** From `feature_rows.py` on the Group-D
v2 transcode-risk fields:

> All optional: an axis whose input is missing is **renormalised out of the
> weighted risk** rather than scored as zero risk.

Missing input reduces the denominator. A row with three of five axes present is
scored on those three, at full weight. This is the correct handling — absence
does not drag the result down.

**Strategy B — contribute zero.** From the same file, on Group D as a whole:

> Both `None` → D1/D3 contribute **0.0** and D2 falls back to its neutral +2.0.
>
> All `None` on a pilot STUB, which is why a stub scores exactly 0.0 on Group D.

Here absence *is* a low score.

Both appear within one feature group. For the pilot stub this is intentional and
documented — a stub genuinely has no playback quality to assess. But the same
mechanism applies to a *fully-owned* title whose enrichment simply has not run,
and there it produces a systematically lower score for a reason that has nothing
to do with the household's preferences.

**This refines §8 P-C rather than contradicting it.** The pattern is real, and
the fix does not need inventing — Strategy A is already implemented in
[`scoring/device_fit.py`](../scoring/device_fit.py). `GLD-ML-04` is largely a
matter of extending an existing, working pattern to the group level.

### 3.4 Aggregation rules

`ShowFeatureRow` aggregates many episode files into one row, and the choice of
statistic per field is deliberate:

| Field kind | Statistic | Why |
|---|---|---|
| Categoricals (codec, audio codec, container) | **Modal** | The typical file represents the series |
| `video_bitrate` | **Median** | *"one oversized special must not speak for a whole series"* |
| `target_resolution` | **Max** | Best available is what the household can watch |
| `language_consumable_fraction` | **Per-episode fraction** | A dub on only some episodes must not pass the whole series |

The median-not-mean choice for bitrate is the kind of decision that is invisible
until a 4K special in an otherwise-1080p series drags the series' apparent
bitrate above every transcode threshold.

### 3.5 Documented empirical grounding

Several fields carry measured justification in their comments — unusual and
worth preserving:

| Observation | Consequence |
|---|---|
| `video_bitrate` is 0 on **45%** of this library's rows | Scorer falls back to `size_bytes ÷ runtime_minutes` |
| Audio is the **#1 transcode cause, 38% of decisions** | `audio_codec` / `audio_channels` / `audio_languages` are first-class fields |
| Subtitles: the **track count** is the signal, not the languages | Stored slash-joined, counted on read |
| Container appears **only** in `relative_path` | Extension parsing is the sole source |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Frozen dataclasses | G3 — a plan mutated between decision and apply would make the ledger a lie | Mutable / dicts |
| D2 | Every optional field defaults | G2 — partial enrichment is the normal case | Required fields |
| D3 | Signed space | G4 — a dry-run ledger must net, not gross | Unsigned + a direction flag |
| D4 | Column-name knowledge confined to the service layer | G1 — otherwise every scorer re-implements vendor joins | Pass raw rows |
| D5 | `DeleteCandidate` separate from `DeletePlan` | Candidates are *ranked* in a cross-service pool; plans are *decided*. Conflating them loses the pre-decision stage the coordinator needs | One type with a flag |
| D6 | `tier` as an explicit integer (0 watched+grace-expired, 1 unwatched-low) | Makes pool ordering inspectable rather than implied by score alone | Score-only ranking |
| D7 | `critic` as a secondary rank key | Breaks ties between equally-unwatched titles by external quality | Score only |
| D8 | `restore_key` on `DeletePlan` | Deletion must be reversible in the restore-set | Fire and forget |
| D9 | Median bitrate for series aggregation | One oversized special must not characterise a series (§3.4) | Mean |
| D10 | New fields default to a no-op value | G5 — `user_rating: None → user_rating_score returns 0.0 → byte-identical`, stated in-source | Breaking additions |

**D10 is a practice worth naming.** New fields are added with a default that
makes output *byte-identical* until a producer starts populating them. That makes
a contract extension safe to merge independently of the code that fills it.

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No logic in this package — dataclass definitions only. |
| I2 | Every type is `frozen=True`. |
| I3 | No brain module outside this package knows a vendor column name. |
| I4 | Constructing a row with only defaults never raises. |
| I5 | Space figures are signed: + frees, − consumes. |
| I6 | A plan performs no action; the service adapter does. |
| I7 | A new field carries a default that preserves existing behaviour. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| Producer omits a field | Default applies | Consumer sees `None` | Depends on strategy (§3.3) | ❌ **None** |
| Consumer treats `None` as zero where renormalising was intended | **None** | Systematically lower score | 🟡 Biases toward deletion | ❌ **None** |
| Field added without a safe default | Import/construction error | Loud | Immediate | ✅ |
| Type annotation violated at runtime | **None** — nothing enforces | Fails later, further from the cause | Deferred | ❌ **None** |
| `from contracts import QualityPlan` | `ImportError` | `__init__.py` docstring promises re-exports; the module body has none | That call site | ✅ Loud |
| Series aggregation uses the wrong statistic | **None** | Plausible but wrong row | 🟡 Silent | ❌ **None** |

**Row 2 is §8 P-C at the boundary**, and row 5 is a straightforward
documentation-vs-reality mismatch: the package docstring says *"Re-exports the
common types for convenience"* and the module contains only the docstring.

---

## 7. Configuration surface

None. This package reads no config and holds no thresholds.

---

## 8. Implemented capabilities

- ✅ `MovieFeatureRow` — ~45 fields across identity, playback, engagement, critic ratings, classification, protection
- ✅ `ShowFeatureRow` — series aggregation with per-field statistic choices
- ✅ `EpisodeFeatureRow` — episode-level rows carrying the broadcast series score
- ✅ `AffinityContext` — household + per-user affinity, kids/adult split, platform usage, transcode stats
- ✅ `SpaceContext` — floor/target band with `coordinator_owns_deletion`
- ✅ Six plan types covering quality, delete, monitor, grace and acquire
- ✅ Signed space accounting for true dry-run netting
- ✅ Renormalise-out handling for Group-D v2 transcode-risk axes
- ✅ Byte-identical additive field convention
- ✅ Empirical grounding documented in-source (45% zero bitrate, 38% audio transcodes)

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-CON-01` | **Implement the re-exports `__init__.py` promises** — or correct the docstring | §6 row 5: the package docstring advertises an interface that does not exist | S | — |
| `GLD-CON-02` | **Document the two missing-data strategies** (§3.3) as an explicit contract note, so a consumer chooses deliberately rather than by accident | Directly upstream of `GLD-ML-04`; the fix pattern already exists in `device_fit.py` | S | — |
| `GLD-CON-03` | **Coverage field on feature rows** — record which signal groups were populated | Lets a consumer renormalise correctly and lets a score declare its own completeness *(P-C)* | M | `GLD-ML-16` |
| `GLD-CON-04` | **Runtime shape assertions in tests** — construct each type from a real cached row and assert no field silently defaults | §6 row 4: annotations are documentation only | S | — |
| `GLD-CON-05` | **Document the aggregation statistic per `ShowFeatureRow` field** in a table beside the dataclass | §3.4 is inferable from comments but not stated as a contract | S | — |
| `GLD-CON-06` | **`__slots__` or `slots=True`** on the dataclasses | ~45-field rows constructed per title across a large library | S | Python version check |
| `GLD-CON-07` | **Contract versioning** so a shape change is detectable by the replay harness | A silent field change invalidates historical replay | M | `GLD-ML-06` |
| `GLD-CON-08` | **Reject unknown kwargs explicitly** at construction | A typo'd field name currently raises `TypeError` — good — but the message is unhelpful at this field count | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `None` mean "renormalise out" everywhere, with an explicit sentinel for "known absent"? That would make §3.3's two strategies expressible rather than implicit. | `GLD-CON-02`, `GLD-ML-04` |
| Q2 | Should `__init__.py` re-export, or should consumers always import from the specific module? | `GLD-CON-01` |
| Q3 | Is `DeleteCandidate.tier` extensible — are there more than two tiers coming? | — |
| Q4 | Should `AffinityContext` carry a freshness stamp so a consumer knows how stale the household signal is? | `GLD-TAUT-03` |

**Q1 is the substantive one.** A single `None` currently carries two meanings —
"not fetched" and "genuinely absent" — and consumers guess which. A sentinel
distinguishing them would let Strategy A apply automatically to the first and
Strategy B to the second, which is exactly the P-C fix stated as a type.

## 11. Related designs

- [`machine_learning/DESIGN.md`](../DESIGN.md) §3.2 — the scoring model consuming these rows
- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — signal groups A–F
- [`scoring/device_fit.py`](../scoring/device_fit.py) — the working renormalisation pattern
- [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §8 P-C
- [`services/DESIGN.md`](../../services/DESIGN.md) §3.4 — the producer side

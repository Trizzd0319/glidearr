# scoring — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **scoring**

**Package** — `scripts.managers.machine_learning.scoring`
**Status** — ✅ Implemented · 🟡 Not fully pure (two signals read the clock) · 🟡 Two "4K threshold" scales in circulation
**Related** — [README.md](./README.md) · [SCORING_GROUPS.md](./SCORING_GROUPS.md) · [`machine_learning/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Every destructive decision Glidearr makes needs a comparable number for "how much
is this title worth to this household." Three constraints make that hard:

1. **No label exists.** Nobody rates every title. The signal is indirect —
   completion, rewatch, watchlist membership, cast overlap, critic consensus,
   whether it plays without transcoding.
2. **Signals are wildly unequal in reliability.** A `keep_forever` tag is an
   explicit statement. A critic average is a stranger's opinion. Blending them
   naively lets the weakest signal dominate through sheer magnitude.
3. **The output drives irreversible actions.** A wrong low score deletes
   something. There is no feedback loop — the error surfaces months later when
   someone looks for the file.

The answer is **weighted, independently-capped signal groups**. Each group answers
one question with a bounded point budget. Capping per group means an acclaimed
film still needs household signal to reach the top, and an obscure favourite is
not sunk by a missing critic rating.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One score, consumed by acquisition, quality, space and monitoring alike. |
| G2 | No single signal can dominate. |
| G3 | Explainable — every score decomposable into named contributions. |
| G4 | Movie and TV engines cannot drift. |
| G5 | Rungs calibrated to this household, not invented. |
| G6 | Changing weights is regression-tested against a frozen corpus. |
| G7 | Requesting the breakdown never changes the score. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Generalising across households | Deliberately overfit to one library. |
| N2 | Learned weights | Interpretability (G3) outweighs marginal accuracy for destructive calls. |
| N3 | Calibrated probability | It is a ranking score, not a likelihood. `likelihood/` handles that separately. |
| N4 | Hard group caps | Budgets are design guides; only the final clamp is enforced (§3.2). |

---

## 3. Architecture

### 3.1 Composition

```
MovieFeatureRow / ShowFeatureRow  +  AffinityContext
        │
        ▼
   ┌──────────────────────────────────────────────┐
   │  A  household intent   keep tag · completion │  ≈33
   │                        rewatch · rating      │
   │                        watchlist intent      │
   │  B  people affinity    cast/crew overlap     │
   │  C  related graph      Trakt related ·       │  = 0 for shows
   │                        franchise · collection│
   │  D  device fit         resolution · codec ·  │  device_fit.py
   │                        bitrate · audio ·     │  renormalises
   │                        subs · container      │  missing axes out
   │  F  critic             imdb/tmdb/trakt/      │  critic.py
   │                        metacritic/RT blend   │
   │  G  penalties          language · abandoned ·│  negative
   │                        panned · unavailable  │
   └──────────────────┬───────────────────────────┘
                      ▼
        final = clamp(round(Σ contributions), 0, 100)
                      │
      return_breakdown=True → + per-group dict, score unchanged (G7)
```

`_shared.py` holds the tables and helpers both engines use — the structural
guarantee behind G4. `score_show` mirrors the same taxonomy; the only differences
are A2 (recency + breadth rather than completion), F1 (TV rating sources) and
Group C (zero).

### 3.2 Soft budgets, hard clamp

Group budgets are **design guides, not enforced caps**. Only the final clamp to
`[0, 100]` is applied after summation. Two consequences:

- A genuine favourite stacks signals well past a group's nominal budget —
  *The Princess Bride* reaches 67 by stacking A1+A2+A3+A4+B1+F1.
- Heavy penalties can drive a total negative before the clamp — the panned
  foreign sequel totals −23 and floors at 0.

The clamp is doing real work at both ends.

### 3.3 🟡 Two "4K threshold" scales

This is the most confusable thing in the package, and it has already propagated
into other docs.

| Scale | Value | Meaning |
|---|---|---|
| **Watchability ladder** (this package, 0–100) | **38** (p99.5) | 4K *entry rung* — the tier the ladder **proposes** |
| `watch_likelihood.uhd_cutoff` | **75** | Gate on actual 4K acquisition |
| `routing.movies.4k_dual_min_score` | **75** | Gate on dual-version routing |

[`SCORING_GROUPS.md`](./SCORING_GROUPS.md) states plainly that the latter two
*"live on a different scale."*

**⚠️ Correction needed in the wider docs.** Several `DESIGN.md` files written
during this sweep — including [`scripts/DESIGN.md`](../../../DESIGN.md) §5 I2,
[`DOCS_CONVENTIONS.md`](../../../DOCS_CONVENTIONS.md) §7,
[`services/DESIGN.md`](../../services/DESIGN.md), [`radarr/DESIGN.md`](../../services/radarr/DESIGN.md)
and [`machine_learning/DESIGN.md`](../DESIGN.md) — assert **"4K requires score ≥ 70
on the 100-point scale."** That statement matches neither figure here: the
watchability ladder's 4K entry is 38, and the acquisition gates are 75 on a
different axis.

The `≥ 70` figure came from a project-wide convention note, not from this code.
It may refer to a third scale, be a rounded recollection of the 75 cutoff, or be
stale. **It should not be silently "corrected" to 75** — it needs an owner's
answer. Tracked as `GLD-SCO-01` / decision **D22**.

### 3.4 The ladder is knowingly miscalibrated at the 4K rung

Stated in [`SCORING_GROUPS.md`](./SCORING_GROUPS.md):

> The 4K entry rung admits **34 titles** against the **67** the household keeps at
> 2160p today; the rung that reproduces its own curation is **p99 (33)**.
> Documented, deliberate, and overridable.

So the ladder as shipped is roughly half as permissive at 4K as the household's
own behaviour. That is a deliberate, documented conservatism rather than an
error — the safe direction for a threshold that costs disk — but it means the
ladder does **not** reproduce observed curation at that rung, and anyone
comparing "what we'd pick" to "what we keep" will see a gap by design.

### 3.5 🟡 The scorer is not fully pure

Also stated in [`SCORING_GROUPS.md`](./SCORING_GROUPS.md):

> **F3 and G4 read the wall clock.** Both call `datetime.now()` internally
> (recency / "has a release date passed"), so `score_movie` is **not** fully pure
> — its output for those two signals depends on the day it runs.

This is honest and documented, and [`test_score_golden.py`](./test_score_golden.py)
deliberately holds those two clock-stable so the byte-identity oracle works.

But it has a consequence the docs elsewhere do not acknowledge:
[`machine_learning/DESIGN.md`](../DESIGN.md) invariant **I10** asserts *"given
identical inputs, output is deterministic."* For F3 and G4 that is false. Replay
([`eval/replay.py`](../eval/)) cannot reproduce a historical score exactly unless
it injects the original clock.

The fix is well-understood: inject `now` as a parameter rather than reading it.
Tracked as `GLD-SCO-02`.

### 3.6 Explainability is free

`return_breakdown=True` returns a per-group dict
(`{"A1_keep_policy": 15.0, …, "_total_raw": 71.25, "_total_final": 71}`) and the
score is **identical** either way (G7, tested). The persistence path takes the
breakdown; decision paths take the bare int.

This is what makes a score explainer (`GLD-ML-07`) cheap — the data already exists.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Weighted, independently-capped groups | G2 — otherwise the highest-magnitude signal dominates | Single weighted sum |
| D2 | 0–100 float, replacing 1–10 int | Enough resolution for percentile rungs | Keep the integer scale |
| D3 | Soft budgets, hard clamp | Lets a true favourite stack past a budget while bounding the output (§3.2) | Enforce per-group caps |
| D4 | Rungs from real percentiles | G5 — invented thresholds do not reproduce a household's behaviour | Round numbers |
| D5 | Ladder *proposes*; separate gates *decide* 4K | Quality tier and acquisition spend are different questions | One threshold |
| D6 | Conservative 4K rung (34 vs 67 titles) | The safe direction for a threshold that costs disk (§3.4) | Match observed curation |
| D7 | `_shared.py` for common tables | G4 — the structural reason the two engines cannot drift | Duplicate per engine |
| D8 | Breakdown is additive and free | G3 + G7 | Separate explain path |
| D9 | Golden-corpus byte-identity oracle | G6 — catches unintended score movement from any weight change | Unit tests only |
| D10 | Group C zero for shows | The related-graph signal is movie-shaped; forcing it on TV would add noise | Force parity |
| D11 | `device_fit` renormalises missing axes out | Absence should not read as zero risk | Score missing as 0 |
| D12 | Clock-reading confined to F3 and G4, documented | Recency genuinely depends on now; isolating it bounds the impurity | Pretend it is pure |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Final score is `[0, 100]`. |
| I2 | `return_breakdown=True` never changes the score. |
| I3 | Movie and show engines share `_shared.py` tables. |
| I4 | HD-720p is the floor; SD is absorbed into 720p. |
| I5 | The ladder proposes a tier; it never authorises acquisition. |
| I6 | A missing device-fit axis is renormalised out, not scored zero. |
| I7 | Golden-corpus scores are byte-identical across refactors. |
| I8 | **Except F3 and G4**, the scorer is deterministic given identical inputs. |

**I8 is the honest form of `machine_learning/DESIGN.md` I10**, which currently
overstates determinism.

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| Enrichment incomplete | Group-level | D1/D3 contribute 0.0; D2 falls to neutral +2.0 | 🟡 Systematically lower score | ❌ **None** |
| Pilot stub scored | By design | Group D exactly 0.0 | Intended | ✅ Documented |
| Missing device-fit axis | `device_fit.py` | Renormalised out — correct | None | ✅ |
| Critic sources absent | Group F | No bonus; not a penalty | Correct | ✅ |
| Weight change alters scores | [`test_score_golden.py`](./test_score_golden.py) | Test fails | Caught pre-commit | ✅ Loud |
| Replay across days | **None** | F3/G4 differ | 🟡 Replay not byte-exact | ❌ **None** |
| Ladder consulted as a 4K authority | **None** | Proposes 2160p at score 38 while acquisition gates at 75 | 🟡 Confusion, not action | ❌ **None** |
| Score compared across a ladder recalibration | **None** | Rungs move when the axis moves | 🟡 Historical scores incomparable | ❌ **None** |

Rows 1, 6, 7 and 8 have no signal. Row 8 matters for `eval/`: a score recorded
before a recalibration is not comparable to one after it, and nothing stamps
which axis version produced a ledger row.

---

## 7. Configuration surface

| Key | Effect |
|---|---|
| `scoring.quality_ladder` | Overrides `QUALITY_PROFILE_THRESHOLDS` rungs |
| `watch_likelihood.uhd_cutoff` | 75 — gates 4K acquisition (**different scale**, §3.3) |
| `routing.movies.4k_dual_min_score` | 75 — gates dual-version routing (**different scale**) |
| A2 completion threshold | ≈0.9 for the top completion band |
| Weight/threshold overrides | via [`thresholds/registry.py`](../thresholds/) |

---

## 8. Implemented capabilities

- ✅ 0–100 watchability score from capped groups A–G
- ✅ Movie and show engines sharing `_shared.py`, structurally drift-proof
- ✅ Percentile-calibrated quality ladder over 6,449 file-owning titles
- ✅ Group D v2 transcode-risk axes with renormalise-out handling
- ✅ Multi-source critic blend
- ✅ A5 watchlist intent: source × recency × members, feed-ranked
- ✅ G1 language consumability, softened by household audio history
- ✅ Free per-group breakdown
- ✅ Golden-corpus byte-identity regression
- ✅ Documented worked examples at both ends of the scale

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SCO-01` | 🔴 **Resolve the "4K ≥ 70" statement** — it matches neither the ladder's 38 nor the gates' 75. Then correct every `DESIGN.md` and `DOCS_CONVENTIONS.md` §7 that repeats it | A project-wide invariant is stated in terms that do not match either live threshold (§3.3) | S | D22 |
| `GLD-SCO-02` | **Inject `now` into F3/G4** instead of reading the clock | Makes the scorer genuinely pure and replay byte-exact (§3.5); fixes the overstatement in `machine_learning` I10 | S | — |
| `GLD-SCO-03` | **Stamp the ladder/axis version on every ledger row** | Scores either side of a recalibration are not comparable, and nothing records which axis produced a row (§6 row 8) | S | `GLD-ML-06` |
| `GLD-SCO-04` | **Group-level renormalisation**, extending `device_fit.py`'s pattern | The P-C fix; the working pattern already exists in this package | M | `GLD-CON-03` |
| `GLD-SCO-05` | **Reconcile the 4K rung with observed curation** — 34 admitted vs 67 kept — or document the gap in the ladder table itself | §3.4 is deliberate but invisible to anyone reading only the table | S | D22 |
| `GLD-SCO-06` | **Score explainer surface** consuming the existing breakdown dict | Explainability is already computed and thrown away on decision paths | M | `GLD-ML-07`, `GLD-WEB-13` |
| `GLD-SCO-07` | **Rename the scoring-group codes or the enhancement-batch labels** — `A1`/`C2` collide across two namespaces | `SCORING_GROUPS.md` opens with a caution about this; the collision is a standing readability tax | S | — |
| `GLD-SCO-08` | **Expand the golden corpus with adversarial cases** — all-penalties, all-missing, stub, single-signal | Current corpus is regression-only | S | `GLD-ML-14` |
| `GLD-SCO-09` | **Publish the score distribution** each run — histogram + rung occupancy | Makes ladder drift visible; the calibration data exists but is computed once | S | `GLD-WEB-12` |
| `GLD-SCO-10` | **Per-group contribution stats** across the library — which groups actually move scores | Would show whether any group is effectively inert | S | `GLD-SCO-06` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What does "4K requires score ≥ 70" refer to — `uhd_cutoff` (75), a third scale, or a stale figure? *(= D22)* | `GLD-SCO-01` |
| Q2 | Should the 4K rung be moved to p99 (33) to reproduce observed curation, or stay conservative at p99.5 (38)? | `GLD-SCO-05` |
| Q3 | Should F3/G4 stay clock-reading, or take an injected `now`? | `GLD-SCO-02` |
| Q4 | Are group budgets meant to become hard caps, or stay soft guides? | — |
| Q5 | Is Group C genuinely inapplicable to shows, or just unbuilt? | `GLD-SCO-10` |

## 11. Related designs

- [`SCORING_GROUPS.md`](./SCORING_GROUPS.md) — the full signal reference
- [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md)
- [`contracts/DESIGN.md`](../contracts/DESIGN.md) §3.3 — missing-data strategies
- [`likelihood/`](../likelihood/) — `uhd_cutoff` and the other scale
- [`thresholds/`](../thresholds/) — override surface
- [`DESIGN_per_person_codec_profiles.md`](../DESIGN_per_person_codec_profiles.md)

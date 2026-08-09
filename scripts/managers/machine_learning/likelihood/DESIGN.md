# likelihood — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **likelihood**

**Package** — `scripts.managers.machine_learning.likelihood`
**Status** — ✅ Implemented · 🟡 Welded to the scoring axis with no detector
**Related** — [README.md](./README.md) · [`scoring/DESIGN.md`](../scoring/DESIGN.md) · [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md)

---

## 1. Problem statement

"Earn your quality tier." A 4K Remux is ~60 GB; a 720p WEB-DL is ~2 GB. Spending
the former on something nobody will replay is the single most expensive mistake
the system can make at scale, and it is silent — the disk just fills.

But *how much the household values a title* is not the same question as *how
likely is it to be played again*. A critically acclaimed film the household
rated 9/10 and watched once is highly valued and unlikely to be replayed. A
mediocre comfort show on its fifth rewatch is the opposite.

This package answers the second question, and the design problem is
**asymmetric evidence**:

- **Behaviour is strong evidence.** Three rewatches is near-proof.
- **Taste is weak evidence.** Matching cast/genre affinity suggests interest, not
  replay.

Treating them as commensurable — summing or averaging — lets accumulated weak
signal buy a 60 GB file for something never played. The structural answer is
`max(floor, propensity)` with the affinity term **capped one point below the 4K
gate**.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Behaviour floors the tier; taste can only raise an untouched title. |
| G2 | Affinity alone can never reach 4K. |
| G3 | The cold end is sticky — an unwatched, un-affine title stays at 720p. |
| G4 | Callers only ever upgrade, never silently downgrade. |
| G5 | A sub-threshold play can never *raise* a quality target. |
| G6 | One rule, applied identically across Radarr and Sonarr paths. |
| G7 | Pure — config and row reads only. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Calibrated probability | It is an ordering device with named thresholds, not a real `P()`. |
| N2 | Predicting *when* | Only whether, and therefore what tier. |
| N3 | Sharing a scale with watchability | Different axes by design (§3.1). |
| N4 | Executing upgrades | Returns a target profile/resolution; services apply. |

---

## 3. Architecture

### 3.1 🟡 This is a *different* 0–100 axis from watchability

Both scales run 0–100. They are not the same axis and their thresholds are not
interchangeable. This has already caused a documentation error elsewhere in the
repo.

| Axis | Owner | 4K-relevant threshold |
|---|---|---|
| **Watchability score** | [`scoring/`](../scoring/README.md) | Ladder 4K *entry rung* = **38** (p99.5) — proposes a tier |
| **Likelihood** | this package | `uhd_cutoff` = **75** — gates 4K acquisition |
| Routing | `routing.movies.4k_dual_min_score` | **75** |

**Bearing on decision D22.** `DOCS_CONVENTIONS.md` §7 recorded *"4K eligibility:
score ≥ 70 on the 100-point scale."* Having now read both engines, the only
"≥ 70-ish on a 100-point scale gates 4K" mechanism in the codebase is
**`uhd_cutoff = 75` on the likelihood scale**. The watchability ladder's 4K entry
is 38 and is explicitly described as proposing, not authorising.

So `≥ 70` is most likely a rounded or stale recollection of 75 — but it is a
5-point difference on a threshold that governs 60 GB files, and it should be
confirmed rather than assumed. D22 stands, now with a strong candidate answer.

### 3.2 The composition

```
likelihood = max( ENGAGEMENT FLOOR , AFFINITY PROPENSITY )

ENGAGEMENT (graded by watch count)          AFFINITY (untouched only)
  watched ×1        → 50                      untouched_base 25
  each further      → + rewatch_step            + watchability_score × 1.0
  ceiling           → 90                      capped at affinity_cap 74
  20–90 % complete  → 45                                        │
  < 20 %            → ≤ 25                    74 < uhd_cutoff 75 ┘  ← G2
  never touched     → the affinity branch
```

Evaluation order matters: the partial-view branches are checked **before** the
untouched branch, so a sub-threshold play cannot fall through to
`untouched_base + score` (G5). Measured after the global watched bar landed:
**0 titles moved from a floor branch to UNTOUCHED.**

`affinity_cap` sitting exactly one point below `uhd_cutoff` is not a coincidence
— it is the mechanism that makes G2 structural rather than advisory.

### 3.3 🟡 Welded to the scoring axis at gain 1.0

The critical coupling, stated in-source:

> **THIS BRANCH CONSUMES A RAW `watchability_score` AT GAIN 1.0**, so it is
> welded to the 0–100 scoring axis and every translation of that axis lands on
> it in full.

The engagement floors (50/64/78/90) and cutoffs (75/45/20) are constants **on the
likelihood scale**. Only the affinity term moves with the scoring axis — and it
moves one-for-one.

**This has already fired once.** Group D v2 replaced a near-constant +12 bonus
that 92 % of the library received with a 0-to-negative transcode-risk penalty.
That translated the score axis **down ~13 points**:

| Population | Median | p99 | Max |
|---|---|---|---|
| Owned movies | 21 → 8 | 49 → 34 | 71 → 58 |
| File-owning series | 21 → 8 | 43 → 30 | — |

With `untouched_base` left at 12, an untouched title still needed score ≥ 33 to
clear `fhd_cutoff` — a bar that used to sit at the top ~8 % of untouched movies
and now sat past p99.9.

> **MEASURED on the real cache: untouched titles reaching 1080p collapsed
> 456 → 8 (−98.2 %).**

A change in one package silently removed 98 % of a quality tier in another. It
was caught by measurement, **not by any automated check** — and no such check
exists today.

### 3.4 The re-anchor, and why it was a translation

The inverse of a translation is a translation, so `untouched_base` moved 12 → 25.
Three independent boundary checks agree on ~+13:

| Boundary | v1 score needed | Untouched titles at/above | v2 score, same tail mass | ⇒ base′ |
|---|---|---|---|---|
| `fhd_cutoff` / WEB | 33 | 456 | 20.0 | **25.0** |
| Bluray-1080p rung | 43 | 34 | 29.0 | 26.0 |
| Remux-1080p rung | 53 | 1 | 38.0 | 27.0 |

25 is also exactly the median shift of the axis (21 → 8). It restores the
file-owning untouched population reaching 1080p to **461 vs 456 (+1.1 %)** —
movies 134 vs 145, series 327 vs 311.

Neighbouring integers do far worse — **24 → 351 (−23 %)**, **26 → 573 (+26 %)** —
because the v2 score is integer-valued and heavily tied, so no mapping can land
between them.

### 3.5 Why a gain change was rejected

Three measured reasons, and the second is the interesting one:

1. **It over-corrects the upper rungs.** Any gain reproducing the 1080p count also
   multiplies the spread: Bluray-1080p 34 → 51–61, Remux-1080p 1 → 5.
   `base=25/gain=1.0` gives 28 and 1.

2. **It re-creates the problem the exercise removed.** Gain saturates titles
   against `affinity_cap`, and titles pinned at the cap are indistinguishable —
   *a constant*. At gain 2.2, 36 untouched titles (132 library-wide) sit at
   exactly 74. At `base=25/gain=1.0`, **zero** untouched titles reach the cap, so
   the affinity ordering is preserved exactly — an unclamped affine map is
   order-isomorphic.

3. **It makes the low end less sticky, not more.** The cold floor is defined by
   how much score it takes to escape 720p: **20 points** at `base=25/gain=1.0`,
   only **15** at gain 2.2. G3 wants more stickiness, not less.

Reason 2 is a genuinely elegant argument: Group D v2's whole purpose was removing
a near-constant that carried no information, and a gain change would have
reintroduced one at the other end of the scale.

### 3.6 Percentile mode needs no re-anchor — verified, not assumed

`untouched_mode: "percentile"` reads `watchability_percentile`, computed by
`refresh_scores` as `rank(pct=True) * 100` — a **pure rank**, so its distribution
is uniform whatever the score axis does.

Measured: untouched titles reaching 1080p under percentile mode went
**radarr 652 → 652 (0.0 %)**, **sonarr 4,690 → 4,922 (+4.9 %)**. The residual is
tie-structure churn inside `method="average"`, not a level shift — and two orders
of magnitude smaller than absolute mode's −98.2 %.

That is the structural argument for percentile mode: it is **immune to §3.3's
coupling by construction**.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | `max(floor, propensity)`, not a sum | G1 — behaviour and taste are not commensurable; summing lets weak signal buy 60 GB | Weighted sum |
| D2 | `affinity_cap` 74 = `uhd_cutoff` 75 − 1 | G2 structural, not advisory | Cap at the gate, or above |
| D3 | 4K needs ≈3+ watches | 4K is the most expensive tier; only demonstrated replay justifies it | One watch |
| D4 | Partial-view branches before untouched | G5 — otherwise abandoning a title *raises* its target | Single branch |
| D5 | Explicit profile ladder for Radarr, resolution cap for Sonarr | Radarr distinguishes sub-tiers at one resolution; Sonarr's profile ids differ | One mechanism |
| D6 | `ladder_rank()` so callers only upgrade | G4 — prevents a likelihood dip silently downgrading a file | Compare likelihood directly |
| D7 | `untouched_base` re-anchored 12 → 25 | The inverse of a translation is a translation (§3.4) | Leave it; change gain |
| D8 | Gain change rejected | Over-corrects, re-creates a constant at the cap, reduces stickiness (§3.5) | Rescale |
| D9 | Percentile mode verified, not assumed | A pure rank is axis-invariant — but it was measured anyway | Assume invariance |
| D10 | All thresholds config-tunable | Household-specific by nature | Hardcode |

---

## 5. Invariants

Every one of these is pinned by [`test_untouched_anchor.py`](./test_untouched_anchor.py).

| # | Invariant |
|---|---|
| I1 | `affinity_cap` (74) < `uhd_cutoff` (75) — taste alone reaches Remux-1080p, never 4K. |
| I2 | A cold unwatched title with no affinity lands at 25 — below `fhd_cutoff` (45), so 720p floor. |
| I3 | Watched titles keep their engagement-decided tier byte-for-byte. |
| I4 | The 4K trio (19 entry-4K + 8 Bluray-2160p + 75 Remux-2160p) is identical under every mapping tried. |
| I5 | No untouched title reaches `affinity_cap` at gain 1.0 — affinity ordering is preserved exactly. |
| I6 | Partial-view branches are evaluated before the untouched branch. |
| I7 | Callers only upgrade (target rank > current rank). |
| I8 | This package performs no I/O. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| **Scoring axis translates** | **None — measurement only** | Untouched branch re-anchors silently; cost −98.2 % of a tier once | 🔴 Library-wide quality shift | ❌ **None** |
| Watched bar changes | None here | Engagement branch membership shifts | 🟡 Tier changes | ❌ **None** |
| `watchability_score` absent | Affinity term 0 | Lands at `untouched_base` 25 → 720p floor | Safe | ❌ **None** |
| Ladder misconfigured | Rung lookup | Falls to the nearest lower rung | 🟡 Wrong tier | ❌ **None** |
| Likelihood dips below current tier | `ladder_rank` | No downgrade (I7) | Safe | ✅ |
| Percentile mode with a changed axis | Rank invariance | ±0–5 % tie churn | Negligible | ✅ Measured |
| Affinity saturation at the cap | I5 | Cannot occur at gain 1.0 | Safe | ✅ Tested |

**Row 1 is the standing risk.** The coupling is documented, the re-anchor was
done carefully, and the invariants are tested — but nothing *detects* the next
axis translation. The tests pin the current anchor's behaviour; they do not fire
when `scoring/` moves underneath.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `watch_likelihood.uhd_cutoff` | **75** | 4K gate |
| `watch_likelihood.affinity_cap` | **74** | Affinity ceiling — must stay below `uhd_cutoff` |
| `watch_likelihood.fhd_cutoff` | 45 | 1080p gate |
| `watch_likelihood.hd_cutoff` | 20 | No behavioural effect (both res values are 720) |
| `watch_likelihood.watched_floor` | 50 | One watch |
| `watch_likelihood.rewatch_floor` | 90 | Rewatch ceiling |
| `watch_likelihood.rewatch_step` | — | Per-rewatch increment |
| `watch_likelihood.started_floor` | 45 | 20–90 % complete |
| `watch_likelihood.abandoned_ceiling` | 25 | <20 % complete |
| `watch_likelihood.untouched_base` | **25** | Never-watched anchor (§3.4) |
| `watch_likelihood.untouched_mode` | absolute | `absolute` \| `percentile` |
| `watch_likelihood.untouched_pct_floor` | 0.0 | Percentile-mode floor |
| `radarr_quality_ladder` | — | `[[min_likelihood, profile_id], …]` ascending |

---

## 8. Implemented capabilities

- ✅ `max(engagement, affinity)` composition with structural 4K protection
- ✅ Watch-count-graded engagement floors
- ✅ Partial-view grading, ordered before the untouched branch
- ✅ Explicit Radarr profile ladder with rank-based upgrade-only semantics
- ✅ Sonarr resolution-cap mapping
- ✅ `explain_likelihood` for attribution
- ✅ Re-anchored `untouched_base` with three independent boundary confirmations
- ✅ Percentile mode, empirically verified axis-invariant
- ✅ Full invariant test coverage in `test_untouched_anchor.py`
- ✅ Saga engagement / order / progress
- ✅ Survival modelling
- ✅ Every threshold config-tunable

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-LIK-01` | 🔴 **Axis-coupling detector** — assert the scoring-axis distribution (median/p99) against a recorded baseline; fail loudly when it translates | §3.3 cost **−98.2 % of a quality tier** and was caught only by manual measurement. Nothing detects the next one | M | `GLD-SCO-03` |
| `GLD-LIK-02` | **Assert `affinity_cap < uhd_cutoff`** at config load | I1 is the structural guarantee that taste never buys 4K; config can currently break it | S | — |
| `GLD-LIK-03` | **Default `untouched_mode` to `percentile`** | Percentile mode is immune to §3.3's coupling *by construction*, and was measured at 0.0 % / +4.9 % drift versus absolute's −98.2 % | S | `GLD-LIK-01`, Q2 |
| `GLD-LIK-04` | **Surface the likelihood distribution** per run — branch membership and rung occupancy | The −98.2 % collapse would have been immediately visible | S | `GLD-SCO-09` |
| `GLD-LIK-05` | **Report `explain_likelihood` attribution counts** — how many titles landed on each branch | Makes D3/D4 effectiveness measurable | S | `GLD-LIF-03` |
| `GLD-LIK-06` | **Warn when `hd_cutoff` is crossed** with no behavioural consequence, or remove it | A configured threshold that cannot affect anything is misleading | S | — |
| `GLD-LIK-07` | **Validate `radarr_quality_ladder`** is ascending and its profile ids exist | A misordered ladder silently mis-tiers | S | — |
| `GLD-LIK-08` | **Re-verify the anchor after any scoring change** as a documented procedure | The three-boundary method worked; it is currently tribal knowledge in a docstring | S | `GLD-LIK-01` |
| `GLD-LIK-09` | **Record the anchor's provenance in the ledger** — which `untouched_base` produced a decision | Historical decisions are uninterpretable after a re-anchor | S | `GLD-SCO-03` |
| `GLD-LIK-10` | **Test the `affinity_cap` saturation invariant** against live data each run, not just in unit tests | I5 holds at gain 1.0 today; a config change breaks it silently | S | `GLD-LIK-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is `uhd_cutoff = 75` the "≥ 70" recorded in `DOCS_CONVENTIONS.md` §7? *(= D22)* | `GLD-SCO-01` |
| Q2 | Should `untouched_mode` default to `percentile`, given it is structurally immune to axis translation? | `GLD-LIK-03` |
| Q3 | Should `hd_cutoff` be removed, since `hd_res` and `floor_res` are both 720? | `GLD-LIK-06` |
| Q4 | What is the right rewatch count for 4K — is ≈3 empirically correct? | `GLD-LIK-05` |
| Q5 | Should the anchor be derived automatically from the axis distribution rather than hand-set? | `GLD-LIK-01` |

**Q5 is the durable fix.** `untouched_base` is a hand-computed inverse of an axis
translation. Deriving it from the measured distribution each run would make §3.3
self-correcting instead of a documented hazard.

## 11. Related designs

- [`scoring/DESIGN.md`](../scoring/DESIGN.md) §3.3 — the other 0–100 axis and D22
- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — Group D v2, the translation that fired this
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) — the watched bar feeding the engagement branch
- [`MATH_FOUNDATION.md`](../MATH_FOUNDATION.md)
- [`space/DESIGN.md`](../space/DESIGN.md) — the downgrade path these targets feed

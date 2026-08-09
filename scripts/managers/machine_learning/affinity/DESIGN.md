# affinity — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **affinity**

**Package** — `scripts.managers.machine_learning.affinity`
**Status** — ✅ Implemented · 🟢 Decay built but disabled · 🔴 Carries a sixth "watched" bar
**Related** — [README.md](./README.md) · [`scoring/DESIGN.md`](../scoring/DESIGN.md) · [`services/tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md)

---

## 1. Problem statement

Nobody labels their taste. The only evidence is behaviour: what got played, by
whom, how far through, on what device. Turning that into `{name: weight}` maps
the scorer can read has four traps:

1. **Sparse metadata.** Tautulli's per-key metadata fetch is *sampled*. A
   low-volume viewer's handful of `rating_key`s may never make the index — and
   an empty affinity map is indistinguishable from "no strong preferences",
   collapsing that user onto the flat household ranking.

2. **Unstable keys.** `rating_key` churns as Plex re-scans. A metadata index
   keyed on it silently loses entries.

3. **Household ≠ individual.** One person's action-heavy history should not
   define the household's taste, and a child's completion habits differ from an
   adult's.

4. **Taste ages.** A film watched five years ago is weaker evidence than one
   watched last week — but treating it as *zero* evidence would discard most of
   the signal a small library has.

This package addresses all four. Trap 4's mechanism is built and currently
switched off (§3.2).

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Pure — no Tautulli HTTP, no `global_cache`, no logging. |
| G2 | A sparse metadata index must not collapse a user to the household default. |
| G3 | Join on stable identity, not churn-prone keys. |
| G4 | Household and per-user maps key identically across every derivation. |
| G5 | Per-member completion bars, because members differ. |
| G6 | Any behaviour change ships byte-identical by default. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Fetching history | Tautulli managers keep FETCH and the cache-write. |
| N2 | Resolving `rating_key → tmdb_id` | *"those reach into the Radarr cache / registry and stay service-side."* |
| N3 | Transcode usage | Lives in `quality_analytics/transcode.py` — a quality concern. |
| N4 | Scoring | Emits weights; `scoring/` consumes them. |

---

## 3. Architecture

### 3.1 Three derivations, one join convention

```
pre-fetched Tautulli history
   │
   ├─► genre_affinity      {genres, actors, directors, composers,
   │                        producers, studios, format_metrics}
   │                       household + per-user
   │
   ├─► group_completion    {group: {rating_key: {pct, threshold}}}
   │                       max across members, grace-aware
   │
   └─► platform_usage      {platform: count} · {username: {platform: count}}
                           feeds Group D and predict_transcode's
                           per-user platform_weights
```

### 3.2 🟢 Temporal decay — built, tested, disabled

```python
def _entry_weight(entry, half_life_days, now):
    if not half_life_days or half_life_days <= 0:
        return 1                                    # int — byte-identical
    ...
    return math.exp(-age_days / half_life_days)
```

**This closes two items I logged as unbuilt** — `GLD-TAUT-10` and `GLD-ML-10`
both proposed "affinity decay". It exists. It needs a `half_life_days` value.

The `int 1` versus `float 1.0` distinction is deliberate and load-bearing: with
decay off every weight is an integer, so the sums are integers and the maps are
*byte-identical* to the pre-decay implementation. Turn decay on and a bad date
yields `1.0` — same weight, float type, no decay applied rather than the watch
being dropped.

This is the fourth instance of a house pattern worth naming — **byte-identical
opt-in**:

| Mechanism | Default | Guarantee |
|---|---|---|
| `ml.thresholds.mode` | `shadow` | Returns the caller's own object, unconverted |
| grace ramp multiplier | disabled | Exactly `1.0`, so `grace_td × 1.0 == grace_td` |
| `space_exhaustive_downgrade=false` | — | Restores prior behaviour byte-for-byte |
| `half_life_days` | `None` | Integer weights, identical sums |

Ship it off, prove it, flip it. That is a genuine strength of this codebase and
it deserves recording as such rather than only appearing as "not enabled yet."

### 3.3 The library-genre backstop (G2, G3)

The failure it prevents is stated precisely:

> a low-volume, movie-only profile whose handful of rating_keys never made the
> sampled index would otherwise score **affinity=0 and collapse to the flat
> household ranking**

That is §8 **P-C** — absent metadata reading as "no taste" — **caught and fixed**.
It belongs beside `lifecycle/watched_definition.py` as a worked example of the
pattern being closed rather than merely logged.

The fix joins on **stable identity** (G3): movie `title`, or an episode's
`grandparent_title`, normalised to `[a-z0-9]` with a trailing-year fallback so
`Bluey (2018)` and `Bluey` resolve together.

`merge_library_first` then sets precedence: **library genres win**; Tautulli
supplies people/studios and the not-owned fallback. Sensible, because the owned
library is authoritative about its own genres while Tautulli is authoritative
about who appeared in what.

### 3.4 🔴 A sixth "watched" bar

`group_completion` introduces two more thresholds:

| Config | Default |
|---|---|
| `completion_threshold` | **0.9** |
| `grace_threshold` | **0.7** for `grace_members` |

Running tally across the brain:

| # | Consumer | Bar |
|---|---|---|
| 1 | Production producers | `watched_status`, else `≥ 85` |
| 2 | `eval/` | `0.9` |
| 3 | `labels/` movies | `≥ 90` / `≥ 50` relaxed |
| 4 | `labels/` episodes | `≥ 50` |
| 5 | `features/completion_stats` | `≥ 90` (series *and* episodes) |
| 6 | **`affinity/group_completion`** | **`0.9`**, or **`0.7`** for grace members |

**This one is the most defensible of the six**, and arguably more principled
than production's single bar: it is per-group, per-member, configurable, and
models a real phenomenon — some household members reliably stop before the
credits. A single global threshold cannot express that.

But it still *defaults* to 0.9 against production's 85, and nothing reconciles
them. The grace concept is the interesting part: if it is right that members
differ, then `lifecycle/watched_definition`'s single bar is the thing that is
under-specified, not this. That reframes `GLD-LAB-01` — unification may mean
adopting *this* model rather than collapsing onto the production one.

### 3.5 Two deliberate absent-vs-empty choices

**Memberless group ⇒ household-wide wildcard.** *"so an unconfigured / memberless
group still resolves completions instead of coming up empty."* Absent
configuration is read as "everyone", not "nobody" — the useful direction, and
consistent with [`tautulli/`](../../services/tautulli/DESIGN.md)'s
`rating_groups` defaulting to `{"household": {}}`.

**Tie-break favours leniency.** Equal `pct`, the *more lenient* threshold wins —
so a title watched to the same point by a grace member and a regular member is
credited at the grace bar. Consistently biased toward "watched", which is the
protective direction for a signal that feeds deletion guards.

### 3.6 ❓ A join-parity claim worth checking

[`platform_usage.py`](./platform_usage.py) states:

> Groups history by the stable Tautulli `user_id` (falling back to the friendly
> `user` name, then the username), **exactly mirroring
> `affinity.genre_affinity.per_user_affinity`'s join** so a user's device usage
> and genre affinity always key the same way.

But [`genre_affinity.py`](./genre_affinity.py)'s own docstring describes
`per_user_affinity` as *"entries grouped by the `user` field"* — a single field,
not a three-way `user_id → friendly_name → username` fallback.

Either the affinity docstring is simplifying, or the two joins genuinely differ.
**I have read only `genre_affinity`'s docstring, not `per_user_affinity`'s body,
so I am not asserting which.**

It matters because the platform module names the exact consequence of them
disagreeing: a user's device usage and genre affinity would key differently, and
`predict_transcode`'s per-user `platform_weights` would attach to the wrong
person. `GLD-AFF-02`.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Computation here, FETCH and cache-write in Tautulli | G1 — the same keys are written; only the derivation moved | Move the whole manager |
| D2 | Library-first genre index | G2 — a sampled Tautulli index leaves holes that read as zero taste | Trust the Tautulli index |
| D3 | Title join, not `rating_key` | G3 — rating keys churn on re-scan | Key on `rating_key` |
| D4 | Trailing-year fallback in the title join | `Bluey (2018)` and `Bluey` are one show | Exact match |
| D5 | Library genres win, Tautulli supplies people | Each source is authoritative about different fields | One source wins entirely |
| D6 | Decay opt-in, integer weights by default | G6 — byte-identical until deliberately enabled | Ship decay on |
| D7 | Bad date ⇒ weight 1.0, not dropped | Losing a watch is worse than not decaying it | Skip the entry |
| D8 | Per-member completion thresholds with grace | G5 — members genuinely differ | One household bar |
| D9 | Memberless group ⇒ wildcard | Absent config reads as "everyone" | Empty result |
| D10 | Tie-break to the lenient threshold | Biases toward "watched", the protective direction for delete guards | Strict wins |
| D11 | Transcode usage split out to `quality_analytics/` | *"transcode is a quality/playback concern"* | Keep it here |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | No HTTP, no `global_cache`, no logging in this package. |
| I2 | With `half_life_days` unset, affinity maps are byte-identical to raw counts. |
| I3 | A watch with an unparseable date is weighted, never dropped. |
| I4 | Library genres take precedence over Tautulli genres. |
| I5 | Per-user derivations key identically across affinity and platform usage. |
| I6 | A memberless rating group counts every user. |
| I7 | Affinity maps are sorted descending by weight. |
| I8 | Users with no matching history are omitted, not zero-filled. |

**I5 is the one §3.6 questions.**

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Tautulli metadata index sparse | — | Library index backfills (D2) | Handled | ✅ By design |
| Title unmatched in both indexes | — | No genres for that entry; it contributes to people/studios only | 🟡 Partial affinity | ❌ **None** |
| `rating_key` missing | Guarded | Entry skipped in `build_library_index` | 🟡 Silent drop | ❌ **None** |
| Per-user join misses | — | User **omitted** from the map (I8) | 🟡 Join failure and "watched nothing" are indistinguishable | ❌ **None** |
| Affinity and platform joins disagree | **None** | Device weights attach to the wrong user | 🟡 §3.6 | ❌ **None** |
| `percent_complete` unparseable | `except` → `continue` | Entry skipped | 🟡 Silent drop | ❌ **None** |
| Six watched bars disagree | **None** | Completion means six things | 🔴 §3.4 | ❌ **None** |
| Decay never enabled | — | Five-year-old watches weigh as much as last week's | 🟡 Stale taste | ✅ Documented default |

**Row 4 is the sharp one.** `per_user_affinity` omits users with no matching
entries — so a user missing from the output could mean "watched nothing" or "the
join failed." For a per-user signal feeding per-user playlists and device
profiles, those are very different, and nothing distinguishes them.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `half_life_days` | `None` | Temporal decay half-life; unset ⇒ raw counts (§3.2) |
| `rating_groups.<g>.members` | `[]` | Group membership; empty ⇒ household-wide wildcard |
| `rating_groups.<g>.grace_members` | `[]` | Members on the gentler bar |
| `rating_groups.<g>.completion_threshold` | `0.9` | Regular completion bar |
| `rating_groups.<g>.grace_threshold` | `0.7` | Grace completion bar |

Cache keys written by the Tautulli manager from this output: `tautulli/affinity`,
`tautulli/users/{user}/affinity`, `tautulli/group/{g}/tmdb_completions`.

---

## 8. Implemented capabilities

- ✅ Household and per-user genre / actor / director / composer / producer / studio affinity
- ✅ `format_metrics` alongside the taste maps
- ✅ Optional exponential temporal decay, byte-identical when off
- ✅ Library-first genre index on a stable title join with year fallback
- ✅ `merge_library_first` precedence rule
- ✅ Per-group max completion with per-member grace thresholds
- ✅ Household-wide wildcard for memberless groups
- ✅ Leniency-favouring tie-break
- ✅ Household and per-user platform tallies feeding Group D and `predict_transcode`
- ✅ Three test modules including person-affinity aggregation

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-AFF-01` | 🟢 **Choose and set a `half_life_days`** — the decay mechanism is built and tested; only the value is missing. **Closes `GLD-TAUT-10` and `GLD-ML-10`, both logged as unbuilt** | Recent taste currently weighs the same as five-year-old taste | S | D32 |
| `GLD-AFF-02` | ❓ **Confirm the per-user join parity** between `per_user_affinity` and `per_user_platform_usage` — the latter claims to mirror the former exactly; the former's docstring describes a simpler join | §3.6 — if they differ, device weights attach to the wrong user | S | — |
| `GLD-AFF-03` | 🔴 **Reconcile `group_completion`'s 0.9/0.7 with `watched_definition`'s 85** — and consider whether the *grace* model should become the general one | §3.4: the sixth bar, and the most principled of the six | M | `GLD-LAB-01`, D28 |
| `GLD-AFF-04` | **Distinguish "user omitted" from "user watched nothing"** in per-user output | §6 row 4 — a join failure currently looks like an inactive viewer | S | `GLD-AFF-02` |
| `GLD-AFF-05` | **Report affinity coverage** — entries matched vs unmatched, per source (library / Tautulli / neither) | §6 rows 2–3, 6 all drop entries silently | S | `GLD-LAB-03` |
| `GLD-AFF-06` | **Document the byte-identical opt-in pattern** in `DOCS_CONVENTIONS.md` — four instances now follow it | It is a real strength and currently only visible per-module | S | — |
| `GLD-AFF-07` | **Decay sensitivity report** — show how the top-20 genre/person ranking changes across candidate half-lives before committing | Makes `GLD-AFF-01` a measured choice rather than a guess | M | `GLD-AFF-01` |
| `GLD-AFF-08` | **Surface `format_metrics`' consumers** — it is returned by `aggregate_affinity` and its readers are undocumented here | Possible P-A; needs a reader check | S | `GLD-CACHE-12` |
| `GLD-AFF-09` | **Warn on unparseable `percent_complete`** in `group_movie_completions` rather than silently continuing | §6 row 6 | S | — |
| `GLD-AFF-10` | ✅ **Actors in `aggregate_affinity` are now BILLING-TIERED with the shared constant** (operator request 2026-08-07: "make the tautulli/affinity not flat … same semantics for both"): the n-th listed actor contributes `w × billing_weight(n)` via `people_matrix.PERSON_BILLING_DECAY` — ONE weighting semantics for the printed taste profile and both scoring paths; Plex/Tautulli actor arrays arrive in billing order so position is the rank. Scale-safe by construction: `affinity_topk` max-normalizes each map, so Group B sees only the intended RELATIVE reshaping (household leads dominate). `person_billing_decay=0` reproduces the flat legacy tally byte-for-byte; pinned genre tests unaffected (verified). ROLE weights deliberately NOT applied in the tally — Group B's caps (8/6/4/4/3) tier roles at consumption; applying `PERSON_ROLE_WEIGHTS` here too would double-tier | ✅ Done | M | — |
| `GLD-AFF-11` | 🔴→✅ **Group B's `writers_aff` was CONSUMED-NEVER-COMPUTED (P-A live instance)** — `movie_scorer` reads `genre_affinity.get("writers")` into B3 (cap 4.0) but `aggregate_affinity` never built a writers map, so B3 has been permanently 0.0 for every title ever scored. Fixed alongside `GLD-AFF-10`: writers tallied from Tautulli metadata (flat, unordered — no billing semantics) and returned; B3 comes alive on the next affinity rebuild; `taste_profile()` + the elevation breakdown surface `top writers`. ⚠️ NOTE the mirror image discovered at the same time: composers/producers are BUILT-never-scored (no B-term consumes them) — that is `GLD-AFF-12`, an axis decision, not a bug fix | ✅ Fixed | S | — |
| `GLD-AFF-12` | 🎯 **Should composers/producers earn Group-B score terms?** They are tallied, billing-consistent, and now displayed — but no B-term reads them. Adding B6/B7 caps (e.g. 2.0/1.5, mirroring the role-weight ratios) EXPANDS the Group-B axis (20 → ~23.5) — per likelihood/DESIGN §3.3 that is an axis translation requiring the three-boundary re-anchor treatment, exactly like enabling decay (`GLD-AFF-01`/`07`). Operator call; if taken, re-anchor + re-run the billing experiment afterwards | 🎯 Decision | M | `GLD-AFF-07` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What half-life is right for this household? A short one discards most of a small library's signal; a long one barely differs from off. *(= D32)* | `GLD-AFF-01` |
| Q2 | Do the two per-user joins actually agree? | `GLD-AFF-02` |
| Q3 | Should the grace-member model replace `watched_definition`'s single bar, rather than being reconciled to it? | `GLD-AFF-03` |
| Q4 | Is `format_metrics` read anywhere? | `GLD-AFF-08` |

**Q1 has real tension.** Decay is correct in principle, but this library's
evidence is already thin — n ≈ 931 events, `n_pos ≪ 100`
([`foundation/DESIGN.md`](../foundation/DESIGN.md) §2). An aggressive half-life
would down-weight most of it, and the scoring axis is welded to affinity at gain
1.0 ([`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.3) — so enabling decay
is an **axis translation**, and would need the same three-boundary re-anchor
treatment. That is not a reason to avoid it; it is a reason `GLD-AFF-07` should
come first.

## 11. Related designs

- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — Groups A, B and D consume this
- [`likelihood/DESIGN.md`](../likelihood/DESIGN.md) §3.3 — why enabling decay is an axis translation
- [`lifecycle/DESIGN.md`](../lifecycle/DESIGN.md) §3.1 — the definition §3.4 diverges from
- [`services/tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md) — FETCH and cache-write side
- [`quality_analytics/`](../quality_analytics/) — the transcode-usage sibling

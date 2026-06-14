# DESIGN — Recommendation Enhancement ("global propose, local rank")

> **Status:** PROPOSAL — not yet implemented. Captured 2026-06-11 from a cited,
> adversarially-verified research pass (26 sources, 25 claims verified 3-0, 0 killed).
> Implementation deferred to a later cycle. This document is the reference for that work.
>
> **One-line thesis:** The PRIMARY objective is **next-watch propensity** ("what will the
> household watch next") — a *separate* model that drives acquire / surface year-round, with the
> existing deterministic A–G watchability scorecard kept for **curation** (delete / downgrade),
> dominant only under `free_space_limit` T/U pressure. Build next-watch the "global propose, local
> rank" way: let Trakt/MAL do the large-scale collaborative filtering (candidate *generation*);
> spend local CPU on a household-specific re-ranker + Bayesian shrinkage + calibration **on top of**
> Trakt/MAL — never replacing the A–G scorer.

---

## 0. Why this document exists

Two recommendation concerns live in this codebase and are currently **disconnected**:

1. **Watchability score** (`scoring/movie_scorer.py::score_movie`, `show_scorer.py::score_show`)
   — a hand-tuned, expert-weighted **additive scorecard** (`score += a1 + a2 + …`, hard-coded
   constants, 0–100, independently-capped groups A–G; see `scoring/SCORING_GROUPS.md`). It is
   **deterministic and byte-identity golden-tested** (`scoring/test_score_golden.py`). It ranks
   what you **own** (keep / delete / upgrade / monitor).
2. **Website recommendations** — Trakt `/recommendations` + watchlist and MAL `/suggestions`
   flow into `services/acquisition/candidates.py`, which **gathers and de-dups only**
   (`gather()` → `_dedup()`); it does **not** rank candidates by household fit. It decides what
   to **add**.

The goal is to **unify** these (one model proposes-then-ranks across both) and to allow a
heavier learned path **alongside** the explainable scorecard.

---

## 0a. Two objectives: next-watch (primary) vs curation (pressure-gated)

> **Decision 2026-06-11:** the system's PRIMARY objective is **next-watch propensity** — *"what
> will the household watch next"* — built as a **separate** model *alongside* the A–G scorecard,
> **not** by overloading it.

The A–G scorecard answers *"is this owned title worth keeping / upgrading?"* (retention value:
keep tags, completion, collection, penalties). That is **curation**, not viewing intent — a beloved
rewatched classic scores high yet won't be watched again soon; a just-released plan-to-watch film
scores ~nothing yet is the top next-watch. So the two objectives are split:

| Objective | Score | Drives | Primary when |
|---|---|---|---|
| **Next-watch propensity** (NEW, primary) | a 0–1 "will watch soon" probability | acquire · surface / recommend · upgrade-prioritise | **year-round (default)** |
| **Watchability / curation** (existing A–G) | the 0–100 scorecard | delete · downgrade | **only under `free_space_limit` T/U pressure** |

**Space-state switch (your rule, made explicit):**
- **free > U** → next-watch is the driver: acquire / surface what the household will watch.
- **free ≤ T** → curation reclaims: the A–G score sheds the lowest retention-value titles via the
  existing **guarded** space-pressure path (deletion is already gated on T/U, hard-disabled when
  `free_space_limit` is unset).
- **T … U band** → hysteresis between the two, so the system doesn't thrash at the boundary.

This keeps the deterministic, golden-tested A–G scorer intact for the job it's good at, and makes
next-watch the thing we optimise and acquire against the rest of the time.

**Next-watch feature blend (distinct from A–G), in priority order:**
1. **Explicit watchlist membership — the strongest signal, top-weighted.** Union of **Plex watchlist
   ∪ Trakt watchlist ∪ MAL plan-to-watch.** The user literally said *"I want to watch this,"* so it
   outranks any inferred affinity. A watchlisted-and-owned-unwatched title ranks at/near the top of
   next-watch; a watchlisted-but-not-owned item feeds **acquisition** (it serves both pipelines).
2. **On-deck / continue-watching** — a partially-watched item (Tautulli `percent_complete` mid-range,
   or Plex "on deck") is the single most likely *next* play.
3. Recency of *similar* watches · affinity match.
4. Release recency · novelty · time-since-acquired.

> **Data dependency — Plex watchlist (NOT fetched today).** The Plex watchlist is **account-level
> (plex.tv), not the local server**, and the codebase has **no Plex runtime manager** — onboarding
> only captures `plex.plex_token`; the sole Plex code is a stress-test script. Adding it needs a small
> **Plex watchlist fetcher** hitting the plex.tv Discover/metadata provider
> (`metadata.provider.plex.tv/library/sections/watchlist/all`, X-Plex-Token; account-token caveat),
> then mapping Plex GUIDs → tmdb/tvdb so items join the same id space as `movie_files`/Trakt. Today
> only **Trakt** watchlist is wired (`trakt/watchlist`, consumed in `acquisition/candidates.py`).
>
> **Multi-user:** the household runs **multiple Plex Home/managed users, each with their own
> watchlist** — so the fetcher **unions per-user watchlists** (admin token → enumerate Home users →
> per-user access tokens → fetch each → union), **retaining per-user attribution** so the model can
> say *which* member wants it (matching the existing per-user affinity `tautulli/users/<u>/affinity`).
>
> **Auto-discovered (no manual username entry):** Home/managed users are enumerated automatically from
> the account-owner token (`plex.tv/api/v2/home/users`) and per-user tokens obtained via the user-switch
> flow (`…/home/users/{uuid}/switch`). Operator input reduces to: **PINs** for any PIN-protected profiles
> (PIN-less ones are fully automatic) and an **optional include/exclude filter**. Caveats: the captured
> `plex.plex_token` must be the **account-owner** token (the existing Plex code only uses it server-side,
> so account scope is unverified); and the plex.tv Home/switch/watchlist endpoints are
> community-documented, not officially stable — verify at build. Reconcile discovered Plex usernames with
> the household identity in `rating_groups` / Tautulli users (a Plex username need not equal the Tautulli one).

The eval harness (§8) **already measures exactly this objective** (held-out next watches), so it
scores any next-watch ranker — watchlist-weighted or not — directly.

**Build path:** a deterministic next-watch heuristic first (a cheap baseline the eval can score),
then the learned re-ranker (§4 Phase 2) trained on *"what did they watch next"* — now feasible on the
Trakt-enriched history. The leakage-free A–G recompute being ~random at next-watch (§8) is the
*motivation*, not a contradiction: A–G was never a next-watch model; this gives that job its own.

---

## 1. How Trakt & MAL actually determine recommendations

Both expose **per-user, OAuth-gated personalized surfaces whose exact algorithms are
undisclosed.** Any design must treat their internals as a black box.

### Trakt
- *"Trakt recommendations are built on top of your viewing activity and preferences … we also
  use other factors to further personalize."* A client mirror adds: *"based on the watched
  history for a user and their friends."* → **large-scale collaborative filtering across the
  Trakt user base.** Exact CF method (user-user vs item-item vs MF vs learned) is **not
  published** and must be inferred.
- **`GET /recommendations/movies`**, **`GET /recommendations/shows`** — OAuth (per-user),
  default 10, **up to 100/page**, params `ignore_collected`, `ignore_watchlisted`,
  `ignore_watched`, `watch_window` (days).
  *(Note: `ignore_watchlisted`/`ignore_watched`/`watch_window` are recent additions — verify
  against the live contract; older mirrors list only `limit`, `ignore_collected`.)*
- **`GET /movies/:id/related`**, **`GET /shows/:id/related`** — **item-to-item, non-personalized,
  no OAuth**, paginated. This is a **candidate-generation primitive** distinct from the
  personalized surface. Today it feeds only the C3 "related-graph" scoring signal — it is
  **under-used as a candidate source.**

### MyAnimeList
- **`GET /v2/anime/suggestions`** — OAuth (`write:users`), **returns an empty list for
  newcomers**, proving suggestions are derived from the user's own list (and exhibit
  cold-start). Internal algorithm **undisclosed**.
- Whether a separate **community item-item** recommendations surface ("if you liked X, try Y")
  exists distinct from personalized suggestions was **not resolved** from primary docs — see
  Open Questions.

### The load-bearing implication
Trakt and MAL already perform cross-user CF over millions of users that **a single household
cannot replicate from its own data.** Therefore: **do not try to out-collaborative-filter
Trakt locally.** Let them propose; build a local ranker on top.

---

## 2. Target architecture — "global propose, local rank"

```
 PROPOSE  (global CF — external, do NOT rebuild)
   Trakt /recommendations + /watchlist + MAL /suggestions   ─┐
   Trakt /related  (item-item; today only feeds C3)          ─┤→ services/acquisition/candidates.py
                                                              │     (today: gather + dedup ONLY  ← the gap)
 MARSHAL features                                            │
   machine_learning/features/{movie,show,episode}_features.py ┘  (already built for the scorecard)
                                                              
 RANK  (local, household-specific — the NEW part)            
   learned re-ranker (LightGBM LambdaRank) + Platt calibration
     → lives beside machine_learning/likelihood/watch_likelihood.py
     → the A–G scorecard becomes BOTH a model feature AND the explainable fallback
     → trained from machine_learning/updates/dataset_builder.py labels
       (Tautulli completions + Trakt watched = positive implicit feedback)
```

The scorecard is **kept**. The research's calibration framing supports layering learning on
top (calibration + re-rank) rather than ripping out an explainable, golden-tested model.

---

## 3. Technique-by-technique verdict

| Technique | Research finding | Verdict here | Plugs into |
|---|---|---|---|
| **Local CF / matrix factorization** (ALS, BPR, LogMF via `implicit`) | CPU-only & purpose-built, but **benchmarked on many-user data**; MF needs many users to learn latent factors | **SKIP for candidate-gen** — single-household data is starved; Trakt/MAL + `/related` do it better | — |
| **Supervised re-ranker** (LightGBM **LambdaRank**) | Native LTR objective; *loss can matter more than capacity* (8-dim BPR-MF ≈ 128-dim WR-MF) | **DO — the centerpiece**; ranks external candidates **and** owned titles | `candidates.py` rank step + `likelihood/` |
| **Bayesian shrinkage** (empirical-Bayes, beta-binomial, Wilson) | Canonical fix for sparse/noisy rates | **DO — cheapest high-value win**; replaces noisy hand-tuned constants | `scoring/_shared.py`, `scoring/critic.py`, `features/completion_stats.py` (A2 completion, F1 critic blend, B affinity counts) |
| **Calibration** (Platt/sigmoid) | Sigmoid is data-efficient **< ~1000 samples**; isotonic overfits below that; tree ensembles need post-hoc calibration | **DO** — wrap re-ranker into a 0–1 "watch probability" | `likelihood/watch_likelihood.py` |
| **Content embeddings** (TF-IDF on genres/cast/keywords) | Standard cold-start lever | **DO (lightweight)** — cold-start for items with no collaborative signal | `features/` |
| **Learned scorecard weights** | Calibration preferred over wholesale replacement when explainability matters | **DO as a parallel calibrated path** — preserves the deterministic golden-tested scorer | parallel to `movie_scorer.py` |
| **Diversity / novelty** (MMR, DPP) | Both valid; DPP heavier | **MMR LATER** (cheap final-list pass); **DPP skip** (overkill at this scale) | post-rank in `candidates.py` |
| **Offline eval harness** (precision@k / NDCG, temporal leave-last-out, head/torso/tail or IPS) | Offline eval has **severe popularity/selection bias** — must stratify or IPS-correct | **DO FIRST — prerequisite**; nothing above is tunable without it | `updates/` + `features/watched_set.py` |

---

## 4. Phased roadmap (preserves the deterministic scorer + brain-purity guard)

**Phase 0 — Eval harness (PREREQUISITE).**
NDCG@k / precision@k vs held-out watch history; temporal **leave-last-out** split;
head/torso/tail stratification to guard popularity bias. Without it every later change is a
guess — and it empirically answers the unknown: *is there enough household history for a
learned model to beat the scorecard at all?* Built on `updates/dataset_builder.py` +
`features/watched_set.py`.

**Phase 1 — Cheap wins, no new dependencies.**
- Bayesian-shrink the noisy scorecard constants (A2 completion, F1 critic blend, B affinity
  counts) — pure-Python, inside the existing brain.
- Wire Trakt `/related` into candidate **generation** in `candidates.py` (not just the C3
  signal).
- Add an MMR diversity pass to the final ranked list.

**Phase 2 — The learned re-ranker (opt-in).**
LightGBM LambdaRank over `candidates.py` output **and** owned titles; features from
`features/*`; trained from `dataset_builder` labels; **Platt-calibrated** into
`likelihood/watch_likelihood.py`. Scorecard stays as fallback + a feature. Gated behind config.
**Brain-purity holds:** the model is *data* — training/IO lives in the service layer, inference
reads a cached model, and `plan()` stays pure (satisfies `scripts/hooks/brain_purity.py`).

**Phase 3 — Refinement.**
Learned scorecard-weight calibration layer; content embeddings for cold-start; per-household
model persistence + retrain cadence (the **enrich daemon** is the natural place to retrain
out-of-band).

---

## 5. Caveats (from the verified research)

- **Trakt/MAL algorithms are undisclosed** (verified — not a gap in our research). Do not
  design against assumed internals.
- **The single-household data threshold** at which a learned re-ranker beats the scorecard is
  **unquantified** — no source pins it. Phase 0 answers it empirically; if history is too thin,
  the scorecard wins, and that is a legitimate outcome.
- **"Don't out-CF Trakt locally"** is the report's own synthesis (**medium confidence**), but
  the chain of primary facts behind it is solid.
- The **deterministic, golden-tested scorecard is an asset, not an obstacle.** Every step here
  layers on top of it.

## 6. Open questions (need a spike / experiment to resolve)

1. What CF method does Trakt actually use, and how heavily are "friends" + the undisclosed
   "other factors" weighted vs. the user's own history? (Undocumented; would need
   response-behavior reverse-engineering.)
2. Does MAL `/v2/anime/suggestions` reflect community item-item recs, an algorithmic model, or
   a blend — and is there a separate community item-item surface to also consume?
3. Minimum effective interaction count before a calibrated local re-ranker's NDCG@k beats the
   deterministic scorecard; how to estimate pseudo-negatives / IPS propensities when only
   positive watch history exists.
4. MMR vs DPP, and head/torso/tail stratification vs IPS, for best offline–online agreement at
   N = 1 household (where IPS variance is severe).

## 8. Phase 0 — status & first results (2026-06-11)

**Built:** the pure eval core `machine_learning/eval/` (metrics · temporal split · popularity
stratification · pre-cutoff replay; 22 hand-verified tests, brain-purity-enforced) + a CLI driver
`support/tools/eval_recommender.py` that loads real caches, runs **two baselines side by side**
(stamped = the `watchability_score` already in `movie_files.parquet`, optimistic/leaky; recomputed =
the A–G scorer re-run on pre-cutoff household state via `eval/replay`, leakage-free), stratifies,
unions all Radarr instances, and self-reports diagnostics.

**Data:** Tautulli alone (`tautulli/history/all`) was **too sparse** — 34 movie plays / 24 distinct
→ 0 held-out new watches at the 0.9 threshold. Adding **Trakt** (`trakt/history/movies`) brought it
to **436 events / 238 distinct / 14 held-out new watches** — enough to evaluate. Trakt broadens the
watched-set + timeline; Tautulli still supplies completion fractions + the Plex metadata used for affinity.

**First numbers** (movies, holdout 0.2, threshold 0.9, 1888-candidate library):

| baseline | MAP | NDCG@20 | HR@20 |
|---|---|---|---|
| stamped (leaky upper bound) | 0.157 | 0.243 | 1.00 |
| recomputed (leakage-free) | 0.011 | 0.000 | 0.00 |

**Reading it — caveats that matter:**
- The **leakage gap is real and large** (0.157 → 0.011): the stamped score "predicts" watches mostly
  because it was computed *from* them. This validates why the temporal split is mandatory.
- The recompute near-zero is a **FLOOR, not the scorecard's ceiling**: today's recompute ranker is
  intentionally PARTIAL — it zeroes Groups C/D/E (`collection_members={}`, no platform/transcode/
  per-user/related) and computes affinity from **Tautulli-only** history (Trakt entries carry no Plex
  metadata). It is a handicapped subset of the production scorer.
- The A–G scorecard is designed to rank **owned** titles for keep/delete/upgrade, **not** as a
  "what will you watch next" recommender — so a weak next-watch score is partly by-design.
  **→ Now addressed:** §0a makes next-watch its own model (primary, year-round); A–G stays for
  curation (delete/downgrade), dominant only under `free_space_limit` T/U. This §8 result is the
  motivation for that split, not a knock on the scorecard.

**Phase-1 next-watch baseline (added 2026-06-11):** a deterministic next-watch ranker now runs as a
third column — rank *unwatched* candidates by pre-cutoff **genre affinity** (then rating), excluding
already-watched (a next watch is by definition unwatched). Result (14 held-out, 1888-library):

| ranker | MAP | NDCG@20 |
|---|---|---|
| stamped (leaky upper bound) | 0.157 | 0.243 |
| recomputed (A–G curation, leakage-free) | 0.011 | 0.000 |
| **nextwatch (genre + cast + crew, leakage-free)** | **0.017** | 0.000 |

The purpose-built next-watch baseline edges the curation recompute (right objective) but both sit near
the floor. Adding **cast + crew** affinity over the **full watched-set** (the movie_files people columns
`cast_names`/`director_names`/`composer_names`/`producer_names`, actors weighted highest — not the
~34-play Tautulli metadata base) nudged it **0.015 → 0.017 MAP — within noise at N=14, still NDCG@20 = 0**.
**Takeaway: content affinity has a low ceiling for next-watch** — richer content features barely move it.
The real levers are **explicit watchlist intent** (the §0a top signal, validated *forward*) and the
**learned re-ranker** (non-linear signal combination), not more content features. Headroom is real (the
stamped column shows signal exists).

**Cross-medium TV affinity (added + toggle-tested):** fold watched-TV genres + cast/crew into the movie
taste (an actor known from a show boosts their movies; TV resolved via the Tautulli metadata index,
discounted ×0.5 as a weaker cross-medium signal). It *contributes* (502 actors from TV) but **did not
help** (0.017 → 0.016, within noise) — because the metadata index resolves only **33 of 440** watched TV
episodes (93% unresolved; the cached `tautulli/metadata/index` holds ~72 items for 800+ watch events),
and that small sample is skewed (Animation/Family/Children). **The cross-medium wiring is correct; the
bottleneck is metadata-index COVERAGE, not the idea.** ⚠️ This also limits the *production* Group-B
affinity (same index) — worth checking whether the index is chronically under-populated or just stale in
this snapshot. Same low-ceiling conclusion for next-watch either way.

**Update — rewired to the merged parquet (one source).** The coverage bottleneck is fixed at the source:
the enrich daemon now also fetches show **genres** (`shows/{id}?extended=full`), and a new
`episode_files.refresh_enrichment` broadcasts per-series **genres + cast/crew + Trakt rating** onto the
episode rows (via the reusable `factories/daemons/bucket_merge.py`, mirroring the movie_files columns). The
cross-medium TV taste now reads those **parquet columns** (one source — the daemon's ~3,800-show coverage),
matched by `grandparent_title`, **not** the sparse Tautulli metadata index. With the rich source it still
only nudges the baseline (**0.017 → 0.018, within noise, NDCG@20 = 0**) — confirming **content affinity's
low ceiling for next-watch holds regardless of coverage.** Net: the data pipeline is now correct
(daemon → parquet → consumers read one source); the lever remains **watchlist intent** + the **learned
re-ranker**, not more content metadata. (Movies need no such merge — Radarr already fills the movie_files
cast/crew/genre/rating columns; only `related_tmdb_ids` is daemon-additive there.)

> **Methodological blind spot (important).** The strongest next-watch signal — **explicit watchlist
> intent** — is **NOT validatable by this retrospective eval.** A watchlist is forward-looking, watched
> items typically *leave* it, and we hold no historical snapshots, so *current-watchlist ∩
> past-held-out ≈ 0*. Its value must be measured **forward / online** ("did they watch what was on the
> list?"), not backtested. So: use this eval to develop the **affinity / recency** next-watch signals;
> validate **watchlist intent forward** once the Plex/Trakt/MAL watchlist union (§0a, task #8) is wired.
> **Built:** the forward harness exists — `eval/forward.py` (pure: hit-rate, base-rate, **LIFT**) +
> `eval_recommender.py --forward`, which reads the Plex `plex/watchlist/snapshot/<ts>` snapshots and,
> for each snapshot older than `--window-days`, reports watchlist hit-rate vs the base watch-rate of
> non-watchlisted owned movies (**lift**, not hit-rate, is load-bearing). It matures as snapshots age +
> watching happens (empty on a fresh Plex install — by design). This is the *only* honest measure of
> the watchlist signal; a lift ≫1 would justify weighting it heavily in the production next-watch ranker.

## 7. Primary sources

- Trakt API contracts — `github.com/trakt/trakt-api` (Zod/TS schemas); Apiary docs `trakt.docs.apiary.io`.
- MAL API v2 — `myanimelist.net/apiconfig/references/api/v2`.
- Hu, Koren, Volinsky, *Collaborative Filtering for Implicit Feedback Datasets*, ICDM 2008
  (iALS confidence `c_ui = 1 + α·r_ui`, α≈40; O(f²N + f³m), linear in observations).
- Rendle et al., *BPR: Bayesian Personalized Ranking from Implicit Feedback*, UAI 2009
  (arXiv:1205.2618).
- `implicit` library — `benfred.github.io/implicit` (CPU ALS/BPR/LogMF/item-item-NN).
- LightGBM v4.4.0 — LambdaRank (`objective=lambdarank`, `label_gain`, `lambdarank_truncation_level`).
- scikit-learn calibration docs; Platt scaling (sigmoid two-parameter logistic).
- Castells & Moffat, *Offline Recommender System Evaluation*, AI Magazine 2022
  (doi:10.1002/aaai.12051) — popularity/selection bias, head/torso/tail, IPS.

*Full per-claim evidence + verification votes: deep-research run `wf_c62c094e-38b`.*

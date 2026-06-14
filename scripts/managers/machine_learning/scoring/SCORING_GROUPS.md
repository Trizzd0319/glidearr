# Watchability Scoring Groups (A1–G4)

Reference for the signal groups that make up the **0–100 watchability score** produced by
`scoring/movie_scorer.py::score_movie` (and its TV twin `scoring/show_scorer.py::score_show`).

> **Naming caution.** These group codes (`A1`…`G4`) name the *scoring signals* and are
> **unrelated** to the enhancement-batch labels (`A1` temporal-decay, `C2` grace-window,
> etc.) used in PRs/commit messages. Same letters, different namespace.

---

## 1. What the score is and why it exists

`score_movie` replaces the old 1–10 integer scale with a **0–100 float** built from
**weighted, independently-capped signal groups**. Each group answers a different question
("does the household intend to keep this?", "does it match their taste?", "will it play
without transcoding?", "is it any good?") and contributes a bounded number of points. The
points are summed and clamped:

```
final = max(0, min(100, round(sum of every group contribution)))
```

Because each group is capped on its own, no single signal can dominate — a critically
acclaimed film still needs household/affinity signal to reach the top tiers, and a
beloved-but-obscure title isn't sunk by a missing critic rating.

The result is a single number that drives, downstream:

- **Quality-profile selection** — which resolution/encode to grab/keep (table below).
- **Space-pressure deletion & downgrade** — lowest-watchability titles are shed first.
- **Monitoring / triage** — what to (un)monitor or re-acquire.
- **Grace periods & JIT upgrades** — keyed off the score / its percentile.

### Score → quality-profile tier

| Score | Profile tier | Meaning |
|------:|--------------|---------|
| 0–19 | SD / WEB-DL 480p | background noise, no interest signal |
| 20–34 | HD-720p | some interest, standard streaming quality |
| 35–49 | WEBDL-1080p | good affinity, direct-play friendly |
| 50–59 | Bluray-1080p | household watched / affinity content |
| 60–69 | Remux-1080p | strong affinity, active collection |
| 70–79 | Remux-2160p HDR | franchise/universe + device supports 4K |
| 80–100 | Remux-2160p DV | full household intent + keep policy |

When `return_breakdown=True`, the scorer additionally returns a per-group `breakdown` dict
(`{"A1_keep_policy": 15.0, ..., "_total_raw": 71.25, "_total_final": 71}`); the score is
**identical** whether or not the breakdown is requested.

---

## 2. The groups

Point values below are the **actual coded contributions** (a few module header comments
quote stale per-group maxima; the tables here reflect the function body).

### GROUP A — Household Intent — *“do they want this?”*  (budget ≈ 25)

The strongest positive signal: explicit curation and actual viewing behaviour.

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **A1** | keep_policy tag | +15 / +8 / +4 | `keep_forever`/`keep_movie` → 15; `keep_universe` → 8; bare `universe` → 4 |
| **A2** | completion rate | +12 … −6 | ≥ threshold (≈0.9) → +12; ≥0.75 → +6; ≥0.5 → +2; ≥0.2 → −3; >0 → −6; =0 → 0 |
| **A3** | rewatch count | +8 / +5 / +2 | watched ≥3× → 8; ==2 → 5; ==1 → 2 |
| **A4** | user Trakt rating | +10 … −ve | linear: 0 at 5/10, +10 at 10/10, negative below 5 (`user_rating_score`) |

> **📽️ Example** — *The Princess Bride* in a household that adores it: tagged `keep_forever` → **A1 +15**, finished every viewing → **A2 +12**, rewatched 5× → **A3 +8**, rated 10/10 on Trakt → **A4 +10**. A film someone started and bailed on at 15% instead takes **A2 −6**, with no keep tag and no rating — and it trips the G2 penalty below.

*TV difference:* A2 is measured by **recency + breadth** of episode watching, not lifetime
completion — a long-running series you actively follow scores high even though its
lifetime-completion fraction is low.

### GROUP B — Household Affinity — *“does it match their taste?”*  (budget ≈ 20)

Cast/crew/genre/studio overlap between this title and what the household actually watches
(`genre_affinity` maps from Tautulli history). Each uses the shared `affinity_topk` helper
and is scaled by `affinity_boost` (≥1.0 lets strong affinity push the score — and the
upgrade tier — higher).

| ID | Signal | Points | Source matched against history |
|----|--------|-------:|-------------------------------|
| **B1** | actor affinity | +8 | top-10 billed cast |
| **B2** | director affinity | +6 | credited directors |
| **B3** | writer affinity | +4 | credited writers (screenplay/story/writer) |
| **B4** | genre affinity | +4 | the title's genres |
| **B5** | studio affinity | +3 | production companies / studio |

> **📽️ Example** — a house that keeps rewatching *The Princess Bride* builds affinity for Cary Elwes, Robin Wright and Mandy Patinkin, so a **new film starring that cast** is lifted by **B1 +8**; loving Rob Reiner's catalogue lifts *Stand By Me* via **B2 +6**. Fantasy-adventure fans elevate *Willow* / *Legend* through **B4 genre affinity**. A lone horror title in a household that never watches horror earns **0** across Group B.

### GROUP C — Collection / Universe — *“is it part of something they’re working through?”*  (budget ≈ 16)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **C1** | collection completeness | +8 / +5 / +2 | siblings in the same collection watched: ≥75% → 8; ≥50% → 5; ≥25% → 2 |
| **C2** | universe siblings | +4 / +2.5 / +1 | franchise/universe siblings watched: ≥5 → 4; ≥2 → 2.5; ≥1 → 1 |
| **C3** | related-graph affinity | up to +`related_graph_cap` (≈4) | Trakt-related neighbours the household has watched — generalises C1/C2 onto the similarity graph ("people like me") |

> **📽️ Example** — having watched *The Fellowship of the Ring* and *The Return of the King*, the still-unwatched *The Two Towers* is lifted by **C1 +8** (collection ≥75% watched). Five MCU films watched lifts the next Marvel release by **C2 +4**. *The Princess Bride*'s mostly-watched Trakt-related swashbucklers add **C3** up to +4. A true standalone in no collection scores **0** here.

*TV difference:* GROUP C is **0** for shows — TV has no native collection concept;
franchise value flows through keep-tags in Group A instead.

### GROUP D — Device / Playback Fit — *“will it play cleanly?”*  (budget ≈ 15)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **D1** | primary-device capability | +6 / +3 / −2 | primary device's resolution ceiling vs target: at ceiling → 6; below → 3; above (needs downscale) → −2 |
| **D2** | transcode avoidance | +5 / +2 | no known transcode events for the codec → 5; transcode-friendly codec → 2; unknown codec → 2 |
| **D3** | platform resolution ceiling | +4 / +2 / +1 | share of plays from devices that support the target resolution: ≥75% → 4; ≥50% → 2; ≥25% → 1 |

> **📽️ Example** — a 4K HEVC remux of *The Princess Bride* on a household whose primary device is an **Apple TV 4K** earns **D1 +6** (plays natively at the device ceiling) and **D2 +5** (HEVC never transcodes there). The same 4K file in a home whose main screen is a **1080p Roku** takes **D1 −2** (it would have to downscale).

D1/D3 depend only on the household's `platform_usage` + the `target_resolution` being
evaluated (not on the individual title), so they're effectively household-constant per pass.

### GROUP E — Audience Alignment — *“is it for the right viewer / library?”*  (budget ≈ 10)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **E1** | kids content on kids devices | +6 / +2 | kids-cert title × kids-user genre affinity → up to 6; kids cert with no per-user data → 2 |
| **E2** | adult content affinity | +4 | adult-cert title × adult-user genre affinity |
| **E3** | library routing fit | +4 / +2 | kids-cert family/animation in kids library → 4; anime genres (non-kids library) → 2 |

> **📽️ Example** — *The NeverEnding Story* (rated PG) in a home with kid viewers who watch family/animation is lifted by **E1 +6**, and sitting correctly in the **Kids library** adds **E3 +4**. An R-rated thriller that matches the adult viewers' taste earns **E2 +4** instead; an anime placed in the anime library picks up **E3 +2**.

### GROUP F — Content Quality — *“is it any good?”*  (budget ≈ 24)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **F1** | critic consensus | +20 / +14 / +8 / +3 | weighted blend (IMDb 35% · Trakt 25% · RT 25% · MC 15%; TMDb fallback): avg ≥8.5 → 20; ≥7.5 → 14; ≥6.5 → 8; ≥5.5 → 3 |
| **F2** | popularity | +2 / +1.5 / +0.75 | trending: ≥100 → 2; ≥50 → 1.5; ≥20 → 0.75 |
| **F3** | recency | +2 / +1 | released ≤1yr → 2; ≤2yr → 1 |

> **📽️ Example** — *The Princess Bride* (IMDb ~8.0, RT ~97%) lands a critic average near 8 → **F1 +14**. Its widely-panned sequel *The NeverEnding Story III* (RT ~0%) earns **no F1 bonus** — and **−5 from G3** below. A brand-new release also picks up **F3 +2** for recency and, if trending, **F2** up to +2.

F1 is deliberately the strongest **single** positive signal (+20, ranked above director
affinity) so a critically-acclaimed title survives the prune and earns monitoring even while
unwatched. *TV difference:* F1 averages whatever 0–10 ratings TV exposes (Sonarr aggregate +
Trakt show rating) through the same tier table.

### GROUP G — Penalties — *“reasons to deprioritise”*  (negative)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **G1** | language mismatch | −8 / −4 / −1 | non-preferred original language with no watch history; softened if the household watches that audio language (0 plays → −8; ≤2 → −4; else −1) |
| **G2** | abandoned | −10 | started but bailed early (0 < completion < 0.2) |
| **G3** | critically panned | −5 / −2 | weighted critic avg < 4.0 → −5; < 5.0 → −2 |
| **G4** | not yet available | −5 | unavailable and no physical/digital/cinema release date has passed |

> **📽️ Example** — *Amélie* (French original language) in an English-only household with no French watch history takes **G1 −8**; a home that regularly watches French cinema softens that to **−1**. A film everyone abandoned at 10% takes **G2 −10**; *The NeverEnding Story III* (critic avg < 4) takes **G3 −5**; an announced-but-unreleased title with no release date yet passed takes **G4 −5**.

---

## 3. Worked end-to-end examples

*Group budgets are design guides, not hard caps — only the final clamp to `[0, 100]` is enforced (see Notes), so a true favourite stacks signals well past a group's nominal budget.*

### ⬆️ Elevated — *The Princess Bride* (a household favourite)

| Group | Why | Pts |
|-------|-----|----:|
| A1 | tagged `keep_forever` | +15 |
| A2 | finished every viewing | +12 |
| A3 | rewatched 5× | +8 |
| A4 | rated 10/10 on Trakt | +10 |
| B1 | beloved recurring cast | +8 |
| F1 | acclaimed (~8.0 critic avg) | +14 |
| D1 / D2 | 4K HEVC on an Apple TV 4K | +6 / +5 |
| **Total** | | **78** |

**78 → 70–79 tier → Remux-2160p HDR.** Grabbed/kept at near-top quality, monitored, given a
long grace window, and among the **last** titles space-pressure would ever delete.

### ⬇️ Lowered — a never-watched, panned, foreign sequel

| Group | Why | Pts |
|-------|-----|----:|
| A* | never watched, no keep tag, no rating | 0 |
| F1 | panned — no critic bonus | 0 |
| G1 | foreign language, no household history | −8 |
| G2 | someone abandoned it at 10% | −10 |
| G3 | critic avg < 4 | −5 |
| **Total** | | **−23 → clamps to 0** |

**0 → 0–19 tier → SD/480p.** Never monitored, shortest grace, and the **first** title
space-pressure downgrades or deletes. *The NeverEnding Story III* is the archetype — same
franchise as a beloved original, but unwatched + panned ⇒ bottom of the pile.

---

## 4. Notes & gotchas

- **Independently capped, then summed.** Each group's max is a soft design budget; the final
  value is clamped to `[0, 100]` after summation, so heavy penalties + light positives can
  still floor at 0.
- **F3 and G4 read the wall clock.** Both call `datetime.now()` internally (recency / "has a
  release date passed"), so `score_movie` is **not** fully pure — its output for those two
  signals depends on the day it runs. (The byte-identity golden oracle in
  `test_score_golden.py` deliberately holds these clock-stable.)
- **Movie vs. show parity.** `score_show` mirrors the same A–G taxonomy and the same capped
  accumulation; the only structural changes are A2 (recency+breadth), F1 (TV rating sources),
  and C (= 0 for shows). Shared tables/helpers live in `scoring/_shared.py` so the two engines
  can never drift.
- **Explainability is free.** `return_breakdown=True` adds the per-signal `breakdown` dict
  without changing the score — used by the persistence path; decision paths take the bare int.
- **Where the inputs come from.** The pure scorer takes plain data; the service adapters
  (`features/movie_features.py`, `_build_show_score_map`) marshal cache/Parquet rows + Trakt
  credits + Tautulli affinity into the scorer's arguments.

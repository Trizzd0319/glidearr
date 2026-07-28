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

The rungs are **CALIBRATED to this household's real distribution**, not invented — they are
the p97 / p98 / p99 / p99.5 / p99.9 percentiles of the **file-owning** title population
(6,449 titles) under Group D v2. They move whenever the score axis does; see the derivation
table on `_shared.QUALITY_PROFILE_THRESHOLDS`, and override with `scoring.quality_ladder`.

| Score | Percentile | Profile tier | Meaning |
|------:|-----------:|--------------|---------|
| 0–24 | — | HD-720p | floor; SD is absorbed into 720p |
| 25–28 | p97 | WEBDL-1080p | good affinity |
| 29–32 | p98 | Bluray-1080p | household watched / affinity content |
| 33–37 | p99 | Remux-1080p | strong affinity, active collection |
| 38–49 | p99.5 | Remux-2160p | 4K entry |
| 50–100 | p99.9 | Remux-2160p | the household's very top tier |

> ⚠ The 4K entry rung admits 34 titles against the 67 the household keeps at 2160p today;
> the rung that reproduces its own curation is p99 (33). Documented, deliberate, and
> overridable — see the constant's comment. The ladder only PROPOSES a tier: actual 4K
> acquisition is still gated by `watch_likelihood.uhd_cutoff` (75) and
> `routing.movies.4k_dual_min_score` (75), which live on a different scale.

When `return_breakdown=True`, the scorer additionally returns a per-group `breakdown` dict
(`{"A1_keep_policy": 15.0, ..., "_total_raw": 71.25, "_total_final": 71}`); the score is
**identical** whether or not the breakdown is requested.

---

## 2. The groups

Point values below are the **actual coded contributions** (a few module header comments
quote stale per-group maxima; the tables here reflect the function body).

### GROUP A — Household Intent — *“do they want this?”*  (budget ≈ 33)

The strongest positive signal: explicit curation, actual viewing behaviour, and — since
`SCORER_REVISION 5` — an explicit *statement* of intent.

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **A1** | keep_policy tag | +15 / +8 / +4 | `keep_forever`/`keep_movie` → 15; `keep_universe` → 8; bare `universe` → 4 |
| **A2** | completion rate | +12 … −6 | ≥ threshold (≈0.9) → +12; ≥0.75 → +6; ≥0.5 → +2; ≥0.2 → −3; >0 → −6; =0 → 0 |
| **A3** | rewatch count | +8 / +5 / +2 | watched ≥3× → 8; ==2 → 5; ==1 → 2 |
| **A4** | user Trakt rating | +10 … −ve | linear: 0 at 5/10, +10 at 10/10, negative below 5 (`user_rating_score`) |
| **A5** | watchlist intent | +8 max | `cap × source × recency × members` (`watchlist_intent_score`). Source reuses the acquisition scorer’s own feed ranking (watchlist/plan-to-watch 1.00 > suggestions 0.65 > seasonal 0.55); members 0.60 solo, +0.12 each, full at five; recency decays only feeds carrying a real `listed_at` (Trakt does, Plex’s union does not). A solo Plex watchlisting scores **4.8**. |

> **A5 is the scorecard’s only EXPLICIT-intent term.** Every other signal *infers* whether
> the household wants a title from behaviour; this one reads a statement of it. It also
> **shields the title from deletion** while the member who asked is still an active viewer
> (`intent_hold_active`, 90-day dormancy — the same window `saga_retention` uses), because
> +4.8 alone cannot lift a weak-taste title over the 17 delete ceiling and deleting
> something the household explicitly asked for is the one deletion that is never
> defensible. It is also the one Group-A signal the **Hidden Gems** taste score admits:
> owned + watchlisted + never played is the highest-value reminder that shelf can produce.

> **One human is one watchlister.** A5 grades by *distinct household members*, so the app
> has to know which Plex/Tautulli account the Trakt (and MAL) lists belong to —
> `trakt.household_member`, falling back to `trakt.username`. Get it wrong and the same
> person counts twice on every title they listed in two places (0.60 → 0.72 of the cap),
> **and** a Trakt/MAL-only title has no Tautulli activity to anchor its shield to, so
> `intent_hold_active` fails closed and the shield silently never fires.

> **The decay is configurable:** `scoring.watchlist_intent.half_life_days` (365) and
> `stale_floor` (0.25) drive `intent_recency_factor` through `resolve_intent_inputs`, and
> both ride in the score memos’ context hash so editing one actually rescores.

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
| **C4** | person affinity | up to +`scoring.person_affinity.cap` (**8**) | the title's cast/crew vs the household's person-affinity vector, keyed on `tmdb_person_id` (immune to name-spelling drift). Weighted per credit by ROLE (lead/director 1.0 → editor 0.2), by BILLING ORDER within the cast, and by how hard the household ENGAGED with the titles that person worked on |

C4's cap is **8**, matching C1: "this is by people you keep coming back to" is as strong
a keep signal as "you are most of the way through this collection". It stays inert (cap
forced to 0.0) until the people-matrix has been built — which now happens every run from
the Radarr relational credits table, covering all seven credited roles (cast, director,
writer, producer, composer, cinematographer, editor).

> **📽️ Example** — having watched *The Fellowship of the Ring* and *The Return of the King*, the still-unwatched *The Two Towers* is lifted by **C1 +8** (collection ≥75% watched). Five MCU films watched lifts the next Marvel release by **C2 +4**. *The Princess Bride*'s mostly-watched Trakt-related swashbucklers add **C3** up to +4. A true standalone in no collection scores **0** here.

*TV difference:* GROUP C is **0** for shows — TV has no native collection concept;
franchise value flows through keep-tags in Group A instead.

### GROUP D — Device / Playback Fit — *“will it transcode?”*  (budget 0 to −15 — a PENALTY)

| ID | Signal | Points | Trigger |
|----|--------|-------:|---------|
| **D4** | transcode risk | 0 … −15 | `−magnitude × Σ_cause weight × risk(title)` — the probability this title makes the household's Plex transcode, weighted by the causes it is OBSERVED to transcode for |
| **D1/D2/D3** | *(v1 legacy)* | 0 | reachable with `scoring.device_fit_v2: false`, which restores the old +6/+5/+4 bonuses byte-for-byte |

**Why the group inverted.** v1 was three bonuses for *"the household's devices can play
this"*. That question's answer is YES for essentially every modern device × file
combination, so on real data **1,840 of 1,997 movies (92%) scored EXACTLY 12.0** — sd 1.92,
the entire tail being 66 titles at 8.0, 43 at 5.0 and 38 at 1.0. For the median movie that
was 12 of 21 points: **57% of the whole score carrying no ranking information**, compressing
the useful range and silently invalidating every absolute threshold anchored on it. v2 asks
the question that actually varies — *will it transcode?* — and scores it the way Group G
scores its penalties: direct play is ~0 (the expectation, not an achievement), likely
transcode is negative.

**The five causes, weighted by observation.** From this household's 133 ground-truth
per-stream decisions (`tautulli/stream_decisions`), classified by the same function the
operator-facing transcode-cause report uses:

| Cause | Observed | What the per-title risk reads |
|-------|---------:|-------------------------------|
| audio | 38.3% | `audio_codec` + `audio_channels` vs each device's `direct_play_audio` / `max_audio_channels`, play-weighted. DTS on a Samsung TV, 5.1 in a browser. |
| bitrate / resolution | 36.1% | the union of (a) play-share of devices whose ceiling is below the file's resolution and (b) a bitrate ramp from a per-resolution reference to 2.5× it, split LAN/WAN at the observed remote share |
| subtitle | 14.3% | subtitle TRACK COUNT (more tracks → likelier the auto-selected one is image-based → burn-in → full video transcode), raised to near-certain when the file has NO preferred-language audio (subs on every play) and halved in MP4/AVI (text subs are rendered, not burned) |
| video codec | 11.3% | `1 − codec_direct_play_share` — v1's entire question, now **one input at its measured weight** |
| container | ~0% | file extension; MP4 free, MKV a cheap remux, the AVI/WMV/VOB family a full transcode. Never the PRIMARY cause here, so it is smoothed to ~1.6% rather than zero |

Weights are blended with a shipped cold-start prior by empirical-Bayes shrinkage
(`w = n/(n+60)`), so **n = 0 returns the prior bit-identically** and a fresh install is a
documented cold start rather than a fit to nothing. Below 10 classified decisions the
observation is ignored outright. `tautulli/transcode_fingerprint` supplies the household
base rate (14.1% here) and the remote share (5.4%) that scales the bandwidth half.

**Calibration check.** The per-title risks are built bottom-up from file facts and device
capabilities and are never shown the base rate; their library mean still lands at 16.8%
against the 14.1% the household actually experiences.

**Unmeasurable ≠ safe.** An axis whose input is missing is RENORMALISED out of the weighted
sum — never scored as zero risk. A title with no file facts at all (a Sonarr pilot **stub**)
scores a hard **0.0**, and a household with no recognised device *and* no observed decisions
gets no profile at all, which falls back to the v1 path its behaviour was already calibrated
against. Neither case is ever a free bonus — that specific mistake (v1's "we have never seen
this transcode, therefore +5") is what the whole redesign removes.

**The device capability matrix** (`_shared._DEVICE_CAPABILITIES`) now has an audio half:
`platform → (max_resolution, direct-play video codecs, direct-play audio codecs, max audio
channels)`. Audio is assigned by CLASS (browser / mobile / TV-streamer / desktop-HTPC) so a
policy change lands in one place; a device in the video table with no audio class is a test
failure, not a silent fallback. Two things the matrix deliberately encodes:

* **A bare family name is conservative.** "Roku" could be a 1080p Express and "Fire TV" a
  Stick Lite, so those families resolve to 1080; only a name-matched 4K SKU ("Roku Ultra",
  "Fire TV Stick 4K", "Chromecast Ultra") claims 2160. Matching prefers the **most
  specific** entry, so a model name is never shadowed by its family.
* **AV1 is never direct-play, on any device.** Nvidia Shield, recent Roku Ultra / Streaming
  Stick 4K, Fire TV Stick 4K/4K Max and Chromecast with Google TV all have AV1 hardware
  decode — but **Plex transcodes AV1 to HEVC/H.264 on virtually every client regardless**,
  so an AV1 file is a transcode risk on this stack and must never score as safe. (HEVC is
  the mirror case: it direct-plays broadly, but Plex cannot direct *stream* it — it is
  direct play or a full transcode, which is why Plex Web on Chrome/Firefox/Edge is excluded
  while Safari is not.) Legacy codecs (XviD/DivX, MPEG-2, VC-1) appear on no entry, matching
  the judgement `scoring.codec_profiles.legacy_regrab` already makes.

An operator extends or overrides the matrix with **`scoring.device_capabilities`** (now
accepting `audio_codecs` / `max_audio_channels` too) and tunes the group with
**`scoring.device_fit_v2`** (`enabled`, `magnitude`, `cause_weights`). An unrecognised
platform is never guessed at: it enters neither numerator nor denominator on any axis.

**What `resolution` means:** the resolution the title is CURRENTLY HELD at
(`movie_files.resolution` for a movie, `max(episode_files.resolution)` for a series) —
never the resolution its profile *would* acquire. The profile is chosen FROM the score, so
feeding the profile's resolution back in would close a feedback loop; and the question the
term asks is a statement about the file on disk. Bitrate reads `video_bitrate` when present
and falls back to `size ÷ runtime × 0.9` — load-bearing, because 45% of this library's movie
rows and 93% of its episode rows report a zero video bitrate.

**The regression guard.** Both scoring passes log the resulting Group-D distribution
(mean / sd / mode / share-at-mode / distinct values) and warn loudly if it starts behaving
as a constant again. v1 collapsed in complete silence; that must not be repeatable.

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
| D4 | 4K HEVC, DD+ 5.1, no subtitle tracks — direct-plays everywhere | −0 |
| **Total** | | **67** |

**67 → above the 50 rung → Remux-2160p.** Grabbed/kept at near-top quality, monitored, given
a long grace window, and among the **last** titles space-pressure would ever delete. Note
what Group D contributes: **nothing**. Playing cleanly is the expectation. Give the same
title a DTS-HD MA 7.1 track and burned-in foreign subtitles and D4 takes several points
back — which is the only direction a playback fact should ever move a taste score.

### ⬇️ Lowered — a never-watched, panned, foreign sequel

| Group | Why | Pts |
|-------|-----|----:|
| A* | never watched, no keep tag, no rating | 0 |
| F1 | panned — no critic bonus | 0 |
| G1 | foreign language, no household history | −8 |
| G2 | someone abandoned it at 10% | −10 |
| G3 | critic avg < 4 | −5 |
| **Total** | | **−23 → clamps to 0** |

**0 → below every rung → 720p floor.** Never monitored, shortest grace, and the **first**
title space-pressure downgrades or deletes. *The NeverEnding Story III* is the archetype — same
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

# DESIGN — Series & Saga Resumption

> **Status:** Planned · opt-in · default-off (`acquisition.resumption.enabled = false`).
> **Thesis:** the rolling window doesn't only move forward. When you're about to return to a show — or the world drops a new season or a sequel from cast/crew you love — Glidearr **circles back**: it re-acquires the recent prior content the Rearview already shed, re-queues it into *Up Next*, and **ramps priority as the release date approaches** so you're caught up before you sit down.
>
> This doc augments [`DESIGN_recommendation_enhancement.md`](DESIGN_recommendation_enhancement.md) (resumption is a new next-watch/acquisition source) and reuses machinery from [`DESIGN_people_matrix.md`](DESIGN_people_matrix.md), the calendar service, and the personal-playlists ordering engine.

---

## 0. TL;DR

Resumption is a new **acquisition source** + a **release-date priority ramp**. It watches three signals, looks **arbitrarily far back** in your watch history (releases can be years apart), and re-grabs the *last N* prior episodes/films so you can refresh and continue — protected from the Rearview until you've had your chance to watch.

Three triggers, one engine:

| # | Trigger | What it re-acquires | Where it lands |
|---|---------|---------------------|----------------|
| 1 | **You return to a show** — add a series (or its pilot) to your watchlist | the last `episode_count` (default 5) episodes you'd already watched, going **backward** from your resume point | re-grabbed + flagged into *Up Next* immediately (R = max) |
| 2 | **A new season is dated** (*House of the Dragon*, *Silo*, …) | the previous `episode_count` episodes of the prior season(s) | *Up Next*, priority **ramping toward the premiere** |
| 3 | **A sequel from cast/crew you love is dated** (*Spider-Man: Brand New Day*, …) | the previous `film_count` (default 2) films in the franchise / by that lead, **best quality** | *Up Next*, priority **ramping toward release day** |

Everything is gated, dry-run-safe, restorable, and routed through the existing `AcquisitionScorer` + space-pressure band — no new acquisition primitives, one new pure planner and one ramp function.

---

## 0a. The three triggers, fleshed out

### Trigger 1 — Return to a show (watchlist intent)
When a series (or a bare pilot you kept) gains watchlist intent — it appears in `plex/watchlist/union` or a Trakt/MAL watchlist — Resumption treats it as *"I'm about to come back to this."* It finds your **resume point** (the highest watched `(season, episode)`) and re-acquires the `episode_count` episodes **immediately preceding and including** it, so the lead-in you forgot is back on disk. There is no future date here, so the ramp is pinned to **max** (you're returning *now*).

> Intent is already scored by [`next_watch.watchlist_intent()`](next_watch/__init__.py): `min(100, 60 + (members − 1)·12)`. Resumption consumes that signal; it does not re-implement it.

### Trigger 2 — A new season is announced/airing
The calendar service already pulls Trakt `calendars/my/shows/premieres/{start}/{days}` and MAL seasonal charts. When a **premiere** is found for a series you **own and have partly watched**, Resumption re-acquires the previous `episode_count` episodes (tail of the prior season) and slots them into *Up Next*, with priority **rising as the premiere nears** so you've rewatched the lead-in before episode 1 drops.

### Trigger 3 — A sequel from cast/crew you love
When the calendar / Trakt surfaces an upcoming **movie** that is either (a) a member of a **franchise/collection** you partly own, or (b) led by **cast/crew above your household affinity threshold** (via the people matrix), Resumption re-acquires the previous `film_count` entries **in best quality**, priority **ramping toward release day**. This is the one case that most needs deep lookback: the prior film can be many years old.

---

## 1. Key requirement — lookback reaches back as far as needed

**Releases can be years apart** (a sequel a decade later; a show returning after a 3-year gap). Resumption lookback is therefore **bounded by count, never by time**:

- `episode_count` / `film_count` bound *how many* prior entries to re-grab.
- `lookback_days_cap` is **`null` (unbounded) by default**. An operator may set a soft cap (e.g. `1825` = 5 years) but the feature spec requires unbounded to be the default and fully supported.

This is already the grain of the existing data: [`next_episode_planner.last_watched_per_series(df)`](acquisition/next_episode_planner.py) computes the resume point with **no time filter**, and the watched-set (`trakt/history/movies`, `tautulli/group/{group}/tmdb_completions`) is a single unbounded blob per bucket — there are no time-indexed variants to age content out. Resumption inherits that: a 10-year-old prior film is just as reachable as last week's episode.

---

## 2. Data model & sources (all existing)

| Need | Source (existing) | Key / function |
|------|-------------------|----------------|
| Watchlist intent (trigger 1) | Plex/Trakt/MAL watchlist union | `plex/watchlist/union`, `next_watch.watchlist_intent()` |
| Resume point + prior episodes | Sonarr episode parquet | `last_watched_per_series(df)`, `episode_files.air_date_utc` |
| Upcoming premieres (trigger 2) | Calendar service | `trakt/{user}/calendar/shows/premieres`, Sonarr `nextAiring` |
| Upcoming anime (trigger 2) | MAL seasonal | `mal/seasonal/{year}/{season}`, `mal_upcoming_above_threshold()` |
| Upcoming films + dates (trigger 3) | Calendar / Trakt + TMDB release dates | `trakt/{user}/calendar/movies`, `movie_files.theatrical_release` / `digital_release` |
| Franchise membership (trigger 3) | Radarr parquet + classifier | `collection_name`, `collection_tmdb_id`, `franchise.resolve_franchise_entries()` |
| Favoured cast/crew (trigger 3) | People matrix | `people_matrix/forward`, `people_matrix/affinity`, `co_occurring()` |
| Affinity / how-much-you-care | Watchability scorer | `score_movie` / `score_show` → 0–100, percentile |

No new fetch primitives are required; Resumption is an **orchestration** of caches that already exist.

---

## 3. Architecture — service FETCH/CACHE/APPLY, brain THINKS

Follows the brain-purity split (cf. [`DESIGN_plex_service.md`](services/plex/DESIGN_plex_service.md) §3.7).

**Service** — `scripts/managers/services/acquisition/resumption/__init__.py` (`ResumptionManager`, a `BaseManager`):
- FETCH: watched-set, resume points, calendar premieres, franchise map, people affinity.
- CACHE: writes resumption candidates to `acquisition/resumption/*` (see §7), `regenerate_on_expiry=True`.
- APPLY: emits `ResumptionCandidate` dicts into the acquisition pipeline; on add, logs the resumption reason; tags re-acquired items for short-term Rearview protection (§6.3).
- Standard `dry_run` capture: `self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False))`.

**Brain** — `scripts/managers/machine_learning/acquisition/resumption_planner.py` (pure):
```
plan(
    watched_index,        # {series_id|tmdb: watched (season,ep) | watched films}
    available_files,      # what is already on disk (skip re-grab if present)
    calendar,             # [{id, kind, release_date}]   kind ∈ {premiere, film}
    franchise_map,        # {collection_id: [films...]}
    people_affinity,      # {person_id: weight}  (favoured cast/crew)
    config,               # counts, caps, ramp params
    today,                # injected date (no Date.now in brain)
) -> list[ResumptionCandidate]
```
`ResumptionCandidate = {type, ids:{tmdb,tvdb,imdb}, season?, episode?, source, reason, S_prior, days_to_release, priority}`.

The planner is **pure** (no HTTP / service / `_api` imports) and **must** be added to `scripts/hooks/brain_purity.py::_GUARDED_SUBPACKAGES`. `today` is injected (the brain may not read the clock), mirroring the ledger-stamp discipline.

---

## 4. Prioritization math (the ramp) — *notated*

> **Note:** the codebase has **no** existing countdown/ramp logic (only `F3 +2` recency and `G4 −5` unavailable). This is a net-new function; everything below is the specification.

Each candidate gets a **resumption priority** `P ∈ [0, 100]`, blending *how much you care* with *how close the moment is*:

```
P = clamp( w_s · S_prior  +  w_r · R(d) ,  0, 100 )
```

- `S_prior` ∈ [0, 100] — watchability / affinity for the content being re-acquired (the show or franchise you're returning to). Reuses `score_show` / `score_movie` percentile. *"How much do you care?"*
- `R(d)` ∈ [0, 100] — the **release-proximity ramp** below. *"How close is the moment?"*
- `w_s`, `w_r` — weights, `w_s + w_r = 1`. Defaults `0.5 / 0.5` (`resumption.weight_affinity`, `resumption.weight_proximity`).

### 4.1 The ramp `R(d)`

Let `d` = **days until the triggering release** (negative once it has released). Parameters (all config, defaults shown):

| Symbol | Config key | Default | Meaning |
|--------|-----------|---------|---------|
| `W` | `resumption.ramp_window_days` | `60` | how early the ramp starts to rise |
| `R0` | `resumption.ready_by_days` | `7` | reach **max** this many days *before* release (be ready early) |
| `G` | `resumption.grace_window_days` | `14` | hold max for this long *after* release (you can still catch up) |
| `τ` | `resumption.decay_tau_days` | `30` | post-grace exponential decay constant |

```
            ⎧ 0                         if d > W                 (too far out — ignore for now)
            ⎪ 100 · (W − d) / (W − R0)  if R0 < d ≤ W            (approaching — linear rise)
   R(d) =   ⎨ 100                       if −G ≤ d ≤ R0           (ready window through grace — max)
            ⎪ 100 · exp(−(−d − G) / τ)  if d < −G                (released a while ago — decay)
            ⎩
```

- `d = W` → `R = 0`; `d = R0` → `R = 100`; the ramp **peaks a week early** so re-acquisition can actually finish in time.
- Through release day and `G` days after, `R = 100` (you can still be caught up if it just dropped).
- Afterwards `R` decays with half-life `τ·ln2 ≈ 21 days` — you missed the moment, so it yields slots to fresher triggers.

**Trigger 1 (resume now)** has no future date → set `d := R0`, i.e. `R = 100`. Returning is "now."

### 4.2 What `P` controls
1. **Admission order** — when more candidates exist than `resumption.max_adds` (default 50) allows this run, the highest `P` win the slots.
2. **Source score into `AcquisitionScorer`** — resumption candidates carry `source = "resumption_show" | "resumption_movie"` with `_SOURCE_SCORE = 90` (between an explicit watchlist add `100` and a recommendation `65`). The standard 0–100 blend (genre 35 / source 25 / rating 15 / popularity 10 / recency 15) still applies; `P` rides in as a sort key, not a replacement.
3. **Space-pressure precedence** — when free space `< U` (the band floor `T = free_space_limit`, top `U = T·1.10`), low-`P` resumption adds are deferred to `acquisition/deferred_search` and re-armed event-driven when free `≥ U` (no countdown needed; the existing band logic handles it). High-`P` (imminent-release) items are acquired first.

### 4.3 Worked examples
- *Silo* S2 premieres in **40 days**, you love it (`S_prior = 82`): `R = 100·(60−40)/(60−7) ≈ 37.7`; `P = 0.5·82 + 0.5·37.7 ≈ 59.8`. In-window but patient.
- Same show **5 days** out: `d ≤ R0` → `R = 100`; `P = 0.5·82 + 0.5·100 = 91`. Now urgent — wins slots and survives pressure.
- *Spider-Man: Brand New Day* releases in **10 days**, prior film `S_prior = 70`: `R = 100·(60−10)/53 ≈ 94.3`; `P ≈ 0.5·70 + 0.5·94.3 = 82.2`. Re-grab the previous film in best quality, now.

---

## 5. Lookback & counts

| Config | Default | Range | Meaning |
|--------|---------|-------|---------|
| `resumption.episode_count` | `5` | 1–50 | prior episodes to re-grab (triggers 1 & 2), counted **backward** from the resume point / season boundary, crossing season boundaries as needed |
| `resumption.film_count` | `2` | 1–10 | prior franchise/lead films to re-grab (trigger 3) |
| `resumption.lookback_days_cap` | `null` | null or days | **unbounded by default**; optional soft age cap |
| `resumption.max_adds` | `50` | — | per-run ceiling on resumption adds (prevents multi-season-backlog floods) |

Counts bound *quantity*; time never bounds *reach* (§1). If the prior content is already on disk, it is skipped (no double-grab). Films are re-grabbed at the title's normal best-quality ladder (no special profile — resolver logic is unchanged).

---

## 6. Acquisition seam & Rearview coordination

### 6.1 Wiring
`ResumptionManager.run()` executes in **Phase 3**, after Trakt/Calendar fetch and **before** `AcquisitionManager.run()`. `CandidateGatherer.gather()` gains a `_resumption()` branch reading `acquisition/resumption/*` and merging into the candidate list, deduped on `(type, primary_id)` exactly like every other source. No change to the resolver, adder, or instance/profile routing.

### 6.2 Candidate shape
Emits the existing `_norm` dict: `{title, year, type, ids:{tmdb,tvdb,imdb}, genres, rating, source:"resumption_*", is_anime}` plus `priority` (`P`) and `reason` (e.g. `"resumption: new season in 12d — re-grab prior 5 eps"`).

### 6.3 Rearview coordination (important)
Re-acquired catch-up content is **exactly what the Rearview normally sheds**. Without coordination, the trailing edge would delete what Resumption just grabbed. Therefore re-acquired items are tagged `resumption_protect_until = release_date + G` (or `today + G` for trigger 1) and added to the never-delete guard set until that date **or** until you watch them — whichever comes first. This is a thin addition to the existing whole-file delete-guard set (see [`project_whole_file_delete_guards`] discipline), not a new deletion path.

---

## 7. Config & cache keys

**Config** (`config.acquisition.resumption`, onboarding schema at `scripts/managers/factories/onboarding/schema.py`):
```jsonc
"acquisition": {
  "resumption": {
    "enabled": false,            // master gate (opt-in)
    "episode_count": 5,
    "film_count": 2,
    "lookback_days_cap": null,   // unbounded
    "max_adds": 50,
    "weight_affinity": 0.5,
    "weight_proximity": 0.5,
    "ramp_window_days": 60,
    "ready_by_days": 7,
    "grace_window_days": 14,
    "decay_tau_days": 30,
    "triggers": { "watchlist": true, "new_season": true, "sequel": true },
    "people_affinity_min": 0     // reuse people_matrix; 0 = off until people_cooccurrence lands
  }
}
```

**Cache keys** (slash-delimited, `regenerate_on_expiry=True`, TTL 24–72 h):
- `acquisition/resumption/show_candidates` — per-series prior-episode re-grab list + `P`.
- `acquisition/resumption/movie_candidates` — franchise/lead prior-film re-grab list + `P`.
- `acquisition/resumption/protect_index` — `{id: resumption_protect_until}` for Rearview coordination.

---

## 8. Phased roadmap

| Phase | Scope | Gate / parity |
|-------|-------|---------------|
| **P0** | Pure `resumption_planner.plan()` + ramp `R(d)` + unit tests (golden vectors for the math); brain-purity guard entry | no behaviour (planner unused) |
| **P1** | Trigger 1 (watchlist resume) → `show_candidates`; wire `_resumption()` into `CandidateGatherer`; `source_score=90`; dry-run grids | default-off, byte-identical when off |
| **P2** | Trigger 2 (new season) via calendar premieres + ramp; Rearview `protect_index` coordination | default-off ramp; protect-guard is additive |
| **P3** | Trigger 3 (sequel) via franchise map + people-matrix affinity (depends on `people_cooccurrence` landing) | default-off; gated on people-matrix enablement |

Each phase ships as its own gated PR with the brain-purity + secret-scan hooks, default-off ramps, and (for P2/P3) ledger-diff review where the protect-guard touches deletion.

---

## 9. Open questions / risks

1. **Release-date coverage.** Trigger 2/3 ramps need reliable dates. Premiere dates come from Trakt calendars; film dates from TMDB `release_dates` via the enrich daemon. Coverage % should be logged at build time (mirror the playlist readiness diagnostics); titles without a date fall back to `R = 0` (added only on affinity, never ramped).
2. **Franchise-vs-people overlap (trigger 3).** A sequel may match both a collection and a loved lead. Dedup on `(type, primary_id)`; take the higher `S_prior`.
3. **Per-user vs household.** v1 is household-level (matches current acquisition). Per-user resumption (your watchlist add re-grabs *your* lead-in) is blocked on the same per-user watched-set prerequisites as [`DESIGN_personal_playlists.md`](services/plex/DESIGN_personal_playlists.md) §4.
4. **Backlog floods.** A long-dormant show returning could request a whole season; `max_adds` + `P`-ordering bound it, but consider a per-series sub-cap.
5. **Best-quality re-grab vs space.** Trigger 3 wants the prior film in best quality, which fights the Rearview. The `protect_until` window resolves the immediate conflict; long-term the film falls back under normal watchability deletion once watched.

---

## 10. Key files & related docs

**New:**
- `scripts/managers/machine_learning/acquisition/resumption_planner.py` (pure)
- `scripts/managers/services/acquisition/resumption/__init__.py` (`ResumptionManager`)

**Touched:**
- `scripts/managers/services/acquisition/candidates.py` (`_resumption()` branch in `gather()`)
- `scripts/managers/services/acquisition/scorer.py` (`_SOURCE_SCORE["resumption_*"] = 90`)
- `scripts/hooks/brain_purity.py` (guard the new brain module)
- `scripts/managers/factories/onboarding/schema.py` + `config.json` (config surface)
- the whole-file delete-guard set (Rearview `protect_index`)

**Related design docs:**
- [`DESIGN_recommendation_enhancement.md`](DESIGN_recommendation_enhancement.md) — resumption is a new next-watch/acquisition source (the "global propose, local rank" direction).
- [`DESIGN_people_matrix.md`](DESIGN_people_matrix.md) — favoured cast/crew → sequel detection (trigger 3).
- [`services/plex/DESIGN_personal_playlists.md`](services/plex/DESIGN_personal_playlists.md) — *Up Next* ordering the re-grabbed items land in.
- [`DESIGN_auto_run_triggering.md`](../../DESIGN_auto_run_triggering.md) — scheduled runs that drive the ramp's day-by-day re-evaluation.

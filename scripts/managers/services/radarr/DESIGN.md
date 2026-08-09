# radarr — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **radarr**

**Package** — `scripts.managers.services.radarr`
**Status** — ✅ Implemented · 🟡 Multi-instance tier routing partial (Phases 1–2 built)
**Related** — [README.md](./README.md) · [`services/DESIGN.md`](../DESIGN.md) · [`sonarr/DESIGN.md`](../sonarr/DESIGN.md)

---

## 1. Problem statement

Radarr manages the movie half of the library, and it is the more structurally
complex of the two `*arr` integrations for one reason: **movies are
multi-instance.**

A household running a `standard` (HD) instance alongside an `ultra` (4K/UHD)
instance — and optionally a dedicated `anime` instance — needs every acquisition
and every upgrade routed to the instance whose role matches the target tier. Get
that wrong and a 4K-worthy title lands on `standard` with no path to `ultra`.

Four further problems follow from the domain:

1. **A cold `GET /movie` costs ~39 seconds.** The brain needs the full library
   every run. This is why `Main` starts a background prefetch thread before any
   phase begins.
2. **Quality is a per-movie decision, not a per-library setting.** The right
   profile depends on the watchability score, device fit, space pressure and
   whether the title is universe-tagged.
3. **Deletion is irreversible and the signals are indirect.** A movie nobody has
   watched in three years may be the one thing someone re-watches annually.
4. **State drifts.** Files get moved, renamed, deleted outside Glidearr; tags get
   changed by hand; profiles get edited in the Radarr UI. Every run must
   reconcile.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One subsystem per concern, mirroring Sonarr's shape so either is navigable from the other. |
| G2 | Every acquisition and upgrade routes to the instance matching its target tier. |
| G3 | The full movie library is available to the brain with no cold-fetch penalty in the run. |
| G4 | Drift is reconciled every run rather than accumulating. |
| G5 | Every APPLY is `dry_run`-gated. |
| G6 | Cross-instance duplicates are detected and resolved. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Owning indexers or download clients | Radarr does that. |
| N2 | Scoring | Brain layer. This service asks and applies. |
| N3 | Per-episode logic | Movies are atomic. That is Sonarr's problem. |
| N4 | Being the metadata authority | TMDB/Trakt own metadata; this caches it. |

---

## 3. Architecture

### 3.1 Subsystem map

Mirrors Sonarr deliberately (G1). Learn one, navigate both.

| Subfolder | Verb | Responsibility |
|---|---|---|
| [`api/`](./api/) | FETCH | HTTP client + auth |
| [`cache/`](./cache/README.md) | CACHE | Snapshots, enrichment, relational tables, history, tags, quality, monitoring |
| [`instance/`](./instance/README.md) | — | Multi-instance resolution, updater |
| [`movies/`](./movies/README.md) | FETCH/CACHE | Retrieval, enrichment, credits, keywords, dataframe, sync |
| [`quality/`](./quality/README.md) | APPLY | Profile selection, custom formats, file size, space pressure, universe |
| [`monitoring/`](./monitoring/README.md) | APPLY | Monitor flags, rules, scheduling, history |
| [`storage/`](./storage/README.md) | **APPLY** | Space, selection, deletion, relocation, cross-instance move + dedup |
| [`repair/`](./repair/README.md) | APPLY | Anomalies, orphans, metadata, tags, quality, storage drift |
| [`sync/`](./sync/README.md) | APPLY | Push custom formats, naming, folders, media management, tags, profile scores |
| [`validator/`](./validator/README.md) | — | Auth, health, keys, cache validity |
| [`orchestration/`](./orchestration/README.md) | — | Intra-service sequencing, enrichment ETA |

### 3.2 Control flow

```
Main constructs RadarrManager (after Tautulli + Trakt — Trakt's registry entry
must be visible to RadarrOrchestrationManager during run_relational_pull)

Main.run():
    _start_radarr_library_prefetch()   ← daemon thread, ~39s GET /movie
        …Tautulli, Trakt, MAL, Sonarr phases run…
    prefetch.join(timeout=90)          ← bounded; on timeout repair does its own fetch
    radarr.prepare()
    radarr.run()
        instance → cache → movies → quality → monitoring → sync → storage → repair
```

### 3.3 Instance routing — the partial subsystem

**Routing rule** (from [README.md](./README.md)):

| Instances | Behaviour |
|---|---|
| 1 | No routing decision. No-op. |
| 2+ | Route by target tier via `radarr_instances_categorized`: ≤1080p → `standard`/HD · 4K/UHD → `ultra` · anime + anime instance exists → `anime` |

Target tier comes from the watchability score → quality-profile tier
(see [`SCORING_GROUPS.md`](../../machine_learning/scoring/SCORING_GROUPS.md);
e.g. 70–79 → Remux-2160p HDR).

**What is built (Phases 1–2):**
- Onboarding writes `radarr_instances_categorized` (resolution tiers + optional anime)
- `gateway.categorized_instance` is service-aware
- [`acquisition/resolver.py`](../acquisition/resolver.py) routes **anime movies** to
  the categorized anime instance, falling back to the default when no anime
  session is set (reusing the existing `classify_movie`/`_is_anime` classifier)

**What remains:**
- Resolution-tier routing for **new adds** happens post-landing in
  [`router_movie.py`](../../../support/tools/router_movie.py) — the file does not
  exist at add-time — so wiring that to `categorized_instance` is a separate task
- Add-if-absent / tag-for-auto-add
- Safe make-before-break migration (the upgrade/downgrade path)

**Current gap:** acquisitions and upgrades resolve to `gateway.default_instance()`.
A 4K-worthy upgrade lands on `standard` with no path to `ultra`.

Note that Sonarr is single-instance and **no longer has a categorized-instance
map**, so `categorized_instance` is effectively Radarr-only — there is no Sonarr
anime-routing pattern left to mirror.

### 3.4 Data contracts

| Artifact | Produced by | Consumed by |
|---|---|---|
| `radarr/<instance>/library` snapshot | prefetch + [`cache/`](./cache/README.md) | everything |
| `_movies_enriched` Parquet | [`movies/enrich.py`](./movies/enrich.py) | brain features |
| `_people_enriched` Parquet | [`movies/credits.py`](./movies/credits.py) | people matrix |
| Relational tables | [`cache/relational.py`](./cache/relational.py) | people matrix (movie half) |
| `radarr/run_stats` | this manager | `Main`, Discord |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Folder shape mirrors Sonarr | G1 — halves navigation cost across the two largest subsystems | Shape to the API |
| D2 | Background prefetch started before any phase | G3 — overlaps the ~39s cold fetch with Tautulli/Trakt/Sonarr | Fetch inline |
| D3 | `join(timeout=90)` on the prefetch | Bounded — a pathological fetch can never hang the run; repair falls back to a live fetch | Unbounded join |
| D4 | Constructed after Trakt | `RadarrOrchestrationManager` resolves Trakt from the registry during `run_relational_pull` | Any order |
| D5 | Repair reconciles every run | G4 — drift is continuous, so reconciliation must be too | On-demand repair |
| D6 | Instance routing driven by target *tier*, not by title | The tier is the thing an instance's role maps to | Route by genre/classification alone |
| D7 | Anime routing falls back to default when no anime session exists | A missing optional instance must not block acquisition | Hard failure |
| D8 | Cross-instance dedup + move as first-class | G6 — two instances sharing storage will otherwise hold duplicates | Ignore duplicates |
| D9 | `movieRootFolders` drives bucket assignment | Classification should determine placement | Single root |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Every APPLY checks `dry_run` first. |
| I2 | HD-720p is the floor; no path targets SD. |
| I3 | 4K requires score ≥ 70. |
| I4 | `score is None` ⇒ safe mid-tier (HD Bluray + WEB), never UHD. |
| I5 | `keep-universe` is never deleted — quality change only. |
| I6 | Bare `universe` is deletable only as a last resort. |
| I7 | No scoring logic in this service. |
| I8 | A second consecutive run is a no-op. |
| I9 | With one instance, routing is a no-op — never a spurious move. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| Radarr unreachable | `validate_all` | Movie phases skipped | Movies | ✅ Auth line |
| Prefetch times out | `join(timeout=90)` | Repair does its own live fetch | +39s | 🟡 Debug only |
| Prefetch thread raises | Caught | Same fallback | +39s | 🟡 Debug only |
| Snapshot stale within TTL | `radarr_movie_library_max_age_s` | Reused, live fetch skipped | Freshness | ❌ **None** |
| 4K-worthy upgrade with 2+ instances | **None** | Lands on `standard`; no path to `ultra` | 🟡 Wrong tier, silently | ❌ **None** |
| `movieRootFolders` empty | **None** | `classify_movie` buckets computed then **discarded**; all movies → `/data/media/movies/standard` | 🔴 Classification inert | ❌ **None** |
| Cross-instance duplicate | [`cross_instance_dedup_apply.py`](./storage/cross_instance_dedup_apply.py) | Dedup plan | Handled | ✅ Ledger |
| Partial APPLY batch | **None** | Ledger may not match actual state | 🟡 Drift until next repair | ❌ **None** |
| Tag changed by hand in Radarr UI | [`repair/tags.py`](./repair/tags.py) | Reconciled | Handled | ✅ |
| Profile edited in Radarr UI | [`sync/`](./sync/README.md) | Overwritten on next sync | Intended | 🟡 Silent overwrite |

**Applying the §8 P-D sweep question** — *how does the operator learn?* — four
rows above have no signal at all. Rows 5 and 6 are the consequential ones: a
mis-tiered upgrade and inert movie classification both look exactly like normal
operation.

---

## 7. Configuration surface

| Key | Type | Default | Effect |
|---|---|---|---|
| `radarr_instances` | dict | `{}` | Instance map + `default_instance` pointer |
| `radarr_instances_categorized` | dict | — | Role map: resolution tiers + optional anime. Written by onboarding |
| `radarr_movie_library_max_age_s` | int | `900` | Snapshot freshness window for the prefetch |
| `movieRootFolders` | list | `[]` | 🔴 Empty — classification buckets discarded |
| `dry_run` | bool | `False` | APPLY gate |

Identity: Radarr instance `standard`.

---

## 8. Implemented capabilities

- ✅ Full movie library sync with background prefetch and bounded join
- ✅ Enriched movie / credits / keywords / people Parquet
- ✅ Relational tables feeding the people matrix (movie half)
- ✅ Per-movie quality-profile selection from watchability score
- ✅ Custom-format sync, naming, folders, media management, profile scores
- ✅ Space-pressure-aware quality adjustment
- ✅ Universe membership handling with `keep-universe` protection
- ✅ Deletion, relocation, selection planning
- ✅ Cross-instance dedup + move with shared-storage detection
- ✅ Repair: anomalies, orphans, metadata, tags, quality, storage drift
- ✅ Monitoring rules + scheduler
- ✅ Auth / health / key / cache validators
- ✅ Multi-instance categorization written by onboarding (Phase 1)
- ✅ Anime-movie routing to a categorized anime instance with default fallback (Phase 2)
- ✅ `dry_run` propagated to every submanager

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-RAD-01` | 🔴 **Complete multi-instance tier routing** — add-if-absent/tag + safe make-before-break migration for the upgrade/downgrade path | The headline gap: a 4K-worthy upgrade lands on `standard` with no path to `ultra`. Phases 1–2 are built; this is the remainder | L | `radarr_instances_categorized` |
| `GLD-RAD-02` | **Wire `router_movie.py` to `categorized_instance`** for new adds — resolution-tier routing currently happens post-landing because the file doesn't exist at add-time | Closes the new-add half of `GLD-RAD-01` | M | `GLD-RAD-01` |
| `GLD-RAD-03` | **Extend `RadarrQualitySelector` to pick the instance from target tier first**, before resolving the best-fit profile per instance | The selector already resolves per instance; this is the hook point named in the README | M | `GLD-RAD-01` |
| `GLD-RAD-04` | **Signal on mis-tiered placement** — report when a title's target tier doesn't match its instance | Makes §6 row 5 visible instead of silent *(P-D)* | S | — |
| `GLD-RAD-05` | **Signal on discarded classification** — warn when `movieRootFolders` is empty and buckets are being thrown away | Makes §6 row 6 visible *(P-D)* | S | `GLD-CFG-03` |
| `GLD-RAD-06` | **Prefetch outcome in the run summary** — hit / miss / timeout / stale-reuse | §6 rows 2–4 are debug-only today *(P-D)* | S | — |
| `GLD-RAD-07` | **Profile-drift report** — list profiles overwritten by sync, rather than silently overwriting | §6 row 10 *(P-D)* | S | — |
| `GLD-RAD-08` | **Incremental library fetch** via a cursor instead of a full `GET /movie` | Removes the ~39s cold cost entirely rather than hiding it behind a thread | M | Radarr API support |
| `GLD-RAD-09` | **Per-instance failure isolation** — one dead instance shouldn't skip the others | Failure is service-level today | M | `GLD-SVC-12` |
| `GLD-RAD-10` | **Document `api/`** — the only Radarr subfolder with no README | Doc parity | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | For add-if-absent: direct API add, or tag-for-auto-add? README leans direct add as the default. | `GLD-RAD-01` |
| Q2 | What does make-before-break migration look like — add to target, verify, then remove from source? | `GLD-RAD-01` |
| Q3 | Should `movieRootFolders` be derived from classification or configured per bucket? *(= D3)* | `GLD-CFG-03` |
| Q4 | Should a hand-edited Radarr profile be preserved or overwritten? Currently overwritten silently. | `GLD-RAD-07` |

## 11. Related designs

- [`sonarr/DESIGN.md`](../sonarr/DESIGN.md) — the mirrored TV subsystem
- [`services/DESIGN.md`](../DESIGN.md) §3.3 — the uniform shape
- [`machine_learning/scoring/SCORING_GROUPS.md`](../../machine_learning/scoring/SCORING_GROUPS.md) — score→profile table driving tier selection
- [`machine_learning/DESIGN_codec_routing_build_plan.md`](../../machine_learning/DESIGN_codec_routing_build_plan.md)
- [`acquisition/README.md`](../acquisition/README.md) · [`coordinator/README.md`](../coordinator/README.md)
- [`onboarding/README.md`](../../factories/onboarding/README.md) — the role map this consumes

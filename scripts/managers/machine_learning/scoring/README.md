# scoring

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **scoring**

**Package** — `scripts.managers.machine_learning.scoring`
**Run position** — Called synchronously by service adapters during phases 2–3.
**One-liner** — The watchability model: a 0–100 float built from weighted, independently-capped signal groups A–G, driving quality selection, deletion, monitoring and grace.

---

## Purpose

This package produces **the number everything else keys off**. Project goal G1
says acquisition, quality and space share one model; this is that model.

`score_movie` (and its TV twin `score_show`) replaces an older 1–10 integer scale
with a 0–100 float:

```
final = max(0, min(100, round(sum of every group contribution)))
```

Each group is capped independently, so no single signal dominates — an acclaimed
film still needs household signal to reach the top tiers, and a beloved-but-obscure
title isn't sunk by a missing critic rating.

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`movie_scorer.py`](./movie_scorer.py) | [`SCORING_GROUPS.md`](./SCORING_GROUPS.md) | `score_movie` — the movie engine | ✅ Implemented |
| [`show_scorer.py`](./show_scorer.py) | [`SCORING_GROUPS.md`](./SCORING_GROUPS.md) | `score_show` — mirrors A–G; A2 is recency+breadth, F1 uses TV rating sources, Group C = 0 | ✅ Implemented |
| [`_shared.py`](./_shared.py) | — | Shared tables and helpers, incl. `QUALITY_PROFILE_THRESHOLDS`. **Exists so the two engines can never drift** | ✅ Implemented |
| [`device_fit.py`](./device_fit.py) | — | Group D v2 transcode-risk axes. **Renormalises a missing axis out of the weighted risk** rather than scoring it zero | ✅ Implemented |
| [`critic.py`](./critic.py) | — | Group F critic blend across imdb/tmdb/trakt/metacritic/RT | ✅ Implemented |
| [`golden_corpus.py`](./golden_corpus.py) | — | Byte-identity regression oracle | ✅ Implemented |
| [`golden_scores.json`](./golden_scores.json) | — | Frozen expected scores | ✅ Implemented |
| [`SCORING_GROUPS.md`](./SCORING_GROUPS.md) | — | **The reference.** Every group, every point value, worked examples | 📄 Reference |

## Test coverage

| Test | Covers |
|---|---|
| [`test_score_golden.py`](./test_score_golden.py) | Byte-identity against the golden corpus; holds the clock-reading signals stable |
| [`test_breakdown.py`](./test_breakdown.py) | `return_breakdown=True` does not change the score |
| [`test_device_fit.py`](./test_device_fit.py) · [`test_device_capabilities.py`](./test_device_capabilities.py) | Group D |
| [`test_quality_ladder.py`](./test_quality_ladder.py) | Score → profile tier |
| [`test_person_affinity_score.py`](./test_person_affinity_score.py) · [`test_person_affinity_cap.py`](./test_person_affinity_cap.py) | Group B |
| [`test_language_consumability.py`](./test_language_consumability.py) | G1 |
| [`test_a5_intent.py`](./test_a5_intent.py) | A5 watchlist intent |
| [`test_c4_integration.py`](./test_c4_integration.py) | Group C4 |

---

## The signal groups

Full tables in [`SCORING_GROUPS.md`](./SCORING_GROUPS.md). Summary:

| Group | Question | Budget |
|---|---|---|
| **A** — Household intent | *Do they want this?* keep tags, completion, rewatch, user rating, watchlist intent | ≈33 |
| **B** — People affinity | *Do they like these people?* cast/crew overlap with watched titles | — |
| **C** — Related graph | *Is it near things they watch?* Trakt related, franchise, collection. **Zero for shows** | — |
| **D** — Device/playback fit | *Will it play without transcoding?* resolution, codec, bitrate, audio, subtitles, container | — |
| **F** — Critic | *Is it any good?* weighted blend of imdb/tmdb/trakt/metacritic/RT | — |
| **G** — Penalties | language mismatch (−8/−4/−1), abandoned (−10), panned (−5/−2), unavailable (−5) | negative |

> ⚠️ **Naming collision.** These codes (`A1`…`G4`) name *scoring signals*. The
> enhancement-batch labels in PRs and commits (`A1` temporal-decay, `C2`
> grace-window) are a **different namespace** using the same letters.

**Group budgets are soft.** Only the final clamp to `[0, 100]` is enforced, so a
genuine favourite stacks past a group's nominal budget.

---

## Score → quality tier

Rungs are **calibrated to this household's real distribution** — p97/p98/p99/p99.5/p99.9
of the file-owning population (6,449 titles) under Group D v2. They move whenever
the score axis moves. Override with `scoring.quality_ladder`.

| Score | Percentile | Tier |
|---:|---:|---|
| 0–24 | — | HD-720p (floor; SD absorbed into 720p) |
| 25–28 | p97 | WEBDL-1080p |
| 29–32 | p98 | Bluray-1080p |
| 33–37 | p99 | Remux-1080p |
| 38–49 | p99.5 | Remux-2160p — 4K entry |
| 50–100 | p99.9 | Remux-2160p — top tier |

**The ladder only *proposes* a tier.** Actual 4K acquisition is separately gated
by `watch_likelihood.uhd_cutoff` (75) and `routing.movies.4k_dual_min_score` (75),
**which live on a different scale**. See [`DESIGN.md`](./DESIGN.md) §3.3 — this is
a live source of confusion.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md) · **Reference:** [`SCORING_GROUPS.md`](./SCORING_GROUPS.md)
- **Inputs:** [`contracts/`](../contracts/README.md) · [`features/`](../features/) · [`affinity/`](../affinity/)
- **Consumers:** [`space/`](../space/) · [`lifecycle/`](../lifecycle/) · [`likelihood/`](../likelihood/)

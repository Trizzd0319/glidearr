All facts confirmed against live code. I have everything needed to write the plan.

# PER-VIEWER, CODEC-AWARE TRANSCODE REDUCTION — PHASED BUILD PLAN

Lead-architect synthesis of the four specs against verified code. All citations re-confirmed in this session.

---

## A. DECISION LOG — reconciling `DESIGN_per_person_codec_profiles.md` with the locked decisions

The design doc's backbone stands. Three locked decisions supersede its wording, and one structural gap it never named must be added.

| # | Topic | Doc said | LOCKED decision | Verdict |
|---|---|---|---|---|
| 1 | **Objective** | "highest-quality profile that direct-plays" (loose) | Minimize the **watch-share-weighted count of likely viewers who transcode**, tie-break smaller file | **CHANGE** — codify as `viewer_transcode_cost`. The objective collapses correctly: single-viewer → "the fingerprint that viewer direct-plays"; multi-viewer → coverage-max automatically (argmin of Σ w·P is minimized when the majority share direct-plays). |
| 2 | **Shared-title policy** | Open recommendation, *included* a "blend to common-denominator H.264" option | **COVERAGE-MAX / ACCEPT-MINORITY**, ONE copy, no multi-version, no common-denominator | **CHANGE + REJECT** — drop the common-denominator branch entirely. Accept-minority is *automatic* in the argmin (low-weight viewers' transcode is a tolerated residual term). Add a `min_coverage` knob (default 0.0 = pure argmin) for operators who want a floor. |
| 3 | **Fingerprint scope** | Video-codec-centric; audio/HDR/DV "follow-ons" | **FULL-FINGERPRINT** (video + audio + HDR/DV + container) in scope from MVP | **CHANGE — but FREE.** The engine already keys on the 5-tuple `(video, audio, subtitle, res_hdr, location)` (`transcode_fingerprint.py:113`, `:221`). `predict_transcode` already weighs Atmos/DTS and HDR/DV first-class. No new probability math — Samsung DV/DTS ride the existing `audio` + `res_hdr` axes. We **keep** video-codec as the primary acquire-time *steer*, treat audio/HDR as scoring axes + tie-break CFs (not hard gates — Radarr CFs can only *prefer* an audio track, not guarantee it). |
| 4 | **Sidegrade gap** | **Silent** — doc never noticed planners are rank-delta-only | (driver of locked plan) | **ADD.** Verified: `upgrade_planner.py:119` `if target_rank <= cur_rank: already_best; continue`; JIT `episode_files.py` `action = 'DOWNGRADE' if cur_res>target else 'UPGRADE'`; enum is `delete\|downgrade\|upgrade\|None` (`movie_files.py:106,119`). A same-res codec change = rank-delta 0 = swallowed. MVP introduces a **codec re-pick at acquisition only** (no owned file → no sidegrade); the true `sidegrade` action is **deferred to Phase 3**. |
| 5 | **Space justification** | not addressed for codec | (driver) | **ADD.** Verified: `estimate_gb_for_profile(..., codec=)` exists at `size_model.py:319/325` and is **dead** (no caller). `downgrade_planner.step_targets` est<cur_gib veto would reject a larger H.264 re-grab. Decision: sidegrades **bypass** `step_targets` entirely with their own `sidegrade_budget_gb` gate. |
| 6 | **Source-codec axis** | "source codec" only | (verified-fact gap) | **WIDEN** to source *fingerprint*. `per_user_source_codec_rates` is the history⋈metadata join; **not a blocker** — `predict_transcode`'s graded fallback coarsens streamed→codec_only. Ship against the **streamed-codec matrix first** (Phase 1), drop-in source-codec matrix later (Phase 2). Same tuple shape, no selector change. |

**KEEP (unchanged from doc):** the managed **profile matrix** (res-tier × codec-pref, CF scores baked once, idempotent), the **brain-decides / service-applies** split, the genuinely-missing `update_profile_custom_format_scores` PUT API, golden-harness gating, and `profile_selector.py` as the migration-target home for the policy brain.

---

## B. ARCHITECTURE — end-to-end data flow

```
                          ┌─────────────────── PRODUCERS (cached, mostly exist) ──────────────────┐
 history (tautulli) ──┬──▶ per_user_transcode_fingerprint_matrix  (DONE :151, cached, UNCONSUMED)
                      ├──▶ per_user_platform_usage          (NEW — affinity/platform_usage.py)
                      ├──▶ build_per_title_watchers / jit_watchers  (generalize :3471 → movies+counts)
                      └──▶ per_user_source_codec_rates  (Phase 2 — history ⋈ metadata_index)
 metadata_index ─────────▶ (+ hdr/container axes added to build_metadata_index :42-43)
 per_user affinity (cached tautulli/users/{safe}/affinity) ─┐
 popularity prior ─────────────────────────────────────────┤
                                                            ▼
                  ┌──────────────────── BRAIN (pure, profile_selector.py + likely_viewers.py) ───────────────────┐
                  │  infer_likely_viewers(feature_row, ...) ───────────────▶ {user: watch_share}                  │
                  │  classify_profile_axes(profile, cf_id_to_name) ───────▶ {codec,hdr,dv,audio_pref,res_tier}    │
                  │  candidate_fingerprint(axes) ──[delegates to source_fingerprint]──▶ 5-tuple                   │
                  │  viewer_transcode_cost(fp, likely_viewers, matrices) = Σ w·predict_transcode(...)             │
                  │  choose_codec_profile(tier, likely_viewers, matrices, candidate_profiles) ─▶ (profile_id, reason)│
                  │  title_needs_sidegrade(owned_file_fp, matrix, weights) ─▶ (bool, alt, reason)  [Phase 3]      │
                  └────────────────────────────────────────────────────────────────────────────────────────────┘
                                                            │ profile_id
        ┌──────────────────────────── INTEGRATION SEAMS (service adapters) ───────────────────────────┐
        │ ADD:     resolver._profile_for_score (:286-298)  — codec pick WITHIN earned tier            │
        │          resolver.resolve_quality   (:300-319)   — reads matrices once, threads down        │
        │ UPGRADE: scoring/_shared.select_profile_id (:368) — codec dim after res ceiling             │
        │          upgrade_planner.py (:119)  — emit 'sidegrade' when rank ties [Phase 3]             │
        │ JIT:     episode_files.py (~5461)   — 'SIDEGRADE' branch [Phase 3]                          │
        └──────────────────────────────────────────────────────────────────────────────────────────┘
                                                            │
        ┌──────────────────────────────── APPLY (reused primitives) ────────────────────────────────┐
        │ PROVISION (once): update_profile_custom_format_scores → PUT qualityProfile/{id} formatItems │
        │ ASSIGN (per title): GET → set qualityProfileId → PUT → search                               │
        │    radarr space_pressure.py:1210-1239  |  radarr/movies/quality.py:update_movie_profile     │
        │    sonarr JIT QP-flip + EpisodeSearch worker  |  routing/cross_instance_move.py:105-108      │
        └──────────────────────────────────────────────────────────────────────────────────────────┘
```

**New pure functions (canonical names + homes):**

| Function | File | Signature |
|---|---|---|
| `infer_likely_viewers` | `quality_analytics/likely_viewers.py` (NEW) | `infer_likely_viewers(feature_row, per_user_affinity, *, per_title_plays=None, jit_watchers=None, popularity=None, tracked_users=None, min_plays=3, threshold=0.15, half_life_days=None) -> dict[str,float]` |
| `platform_weights_for_viewers` | `quality_analytics/likely_viewers.py` | `platform_weights_for_viewers(likely_viewers, per_user_platform_usage) -> dict[str,float]` |
| `classify_profile_axes` | `quality_analytics/profile_selector.py` | `classify_profile_axes(profile, cf_id_to_name=None) -> dict` |
| `candidate_fingerprint` | `quality_analytics/profile_selector.py` | `candidate_fingerprint(axes, *, location="unknown") -> tuple` |
| `viewer_transcode_cost` | `quality_analytics/profile_selector.py` | `viewer_transcode_cost(profile_fp, likely_viewers, per_user_matrix, per_user_platform_weights, *, none_p=0.5, min_n=3) -> float` |
| `choose_codec_profile` | `quality_analytics/profile_selector.py` | `choose_codec_profile(resolution_tier, likely_viewers, per_user_fingerprint_matrix, candidate_profiles, *, per_user_platform_weights, size_hint=None, min_n=3, none_p=0.5, min_coverage=0.0) -> tuple[int|None, dict]` |
| `per_user_platform_usage` | `affinity/platform_usage.py` (extend) | `per_user_platform_usage(history_entries, user_list) -> dict[str, dict[str,int]]` |
| `per_user_source_codec_rates` | `quality_analytics/device_codec_capabilities.py` (NEW) | `per_user_source_codec_rates(history_entries, metadata_index, user_list=None) -> dict[str,dict]` |
| `title_needs_sidegrade` | `quality_analytics/profile_selector.py` | `title_needs_sidegrade(current_file_fingerprint, matrix, platform_weights, *, transcode_thresh=0.34, candidate_profiles=None) -> tuple[bool, str|None, str]` |
| `plan_codec_repicks` | `space/codec_repick_planner.py` (NEW) | `plan_codec_repicks(df, likely_viewers_map, matrices, ranked_profiles, *, extra_gb_budget, thresh, sidegrade_floor) -> tuple[list, dict]` |
| `update_profile_custom_format_scores` | `radarr/quality/custom_formats.py` (+ Sonarr twin) | `update_profile_custom_format_scores(instance, profile_id, scores: dict[int,int]) -> bool` |

---

## C. PHASED PLAN

### PHASE 1 — MVP: bias NEW acquisitions toward coverage-max, default-off, NO sidegrade

The smallest useful slice: at *add* time there is no existing file, so picking the codec variant within the earned resolution tier costs nothing and can **never** trigger a sidegrade. Ships the policy brain + its first consumer.

**NEW (pure brain):** `quality_analytics/profile_selector.py` — fill the empty stub (`:21-24` is a TODO migration shim):
- `classify_profile_axes(profile, cf_id_to_name=None)` — parse codec from name suffix `(H264)/(HEVC)/(AV1)` reconciled with CF scores (positive `AVC/x264`/`HEVC`/`AV1` = steered-in; `-10000/-35000` = banned); `res_tier` from `profile_max_quality`. Accepts both live `formatItems[{format,score}]`+`cf_id_to_name` AND the blueprint `cf_scores` name→score dict.
- `candidate_fingerprint(axes, *, location)` — delegate to `transcode_fingerprint.source_fingerprint` so normalization is **identical** to the live Stage-C path (`routing_targets.py:139` precedent).
- `viewer_transcode_cost(...)` — `Σ_v w_v · (p if p is not None else none_p)`, `p,_ = predict_transcode(matrix[v], fp, weights[v], min_n=min_n)`.
- `choose_codec_profile(...)` — argmin over candidates at the earned tier by `(cost, est_size)`, final tie → prefer space-efficient codec (HEVC/AV1) so a cold/empty household gets the bandwidth-optimal default not `eligible[-1]`. Returns `(profile_id, reason_dict)`.

**NEW (pure):** `quality_analytics/likely_viewers.py` — `infer_likely_viewers` MVP path (b only): per-user affinity `genre_match` propensity (reusing `acquisition/demand.py` per-user terms) + popularity prior for cold users, renormalized to watch-share. Degradation ladder: no affinity for any user → uniform-over-roster; no roster → `{}` (caller skips). Plus `platform_weights_for_viewers`.

**NEW (pure):** `affinity/platform_usage.py` add `per_user_platform_usage` — group history by `user_id` (same join as `per_user_affinity`), run existing `platform_usage` per slice.

**MODIFIED (service adapter, flag-gated):**
- `acquisition/resolver.py:_profile_for_score (:286-298)` — when gate ON, instead of `return eligible[-1]`, group `eligible` by res tier and within the earned tier call `choose_codec_profile`. **OFF → returns `eligible[-1]` byte-identical.**
- `acquisition/resolver.py:resolve_quality (:300-319)` — read `tautulli/transcode_fingerprint` (deserialize), per-user platform usage, and compute `infer_likely_viewers(enriched)` ONCE here; thread matrices+weights into `_pick_profile`→`_profile_for_score`. Size via `estimate_gb_for_profile(profile, runtime, codec=classified_codec)` — **first caller of the dead `codec=` arg**.
- `size_model.py:estimate_gb_for_profile` — **no code change**; just gets its first caller (the tie-break accuracy depends on it).
- `onboarding/schema.py` + a `codec_profiles_enabled(config)` gate mirroring `transcode_gate_enabled (routing_targets.py:102)` — config block `scoring.codec_profiles: {enabled: false, transcode_thresh: 0.34, none_p: 0.5, min_coverage: 0.0, min_n: 3}`.

**CAVEAT (locked, structural):** at add time the candidate `_lookup` obj (`resolver.py:~70`) has **no source codec** — the release codec is unknown pre-grab. So Phase 1 is a **profile preference** (pick the matrix profile whose baked CF scores steer the direct-play codec), not a prediction on a known file. This requires the codec-aware **profile matrix to exist**; without ≥2 codec variants at a tier, `choose_codec_profile` degenerates to `eligible[-1]` (byte-identical). Matrix provisioning via `update_profile_custom_format_scores` is therefore a Phase-1 prerequisite *or* the blueprint profiles (`radarr_profiles.json:1723-2303`) are imported manually.

**Tests (golden + unit):** synthetic per-user matrices asserting (1) single-viewer → that viewer's direct-play codec; (2) shared incompatible devices → accept-minority argmin (majority watch-share wins, minority residual tolerated); (3) size tie-break; (4) empty/cold matrix → space-efficient default; (5) gate-off → byte-identical resolver output (parity test against current `eligible[-1]`); (6) `classify_profile_axes` on real blueprint profiles + live formatItems shapes.

**Dependencies:** matrix provisioning (`update_profile_custom_format_scores`) OR manual blueprint import. **Effort: M.**

---

### PHASE 2 — source-codec correctness + full `infer_likely_viewers` + new-title prediction

**NEW (pure):** `quality_analytics/device_codec_capabilities.py` — `per_user_source_codec_rates(history, metadata_index, user_list)`: re-key each play's transcode outcome by the title's **SOURCE** `video_codec` (history.rating_key ⋈ `metadata_index.video_codec`). Drops into `choose_codec_profile` **unchanged** — same tuple shape, no selector change.

**MODIFIED:** `tautulli/metadata/__init__.py:build_metadata_index (:42-43)` — already carries `video_codec`+`audio_codec`; **ADD** source `hdr/dovi` flag + `container` so the source *fingerprint* (not just codec) is buildable.

**ENHANCED:** `infer_likely_viewers` gains the dual regime:
- branch (a) OWNED-with-history: `build_per_title_watchers` — generalize `_build_jit_watchers (episode_files.py:3471)` from series-only/latest-ts to movies+series and play-**counts**, recency-decayed via the affinity half-life; reuse the `_norm_title` stable join. Cached at `tautulli/per_title_watchers`.
- Blend: confidence·(a) + (1−confidence)·(b), `confidence = min(1, Σplays/min_plays)`. TV uses `jit_watchers` for new-title prediction.

**MODIFIED (caching):** `tautulli/__init__.py` derived-stats block (~255-265) — write `tautulli/users/{safe}/platform_usage` + `tautulli/per_title_watchers` alongside existing `transcode_fingerprint`/`platforms`, same sanitized-key pattern.

**MODIFIED (upgrades, still no sidegrade):** `scoring/_shared.py:select_profile_id (:368)` — add codec dimension after the `_max_res <= target_resolution` ceiling filter, so upgrade targets land on the codec-aware matrix profile (still resolution-driven actuation; codec is *which variant at the earned tier*).

**Tests:** golden per-user source-codec-rate derivations; `infer_likely_viewers` owned/sparse/new/cold ladder; matrix swap parity (selector unchanged); `build_per_title_watchers` movie+series join.

**Dependencies:** Phase 1. **Effort: L** (the source-codec join, the dual-regime viewer model, and the cache plumbing each land independently).

---

### PHASE 3 — the sidegrade action + owned-title detector + re-grab + separate space justification

**NEW (pure):** `title_needs_sidegrade(current_file_fingerprint, matrix, platform_weights, *, transcode_thresh, candidate_profiles)` in `profile_selector.py` — build the OWNED file's **full** `source_fingerprint` from cached mediainfo (`movie_files.py:93-98` video/audio/hdr/height/subs; sonarr episode mediainfo). If `predict_transcode >= thresh`, search same-res codec variants for lower P; return the sidegrade target. **None on cold data → skip** (conservative-hold, not explore — a wrong re-grab costs a whole file + budget).

**NEW (planner):** `space/codec_repick_planner.py:plan_codec_repicks(...)` — the missing action path. Runs the detector per owned title; emits candidates re-grabbing at the chosen codec profile, sorted by transcoders-reduced-per-GB, filled until `sidegrade_budget_gb`. **Bypasses `downgrade_planner.step_targets` entirely** (its est<cur_gib veto at `:76` would reject a larger H.264 re-grab — that's correct for downgrades, wrong here). Own budget gate: `delta_gb = estimate_gb_for_profile(target, runtime, codec=alt) - cur_gib`; proceed only when `projected_free - delta_gb >= reserve_gb` (the JIT already computes exactly this at `episode_files.py:~5431`).

**MODIFIED (action derivation — 3 sites):**
- `radarr/cache/movie_files.py:119` + `sonarr/cache/episode_files.py:189` — extend `planned_action` free-text values+comment to include `'sidegrade'` (string columns, no Enum class — additive, no type change).
- `upgrade_planner.py:119` — BEFORE the `if target_rank <= cur_rank: already_best` guard: when `target_rank == cur_rank` AND `title_needs_sidegrade` fires, emit `action='sidegrade'` with the codec-variant target, bypassing the rank gate.
- `episode_files.py JIT (~5461)` — add `elif _cr == target_res and codec_sidegrade_wanted: action='SIDEGRADE'` before the `else`; respect the active-watch guard (`:~5468`); route through the **same** QP-flip+EpisodeSearch worker.

**MODIFIED (apply, reused verbatim):** Radarr sidegrade pass alongside `space_pressure.py:1200-1247`, applying via the existing PUT-qualityProfileId+`MoviesSearch` primitive (`:1210-1239`) — only `target_id` + ledger reason differ. Add `codec_reason:str` to `QualityPlan`.

**EXCLUSIONS (v1):** `radarr/quality/universe.py` keep_universe titles excluded (universe owns its ladder — mirrors how `downgrade_planner` SKIPs keep_universe); `downgrade_planner.step_targets` **untouched** (documented bypass).

**Config:** `scoring.codec_sidegrade: {enabled: false, transcode_thresh: 0.34, coverage_min, sidegrade_budget_gb, min_n}` + `codec_sidegrade_enabled(config)` gate.

**Tests:** sidegrade emission only when rank ties AND detector fires AND floor cleared; budget cap fill order; gate-off parity (planners never emit `sidegrade`); cold-data → skip; oscillation cooldown (ledger stamp); UNACQUIRABLE-codec fallback ladder.

**Dependencies:** Phases 1+2. **Effort: L** (touches load-bearing rank-delta logic in 3 planners; needs careful gating + parity tests to stay byte-identical when off).

---

## D. RISKS & GUARDRAILS

1. **Oscillation / hysteresis (Phase 3).** A codec re-pick re-downloads a whole file; a flapping likely-viewer set could re-grab the same title repeatedly. **Guard:** ledger stamp + cooldown (mirror the demand/saga no-oscillation work), a hard `sidegrade_budget_gb` cap per run, and a `sidegrade_floor` (≥1 expected-transcoder reduction). **UNACQUIRABLE ladder:** chosen codec → any direct-playing codec → keep current, with an UNACQUIRABLE ledger (mirror pilot-search) so we don't thrash search on an impossible target (e.g. only HEVC exists for that title).

2. **Storage accounting.** Sidegrades CONSUME space while downgrades RECLAIM it. **Guard:** sidegrades never enter the reclaim/spread machinery; their own budget gate. **Ordering under pressure:** suppress sidegrades entirely while `free < reserve` — only sidegrade when roomy. The `est < cur_gib` veto must never apply (a bigger H.264 re-grab that kills a transcode is the whole point).

3. **Full-fingerprint coverage (audio/HDR/DV).** First-class via the engine's 5-tuple — no new math. **Limit:** Radarr CFs can only *prefer* an audio track, not guarantee the released file's audio. **Guard:** bake audio as a tie-breaker CF, not a hard gate; HDR/DV ride `res_hdr`. Source HDR/container unavailable pre-grab → coarsen via the predictor's graded fallback (as `uhd_remote_play_ok` already does), defer true source-HDR targeting to Phase 2's metadata-index extension.

4. **Per-platform PII granularity cap.** `predict_transcode` share-weights a user's devices, but for a SHARED title "which device for THIS play" is unknowable pre-play. **Accept** the share-weighted expectation (don't optimize for worst-case device). The device-capability seed (`DEVICE_CODEC_MATRIX` Samsung-DV/DTS, Roku-AV1, Apple-no-AV1) shifts yearly — **extract to a checked-in JSON with a date stamp + staleness check**, not hand-maintained inline.

5. **Thin-data degradation.** `predict_transcode` returns `None` on sparse data. **Policy (locked direction):** at ADD time → `none_p` neutral prior (default 0.5, tunable toward `transcode_thresh` 0.34 to explore newer codecs); at SIDEGRADE time → **conservative-hold (skip)**, because the harmful direction differs (a wrong re-grab costs space + churn, unlike the 4K-gate's explore-by-grabbing bias). `infer_likely_viewers` self-degrades: owned-history → blend → affinity → uniform-roster → `{}` (never empty-crashes the selector; falls to the household `hh_codec` matrix cell).

---

## E. BOTTOM LINE

| Phase | Slice | Effort |
|---|---|---|
| **1 (MVP)** | Policy brain (`choose_codec_profile`+classifier+assembler) + `infer_likely_viewers` (affinity-only) + `per_user_platform_usage` + acquisition wiring + matrix provisioning, default-off | **M** |
| **2** | Source-codec correctness (`per_user_source_codec_rates` + metadata-index HDR/container) + full dual-regime `infer_likely_viewers` + per-title watchers + upgrade codec dim | **L** |
| **3** | `sidegrade` action across 3 planners + `title_needs_sidegrade` + `plan_codec_repicks` + separate space budget + apply wiring | **L** |
| **Total** | | **L** (M + L + L; XL only if Phase 3's owned-file re-code scope balloons) |

**Recommended first commit (Phase 1, pure-only, no wiring):** fill the empty `quality_analytics/profile_selector.py` stub with the three pure functions — `classify_profile_axes`, `candidate_fingerprint` (delegating to `source_fingerprint`), and `choose_codec_profile` (with `viewer_transcode_cost`) — plus their golden/unit tests on synthetic per-user matrices. This is:
- **byte-identical-safe** — no caller yet, zero behavioral change, nothing actuates;
- **the critical-path unblocker** — every later phase consumes it;
- **`profile_selector.py`'s first real content** (today a TODO migration shim), honoring its no-HTTP/no-cache/no-log contract;
- **the FIRST consumer** of the already-built-but-unconsumed `per_user_transcode_fingerprint_matrix` (`transcode_fingerprint.py:151`).

Wire it into `resolver._profile_for_score` behind `scoring.codec_profiles.enabled` only in the **second** commit, after the selector is golden-tested in isolation.

**Key files:** `scripts/managers/machine_learning/quality_analytics/profile_selector.py` (fill stub), `.../quality_analytics/likely_viewers.py` (new), `.../quality_analytics/device_codec_capabilities.py` (new, Phase 2), `.../space/codec_repick_planner.py` (new, Phase 3), `.../affinity/platform_usage.py` (extend), `scripts/managers/services/acquisition/resolver.py:286-319` (wire), `.../space/upgrade_planner.py:119` (Phase 3), `.../services/sonarr/cache/episode_files.py:~5461` (Phase 3), `.../services/radarr/quality/space_pressure.py:1210-1239` (apply), `.../sizing/size_model.py:319` (first `codec=` caller).
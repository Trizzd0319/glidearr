# Design — Per-Person, Codec-Aware Quality Profiles

> Status: **planned** (implement after ML migration Step 3c). Grounded by a 4-lens
> codebase investigation (workflow wf6rmp7jk). Brain (`machine_learning/`) DECIDES;
> the Radarr/Sonarr service APPLIES.

## Goal
For each household member, derive a device + codec profile (which devices they use,
which **source** codecs direct-play vs transcode for them) from Tautulli history, and
use it to acquire/keep the **highest-quality version that DIRECT-PLAYS for the likely
viewer(s)** of a title — avoiding transcoding while maximising quality.

---

## The constraint that shapes everything
**Radarr/Sonarr custom-format (CF) scores are PROFILE-scoped, not per-movie.** A
release's CF score comes from its quality profile's `formatItems`. You therefore
**cannot** encode per-title codec preference by mutating a shared profile — it would
thrash every movie on that profile.

**Implementable shape:** a small **managed matrix of codec-aware quality profiles**
(resolution tier × codec preference, e.g. `1080p·prefer-h264`, `1080p·HEVC-ok`,
`2160p·HEVC-ok`), CF scores baked in **once** (idempotent provisioning). The brain then
**assigns each title the profile** matching its likely viewers' device caps — reusing
the per-title profile assignment that already exists. The existing likelihood→profile
ladder simply gains a codec dimension. Keep the matrix small (~2 res tiers × 2-3 codec
prefs) to avoid profile clutter.

---

## Prerequisite finding (quick win, do first)
`tautulli/platforms` and `tautulli/transcode` are READ by the scorers
(`radarr/quality/space_pressure.py` ~387-388; `sonarr/cache/episode_files.py` ~858-859)
but appear to be **never written** in `TautulliManager.run()` (~line 207-212) — so
Group-D's D1/D2/D3 device/transcode scoring may be partly inert today. Verify; if
confirmed, add the two `cache.set()` calls. Benefits current scoring immediately and
de-risks this feature (the per-user signals extend the same path).

---

## Data model (the foundation)
Join **history ⋈ metadata** (both already cached):
- `tautulli/history/all` per-play: `user, platform, transcode_decision, stream_video_codec, rating_key`
  (projected in `watch_history/_CACHED_HISTORY_FIELDS`).
- `tautulli/metadata/index`: `rating_key → SOURCE video_codec` (built by
  `tautulli/metadata.build_metadata_index`).

→ per-user, per-device, per-**source**-codec direct-play map:
```
{ user: { primary_device: str,
          devices: [str, ...],
          codecs: { source_codec: {direct_play: int, transcode: int, fail_rate: float} } } }
```
Source codec (not the streamed/output codec the current household `transcode_stats`
uses) is the right axis — it's what you would *acquire*.

---

## Phased plan
Each phase is a disciplined slice (brain-pure function + service delegation + same
cache-key contract + parity/unit gate), exactly like migration Steps 3a/3b.

### Phase 0 — prereqs (small)
- Fix the `tautulli/platforms` / `tautulli/transcode` cache writes (above).
- Null-coalesce `metadata_index.video_codec` for deleted / non-media items.

### Phase 1 — per-user device/codec derivation (brain + service)
- Brain: `affinity/platform_usage.per_user_platform_usage(history, user_list)`;
  new `quality_analytics/device_codec_capabilities.per_user_source_codec_rates(history, metadata_index, user_list)`
  (the history⋈metadata join). Pure; mirrors `affinity/genre_affinity.per_user_affinity`.
- Service: `TautulliUsersManager` / `TautulliDevicesManager` delegate + the
  orchestration caches `tautulli/users/{user}/platform_usage` and
  `tautulli/users/{user}/device_codec_profiles` (same sanitised-key pattern as
  `tautulli/users/{user}/affinity`).
- Gate: golden harness on synthetic history (diverse user/device/codec).

### Phase 2 — likely-viewer model (brain, pure)
- `quality_analytics/likely_viewers.infer_likely_viewers(feature_row, per_user_affinity, rating_groups) -> [(user, propensity)]`.
  Start from per_user_affinity genre/cast/crew match + kids/adult segmentation. Answers
  *whose* device profile to optimise for.

### Phase 3 — codec-aware decision (brain, pure — fills the dormant stubs)
- Device capability model: extend `_shared._DEVICE_RESOLUTION_CEILING` (resolution-only
  today) into `{device: {codecs, max_res, hdr}}`, **seeded** (known device caps) and
  **refined** from Phase-1 evidence (high confidence where data exists, seed fallback).
- `quality_analytics/profile_selector.choose_codec_profile(resolution_tier, likely_viewers, per_user_codec_caps, available_profiles) -> profile_id`
  — among profiles at the title's earned resolution tier, pick the highest-quality codec
  variant that direct-plays for ≥X% of likely viewers (tunable). `transcode_analyzer.codec_penalty(...)`
  is its scoring helper.
- Optional: make scorer **Group-D D1/D2 per-likely-viewer aware** (backward-compatible
  new params; falls back to household when absent). Files: `scoring/movie_scorer.py`
  (D1 ~356-378, D2 ~380-396), `scoring/show_scorer.py`, `scoring/_shared.py`
  (`_DEVICE_RESOLUTION_CEILING`, `_TRANSCODE_FRIENDLY_CODECS`).

### Phase 4 — APPLY (service)
- **New API method** (the one genuinely missing piece):
  `RadarrCustomFormatsManager.update_profile_custom_format_scores(instance, profile_id, {format_id: score})`
  — PUT `qualityProfile/{id}` with mutated `formatItems`; Sonarr twin. Used to
  **provision/maintain the codec-aware profile matrix** idempotently, NOT per-title.
  (`base_instance_manager._make_request` already supports the PUT.)
- Wire `choose_codec_profile`'s output into `RadarrQualityUniverseManager.apply_quality_actions()`
  and the JIT / space-pressure profile assignment — they already GET → set
  `qualityProfileId` → PUT; just feed the codec-aware target id.
- Existing per-title assignment hooks: `radarr/quality/selector.request_quality_change`
  (~62-82), `radarr/movies/quality.update_movie_profile` (~37-54), Sonarr equivalents.

### Phase 5 — contracts + config
- `contracts/context.AffinityContext`: add `per_user_platform_usage`,
  `per_user_device_codec_capabilities`.
- `contracts/plans.QualityPlan`: add `codec_reason: str` (explainability; ties into the
  watchability-breakdown work).
- `onboarding/schema.py`: `scoring.codec_profiles` (matrix mapping + ≥X% direct-play
  threshold) and optional `users.{name}.device_codec_hints` so operators can assert
  caps without complete history.

---

## Risks / open decisions
- **Multi-viewer titles** (kids + adults, different devices): policy — optimise for the
  highest-propensity viewer, blend constraints to a common-denominator codec, or accept
  one transcode for the minority. Recommend highest-propensity + a configurable floor;
  document it.
- **Capability inference confidence:** "never transcoded HEVC here" ≠ "device can't do
  HEVC" (maybe never encountered). Seed table + evidence; mark low-confidence; lean
  conservative.
- **Beyond codec:** HDR tone-mapping, Atmos/TrueHD, container/bit-depth also force
  transcodes. Phase-1's source-codec map is the start; these are follow-ons.
- **CF score profile-scope** (see top): the reason we use a managed profile matrix, not
  per-title CF mutation.

## Test strategy
Unit-test each pure brain function on synthetic history (direct-play rates, viewer
inference, profile choice). Golden-harness the per-user derivations (byte-identical on a
captured history). For Phase 4, **dry-run** the matrix provisioning and assert the
intended `formatItems` PUT payload **without** sending it.

## Dependency
Depends on **Step 3c** (the feature-row adapter): the per-user device/codec context
rides through the same `AffinityContext`/feature-row plumbing 3c establishes, so the
scorer-callers don't grow yet another ad-hoc cache read.

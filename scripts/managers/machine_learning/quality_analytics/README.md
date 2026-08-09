# quality_analytics

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **quality_analytics**

**Package** — `scripts.managers.machine_learning.quality_analytics`
**Run position** — Transcode-usage derivations during the Tautulli phase; profile selection wherever a codec variant is chosen.
**One-liner** — Per-viewer, codec-aware transcode reduction: who watches a title, what their devices direct-play, and which codec variant of the earned quality tier minimises transcoding across them.

---

## Purpose

Plex transcoding is the tax a wrong codec choice levies on every playback. This
package is the decision brain for avoiding it:

> given the resolution tier a title earned, the users likely to watch it, and the
> per-user device→transcode capability matrix, pick the codec **variant** of the
> quality profile that minimises Plex transcoding across those viewers.

Resolution is decided elsewhere ([`likelihood/`](../likelihood/README.md)). This
package chooses *which encode of that resolution*.

`__init__.py` labels the package *"existing-ML, partly stubs. Lowest priority
(MIGRATION.md Step 9)"* — which understates it. `profile_selector.py` is a
complete, tested, pure implementation. See [`DESIGN.md`](./DESIGN.md) §3.2.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`profile_selector.py`](./profile_selector.py) | `choose_codec_profile` — the argmin. Also `classify_profile_axes`, `candidate_fingerprint`, `viewer_transcode_cost` | ✅ Implemented |
| [`transcode_fingerprint.py`](./transcode_fingerprint.py) | `predict_transcode`, `source_fingerprint` — the per-user capability matrix (17.7 KB, the largest module) | ✅ Implemented |
| [`likely_viewers.py`](./likely_viewers.py) | `infer_likely_viewers`, `platform_weights_for_viewers` | ✅ Implemented |
| [`transcode.py`](./transcode.py) | `transcode_stats`, `device_codec_matrix`, `codec_direct_play_rate` — usage derivations | ✅ Implemented |
| [`transcode_causes.py`](./transcode_causes.py) | Cause attribution | ✅ Implemented |
| [`codec_report.py`](./codec_report.py) | Codec routing report | ✅ Implemented |
| [`legacy_codec.py`](./legacy_codec.py) | Legacy-codec regrab support | ✅ Implemented |
| [`transcode_analyzer.py`](./transcode_analyzer.py) | `transcode_penalty(profile, device_caps)` — **declared, not implemented** | 🔵 Planned |

## Test coverage

Six test modules — [`test_transcode_fingerprint.py`](./test_transcode_fingerprint.py)
(12.3 KB), [`test_profile_selector.py`](./test_profile_selector.py),
[`test_codec_report.py`](./test_codec_report.py),
[`test_legacy_codec.py`](./test_legacy_codec.py),
[`test_transcode_causes.py`](./test_transcode_causes.py),
[`test_transcode.py`](./test_transcode.py),
[`test_likely_viewers.py`](./test_likely_viewers.py).

---

## The decision chain

```
Tautulli history
   │
   ├─► transcode.device_codec_matrix     {device: {v/a: {direct, transcode}}}
   │   transcode.transcode_stats         {"<v>/<a>": count}
   │
   ├─► affinity.per_user_platform_usage  {user: {platform: count}}
   │        │
   │        ▼
   │   likely_viewers.platform_weights_for_viewers   {user: {platform: share}}
   │
   ├─► likely_viewers.infer_likely_viewers           {user: watch_share}
   │        1. actual per-title watchers   ← ground truth
   │        2. per-user affinity propensity ← prediction, threshold 0.15
   │        3. neither → {}                 ← caller falls back to household
   │
   └─► transcode_fingerprint.predict_transcode       per-user P(transcode)
            │
            ▼
   profile_selector.choose_codec_profile
        argmin over candidates AT the earned tier of
            Σ watch_share × P(transcode)
        tie-broken by space efficiency: AV1 < HEVC < H264
        → (profile_id | None, reason)
```

---

## The objective, stated precisely

> For a **single-viewer** title this is "the codec that viewer direct-plays"; for
> a **shared** title it is coverage-max / accept-minority (the low-share viewers'
> transcode is a tolerated residual) — **both fall straight out of the argmin.**

One formula, two intuitive behaviours, no special-casing.

---

## Three explicit unknown-vs-zero distinctions

This package is unusually disciplined about absence:

| Function | Returns | Because |
|---|---|---|
| `codec_direct_play_rate` | `None` when never streamed | *"so a caller can distinguish 'always direct-played' from 'never tried'"* |
| `device_codec_matrix` | explicit `direct` **and** `transcode` counts | *"disambiguates the event-only `transcode_stats` (which can't tell '0 transcodes = safe' from '= never tried')"* |
| `viewer_transcode_cost` | `none_p = 0.5` neutral prior | *"an untrusted (None) read uses the neutral prior so a cold viewer neither vetoes nor forces a codec"* |

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Inputs:** [`affinity/platform_usage.py`](../affinity/README.md) · [`sizing/size_model.profile_max_quality`](../sizing/README.md)
- **Related:** [`DESIGN_per_person_codec_profiles.md`](../DESIGN_per_person_codec_profiles.md) · [`DESIGN_codec_routing_build_plan.md`](../DESIGN_codec_routing_build_plan.md)
- **Knowledge:** [`support/knowledge/reducing-plex-transcoding.md`](../../../support/knowledge/reducing-plex-transcoding.md)

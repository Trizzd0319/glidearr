# quality_analytics — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **quality_analytics**

**Package** — `scripts.managers.machine_learning.quality_analytics`
**Status** — ✅ Implemented (7 of 8 modules) · ❓ Wiring unconfirmed · 🟡 Package label understates it
**Related** — [README.md](./README.md) · [`DESIGN_per_person_codec_profiles.md`](../DESIGN_per_person_codec_profiles.md) · [`services/tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md)

---

## 1. Problem statement

A file that transcodes costs CPU on every playback, degrades quality, and can
fail outright on a weak client. Glidearr already decides *resolution* by
watchability. The remaining question is **which encode of that resolution** —
H.264, HEVC, or AV1.

The naive answers are both wrong:

- **"Always H.264"** — maximum compatibility, but the largest files, which
  wastes the disk the whole space subsystem exists to manage.
- **"Always AV1/HEVC"** — smallest files, but transcodes on any client that
  cannot decode them.

The right answer is **per-title and per-viewer**: it depends on who actually
watches *that* title and what their devices direct-play. A film only one person
watches on an Apple TV should be optimised for that Apple TV. A film the whole
household watches should be optimised for coverage, accepting that the one
person on an old Roku will transcode.

Three sub-problems follow:

1. **Who watches this?** Ground truth exists for owned titles with history;
   nothing exists at acquisition time.
2. **What can their devices play?** Only observable from what has actually
   direct-played versus transcoded — and *"0 transcodes"* is ambiguous between
   "safe" and "never tried."
3. **Which candidate profile?** The codec a profile targets is not a field — it
   is encoded in the name suffix and contradicted or confirmed by custom-format
   scores.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One objective covering both single-viewer and shared titles. |
| G2 | Never let an unknown read veto or force a codec. |
| G3 | Distinguish "never tried" from "always fine" everywhere. |
| G4 | Degrade from ground truth to prediction to household default. |
| G5 | Falling back must be byte-identical to the resolution-only pick. |
| G6 | Pure — no HTTP, no cache, no logging, no service imports. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Choosing resolution | `likelihood/` earns the tier; this picks the variant. |
| N2 | Fetching candidate profiles | *"the service FETCHes it."* |
| N3 | Re-scoring affinity | *"This module does NOT re-score"* — propensity is passed in. |
| N4 | Applying the change | Emits a profile id + reason; the adapter PUTs it. |

---

## 3. Architecture

### 3.1 The objective

```
choose_codec_profile
    = argmin over candidates AT the earned resolution tier of
          Σ_viewers  watch_share × P(transcode | viewer, profile_fingerprint)
      tie-broken by space efficiency (AV1 < HEVC < H264)
```

The elegance is in what the argmin gives for free:

> For a **single-viewer** title this is "the codec that viewer direct-plays"; for
> a **shared** title it is coverage-max / accept-minority … **both fall straight
> out of the argmin.**

No branch on viewer count. The watch-share weighting *is* the policy.

### 3.2 🟡 The package label understates what is here

`__init__.py`: *"Transcode-penalty + profile-scoring analytics (existing-ML,
partly stubs). **Lowest priority (MIGRATION.md Step 9).**"*

That was written when the package was stubs. It now contains:

| Module | Size | Tests |
|---|---|---|
| `transcode_fingerprint.py` | 17.7 KB | 12.3 KB |
| `profile_selector.py` | 10.3 KB | 6.0 KB |
| `transcode_causes.py` | 6.8 KB | 4.2 KB |
| `codec_report.py` | 7.1 KB | 4.9 KB |
| `legacy_codec.py` | 5.4 KB | 3.8 KB |
| `likely_viewers.py` | 3.7 KB | 1.6 KB |
| `transcode.py` | 4.0 KB | 2.6 KB |

Only `transcode_analyzer.py` remains a stub — and it is the *one* module the
`ARCHITECTURE.md` map names for this package. A reader trusting either the
`__init__` docstring or the migration map would conclude the package is
unbuilt. It is the most heavily-tested package in the brain.

### 3.3 ❓ Is any of it wired?

This is the question the docs cannot answer, and it matters most.

`profile_selector.choose_codec_profile` is pure, complete and tested. Nothing in
this package can tell me whether a service calls it. The candidate list is
FETCHed by the service; the result is applied by the service; neither side is
visible from here.

Two facts make the question sharper rather than idle:

- [`sonarr/DESIGN.md`](../../services/sonarr/DESIGN.md) §3.3 found
  `SonarrQualityManager` **filtered out of `component_dependencies` and never
  loaded** — the exact shape of a built-but-disconnected subsystem.
- [`tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md) recorded
  `tautulli/device_codec_matrix` as *"the keystone signal for per-device profile
  selection"* with **no consumer**.

**A refinement on that second point.** The derivation `device_codec_matrix()`
does exist here, and `codec_direct_play_rate()` reads a matrix. But
`choose_codec_profile` consumes a **per-user fingerprint matrix**
(`transcode_fingerprint.py`), *not* the per-device codec matrix. So the cached
`tautulli/device_codec_matrix` key and the per-user matrix the selector actually
uses are **different artifacts**.

`GLD-TAUT-01` therefore stands, but reframed: the question is not "build a
consumer for the device matrix" — it is **"which of the two matrices is the live
one, and is the other redundant?"** `GLD-QAN-01` and `GLD-QAN-02`.

### 3.4 The degradation ladder in `infer_likely_viewers`

```
1. per_title_watchers positive  → ACTUAL watch shares      (ground truth)
2. else per_user_propensity     → predicted shares
        drop users below threshold=0.15, renormalise
        BUT: if EVERY user is below threshold → keep them all
3. neither positive             → {}   (caller falls back to household matrix)
```

Step 2's guard is the thoughtful part. Dropping the long tail focuses the codec
choice on *"the handful who'll actually watch it"* — but on a flat or cold
affinity field every user falls below 0.15, and naive filtering would return
`{}` and silently discard the title from per-viewer optimisation entirely.
*"(don't drop everyone)"*.

Note the separation-of-concerns discipline: this module *"does NOT re-score"* —
affinity scoring already lives in the feature pipeline, and the service passes
the computed propensity in *"so it is trivially testable and never drifts from
the scorer."* That is the same anti-drift instinct as
[`foundation/`](../foundation/DESIGN.md)'s delegation rule.

### 3.5 Three unknown-vs-zero distinctions, all correct

This package is the most disciplined I have read on §8 **P-C**:

| Mechanism | Handling |
|---|---|
| `codec_direct_play_rate` | Returns `None` on no sample — *"so a caller can distinguish 'always direct-played' from 'never tried'"* |
| `device_codec_matrix` | Tracks `direct` **and** `transcode` explicitly, because event-only `transcode_stats` *"can't tell '0 transcodes = safe' from '= never tried'"* |
| `viewer_transcode_cost` | `none_p = 0.5` — *"a cold viewer neither vetoes nor forces a codec"* |
| `platform_weights_for_viewers` | A viewer with no device usage → `{}` → *"the predictor then has no device read for them → the neutral `none_p` prior"* |

The second is a **P-C instance caught and fixed by design**: `device_codec_matrix`
exists *specifically because* the simpler tally conflated absent with zero. That
is the third worked example, after `watched_definition` and `build_library_index`.

### 3.6 Byte-identical fallback — the fifth instance

> Returns `(None, reason)` when no candidate sits at the tier (**the caller keeps
> its resolution-only pick — byte-identical**).

Plus `min_coverage` defaulting to `0.0` = pure argmin, and `none_p = 0.5` as a
non-committal prior. The house pattern again (see
[`affinity/DESIGN.md`](../affinity/DESIGN.md) §3.2): a new capability that
provably changes nothing until it has something to say.

### 3.7 Reading a profile's codec

There is no codec field. `classify_profile_axes` reconstructs it:

1. Read the **name suffix** — `(H264)` / `(HEVC)` / `(AV1)` / `x264` / `x265`.
2. Reconcile against **custom-format scores**: a codec scored at or below
   `_BAN_THRESHOLD = -1000.0` is excluded, *"the user's live-action profiles ban
   x265/AV1 at -10000"*.
3. If the name is silent, the highest-scored non-banned codec wins.

`res_tier` comes from `size_model.profile_max_quality` — so the resolution axis
is shared with [`sizing/`](../sizing/README.md) rather than re-derived.

The tie-break order `_CODEC_SIZE_RANK = {av1: 0, hevc: 1, h264: 2, unknown: 3}`
means that among profiles with equal predicted transcode cost, the smallest
encode wins — the space subsystem's interest expressed inside the quality
decision.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Watch-share-weighted argmin | G1 — single-viewer and shared behaviour both emerge without branching | Separate policies |
| D2 | Tie-break by space efficiency | Equal transcode cost ⇒ take the smaller file | Prefer compatibility |
| D3 | `none_p = 0.5` neutral prior | G2 — a cold viewer must not decide | Treat unknown as transcode, or as direct |
| D4 | `min_n = 3` trust threshold | Below it a read is noise | Trust any sample |
| D5 | `(None, reason)` when no candidate at tier | G5 — byte-identical fallback | Pick the nearest tier |
| D6 | `min_coverage` defaults 0 | Pure argmin unless explicitly asked otherwise | Coverage-first |
| D7 | Ground truth beats propensity | G4 — actual plays outrank predicted taste | Blend them |
| D8 | Keep everyone when all below threshold | Prevents a cold field discarding the title entirely | Return `{}` |
| D9 | Propensity passed in, never re-scored | *"never drifts from the scorer"* | Re-score locally |
| D10 | Codec from name **and** CF scores | Neither alone is reliable; a −10000 CF is a hard ban | Name only |
| D11 | `res_tier` from `size_model` | Shares the resolution axis rather than re-deriving | Local parsing |
| D12 | `device_codec_matrix` tracks both counts | G3 — the event-only tally cannot express "never tried" | Transcode counts only |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Pure — no HTTP, no cache writes, no logging, no service imports. |
| I2 | Watch shares sum to 1 over the returned viewers. |
| I3 | An unknown capability read contributes the neutral prior, never a veto. |
| I4 | `codec_direct_play_rate` returns `None`, never 0, on no sample. |
| I5 | Candidates are considered only **at** the earned resolution tier. |
| I6 | No candidate at tier ⇒ the caller's resolution-only pick is unchanged. |
| I7 | A codec banned by custom format is never selected. |
| I8 | Affinity is consumed, never recomputed. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| No watch history for a title | Ladder step 2 | Falls to propensity | Correct | ✅ By design |
| Flat/cold affinity field | Threshold guard | Keeps all viewers | Correct | ✅ |
| No signal at all | Ladder step 3 | `{}` → household matrix | Correct | ✅ |
| Viewer has no device usage | `{}` weights | Neutral prior | Correct | ✅ |
| Fewer than `min_n` samples | Trust threshold | Neutral prior | Correct | ✅ |
| No candidate at the tier | Guard | `(None, reason)` — byte-identical | None | ✅ Reason dict |
| Profile name silent on codec | CF-score fallback | Highest non-banned wins | 🟡 Inference | ❌ **None** |
| **Selector never invoked** | **None** | Codec optimisation simply does not happen | 🔴 §3.3 | ❌ **None** |
| Two matrices, unclear which is live | **None** | Possible duplicated derivation | 🟡 §3.3 | ❌ **None** |
| `transcode_analyzer` stub called | `ImportError` on the symbol | Loud | Immediate | ✅ |

**Row 8 is the one that matters.** Every other failure degrades gracefully into a
documented fallback. If nothing calls `choose_codec_profile`, all of that
careful machinery is inert — and it would look exactly like this from inside the
package.

---

## 7. Configuration surface

Constants rather than config keys:

| Constant | Value | Effect |
|---|---|---|
| `none_p` | 0.5 | Neutral prior for an untrusted read |
| `min_n` | 3 | Samples before a capability read is trusted |
| `min_coverage` | 0.0 | Pure argmin; >0 prefers direct-play coverage first |
| `threshold` | 0.15 | Viewer watch-share floor before dropping |
| `_BAN_THRESHOLD` | −1000.0 | CF score at/below which a codec is banned |
| `_CODEC_SIZE_RANK` | av1 0 · hevc 1 · h264 2 · unknown 3 | Tie-break order |

Cache key produced by the Tautulli service from `device_codec_matrix`:
`tautulli/device_codec_matrix`.

---

## 8. Implemented capabilities

- ✅ Watch-share-weighted codec argmin with space-efficiency tie-break
- ✅ Per-user transcode fingerprint matrix and prediction
- ✅ Likely-viewer inference with a three-step degradation ladder
- ✅ Per-viewer platform weights from device usage
- ✅ Per-device codec matrix separating direct from transcode
- ✅ Direct-play rate with an explicit no-sample signal
- ✅ Transcode-usage tally and cause attribution
- ✅ Codec routing report and legacy-codec regrab support
- ✅ Profile-axis classification from name + custom-format scores, ban-aware
- ✅ Byte-identical fallback when no candidate sits at the tier
- ✅ Six test modules, the heaviest coverage in the brain

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-QAN-01` | ❓ **Confirm whether `choose_codec_profile` is invoked by any service** | §3.3 — a complete, tested, pure selector with unknown wiring. Same shape as `GLD-SON-01` | S | `GLD-SON-01` |
| `GLD-QAN-02` | ❓ **Determine which matrix is live** — the cached per-device `tautulli/device_codec_matrix`, or the per-user fingerprint matrix the selector consumes. **Reframes `GLD-TAUT-01`** | Two derivations may cover one need; one may be genuinely orphaned | S | `GLD-TAUT-01` |
| `GLD-QAN-03` | 🟡 **Correct the package label** — `__init__.py` says *"partly stubs, lowest priority"* and `ARCHITECTURE.md` names only the one module that *is* a stub. This is the most-tested package in the brain *(P-G)* | A reader trusting either doc concludes it is unbuilt | S | `GLD-ROU-06` |
| `GLD-QAN-04` | **Implement or drop `transcode_analyzer.transcode_penalty`** | The one remaining stub, and the only module `ARCHITECTURE.md` lists here | M | `GLD-QAN-01` |
| `GLD-QAN-05` | **Report codec-selection outcomes** per run — titles considered, variant chosen, predicted transcode cost avoided | The objective is measurable and currently unmeasured | S | `GLD-QAN-01` |
| `GLD-QAN-06` | **Surface the `reason` dict** the selector already returns | Explainability is computed and discarded, same as `scoring`'s breakdown | S | `GLD-SCO-06` |
| `GLD-QAN-07` | **Warn when codec is inferred from CF scores** rather than read from the name | §6 row 7 — an inference silently drives a real profile change | S | — |
| `GLD-QAN-08` | **Validate `min_coverage`** as an alternative to pure argmin against real viewer sets | Built, defaulted off, never evaluated | M | `GLD-QAN-05` |
| `GLD-QAN-09` | **Feed `codec_direct_play_rate` into the report** so per-device capability is inspectable | The data exists; nothing renders it | S | `GLD-WEB-04` |
| `GLD-QAN-10` | **Cross-check `_BAN_THRESHOLD` against live profiles** — it assumes the −10000 convention | A profile banning at −900 would not register as a ban | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does any service call `choose_codec_profile`? *(= D34)* | `GLD-QAN-01` |
| Q2 | Are the per-device and per-user matrices both needed, or is one redundant? | `GLD-QAN-02` |
| Q3 | Should `min_coverage` be non-zero by default — is accept-minority right for a household with one weak client? | `GLD-QAN-08` |
| Q4 | Is `threshold = 0.15` right? It determines how many viewers a shared title is optimised for. | `GLD-QAN-05` |

**Q3 is the interesting policy question.** Pure argmin accepts that a minority
viewer transcodes. That is correct when transcoding is merely inefficient — and
wrong when the minority viewer's device *cannot* play the codec at all, turning a
tolerated residual into a failed playback. `min_coverage` exists precisely for
that case and has never been exercised.

## 11. Related designs

- [`DESIGN_per_person_codec_profiles.md`](../DESIGN_per_person_codec_profiles.md) · [`DESIGN_codec_routing_build_plan.md`](../DESIGN_codec_routing_build_plan.md)
- [`services/tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md) — `device_codec_matrix` and `GLD-TAUT-01`
- [`affinity/DESIGN.md`](../affinity/DESIGN.md) — `per_user_platform_usage`, and the transcode-usage split
- [`sizing/DESIGN.md`](../sizing/DESIGN.md) — `profile_max_quality` supplies `res_tier`
- [`support/knowledge/reducing-plex-transcoding.md`](../../../support/knowledge/reducing-plex-transcoding.md)

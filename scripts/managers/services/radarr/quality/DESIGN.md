# radarr/quality — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [radarr](../README.md) › **quality**

**Manager** — `RadarrQualityManager`
**Status** — ✅ Live · 🟢 The heaviest-tested folder in the repo
**Existing docs** — [`README.md`](./README.md) + a `.md` per module

> **Coverage:** this pass read `space_pressure.py`'s **first 125 lines** (docstring,
> imports, class constants) out of **151 KB**, plus the directory inventory.
> `universe.py` (59.5 KB), `universe_membership.py` (16.5 KB), `selector.py`,
> `file_size.py`, `custom_formats.py` and `adjuments.py` are **unread**.

---

## 1. Scale

| File | Size |
|---|---|
| [`space_pressure.py`](./space_pressure.py) | **151.1 KB** — the largest file in the repo |
| [`universe.py`](./universe.py) | 59.5 KB |
| [`universe_membership.py`](./universe_membership.py) | 16.5 KB |
| `selector.py` · `custom_formats.py` · `file_size.py` · `adjuments.py` | 7.9 / 7.0 / 6.7 / 3.8 KB |

**15 test files, ~148 KB** against ~282 KB of source. In absolute terms the best
test coverage in the repo, and the names follow the behaviour-naming discipline
[`sonarr/series/`](../../sonarr/series/DESIGN.md) §5 sets:
`test_exhaustive_downgrade`, `test_universe_floor_gate`,
`test_universe_realize_downgrade`, `test_universe_stamp_collision`,
`test_intent_memo_invalidation`, `test_watchlist_shield_pool`,
`test_downgrade_protect_threshold`.

⚠️ Note `test_size_anomaly_remediate.py` and `test_size_anomaly_report.py` live
**here**. My session-4 note placed that pair in Sonarr's `cache/` *and*
`quality/`, and session 36 retracted it because Sonarr's `quality/` has no tests.
The pair is real — it is just **Radarr's**. The retraction stands; the original
observation was mis-attributed. `GLD-SON-10` closes as a mis-scoped note rather
than a defect.

---

## 2. The two-stage pipeline

Docstring, condensed:

```
STAGE 1 — DOWNGRADE TO HD-720P
    Set the profile to HD-720p, then MovieSearch so Radarr's cutoff-unmet
    logic fetches the smaller file and replaces the existing one.

    Candidates:  score < WATCHABILITY_PROTECT_THRESHOLD · unwatched ·
                 collection-recent · universe-tagged (quality-change only)
    Excluded:    keep_forever / keep_movie · already ≤720p ·
                 watched within 7 days · score ≥ protect threshold

STAGE 2 — DELETE (LAST RESORT)
    Only if still below threshold. Only watched + grace-expired + already-720p.
    Lowest score first.
    NEVER: universe, franchise entries, keep-forever, keep-movie,
           anything watched within 30 days.
```

And the exhaustive policy, stated as a principle:

> **"Deletion is the TRUE last resort"**: downgrade EVERYTHING that can still be
> downgraded before ANYTHING is deleted. 720p is the absolute floor.

This is the Radarr original that
[`sonarr/series/space_pressure.py`](../../sonarr/series/VERIFICATION_space_pressure.md)
calls itself the twin of — and it confirms
[`space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) §3.4's account of
`space_exhaustive_downgrade` from the consuming side, including the
byte-for-byte reversibility guarantee.

---

## 3. 🔴 A fourth pressure constant — and a different **name**

```python
PRESSURE_THRESHOLD_GB = 25.0   # last-resort floor only
```

| Site | Identifier | Value |
|---|---|---|
| `machine_learning/space/space_targets.py` | `PRESSURE_FALLBACK_GB` | 25.0 |
| `sonarr/series/space_pressure.py` | `PRESSURE_FALLBACK_GB` | 25.0 |
| **`radarr/quality/space_pressure.py`** | **`PRESSURE_THRESHOLD_GB`** | **25.0** |
| `services/coordinator/space_coordinator.py` | `PRESSURE_FALLBACK_GB` | **1000.0** |

**Three of four agree at 25.0.** The coordinator is the lone value outlier
(`GLD-COORD-01`), and Radarr is the lone *naming* outlier — same concept, same
value, same inline comment (*"last-resort floor only"*), different identifier.

So a grep for `PRESSURE_FALLBACK_GB` finds three of the four sites and misses
Radarr's entirely. `GLD-RQ-01`.

---

## 4. 🟡 A shared vocabulary that is copied, not imported

```python
# Plex parental-controls age tiers that count as a KID for movie_scorer's E1/E2
# cohort terms. Matches the vocabulary PlexUsersManager resolves onto
# ``restriction_profile`` … so the scorer and the playlist age-gate
# CANNOT DISAGREE about who is a child.
_KID_AGE_TIERS = frozenset({"little_kid", "older_kid", "teen", "kid", "child"})
```

The intent is explicit — *"cannot disagree"* — and the mechanism is a **hardcoded
copy** of Plex's vocabulary. Nothing enforces the agreement it asserts.

This is the same shape as
[`next_watch`](../../../machine_learning/next_watch/DESIGN.md) §3.5's
`INTENT_SOURCE_STRENGTH`, with one difference that matters: **there, the brain
could not import from a service, so copying was forced.** Here the import is
available — `GLD-SP-03` records two live `services → services` imports, including
Sonarr importing from Plex.

So this copy is avoidable in a way `next_watch`'s is not. `GLD-RQ-02`.

The consequence if they drift: a Plex age tier added or renamed changes who the
playlist age-gate treats as a child, and does **not** change who `movie_scorer`'s
E1/E2 cohort terms treat as one — which is precisely the disagreement the comment
says cannot happen.

---

## 5. 🟡 `WATCHABILITY_PROTECT_THRESHOLD = 6`

```python
WATCHABILITY_PROTECT_THRESHOLD = 6   # score >= this → protect from downgrade
```

Against the AXIS V2 distribution
([`thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.4:
owned-movie **median 8**, p95 24, p99 34, max 58), a protect-floor of **6 sits
below the median** — so in principle it shields more than half the owned library
from downgrade.

That is reconciled by exhaustive mode:
[`space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) §3.4 records that
`space_exhaustive_downgrade` (**default on**) *"drops the watchability-score
ceiling as an eligibility filter."* So this constant governs only the
non-exhaustive legacy path.

Two things follow, both worth checking:

- It is a **live watchability threshold that is not in `THRESHOLD_SPECS`** — at
  least, not under a name I have seen. The module imports `get_threshold`, so it
  may route; the class constant alone does not show it.
- Its value was set against an axis that has since translated ~13 points
  (Group D v2). The delete family was re-anchored 20 → 17; a protect floor of 6
  on the same axis was not obviously part of that sweep.

`GLD-RQ-03`.

---

## 6. Confirmations and cross-references

| Finding | This file's evidence |
|---|---|
| `GLD-BKP-07` — do destructive paths read the backup gate? | ✅ **Second confirmation** — imports `effective_dry_run`. After `sonarr/cache/episode_files.py`, both services' primary destructive paths are covered |
| `GLD-COORD-02` — shim callers | **Sixth caller**, and it uses **two** shims in one file: `support.utilities.watch_likelihood` (`affinity_boost`) and `support.utilities.space_targets` |
| `GLD-ACQ-03` — `playlists/` as a shared-primitive home | **Third consumer** — imports `PLACEHOLDER_AFFINITY` from `playlists/models`, after `acquisition/demand` and `quality_analytics/likely_viewers` each take `genre_match`. Three consumers now import from a presentation package |
| `GLD-SP-01` — canonical `dry_run` | Starts the same walk (`kwargs` → `parent` → …); the file was read past the point where the raise would appear, so **unverified here** |

---

---

## 6.5 `selector.py` — session 55

### 6.5.1 ✅ D34's Radarr half: **it does not call `choose_codec_profile`**

Read in full (8 KB). The module does profile fetching, default assignment,
resolution validation, and one selection method:

```python
def get_best_profile_for_instance(self, instance: str) -> int:
    cf_scores = self.global_cache.get(f"radarr.quality.{resolved}", default={}) or {}
    for profile in profiles:
        if not self._is_valid_profile(name, resolved): continue
        score = cf_scores.get(name, 0)
        if score > best_score: best_score, best_id = score, pid
```

Selection is **resolution-tier + custom-format score**. There is no import of
`machine_learning.quality_analytics`, no `predict_transcode`, no per-viewer term.

Combined with [`sonarr/series/quality.py`](../../sonarr/series/VERIFICATION_quality.md)
§5.2 — which explicitly **excludes** codec variants from upgrade targets because
*"the device→codec stage assigns the codec"* — **both services' quality selectors
defer to a codec stage, and neither invokes it.**

**But the next probe is identified.** This folder contains
[`test_codec_routing_report.py`](./test_codec_routing_report.py) (7.6 KB), and
`space_pressure.py` is the only module here large enough to hold its subject. So
the codec-routing consumer is most likely inside the 151 KB module, not in
`selector.py`. `GLD-RQ-07` re-aims there. D34 stays open, one read narrower.

### 6.5.2 🔴 A third cache-key format — and it may collapse the selection

```python
cf_scores = self.global_cache.get(f"radarr.quality.{resolved}", default={}) or {}
# cf_scores expected shape: {profile_name: score}
```

**Dot-separated.** The repo now has four key styles:

| Style | Example | Where |
|---|---|---|
| Slash *(the registry)* | `radarr/<instance>/quality/profiles` | `CacheKeyPaths` |
| Slash, hand-built | `sonarr/{inst}/viewer_positions` | `episode_files.py` |
| **Double-colon** | `sonarr::{inst}::series` | `repair/anomaly.py` (`GLD-REP-08`) |
| **Dot** | `radarr.quality.{inst}` | **here** |

`CacheKeyPaths.radarr.QUALITY_PROFILES` is `radarr/<instance>/quality/profiles`.
Nothing in the registry produces `radarr.quality.<instance>`.

If nothing writes that key, `cf_scores` is `{}` and every profile scores `0`.
The comparison is strict (`score > best_score`), so the **first** valid profile
wins and every later tie is rejected — turning a *best-by-custom-format* selection
into **first-valid-in-API-order**.

Same shape as `GLD-REP-08`, but live: `selector.py` is a loaded component of a
running manager. The write-side check is the one thing needed to confirm it.
`GLD-RQ-10`.

### 6.5.3 🟡 Weak `dry_run` in a manager that PUTs

```python
self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
```

Two levels, defaulting to **`False`** — and `request_quality_change` issues
`PUT movie/{id}`. Fourth manager found with the weak form:

| Form | Managers |
|---|---|
| Canonical (4 levels, **raises**) | `sonarr/series/space_pressure` |
| Strong (3 levels, never defaults) | `coordinator` |
| **Weak (2 levels, defaults `False`)** | `writeback` · `calendar` · **`radarr/quality/selector`** |

The `dry_run` branch itself is correct — it logs and returns `True` before the
PUT. The weakness is only in *resolving* the flag. `GLD-RQ-11`.

### 6.5.4 🟡 `_cached_profiles` is never invalidated

```python
self._cached_profiles: dict = {}
...
if resolved in self._cached_profiles: return self._cached_profiles[resolved]
```

Populated on first fetch and never cleared for the manager's lifetime. Radarr's
config sync **does** run (unlike Sonarr's — `GLD-SYNC-02`), so a profile set
updated mid-run would be served stale from here. `GLD-RQ-12`.

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-RQ-10` | 🔴 **`selector.py` reads `radarr.quality.{instance}` — dot-separated**, a **fourth** key format the registry never produces (`CacheKeyPaths.radarr.QUALITY_PROFILES` is slash-separated). If nothing writes it, `cf_scores` is `{}`, every profile scores 0, and strict `>` makes **first-valid-in-API-order** win — turning best-by-custom-format into arbitrary. Live, unlike `GLD-REP-08` | S | `GLD-STO-08`, `GLD-REP-08` |
| `GLD-RQ-11` | 🟡 **Weak `dry_run` in `selector.py`**, which issues `PUT movie/{id}` — fourth manager with the two-level defaults-`False` form | S | `GLD-SP-01` |
| `GLD-RQ-12` | 🟡 **`_cached_profiles` never invalidates** — Radarr's config sync *does* run, so a mid-run profile change is served stale | S | — |
| `GLD-RQ-01` | 🔴 **Radarr names the pressure constant `PRESSURE_THRESHOLD_GB`** while three other sites use `PRESSURE_FALLBACK_GB` for the same concept, comment and value. A grep for the common name misses this site entirely. **Three of four agree at 25.0**; the coordinator is the value outlier, Radarr the naming one | S | `GLD-COORD-01` |
| `GLD-RQ-02` | 🟡 **`_KID_AGE_TIERS` is a copied Plex vocabulary** asserting the scorer and playlist age-gate *"cannot disagree"* — with no mechanism enforcing it. Unlike `next_watch`'s forced copy, a `services → services` import **is** available here | S | `GLD-SP-03`, `GLD-NXW-01` |
| `GLD-RQ-03` | 🟡 **`WATCHABILITY_PROTECT_THRESHOLD = 6`** sits *below* the AXIS V2 median (8) and appears absent from `THRESHOLD_SPECS`. Confirm whether it routes through `get_threshold`, and whether it was re-anchored with the delete family when Group D v2 translated the axis | S | `GLD-THR-01`, `GLD-SER-05` |
| `GLD-RQ-04` | ❓ **Confirm `GLD-FEA-04`** — does `space_pressure._score_row` compute independently of `features/movie_features`? If so it is a **third** watchability path. The head read did not reach it | S | `GLD-FEA-04` |
| `GLD-RQ-05` | ❓ **Confirm `space_pressure` uses the canonical raise-on-unresolvable `dry_run`** — its Sonarr twin does | S | `GLD-SP-01` |
| `GLD-RQ-06` | **Read `universe.py` (59.5 KB) and `universe_membership.py` (16.5 KB)** — the `keep-universe` / bare-`universe` protection tier this repo's I1/I2 invariants rest on | L | — |
| `GLD-RQ-07` | **Read `selector.py`** — D34's Radarr half: does it reach `choose_codec_profile`? ✅ **Session 55: NO.** Selection is resolution-tier + custom-format score, with no `quality_analytics` import at all. **Re-aimed:** `test_codec_routing_report.py` sits in this folder and `space_pressure.py` is the only module big enough to hold its subject — the codec consumer is most likely inside the 151 KB module | S | `GLD-QAN-01`, D34 |
| `GLD-RQ-08` | **Check Radarr's `warm_cache` for `GLD-STO-01`'s key bug** — `radarr` declares an identical `SPACE_ESTIMATES` template | S | `GLD-STO-08` |
| `GLD-RQ-09` | **Split `space_pressure.py`** — 151 KB in one module, holding two pipeline stages, the shared step-down picker, size-anomaly handling and the codec-routing report | L | — |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `_score_row` compute independently? *(= `GLD-FEA-04`)* | `GLD-RQ-04` |
| Q2 | Is `WATCHABILITY_PROTECT_THRESHOLD` registry-routed, and was it re-anchored? | `GLD-RQ-03` |
| Q3 | Does `radarr/quality/selector.py` call `choose_codec_profile`? | `GLD-RQ-07`, D34 |
| Q4 | Does Radarr's `warm_cache` share Sonarr's unformatted-key bug? | `GLD-RQ-08` |

**Q2 is the one with reach.** A protect-floor below the median means the
non-exhaustive path shields most of the library from downgrade — which is fine if
that path is genuinely legacy, and a large behaviour difference if anyone sets
`space_exhaustive_downgrade=false` expecting *"the previous behaviour
byte-for-byte."* The docstring promises exactly that reversibility, so the two
paths' thresholds need to be as carefully anchored as each other.

## 9. Related designs

- [`machine_learning/space/DESIGN.md`](../../../machine_learning/space/DESIGN.md) §3.4 — the exhaustive policy, from the deciding side
- [`sonarr/series/VERIFICATION_space_pressure.md`](../../sonarr/series/VERIFICATION_space_pressure.md) — the twin, and the shared `_pick_stepdown_release`
- [`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) — the Phase-2.5 capstone that unifies Stage 2 across services
- [`machine_learning/thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.4 — AXIS V2, against which §5's `6` should be read

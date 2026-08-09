# playlists

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **playlists**

**Package** — `scripts.managers.machine_learning.playlists`
**Run position** — Phase 3, driven by [`services/plex/playlists/`](../../services/plex/README.md).
**One-liner** — The pure ordering brain for personalised Plex playlists: group by franchise, order by timeline within, rank by watchability across — with spoiler safety as a checkable invariant.

---

## Purpose

From [`__init__.py`](./__init__.py):

> **THINKS only.** No HTTP, no Plex, no cache, no ratingKey *resolution* — the
> service layer fetches owned items, resolves each to a Plex ratingKey, attaches a
> per-user watched flag + a watchability score, and hands the brain a list of
> `PlaylistInput`. The brain returns an ordered `PlaylistPlan`.

### The crown-jewel ordering rule

> * **GROUP** items that share a series / franchise / universe so they stay contiguous.
> * **WITHIN** a group, order by timeline (explicit timeline index if given, else
>   chronological); a series is ordered by **(season, episode) — NOT air date** — so a
>   missing / out-of-order air date can never surface a later episode before an
>   earlier one (**the #1 spoiler trap**).
> * **ACROSS** groups (and for standalone items), order by watchability.

---

## Determinism

> PURE + deterministic (brain_purity-guarded): same input → same output, **no
> wall-clock, no randomness, no input-order dependence** (every tie has an explicit
> deterministic breaker so a golden corpus can pin the result).

Stronger than [`scoring/`](../scoring/README.md), which acknowledges that F3 and
G4 read the clock. This package claims none.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`ordering.py`](./ordering.py) | 16.7 KB | `order_items` — the top-level ordering | ✅ 11.9 KB |
| [`cert_gate.py`](./cert_gate.py) | 11.7 KB | `cert_allowed`, `tier_level`, `is_restricted` | ✅ 9.1 KB |
| [`engagement.py`](./engagement.py) | 8.8 KB | Engagement signals | ❌ **none** |
| [`per_user.py`](./per_user.py) | 8.1 KB | `tilt_score` — per-user personalisation | ✅ 7.2 KB |
| [`timeline.py`](./timeline.py) | 6.4 KB | `order_within_group`, `_episode_sort_key` | ✅ 3.5 KB |
| [`grouping.py`](./grouping.py) | 6.0 KB | `group_items`, `coverage_stats` | ✅ 4.5 KB |
| [`models.py`](./models.py) | 5.7 KB | `PlaylistInput`, `PlaylistItemPlan`, `PlaylistPlan` | ❌ none |
| [`rationale.py`](./rationale.py) | 3.1 KB | `explain_reason` | ✅ 1.9 KB |
| [`caps.py`](./caps.py) | 2.3 KB | `apply_size_cap` — group-atomic · `limit_per_group` — one-offs trim | ❌ **none** |
| [`habits.py`](./habits.py) | 9.0 KB | `weekday_profile`, `derive_key`, `session_profile`, `runtime_cap_for` | ❌ **none** |
| [`candidates.py`](./candidates.py) | 7.2 KB | `episode_candidates`, `movie_candidates`, `pick_one_per_group` | ❌ **none** |
| [`outcomes.py`](./outcomes.py) | 11.7 KB | `record_surfaced`, `measure`, `leaderboard` — per-family hit rate | ❌ **none** |
| [`provenance.py`](./provenance.py) | 8.2 KB | `record`, `resolve`, `jitter_histogram`, `exclusivity` | ❌ **none** |
| [`backtest.py`](./backtest.py) | 7.5 KB | `backtest`, `best_cell` — walk-forward threshold selection | ❌ **none** |
| [`expansion.py`](./expansion.py) | 1.7 KB | `expand_show` | ✅ 1.5 KB |
| [`spoiler.py`](./spoiler.py) | 1.5 KB | `is_spoiler_safe` | ❌ **none** — see [`DESIGN.md`](./DESIGN.md) §3.4 |

Also [`test_recency.py`](./test_recency.py) (8.4 KB), which has no matching source
module — presumably covering recency logic inside `ordering.py` or `engagement.py`.

---

## The size cap keeps groups whole

`apply_size_cap` walks groups in ranked order, takes each whole group that fits,
and **skips** one that doesn't — then keeps filling from smaller lower-ranked
groups behind it.

> Skipping (rather than stopping at the first overflow) is deliberate: a single
> oversized group — e.g. a **200-member mega-group** — must not be able to starve
> the entire playlist down to the handful of items that happened to rank ahead of
> it.

Two consequences:

- `kept` is **not a prefix** of the flattened blocks, so *"the caller aligns
  metadata by item identity, not by slicing."*
- **The dropped count is always returned** — *"so truncation is observable, never
  silent."*

If even the smallest group exceeds the whole cap, it truncates within the
top-ranked group — *"better than an empty playlist."*

---

## Spoiler safety as an invariant

`is_spoiler_safe` is **verification, not mutation**:

> The contract every ordering must satisfy: walking the final playlist
> top-to-bottom, a viewer never reaches a later episode of a series before an
> earlier **unwatched** one of that same series.

Specials (season 0 / `is_special`) are exempt — *"they legitimately sit at a
track tail and carry no 'must precede' relationship."*

---

## Show expansion is always capped

Plex playlists hold playable items, never a show object.

> This is the **single biggest blast radius** (a 20-season library could explode a
> playlist), so expansion is ALWAYS capped.

| Mode | Behaviour |
|---|---|
| `next_unwatched_n` *(default)* | Earliest `cap` **unwatched** episodes in (season, episode) order |
| `full_series` | All episodes in order, still capped |

`cap` defaults to **25**; specials excluded unless asked.

---

## Tonight: a per-profile list for one upcoming day

Five to ten different groups, **one item each**, built for *tomorrow*. If they
want the next episode Plex autoplays it — a list offering six episodes of one
series would be a binge queue wearing a different name.

```
Tautulli history -> habits.weekday_profile   which groups own which weekday
                 -> habits.shows_for_day     the ones that own TOMORROW
                 -> candidates.*             what to offer for each
                 -> candidates.pick_one_per_group
                 -> plex/playlists/tonight_plan/{safe_user}
```

### Grouping ladder

`series:` → `franchise:` → `genre:` → `medium:`, most specific first, namespaced
so a franchise named "3" cannot fuse with a series whose ratingKey is "3".

A show repeats; **a movie is watched once**, so movies group one level up. See
[`DESIGN.md`](./DESIGN.md) §8a.2 — detectability falls off down the ladder, which
makes it a confidence ordering, not just a fallback.

### The session-length cap is learned, not assumed

"Long films at the weekend" was wrong for two of this household's three active
profiles — one is a midweek watcher, one a Monday watcher. `session_profile`
starts at the conventional week and shrinks toward observation by `n / (n + 8)`,
so day one is sensible and month three is right.

Runtime comes from the parquets already on disk — `runtime_minutes` (Radarr,
100% coverage) and `runtime_seconds` (Sonarr, 99.7% of owned episodes) — so the
cap costs **zero Plex calls**.

### Thresholds come from `backtest.py`, not from taste

Walk-forward: for target day D the profile is built only from plays strictly
before D's local midnight. A day nobody watched is dormant and excluded.
`DEFAULT_DAY_THRESHOLD` moved 0.28 → 0.16 on the first real run — hit rate 0.07
→ 0.32, silent days 39% → 9%.

### Attribution is strict

Played on the day Tonight was built for → Tonight. Any other day → Up Next. No
grace and no knob: a Wednesday play means the show was right and the **day** was
wrong, which is the error the model has to see. This is an inference, not a
measurement — Tautulli records no referrer — and `provenance.exclusivity` says
how much of the data is unambiguous.

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Service:** [`services/plex/playlists/`](../../services/plex/README.md) · [`DESIGN_personal_playlists.md`](../../services/plex/DESIGN_personal_playlists.md)
- **Knowledge:** [`support/knowledge/personalized-playlists.md`](../../../support/knowledge/personalized-playlists.md)

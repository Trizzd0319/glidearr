# next_watch

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **next_watch**

**Package** — `scripts.managers.machine_learning.next_watch`
**Run position** — Built once per run by the Radarr and Sonarr score passes; consumed by Group-A5 and the watchlist delete shield.
**One-liner** — Forward intent: folds every watchlist feed — Plex, Trakt, MAL — into one index that both ranks next-watch and carries the A5 scoring signal.

---

## Purpose

From [`__init__.py`](./__init__.py):

> The thin consumer sequenced in P1 so the flagship watchlist signal is **not
> inert / unvalidatable**. It THINKS only: pure `dict` in, `dict` out over the
> ALREADY-FETCHED `plex/watchlist/union`.

`build_intent_index` is *"why this subpackage is no longer orphaned"* — the Radarr
and Sonarr score passes build one per run and hand each title's entry to the
scorer, so **the same forward-intent feed that ranks next-watch also carries A5**.

> The deterministic A–G scorecard stays the curation authority; this only **RANKS**
> the forward intent feed.

Unusually, the entire implementation lives in `__init__.py` — there are no
submodules.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`__init__.py`](./__init__.py) | Everything: `watchlist_intent`, `rank_next_watch`, `build_intent_index`, `member_fraction` | ✅ Implemented |

## Test coverage

[`test_next_watch.py`](./test_next_watch.py) · [`test_intent_index.py`](./test_intent_index.py)

---

## Source strength

```python
INTENT_SOURCE_STRENGTH = {
    "plex_watchlist": 1.00,  "trakt_watchlist": 1.00,  "mal_plantowatch": 1.00,
    "trakt_recommendations": 0.65,  "mal_suggestions": 0.65,
    "plex_playlist": 0.60,  "people_cooccurrence": 0.60,
    "plex_hubs": 0.58,  "mal_seasonal": 0.55,
}
```

> **THE SAME RANKING** as `services/acquisition/scorer._SOURCE_SCORE`, divided by
> 100… **Deliberately NOT a second opinion**: a title should not be graded one way
> when we decide to ACQUIRE it and another way when we decide to KEEP it.
>
> Any feed missing from this table contributes **nothing (0.0)** rather than a
> guessed tier.

The tiers encode: *"the household literally said watch this"* (1.00) vs *"an
algorithm suggested it"* (0.65) vs *"it is merely airing"* (0.55).

---

## Dated vs undated feeds

```python
DATED_SOURCES = frozenset({"trakt_watchlist", "mal_plantowatch"})
```

Only feeds with a **real per-item timestamp** decay. Plex does not have one, and
the module refuses to invent one — see [`DESIGN.md`](./DESIGN.md) §3.2.

| Feed | Timestamp | Decays? |
|---|---|---|
| Trakt watchlist | `listed_at` — *"reaches back to 2020"* | ✅ |
| MAL plan-to-watch | `list_status.updated_at` | ✅ |
| Plex union | **none** | ❌ Full strength, no decay |

---

## The member ladder

```
1 member → 0.60 of cap    2 → 0.72    3 → 0.84    4 → 0.96    5+ → 1.00
```

> A solo watchlister is deliberately worth **0.60** of the cap rather than the
> whole thing — otherwise the member term is invisible (every title already at
> cap) on the **258-of-259 solo distribution this household has today**, and the
> signal could never grade a title two people both asked for.

One ladder, two consumers: the next-watch ranker scales it to 0–100, Group-A5
scales it to `scoring.watchlist_intent.cap`.

**Account sprawl is guarded.** `trakt_member` resolves the Trakt account to a
household member so *"Trizzd watchlisted it on Plex AND Trakt"* counts as **one**
member. MAL rides the same member — *"one human keeping three lists is one person
asking, and counting them separately would walk a solo title up the member ladder
(0.60 → 0.84 of the cap) on nothing but account sprawl."*

---

## The index

```
build_intent_index(...) → {"movies": {tmdb: entry}, "shows": {tvdb: entry}}

entry = {"sources": (feed, …), "members": (member, …),
         "dated": {feed: iso}, "anchor": iso | None}
```

`anchor` is *"the MOST RECENT activity among the members who asked for this
title"* — the delete shield's expiry: **intent lives while the member who
expressed it is still active.**

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Consumers:** [`scoring/_shared.watchlist_intent_score`](../scoring/README.md) (Group-A5) · the watchlist delete shield
- **Mirrors:** `services/acquisition/scorer._SOURCE_SCORE`
- **Related:** [`people_matrix/`](../people_matrix/README.md) (`people_cooccurrence` feed) · [`services/mal/id_bridge`](../../services/mal/README.md)

# tautulli/watch_history — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [tautulli](../README.md) › **watch_history**

**Manager** — `TautulliWatchHistoryManager`
**Status** — ✅ Implemented · 🎯 The reference PII-minimisation in the repo
**Existing docs** — [`README.md`](./README.md) (8.4 KB)

> **Coverage:** `__init__.py` head (~85 of 9.3 KB) — the TTL, the field
> projection and its rationale. The fetch/pagination body is unread.

---

## 1. Why this folder matters

`tautulli/history/all` is the **single source** of every watch signal in the
system: affinity, completion, recency, the watched bars, `next_watch`'s
resumption, `labels/`' ground truth, the delete grace window. Everything
downstream is a projection of this cache.

It is also the only cache carrying household **PII**.

---

## 2. 🎯 The reference data-minimisation

Every field is either admitted **with its consumer named**, or dropped **with the
reason stated**:

| Dropped | Why |
|---|---|
| `friendly_name` | *"household members' real display names (PII). Not read from this cache; `user` is the identifier every consumer uses"* |
| `ip_address` | *"WAN IP of the viewer (PII / location-linkable). **Never read.**"* |
| `machine_id` | *"device fingerprint that **can re-identify a viewer** (PII). Never read. (device granularity stays per-platform, not per-box.)"* |

And the borderline **retentions** are argued rather than assumed:

| Retained | Argument |
|---|---|
| `location` | *"coarse lan/wan bit only (home vs. remote); **NOT an IP and NOT geolocation**, so far less identifying than the dropped `ip_address`"* |
| `row_id` | *"an internal DB row id, **not a person/device**"* |
| `user_id` | *"a **non-PII stable identifier**"* — kept precisely because `friendly_name` is dropped |

With a stated verification method:

> We project each record down to only the fields the codebase actually reads
> (**verified by grepping consumers** in `scripts/managers/services/tautulli`,
> `radarr/orchestration` and `sonarr/series/sync`).

That is a documented minimisation **audit** — scope, method, per-field
justification, and an explicit note that `machine_id`'s exclusion costs device
granularity (*"per-platform, not per-box"*). Nothing else in the repo documents a
privacy decision to this standard. `GLD-TWH-01`.

---

## 3. 🔴 This is the root of the seven-watched-bars problem — and it states the rule

On `watched_status`:

> Tautulli's OWN watched verdict for the play (1 / 0.5 / 0)… It already reflects
> whatever completion threshold **the operator configured in Tautulli** (85 % out
> of the box, shared with Plex), so consumers that need *"did they actually watch
> this, or sample it?"* can **honour the server's answer instead of inventing a
> second, disagreeing definition.**

The principle is stated at the source, in the field's own admission note.
[`lifecycle/watched_definition.py`](../../../machine_learning/lifecycle/DESIGN.md)
follows it — verdict-when-present, `percent_complete` only as fallback.

**[`writeback/trakt_history.py`](../../writeback/DESIGN.md) §3.1 does not.** It
uses `watched_status == 1` **OR** `pct ≥ 85`, so a row Tautulli marked
**unwatched (0)** or **partial (0.5)** is pushed to the user's permanent Trakt
history if the percentage happens to clear the bar.

That is not merely an inconsistency between two modules. It is **the exact thing
this field's documentation says not to do**, in the one module whose output
leaves the system. `GLD-WB-01` strengthens accordingly: the divergence
contradicts a contract written at the producer.

---

## 4. 🎯 A schema migration handled with absent-vs-zero discipline

> **ABSENT** on rows cached before this key was admitted, and on a Tautulli old
> enough not to emit it — `watched_by_tautulli` falls back to `percent_complete`
> (which those rows DO carry), so **the transition is silent: no historical row
> reads as unwatched merely because the cache has not cycled yet.**

Adding a field to a projection is exactly where §8 **P-C** bites: every
pre-existing row suddenly lacks it, and a consumer reading absent-as-`0` would
mark the entire watch history unwatched — which, given what watch history gates,
would have looked like a household that never watched anything.

The fallback is chosen so the *transition* is invisible, and the reasoning is
recorded. **Ninth** instance of the discipline, and the first applied to a
**schema migration** rather than a missing measurement.

The same treatment appears immediately below for `video_decision` /
`audio_decision`: *"Absent on older cached rows (the projection drops a missing
key) → the breakdown falls back to a source-vs-streamed-codec heuristic."*
**Tenth** instance, in the same file.

---

## 5. The TTL is a fan-out coordinator, not just a staleness bound

```python
_HISTORY_TTL = 3_600  # 1 hour
```

> Refreshed hourly (**down from 24 h**): with things watched frequently, the
> resume/recency/JIT signals the playlists ride on need to reflect what was JUST
> watched. A full re-fetch is cheap (one paginated call of ~hundreds of rows) and
> — **unlike a date-delta** — also picks up in-progress %-complete updates to
> already-seen watches. **Kept positive (not per-run) because ~6 consumers call
> `get_all_history_cached` per run; the TTL must exceed a run's duration so they
> share one fetch.**

Three separate arguments in one constant:

1. **Freshness** — hourly because resume/JIT signals ride on it.
2. **Why full re-fetch, not delta** — a delta keyed on date would miss
   `percent_complete` updates to rows it has already seen. An in-progress episode
   watched further does not create a new row.
3. **Why not per-run** — six consumers per run; a per-run TTL would make each
   re-fetch. **The TTL must exceed a run's duration** so they share one fetch.

Point 3 is the subtle one: the TTL is doing double duty as a request-coalescing
mechanism, and the lower bound is set by *run duration* rather than by staleness
tolerance. Worth recording because a future "make it fresher" change that drops
below run duration would silently sextuple the fetch count. `GLD-TWH-02`.

---

## 6. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-TWH-01` | 🎯 **Cite this projection as the PII-minimisation reference** — per-field justification for every admission *and* every drop, borderline retentions argued (`location` as lan/wan not IP), a stated verification method (grepping named consumers), and the cost of exclusion noted (`machine_id` ⇒ per-platform not per-box granularity) | S | `GLD-PLX-07` |
| `GLD-TWH-02` | **Document the TTL's lower bound** — it must exceed a run's duration so ~6 consumers share one fetch. A future freshness change below that silently multiplies the fetch count | S | — |
| `GLD-TWH-03` | **Add a field-drift check** — the projection is *"verified by grepping consumers"*, which is a point-in-time audit. A new consumer reading a dropped field gets `None` silently *(P-C)* | S | `GLD-STO-08` |
| `GLD-TWH-04` | 🔴 **CONFIRMED session 68 — `get_all_history` returns `[]` on failure, three ways, and the result is CACHED for an hour.** ① no API → `[]`. ② first-page failure → `[]`; **mid-pagination failure returns a partial history as if complete**. ③ **absent `recordsFiltered` → `total=0` → truncates to ONE page** — exactly the bug `plex/` §3.4 documents avoiding by *"falling through to the empty-page terminator"* (which exists here but is unreachable once `total` is 0). `get_or_generate_cache` cannot tell `[]` from *empty*, so one blip empties **affinity, completion, grace window and `is_watched`** for ~6 consumers for an hour. **The `GLD-TRKT-02` class, live, on the most consequential cache in the system** — and §5's TTL, set above run duration so consumers share one fetch, guarantees they share one **failure** | S | `GLD-TRKT-02` |
| `GLD-TWH-06` | **Fix the stale docstring** — `get_all_history_cached` says *"cached for 24 hours"*; `_HISTORY_TTL` is **3600**. Predates the *"down from 24 h"* change the module header documents | S | — |
| `GLD-TWH-07` | ✅ **A fifth shipped bug recorded at its fix site** — `regenerate_on_expiry=True` carries *"**Frozen history was silently staling everything**."* The cache served past expiry, so every downstream signal aged with nothing noticing. Worth citing alongside the phantom-reclaim and `importMode=Move` notes | S | `GLD-SP-04` |
| `GLD-TWH-05` | **Expand `test_history_fields.py`** — 1.9 KB guarding the projection that every watch signal in the system derives from | S | — |

## 7. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ✅ **ANSWERED session 68 — it returns `[]`, three ways, and caches it.** No API → `[]`; first-page failure → `[]`; absent `recordsFiltered` → one page only. Cached for an hour under `tautulli/history/all` | 🔴 `GLD-TWH-04` |
| Q2 | How many rows does a full re-fetch actually carry — is *"~hundreds"* still true? | `GLD-TWH-02` |
| Q3 | Has any consumer started reading a field this projection drops? | `GLD-TWH-03` |

**Q1 matters most.** ✅ **Answered — and the answer is the bad one.** See
`GLD-TWH-04`: all three failure paths return `[]`, and the result is cached for an
hour under the key ~6 consumers read. The pagination terminator is otherwise the
correct form (grand total, not page length); only the missing-key default breaks
it.

## 8. Related designs

- [`tautulli/README.md`](../README.md) · [`tautulli/DESIGN.md`](../DESIGN.md) — the service root
- [`machine_learning/lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) — `watched_by_tautulli`, which follows §3's rule
- [`writeback/DESIGN.md`](../../writeback/DESIGN.md) §3.1 — the module that does not
- [`machine_learning/affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) — `group_movie_completions`, imported here
- [`plex/playlists/DESIGN.md`](../../plex/playlists/DESIGN.md) §2.2 — the ratingKey decay this cache's `rating_key` field is subject to

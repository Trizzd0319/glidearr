# plex — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **plex**

**Manager** — `PlexManager` (`scripts/managers/services/plex/__init__.py`)
**Status** — ✅ Implemented (P0–P1) · 🔵 P2–P4 built, default-off · 🔴 Snapshot retention breaks forward validation
**Existing docs** — [`README.md`](./README.md) (16 KB, thorough) · [`DESIGN_plex_service.md`](./DESIGN_plex_service.md) (41 KB) · [`DESIGN_personal_playlists.md`](./DESIGN_personal_playlists.md)

> **This document does not restate [`README.md`](./README.md).** That file already
> covers what the manager does, its lifecycle, endpoints, cache keys, config and
> interactions, and covers them well. This adds the 11-section frame the
> [conventions](../../../DOCS_CONVENTIONS.md) require — problem, goals, decisions,
> invariants, failure modes, and the enhancement register linkage — plus the
> cross-package findings the ML sweep surfaced.

---

## 1. Problem statement

Plex holds two signals nothing else in the stack has natively:

1. **The multi-user watchlist.** Not the owner's — *every household member's*. It
   is the strongest explicit forward-intent signal available, and Tautulli cannot
   see it.
2. **The identity crosswalk.** Plex-Home-user ↔ Tautulli-user ↔ `rating_groups`.
   Without it, per-user Plex signals cannot join anything else.

Getting at them is unusually hazardous:

| Hazard | Why |
|---|---|
| **Per-user tokens must be minted** | Reading another Home member's watchlist requires switching to their token — a credential per household member, per run |
| **The endpoints are unstable** | plex.tv v2 and Discover are community-documented; `metadata.provider.plex.tv` already 404s |
| **External and rate-limited** | Unlike the LAN PMS, Discover is a third party with 429s |
| **Display names collide** | `"Rob/Kids"` and `"Rob:Kids"` both sanitise to `Rob_Kids` — and a collision means serving one member another's watchlist |
| **A bare `plex://` guid resolves nothing** | Joining to tmdb/tvdb needs a paid network hop, multiplied by household size if done naively |

The security posture is described in [`README.md`](./README.md) as
*"non-negotiable, **post-incident**"* — so the token-handling rules are the
product of something having gone wrong, not of hypothetical caution.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Read every household member's watchlist, with attribution. |
| G2 | Never persist a minted token, anywhere, ever. |
| G3 | Two members whose names sanitise identically never share a cache key or token. |
| G4 | The paid GUID hop fires at most once per run per item, regardless of household size. |
| G5 | A Plex-less, unreachable or scope-failed install completes the run. |
| G6 | FETCH + CACHE only — no writes in v1. |
| G7 | Degradation is **visible**: a shrunken union is counted, never silent. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Any curation authority | *"makes no value judgement"* — the A–G scorecard decides. |
| N2 | Write-back | v1 is read-only; `dry_run` is threaded so a future write is gated from day one. |
| N3 | Auto-acting on reconcile | *"orphans never auto-feed deletion"* — diagnostic only. |
| N4 | Being critical | Deliberately excluded from `Main._validate_managers`. |

---

## 3. Architecture notes

Full lifecycle and control flow are in [`README.md`](./README.md). What follows
is only what bears on decisions, invariants or the register.

### 3.1 A forbidden cache key

> **`plex/users/<u>/token` — FORBIDDEN.** Minted tokens live only in an in-memory
> dict; this key is never created.

A negative invariant, stated as a named key that must not exist. That is a
notably better formulation than "don't persist tokens" — it is greppable, and a
reviewer can check for its absence.

Reinforced at three layers: each token is registered with the logger scrubber
**the instant it is minted**; a `pin=` redaction pattern sits in the logger; and
every logged URL is query-scrubbed because `X-Plex-Token` travels as a URL
parameter on Discover.

### 3.2 🎯 Collision-safe attribution — the identity problem, solved

> Home profiles `"Rob/Kids"` and `"Rob:Kids"` both sanitize to `Rob_Kids`; the
> per-uuid map disambiguates the second to a uuid-suffixed key… **fail-CLOSED
> attribution** (user A is never served user B's watchlist).

This is the **fourth** appearance of the identity discipline the ML sweep kept
finding — and the highest-stakes instance, because the failure is not a
mis-scored title but one household member reading another's private list.

| Package | Keys on | Collision handling |
|---|---|---|
| `discovery/occupancy` | ownership id | *"never a title (remake collisions)"* |
| `people_matrix` | `person_tmdb_id` | *"a name-keyed graph could not feed it at all"* |
| **`plex`** | **Plex uuid** | **Sanitised-name collision → uuid-suffixed key, fail-closed** |
| `labels/labeling` | normalised title | ❌ none available — the outstanding gap |

Plex had the same problem `labels/` has (a display string that collides) and had a
uuid to fall back on. `labels/` does not, which is exactly what `GLD-LAB-02`
proposes building.

### 3.3 ✅ Confirmed miss vs transient failure

> a **confirmed** Discover miss is memoized so it never re-hops on a later run; a
> **transient** hop failure stays retryable

Two failure modes that both produce "no result" are distinguished and cached
differently. That is §8 **P-C** discipline applied at the network layer, and it is
the same distinction `sizing/file_comparison` makes (`expected <= 0` ⇒ no
opinion) and `quality_analytics/codec_direct_play_rate` makes (`None` on no
sample).

Combined with G4 — *"the paid hop fires at most once per run per `rating_key`
(killing the per-household-user multiplier)"* — the resolution path is both
correct and bounded.

### 3.4 ✅ Two bugs this codebase has hit elsewhere, avoided here

**Pagination.** *"The early-stop uses the grand `totalSize` only (never the
per-page `size`); when `totalSize` is absent it falls through to the empty-page
terminator, so a >100-item watchlist is never silently truncated to one page."*

That is precisely the bug fixed in `TraktWatchlistManager` — no pagination past
100 items. Same shape, caught before shipping, with the reasoning recorded.

**Fail-closed on transient failure.** *"Preserves the prior good union on a
transient all-fail (fail-closed)."*

Also precisely the Trakt bug — returning `[]` on a 429, which the pruner read as
*"the watchlist is empty"* and would have acted on. Here the prior good union
survives.

Two known-hard failure modes, both handled, both in the module that would have
been hurt worst by getting them wrong.

### 3.5 ✅ Visible degradation (G7)

> A PIN-protected profile with no configured PIN is skipped and **counted** in
> `run_stats.users_pin_skipped` — the union shrinks **visibly, never silently**.

This is the fix I have logged as *missing* in eleven other places
(`GLD-LAB-03`, `GLD-AFF-05`, `GLD-LED-03`, `GLD-SIZ-05`, `GLD-PPL-03`, …). Plex
already does it, and `plex/run_stats` carries nine such counters into the Discord
run summary.

Worth citing as the reference implementation when those items are worked.

### 3.6 🔴 Snapshot retention makes forward validation impossible

This is the finding the ML sweep set up and only becomes visible from here.

[`eval/forward.py`](../../machine_learning/eval/README.md) exists to solve the
watchlist blind spot — a watchlist cannot be evaluated retrospectively because
items *leave* it once watched. Its method:

```
1. snapshot the watchlist union at T   ← plex/watchlist/snapshot/{ts}
2. LATER, ask: of the items on the list at T, how many were watched within W?
3. is that hit-rate a LIFT over the base watch-rate?
```

Step 2 requires the snapshot from `T` to still exist at `T + W`, where `W` is the
horizon — **14 days** by `ml.thresholds.horizon_days`.

But [`next_watch/__init__.py`](../../machine_learning/next_watch/README.md),
describing the same store:

> the rolling `plex/watchlist/snapshot/` files retain well under a day —
> **8 files spanning ~7 hours on this install**

**Retention is ~7 hours. The measurement window is 14 days.** Every snapshot is
gone roughly 48× before it can be evaluated, so forward validation cannot
produce a result — and `aggregate_forward` would honestly report
`{"n_snapshots": 0}` rather than erroring, which is why nothing has surfaced it.

The retention is governed by `plex.watchlist.snapshot_retention`. Fixing it is a
config change plus a decision about disk cost. `GLD-PLX-01`, and it unblocks
`GLD-EVA-03` / `GLD-EVA-09`.

Note this also explains why `next_watch` refuses to derive a `first_seen` from
these files — the same short retention that breaks forward validation is what
would have made a derived timestamp a fabrication.

### 3.7 Capability tiers

| Phase | Capability | Default |
|---|---|---|
| P0 | `users`, `metadata` | ✅ on, **critical** |
| P1 | `watchlist` | ✅ on, **critical** |
| P2 | `on_deck`, `ratings` | ❌ off |
| P3 | `libraries` (reconcile) | ❌ off |
| P4 | `collections`, `playlists` | ❌ off |

`critical_keys = {users, metadata, watchlist}` — *"the irreducible v1"*.
Everything above is enrichment layered on top and default-off in scoring.

Note `next_watch` already anticipates one of them: *"the ALREADY-FETCHED
`plex/watchlist/union` (**and, later, `plex/on_deck/union`**)"* — so P2's on-deck
has a declared consumer waiting on a default-off flag.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Optional + non-critical, outside `_validate_managers` | G5 — a Plex-less install must complete | Treat as critical |
| D2 | Tokens in-memory only; the cache key is *forbidden* | G2 — post-incident, and greppable as a negative invariant | Encrypt at rest |
| D3 | Scrubber registration at mint time | The window between minting and registering is the exposure | Register at log time |
| D4 | Per-uuid collision map | G3 — fail-closed attribution; a shared key is a privacy breach | Trust sanitisation |
| D5 | Two-tier GUID resolution, paid hop last | G4 — free parse and bridge first; the hop is the only paid step | Always hop |
| D6 | Memoise confirmed misses, retry transient ones | §3.3 — the two look identical and must not be cached identically | Cache all failures |
| D7 | Paginate on grand `totalSize` only | §3.4 — per-page `size` truncates at one page | Per-page terminator |
| D8 | Preserve the prior union on transient all-fail | §3.4 — an empty union would be acted on | Return empty |
| D9 | Count PIN-skipped users | G7 — a shrinking union must be visible | Skip silently |
| D10 | Everything above P1 default-off | Enrichment must prove itself before it runs | Enable on configure |
| D11 | Reconcile is diagnostic only | *"orphans never auto-feed deletion"* — a set-diff is not evidence of intent | Feed the delete pool |
| D12 | `dry_run` threaded despite no writes | A future write-back is gated from day one | Add the gate later |
| D13 | Sibling resolution by registry class name | Matches Tautulli/Radarr; never `self.manager.<attr>` | Attribute access |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | `plex/users/<u>/token` is never written. |
| I2 | A minted token is scrubber-registered before it can appear in any log line. |
| I3 | No two Home members share a token slot or cache namespace. |
| I4 | Per-user caches contain no email and no token. |
| I5 | The paid Discover hop fires ≤ once per run per `rating_key`. |
| I6 | A confirmed guid miss is never re-hopped; a transient failure always is. |
| I7 | The watchlist union is never replaced by an empty one on transient failure. |
| I8 | Every skipped user is counted in `run_stats`. |
| I9 | Nothing here writes to Plex. |
| I10 | Reconcile output never feeds a deletion decision. |
| I11 | TLS verification is never disabled; logged URLs are query-scrubbed. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| No `plex_token` | `configured` | Self-disables, writes disabled `run_stats` | None | ✅ `run_stats` |
| Token not owner-scoped | `/api/v2/user` non-200 | Owner-only roster; watchlist pass skipped | 🟡 Union shrinks | ✅ `scope_ok=False` |
| PMS unreachable | `/identity` | Local-PMS passes skipped; account passes continue | 🟡 Partial | ✅ Non-fatal |
| PIN-protected, no PIN | Mint failure | Skipped **and counted** | 🟡 Union shrinks | ✅ `users_pin_skipped` |
| Discover 429 | Retry-After, capped 30 s | Backoff | Bounded | 🟡 |
| Transient all-user failure | Guard | **Prior union preserved** | None | ✅ Fail-closed |
| Bare `plex://` unresolvable | Two-tier | Item lacks external ids; `plex/debug/unresolved_guids` | 🟡 Drops from joins | 🟡 Debug key only |
| **Snapshot aged out before `T+W`** | **None** | Forward validation reports `n_snapshots: 0` | 🔴 §3.6 — the measurement never runs | ❌ **None** |
| Name collision | Per-uuid map | Uuid-suffixed key | None | ❌ None |
| Endpoint changes upstream | 404/401 | Sub-pass fails, run continues | 🟡 Silent capability loss | 🟡 Wrapped |

**Row 8 is the one to act on.** Every other row degrades visibly or safely. That
one makes an entire measurement subsystem structurally inoperative while
reporting a legitimate-looking zero.

---

## 7. Configuration surface

Documented in full in [`README.md`](./README.md). The keys this design touches:

| Key | Default | Effect |
|---|---|---|
| `plex.plex_token` | — | Absent ⇒ self-disable |
| `plex.watchlist.snapshot_retention` | ~7 h observed | 🔴 §3.6 — must exceed `ml.thresholds.horizon_days` (14 d) |
| `plex.pins` | `{}` | `{title: {"pin": …}}`; absent ⇒ profile skipped + counted |
| `plex.client_identifier` | generated | Stable; v2 endpoints 401 without it |
| `plex.<cap>.enabled` | `false` | `on_deck`/`ratings`/`reconcile`/`collections`/`playlists`/`sessions` |
| `rating_groups` | `{"household": {}}` | Identity crosswalk |

---

## 8. Implemented capabilities

See [`README.md`](./README.md) for the full list. Structurally:

- ✅ P0–P1 irreducible core: users, metadata, watchlist
- ✅ Multi-user watchlist union with per-user attribution
- ✅ Identity crosswalk with collision-safe, fail-closed keying
- ✅ In-memory-only token handling with a forbidden persistence key
- ✅ Two-tier GUID resolution with confirmed-miss memoisation
- ✅ Correct pagination and fail-closed transient handling
- ✅ Nine-counter `run_stats` feeding the run summary
- 🔵 P2–P4 built and default-off: on-deck, ratings, reconcile, collections, playlists

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-PLX-01` | 🔴 **Raise `watchlist.snapshot_retention` above the label horizon** — ~7 h retention against a 14-day window means **forward validation can never produce a result** | §3.6. **Unblocks `GLD-EVA-03`, `GLD-EVA-09`** — the watchlist blind-spot measurement is currently inoperative and reports a legitimate-looking zero | S | D44 |
| `GLD-PLX-02` | **Warn when retention < horizon** at config load | Makes §3.6 self-detecting rather than requiring two docs to be read together | S | `GLD-PLX-01` |
| `GLD-PLX-03` | 🎯 **Cite `run_stats` as the reference implementation** for coverage reporting — eleven register items ask for exactly what Plex already does | `GLD-LAB-03`, `GLD-AFF-05`, `GLD-LED-03`, `GLD-SIZ-05`, `GLD-PPL-03` and others all want a counter Plex already ships | S | — |
| `GLD-PLX-04` | **Enable `on_deck`** — `next_watch` already names `plex/on_deck/union` as a future input | A declared consumer waiting on a default-off flag | S | `GLD-NXW-04` |
| `GLD-PLX-05` | **Surface `plex/reconcile/{orphans,missing}`** — computed when enabled, with no reader | Possible P-A; diagnostic value is real but unreachable | S | `GLD-WEB-04` |
| `GLD-PLX-06` | **Surface `plex/debug/unresolved_guids`** — items silently absent from every id-join | §6 row 7: a title that never resolves drops out of scoring joins invisibly | S | `GLD-PLX-05` |
| `GLD-PLX-07` | **Expand `test_security.py`** — 898 B against a *"non-negotiable, post-incident"* posture | The collision map, scrubber-at-mint and forbidden key are the highest-stakes invariants here | M | — |
| `GLD-PLX-08` | **Detect upstream endpoint drift** — the v2 and Discover paths are community-documented and one has already moved | §6 row 10: a 404 fails a sub-pass quietly | M | `GLD-PLX-06` |
| `GLD-PLX-09` | **Document the 13 subpackages** — each is its own work item and none has a `DESIGN.md` | The parent is well documented; the children are not | L | — |
| `GLD-PLX-10` | **Reconsider `parent_name` resolving to `"Services"`** — cosmetic, shared with MAL | Same finding as MAL; a two-line fix in `_infer_parent_from_path` | S | `GLD-MGR-03` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What retention does forward validation actually need — `horizon + margin`, or a fixed count? *(= D44)* | `GLD-PLX-01` |
| Q2 | Has forward validation ever produced a non-zero `n_snapshots`? | `GLD-PLX-01` |
| Q3 | Should P2 on-deck be enabled now that `next_watch` names it? | `GLD-PLX-04` |
| Q4 | What was the incident that set the security posture, and is it recorded anywhere? | `GLD-PLX-07` |

**Q2 is the cheap diagnostic.** If `aggregate_forward` has only ever returned
`{"n_snapshots": 0}`, §3.6 is confirmed in one query — and every conclusion drawn
from "the watchlist signal is unvalidated" becomes "the watchlist signal has never
been measurable."

## 11. Related designs

- [`README.md`](./README.md) — the operational reference this complements
- [`DESIGN_plex_service.md`](./DESIGN_plex_service.md) · [`DESIGN_personal_playlists.md`](./DESIGN_personal_playlists.md)
- [`machine_learning/eval/DESIGN.md`](../../machine_learning/eval/DESIGN.md) §3.5 — the forward validation §3.6 blocks
- [`machine_learning/next_watch/DESIGN.md`](../../machine_learning/next_watch/DESIGN.md) §3.2 — the refusal to fabricate a timestamp from these snapshots
- [`machine_learning/playlists/DESIGN.md`](../../machine_learning/playlists/DESIGN.md) — the brain half of P4 playlists
- [`machine_learning/labels/DESIGN.md`](../../machine_learning/labels/DESIGN.md) §3.3 — the identity problem §3.2 solves and `labels/` cannot

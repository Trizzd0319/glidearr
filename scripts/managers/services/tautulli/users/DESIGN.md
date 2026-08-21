# tautulli/users — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [tautulli](../README.md) › **users**

**Manager** — `TautulliUsersManager`
**Status** — ✅ Implemented · 🎯 Near-pure delegation · 🟡 `[]` on failure, three fetchers
**Related** — [`watch_history/DESIGN.md`](../watch_history/DESIGN.md) · [`metadata/DESIGN.md`](../metadata/DESIGN.md)

> **Coverage:** `__init__.py` head (~75 lines). The per-user affinity tail is
> unread.

---

## 1. 🎯 A near-pure delegation layer

Every computation goes to the brain; the service keeps only fetch, logging and
config resolution:

```python
from scripts.managers.machine_learning.affinity.genre_affinity import (
    aggregate_affinity, per_user_affinity,
)
```

> The COMPUTATION lives in the brain (`genre_affinity.aggregate_affinity`); the
> service keeps FETCH + this summary log + the cache-write (`TautulliManager`).

Stated three times across three methods, each naming which half lives where. This
is [`ARCHITECTURE.md`](../../../machine_learning/ARCHITECTURE.md)'s
service/brain split at its cleanest — the manager's own methods are three lines
each and none of them decides anything.

`_compute_affinity_from_entries` is *"kept as a thin method for internal /
back-compat callers. **Pure.**"* — a delegating shim that says so.

---

## 2. This is D32's config surface

```python
def _affinity_half_life(self):
    """Optional recency half-life (days) for affinity decay — config
    ``scoring.affinity_half_life_days``. None/0 = legacy raw counts (default)."""
    return ((self.config or {}).get("scoring", {}) or {}).get("affinity_half_life_days")
```

[`affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) §3.2 records
temporal decay as **built, default off, byte-identical** — and **D32** asks what
`half_life_days` should be, noting that enabling it is an *axis translation*
requiring the same three-boundary re-anchor as Group D v2.

Here is where the value enters the system: `scoring.affinity_half_life_days`,
read by the service, passed to the brain. `None`/`0` ⇒ legacy raw counts.

Worth recording because D32's answer has to be **configured here** and
**re-anchored elsewhere** — the config key and the thresholds it would invalidate
live in different packages, with nothing linking them. `GLD-TUS-01`.

---

## 3. 🟡 `[]` on failure — a third shape in the sibling audit

```python
def get_all_users(self) -> list:
    if not self.tautulli_api:
        return []
    resp = self.tautulli_api.get_users()
    users = ((resp or {}).get("response") or {}).get("data", []) or []
    return users
```

`(resp or {})` turns a `None` response into `{}`, and the trailing `or []`
absorbs anything falsy. **A failed fetch is indistinguishable from a household
with no users.** Same for `get_user_watch_time_stats` and
`get_user_player_stats`.

But the shape differs from both siblings: **there is no loop.** `get_users`
returns the whole roster in one call, which is right for a household of a handful
of accounts — so there is no truncation risk, only the empty-on-failure one.

### 3.1 The sibling audit so far — three shapes, three verdicts

| Sub-manager | Fetch shape | On failure | Verdict |
|---|---|---|---|
| `watch_history` | **page** loop | `break` — abandons the rest, then **cached 1 h** | 🔴 `GLD-TWH-04` |
| `metadata` | **item** loop | `continue` — skips one, **warns per key** | ✅ correct |
| **`users`** | **single call** | `[]`, uncached (as far as read) | 🟡 bounded |

The consequence scales with the shape. A page loop that breaks loses unbounded
data; an item loop that continues loses one item; a single call that returns `[]`
loses one fetch — and `get_all_users` returning `[]` means
`compute_per_user_genre_affinity(user_list=[])` produces an **empty per-user
matrix** for that run, which is recoverable next run *provided nothing caches it*.

**Whether `TautulliManager` caches the roster is the open question.** If it does,
`users` joins `watch_history` in severity; if not, it self-heals. `GLD-TUS-02`.

### 3.2 Four sub-managers still unchecked

`devices`, `transcode`, `episodes`, `series`. The distinguishing keyword is one
token, and a single grep settles all four:

```powershell
$py | Select-String -Pattern 'if not resp' -Context 1,2
```

---

## 4. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-TUS-01` | **Link D32's config key to the thresholds it would invalidate** — `scoring.affinity_half_life_days` is read here and passed to the brain, but enabling decay is an **axis translation** needing the same three-boundary re-anchor as Group D v2. The key and the thresholds live in different packages with nothing connecting them | S | `GLD-AFF-01`, `GLD-LIK-01`, D32 |
| `GLD-TUS-02` | ❓ **Does `TautulliManager` cache the user roster?** `get_all_users` returns `[]` on failure; if that is cached, an empty roster persists and per-user affinity is empty for every consumer until the TTL expires — `GLD-TWH-04`'s severity. If uncached, it self-heals next run | S | `GLD-TWH-04` |
| `GLD-TUS-03` | **Finish the sibling audit** — `devices`, `transcode`, `episodes`, `series` unchecked. Three shapes found in three managers; the difference between correct and broken is one keyword | S | `GLD-TWH-04`, `GLD-TMD-01` |
| `GLD-TUS-04` | 🎯 **Cite this manager as the service/brain split reference** — three methods, each three lines, each naming which half lives where. Nothing here decides anything | S | `GLD-CACHE-S03` |
| `GLD-TUS-05` | ✅ **Read the per-user affinity tail** — `compute_per_user_genre_affinity`'s zero-history handling (*"users with zero matching history entries…"*) was cut off mid-sentence in the original pass | S | ✅ **Answered** (`GLD-TAUT-14`): the brain **OMITS** zero-entry users from the result — it does not zero-value them. That distinction is now rendered rather than implied: the gradings table prints `no history in window` for an omitted account and `none` for one that was graded and matched nothing, so the two facts can no longer be confused in the log (they previously looked identical: both absent) |
| `GLD-TUS-07` | Mirror of `GLD-TAUT-14` — the per-account gradings table lives here (`_log_per_user_affinity_table` + `_top_genres`, fed by the extracted `_family_ids`). It is the EVIDENCE half of `GLD-TUS-06`: scoping's claim that outsiders keep their own grading was previously unverifiable from a run, because the log showed only a household total and a count of matrices. Classification joins on `user_id` against the same id set that scoped the aggregate, so a row cannot contradict the household line above it; unresolvable → Scope `-` and a caption saying scoping is INACTIVE, never a guess. Full registration + verification: §4.16 and §0.1 #62 | One service, one register home (P-E avoidance) | — | `GLD-TAUT-14` |
| `GLD-TUS-08` | Mirror of `GLD-TAUT-15` — **account linking** (`account_links`) lives here (`_account_links` / `_link_history` / `_fan_out_links` + the table's Link column). Two Tautulli logins that are one person are graded as one viewer: plays merge onto the group PRIMARY before the brain groups them, and the merged matrix is fanned back out to every member so both Plex profiles render the same recommendations. Three invariants worth keeping: entries are shallow-copied (the household aggregate must keep seeing real account ids), each member gets its own matrix copy (no write-through aliasing), and a link NEVER widens the family aggregate. ⚠️ Only the AFFINITY half of "identical playlists" — the watched-set half is `GLD-TAUT-16`. Full registration + verification: §4.16 and §0.1 #64 | One service, one register home (P-E avoidance) | — | `GLD-TAUT-15` |
| `GLD-TUS-06` | Mirror of `GLD-TAUT-12`/`-13` — household-affinity family scoping lives here (`_family_scope` + the `compute_genre_affinity` wiring): non-Home accounts stop steering the household maps while their per-user matrices still build; family = the Plex users pass's persisted `plex/identity_map` (previous run's — this manager aggregates first); fail-OPEN with a loud `UNSCOPED` warning on an absent/id-less/unreadable map, because an empty household aggregate would hurt every consumer more than one run of drift; unknown-owner plays kept and counted. What it deliberately does NOT scope — the parquet `is_watched` flags (engagement floors, retention gates, people-matrix watched-set) — is `GLD-TAUT-13`, open. Full registration + verification: §4.16 and §0.1 #61 | One service, one register home (P-E avoidance) | — | `GLD-TAUT-12` |

## 5. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is the user roster cached? | `GLD-TUS-02` |
| Q2 | Do the remaining four sub-managers `break` or `continue`? | `GLD-TUS-03` |
| Q3 | What happens to a user with zero matching history entries — omitted, or zero-valued? | ✅ **Omitted** — `GLD-TUS-05`, rendered as `no history in window` by `GLD-TAUT-14`'s table |

**Q3 was a P-C question in miniature — now answered.** The docstring was cut
mid-sentence at *"users with zero matching history entries…"*; whether they are
**omitted** from the matrix or included with **zeros** decides whether a new
household member reads as *"no taste yet"* or *"actively dislikes everything"*.
The answer is **omitted** — and until `GLD-TAUT-14` that answer was invisible at
run time, because an omitted account and an account nobody had ever offered to
the grader both showed up the same way: as nothing at all. The gradings table now
gives the two states different renderings (`no history in window` vs a row that
never appears) and gives *graded-but-matched-nothing* a third (`none`).
`affinity/`'s `build_library_index` exists for exactly that failure at a
different layer.

## 6. Related designs

- [`machine_learning/affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) — `aggregate_affinity`, `per_user_affinity`, and the decay D32 concerns
- [`watch_history/DESIGN.md`](../watch_history/DESIGN.md) §5.5 · [`metadata/DESIGN.md`](../metadata/DESIGN.md) §2 — the other two audit shapes
- [`tautulli/README.md`](../README.md) · [`tautulli/DESIGN.md`](../DESIGN.md)

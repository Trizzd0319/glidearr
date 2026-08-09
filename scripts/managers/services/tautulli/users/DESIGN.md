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
| `GLD-TUS-05` | **Read the per-user affinity tail** — `compute_per_user_genre_affinity`'s zero-history handling (*"users with zero matching history entries…"*) is cut off mid-sentence in this pass | S | — |

## 5. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Is the user roster cached? | `GLD-TUS-02` |
| Q2 | Do the remaining four sub-managers `break` or `continue`? | `GLD-TUS-03` |
| Q3 | What happens to a user with zero matching history entries — omitted, or zero-valued? | `GLD-TUS-05` |

**Q3 is a P-C question in miniature.** The docstring is cut mid-sentence at
*"users with zero matching history entries…"* — whether they are **omitted** from
the matrix or included with **zeros** decides whether a new household member reads
as *"no taste yet"* or *"actively dislikes everything"*. `affinity/`'s
`build_library_index` exists for exactly that failure at a different layer.

## 6. Related designs

- [`machine_learning/affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) — `aggregate_affinity`, `per_user_affinity`, and the decay D32 concerns
- [`watch_history/DESIGN.md`](../watch_history/DESIGN.md) §5.5 · [`metadata/DESIGN.md`](../metadata/DESIGN.md) §2 — the other two audit shapes
- [`tautulli/README.md`](../README.md) · [`tautulli/DESIGN.md`](../DESIGN.md)

# tautulli/metadata — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [tautulli](../README.md) › **metadata**

**Manager** — `TautulliMetadataManager`
**Status** — ✅ Implemented · 🎯 Correct failure structure, unlike its sibling
**Existing docs** — [`README.md`](./README.md) (10.0 KB)

> **Coverage:** `__init__.py` head (~80 of 8.6 KB). The cache generator's tail is
> unread.

---

## 1. Why this folder was read next

[`watch_history/DESIGN.md`](../watch_history/DESIGN.md) §5.5 found `[]`-on-failure
plus two truncation paths, cached for an hour (`GLD-TWH-04`). The obvious
follow-up: **do the sibling fetchers share it?**

They do not. `metadata/` gets the same class of problem **right**, and the
contrast is instructive.

---

## 2. ✅ Per-item failure **skips**, it does not break

```python
for rk in rating_keys:
    resp = self.tautulli_api.get_metadata(rating_key=rk)
    if not resp or (resp.get("response") or {}).get("result") != "success":
        self.logger.log_warning(f"[TautulliMeta] Metadata failed for rating_key={rk}")
        continue                                   # ← skip this key, keep going
```

| | `watch_history.get_all_history` | `metadata.build_metadata_index` |
|---|---|---|
| On a failed fetch | `break` — **abandons the rest** | `continue` — **skips one item** |
| Visibility | Silent | **Warned, per key** |
| Result shape | Truncated list, looks complete | Index missing that key |

`watch_history` iterates *pages*, where a mid-loop failure means the remaining
pages are lost. `metadata` iterates *items*, where a mid-loop failure costs one
item. Both use a loop; only one of them can afford to `break`, and it is the one
that does not.

### 2.1 But it still returns a partial index silently — and that has a known cost

`build_metadata_index` returns whatever it assembled. If 500 of 8,000 keys fail,
the caller receives 7,500 entries with no indication of the shortfall (beyond 500
warning lines).

**That partiality is the documented reason another module exists.**
[`affinity/genre_affinity.build_library_index`](../../../machine_learning/affinity/DESIGN.md)
was written because *"Tautulli's **sampled** metadata index leaves holes"* — a
low-volume profile whose handful of rating_keys never made the index would
otherwise score `affinity=0` and collapse to the flat household ranking.

So the holes this loop creates are already compensated for downstream. Worth
recording as a completed chain rather than an open defect: producer skips → index
is partial → consumer has a backstop. `GLD-TMD-01` proposes returning the skip
count so the backstop's necessity is measurable rather than assumed.

Also `if not self.tautulli_api: return {}` — the same no-API-returns-empty shape
as `GLD-TWH-04` ①, but a `{}` here means *"no metadata for any key"*, which the
consumers already treat as holes.

---

## 3. 🎯 A three-way distinction where most code has two

```python
md = (resp.get("response") or {}).get("data", {})
if not md:
    # Tautulli returns {"result": "success", "data": {}} for items that
    # no longer exist in Plex. Treat as missing so the rating_key ends
    # up in the not_in_metadata debug bucket rather than no_tmdb_guid.
    continue
```

Three outcomes, kept apart:

| Outcome | Meaning | Routed to |
|---|---|---|
| No response / `result != success` | **Fetch failed** | warning, skip |
| `result == success`, `data == {}` | **Item no longer exists in Plex** | `not_in_metadata` bucket |
| Metadata present, no tmdb guid | **Item exists, has no tmdb id** | `no_tmdb_guid` bucket |

A *successful* response carrying empty data is the hardest of the three to
notice — the call worked, so the naive read is "this item has no metadata," which
lands it in the wrong diagnostic bucket. Separating it means the debug output
distinguishes *"Plex forgot this"* from *"this has no tmdb id."*

**Eleventh** instance of the absent-vs-empty discipline, and the first to make a
*three*-way split.

---

## 4. 🎯 A schema-versioned cache — the other answer to the migration problem

```python
existing = self.global_cache.get("tautulli/metadata/index")
if existing and isinstance(existing, dict):
    has_tmdb = any("tmdb_id" in v for v in existing.values())
    if not has_tmdb:
        self.logger.log_info("… (pre-schema cache) — invalidating and rebuilding.")
        self.global_cache.delete("tautulli/metadata/index")
```

> Auto-invalidates the cache when it **pre-dates the `tmdb_id` field** so that the
> first run after a schema change rebuilds immediately rather than waiting for the
> 7-day TTL to expire.

The cache has no version stamp, so the code **probes for the field** and treats
its absence as a version marker. With `_METADATA_TTL = 604_800` (7 days), the
alternative was up to a week of tmdb-less metadata.

### 4.1 Two modules, one problem, two opposite — and both correct — answers

[`watch_history`](../watch_history/DESIGN.md) §4 faced the same thing when it
admitted `watched_status`:

| | Strategy | Why it fits |
|---|---|---|
| `watch_history` | **Fall back** — absent `watched_status` → use `percent_complete` | Refetch is one paginated call, but the fallback is *free* and makes the transition silent. Invalidating would have been fine too |
| `metadata` | **Invalidate + rebuild** | A tmdb-less index is **useless**, not merely older — there is no fallback that recovers a missing id |

The deciding factor is not cost but **whether a fallback exists**. `percent_complete`
substitutes for `watched_status`; nothing substitutes for a tmdb id. Worth stating
as the rule, because "rebuild is expensive so fall back" would give the wrong
answer here — metadata is *the* expensive one (one API call per key) and still
rebuilds. `GLD-TMD-02`.

---

## 5. Freshness without abandoning a long TTL

> a 7-day-cached **FULL** build, topped up **INCREMENTALLY** each run with any
> requested rating_key not already present (so newly-watched items resolve the
> **same run** instead of waiting up to a week for the TTL rebuild)

The long TTL is right for metadata (genres and cast do not change), but a newly
watched item has no entry at all. The incremental top-up resolves those without
touching the other 8,000 — so the TTL governs *refresh* while the top-up governs
*coverage*.

Contrast `watch_history`'s 1-hour TTL: history changes constantly, so it is fully
refetched; metadata is static, so it is appended to.

---

## 6. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-TMD-01` | **Return the skip count from `build_metadata_index`** — it returns a partial index silently (500 failed keys ⇒ 7,500 entries, no shortfall signal beyond 500 warning lines). `affinity.build_library_index` exists *because* of these holes; a count would make that backstop's necessity **measurable rather than assumed** | S | `GLD-AFF-05`, `GLD-TWH-04` |
| `GLD-TMD-02` | 🎯 **Record the migration rule** — `watch_history` **falls back**, `metadata` **invalidates**, and both are right. The deciding factor is **whether a fallback exists**, not cost: `percent_complete` substitutes for `watched_status`; nothing substitutes for a tmdb id | S | `GLD-DIS-04` |
| `GLD-TMD-03` | **Give the cache a real version stamp** — schema detection currently probes for `tmdb_id`, which works once. The next field added has no such probe unless someone writes one | S | `GLD-CACHE-04` |
| `GLD-TMD-04` | ✅ **Cite the three-way split as the reference** — fetch-failed / success-with-empty-data / present-but-no-tmdb, each routed to its own diagnostic bucket. The middle case is the one most code misses | S | `GLD-MDB-05` |
| `GLD-TMD-05` | **Read the cache generator's tail** — the incremental top-up and `rebuilt` flag logic | S | — |

## 7. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | What fraction of rating_keys actually fail per run? | `GLD-TMD-01` |
| Q2 | Do the remaining Tautulli fetchers (`users`, `devices`, `transcode`, `episodes`, `series`) use `break` or `continue` on failure? | `GLD-TWH-04` |
| Q3 | Is `not_in_metadata` vs `no_tmdb_guid` surfaced anywhere an operator sees? | `GLD-TMD-04` |

**Q2 is the remaining half of the sibling audit.** Two of seven Tautulli
sub-managers are now checked: one truncates on failure (`watch_history`, 🔴), one
skips correctly (`metadata`, ✅). Five are unchecked, and the difference between
the two patterns is a single keyword.

## 8. Related designs

- [`watch_history/DESIGN.md`](../watch_history/DESIGN.md) §5.5 — the sibling that breaks where this one continues
- [`machine_learning/affinity/DESIGN.md`](../../../machine_learning/affinity/DESIGN.md) — `build_library_index`, the backstop for §2.1's holes
- [`tautulli/README.md`](../README.md) · [`tautulli/DESIGN.md`](../DESIGN.md) — the service root

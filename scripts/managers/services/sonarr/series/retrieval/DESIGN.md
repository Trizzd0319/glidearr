# sonarr/series/retrieval — Design

> Breadcrumb: [glidearr](../../../../../..) › [scripts](../../../../../README.md) › [managers](../../../../README.md) › [services](../../../README.md) › [sonarr](../../README.md) › [series](../README.md) › **retrieval**

**Manager** — `SonarrSeriesRetrievalManager`
**Status** — 🔴 A truncating fetch, and a drift check that reports it as success
**Related** — [`tautulli/watch_history/DESIGN.md`](../../../tautulli/watch_history/DESIGN.md) §5.5

> **Coverage:** `fetch.py`'s pagination loop (via grep, session 73) and
> `validate.py` in full (session 74). `cache.py`, `enrich.py`, `sync.py`,
> `tvdb.py` unread.

---

## 1. 🔴 The fetch truncates on a failed page

```python
# fetch.py:58
response = self.sonarr_api._make_request(resolved_instance, endpoint,
                                         method="GET", retries=1, fallback=[])
if not response:
    break
all_series.extend(response)
```

`fallback=[]` is **falsy**, so a failed page terminates the loop and `all_series`
is returned **truncated, presented as complete** — on the ~8k-series library.

Structurally identical to
[`tautulli/watch_history`](../../../tautulli/watch_history/DESIGN.md) §5.5, and
the correct form is also in-repo:
`plex/metadata:205` returns `None` with the reason attached
(*"transient — allow retry on a later run"*). `GLD-SRF-01`.

---

## 2. 🔴 And the guard that should catch it compares the fetch against itself

`orchestration/series.py` calls `validate_series_count` immediately after the
refresh, which reads as a mitigation. It is not.

```python
def validate_series_count(self, instance, live_series: list = None) -> float:
    if live_series is None:
        live_series = self.sonarr_api.get_all_sonarr_apis()[resolved].all_series()
    cached_ids = self.series_cache.get_all_series_ids(resolved_instance)

    live_count  = len(live_series)      # ← the TRUNCATED list, passed in by the caller
    cache_count = len(cached_ids)       # ← the cache, written FROM that same fetch
    diff_pct = abs(live_count - cache_count) / max(live_count, 1)
    if diff_pct > 0.10:  warn
    else:                log_info("✅ Library validation passed")
```

**Both sides shrink together.** If the fetch truncates at 3,000 of 8,000, the
cache holds 3,000, `diff_pct` is **0 %**, and the run logs
**"✅ Library validation passed: live=3000, cached=3000, diff=0.00%"**.

A detector that reports success on the exact failure it exists to catch.

### 2.1 The optimisation is what broke it — and its docstring says the opposite

> `live_series`: when the caller already fetched the live `/series` list THIS run
> … pass it in to skip a redundant second full `/series` fetch of all ~8k series.
> **The drift comparison (live count vs cache count) is identical**; we just
> don't pull the same list twice.

**It is not identical.** The whole value of the `live_series is None` branch is
that its fetch is **independent** of the one under test. Two independent fetches
would rarely truncate at the same page, so a drift would surface. Reusing the
fetch under test as its own reference turns the comparison into a tautology.

So the sequence is:

1. `fetch.py` truncates silently on a failed page;
2. `validate_series_count` was the guard against exactly that;
3. a performance optimisation passed the truncated list in as the *reference*;
4. the guard now compares a value against itself and passes.

The optimisation is otherwise well-reasoned — it saves a full ~8k fetch, and
[`orchestration/series.py`](../../orchestration/DESIGN.md) §6.5.3 makes the same
saving on the cached path with sound justification. The cost simply was not
visible from either side: the caller sees "reuse the list we already have", the
validator sees "a live count was supplied". **Neither can see that the supplier
is the thing being validated.** `GLD-SRF-02`.

### 2.2 A second gap even if the reference were independent

```python
if diff_pct > 0.10:
```

A truncation losing **under 10 %** of the library passes silently regardless. On
~8k series that is up to ~800 missing before anything is said, and the threshold
is a bare literal with no stated derivation.

---

## 3. 🟡 `validate_series_tags` builds its key two ways in three lines

```python
tag_key = f"sonarr/{instance}/tags.json"        # ← raw instance, and a .json suffix
...
resolved_instance = self.instance_manager.resolve_instance(instance)   # ← next line
```

Two problems in one expression:

- **Unresolved instance.** Every other line in the method uses
  `resolved_instance`; this one uses the raw argument, computed one line later.
- **A `.json` suffix.** `CacheKeyPaths.sonarr.TAGS` is `sonarr/<instance>/tags`
  — no extension. That is a **fifth** cache-key format after the registry's
  slashes, `repair/anomaly`'s double-colons, `selector.py`'s dots, and the
  hand-built slash keys.

If the key misses, `tag_data` is `[]`, `known_tags` is empty, and **every tag
reference on every series reads as invalid** — the method would report the whole
library as broken.

That it apparently does not is itself informative: either `sonarr_cache` uses a
different convention from `CacheKeyPaths`, or **nothing calls
`validate_series_tags`**. Both are worth knowing; neither is visible from here.
`GLD-SRF-03`.

---

## 4. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SRF-01` | 🔴 **`fetch.py:59` truncates on a failed page** — `fallback=[]` is falsy, so `if not response: break` returns a partial ~8k-series library as if complete. Same shape as `GLD-TWH-04`; the fix is `plex/metadata:205`'s `return None  # transient` | S | `GLD-TWH-04`, `GLD-PMD-01` |
| `GLD-SRF-02` | 🔴 **`validate_series_count` compares the fetch against itself.** The caller passes the truncated list in as `live_series`, and the cache was written from that same fetch — so both sides shrink together and a truncation logs *"✅ Library validation passed … diff=0.00%"*. **A performance optimisation converted the guard into a tautology, and its docstring asserts the comparison is *"identical"* when independence was the entire point.** Restore an independent count, or compare against the *previous run's* cache instead | S | `GLD-SRF-01` |
| `GLD-SRF-03` | 🟡 **`validate_series_tags` uses the raw `instance` and a `.json` suffix** while the next line computes `resolved_instance` and `CacheKeyPaths.sonarr.TAGS` has no extension. A miss makes `known_tags` empty, so **every tag reference reads as invalid**. That this is not observed suggests either a different `sonarr_cache` convention or that **nothing calls the method** — establish which | S | `GLD-STO-08` |
| `GLD-SRF-04` | **Justify or route the 10 % drift threshold** — up to ~800 missing series pass silently, from a bare literal with no derivation | S | `GLD-THR-01` |
| `GLD-SRF-05` | **Read `cache.py`, `enrich.py`, `sync.py`, `tvdb.py`** — the rest of the retrieval package | M | — |

## 5. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `refresh_all_series` write the letter-bucket cache from the same list it returns? *(If so, §2 is confirmed end-to-end.)* | `GLD-SRF-02` |
| Q2 | Is `validate_series_tags` called by anything? | `GLD-SRF-03` |
| Q3 | Does `sonarr_cache` use a different key convention from `CacheKeyPaths`? | `GLD-SRF-03` |

**Q1 is the one that upgrades §2 from strong inference to fact.** The reasoning
holds if the cache is written from the returned list — which the caller's own
comment implies (*"the count-drift check needs a live count, but there's no
reason to pull all ~8k series a second time"*). One read of `fetch.py`'s body
settles it.

## 6. Related designs

- [`tautulli/watch_history/DESIGN.md`](../../../tautulli/watch_history/DESIGN.md) §5.5 — the same truncating shape, cached for an hour
- [`sonarr/orchestration/DESIGN.md`](../../orchestration/DESIGN.md) §6.5.3 — the caller, and its (sound) sibling optimisation on the cached path
- [`plex/DESIGN.md`](../../../plex/DESIGN.md) §3.4 — correct pagination, and the empty-page terminator this loop lacks

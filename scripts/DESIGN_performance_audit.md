# DESIGN — Performance & Efficiency Audit

**Date:** 2026-07-25
**Evidence base:** 187 commits (2025-03-07 → 2026-06-14), 127 archived profiler runs (`support/logs/timings.run-*.json`), the 2026-07-25 dry-run log, and code inspection with file:line references.

> **Provenance caveat:** this working tree's git head is 2026-06-14. The 2026-07-25 run log shows newer features (pilot watchability gate, monitor-by-watchability) that exist only in the live repo (`PycharmProjects\glidearr`). Every finding below was verified against *this* tree's code AND corroborated by the live log where possible; line numbers may have drifted slightly in the live repo, mechanisms have not (the log reproduces all of them: 3× instance pulls, 5× parquet saves, cache stampede, 77s pilot herd, 50s acquire stall).

---

## 1. Project history, briefly

| Period | Activity |
|---|---|
| 2025-03 → 2025-07 | Recommendarr born; 31 commits — core Radarr/Sonarr/Tautulli/Trakt plumbing |
| 2025-08 → 2026-05 | Near-dormant (3 commits) |
| 2026-06 | **153 commits in one month** — scoring engine to ML "brain" layer, space coordinator, JIT upgrades, pilot pipeline, end-of-run report, English-audio ladders, secrets backend, Glidearr rebrand prep |
| 2026-07 | Live repo continues (watchability gates, monitor-by-watchability); public release prep |

Most-churned files are exactly the hot spots this audit lands on: `sonarr/cache/episode_files.py` (29 changes, ~5.3k lines), `radarr/quality/space_pressure.py` (20), `radarr/quality/universe.py` (16). Codebase: 589 Python files, ~84k lines.

## 2. Where the time actually goes

Run-time distribution across the 127 profiled runs: **steady-state runs are 45–65s**, but roughly every 48h a "herd run" spikes to 2–12 minutes (run-115: 190s, run-126: 309s, run-127: **728s, 91% CPU-bound**). Today's live run showed the same shape (pilot herd 77s, owned-episodes 33s, acquire stall 51s, full series fetch, cache stampede — several minutes total).

Top self-time sinks, cumulative over the last 20 archived runs:

| Sink | Total (20 runs) | Mechanism |
|---|---|---|
| `run_reconcile > _get_all_episodes` | 706s (317,313 calls) | per-series episode fetch, serial, no session reuse |
| `run_active_watcher_upgrades > get_series_by_id` | 547s (23,417 calls) | **O(N) linear scan per lookup** |
| `run_pilot_search` (self) | 532s | per-stub CPU loop, dry-run never persists search stamps |
| `refresh_scores > _build_show_score_map` | 279s | per-row Python scoring, 211k people-cache gets |
| `run_monitoring/movie_data_pull` | 266s | **O(N²) instance pulls** + full-library JSON rewrites |
| `sync_from_tautulli > get_series_by_title` | 137s (8,710 calls) | **O(N) linear scan per lookup** |

All-time (127 runs) the same names dominate: `run_pilot_search` 3,492s, `_get_all_episodes` 1,395s / 493k calls, `get_series_by_title` 836s + 681k `load_letter_cache` hits, `get_series_by_id` 556s, pilot batch `_get_episode_files` 469s / 88.7k calls.

---

## 3. Findings, ranked by expected payoff

### P1 — Series lookups are O(N) linear scans; build indexes (biggest CPU win)

`SonarrCacheSeriesManager.get_series_by_title` (`sonarr/cache/series.py:327-334`) walks **every series in every letter bucket** per call. `get_series_by_id` (`sonarr/series/retrieval/fetch.py:71-79`) iterates all 37 buckets scanning for an id match. At ~12k series, each lookup is a full pure-Python scan; `run_active_watcher_upgrades` made 23.4k such calls in 20 runs (547s) and Tautulli sync 8.7k title lookups (137s).

**Fix:** build `{id: series}` and `{title_lower: series}` dicts once per run in `SonarrCacheSeriesManager` (invalidate on bucket write — same lifecycle as the existing `_bucket_memo()` at `series.py:127`). ~30 lines.
**Saving:** ~25–35s on typical runs, far more on heavy ones. Radarr's movie caches deserve the same check.

### P2 — Owned-episodes/reconcile path: 12k serial cache reads per run

`SonarrCacheOwnedEpisodesManager.build_or_refresh` (`sonarr/cache/owned_episodes.py:103-142`) is an unconditional full rebuild: every series → `_get_all_episodes` (serial, one JSON global-cache read each, live GET on 24h expiry) → full parquet rewrite. 35s/run average in the last 20 runs; 493k calls all-time. The concurrent warmer that already exists (`episode_files.py:1397`, 8 workers, used by pilot search) is *not* used here, and the `season_ep_cache` built in `sync_from_tautulli` (`episode_files.py:5130`) is never handed to `_do_acquire_next_episodes` (`:5236`), which re-fetches the same seasons seconds later.

**Fix:** (a) thread one per-run episode session cache through owned-episodes, reconcile, and acquire; (b) parallelize the rebuild with the existing warmer; (c) skip rebuild when the series delta was ±0 and episode caches are fresh.
**Saving:** 25–30s/run.

### P3 — Pilot stub herd: 7.7k serial API calls every 48h, no cap, no spread

`run_pilot_batch`: TTL is hard-coded `CACHE_MAX_AGE = 172_800` (`episode_files.py:94`); stale stubs are all re-checked in one run (`:3337`), `PILOT_BATCH_SIZE = None` = unlimited (`:93`), each stub a live uncached `GET episodefile?seriesId=N`, serial with 2s retry sleeps (`:1464-1487`). Refreshing the timestamp on re-check (`:3439`) guarantees the cohort expires together again — a permanent 48h stampede (77s today; 469s all-time).

**Fix:** cap stale re-checks per run (e.g. 1,000) so the herd amortizes, add ±10% jitter to the stub TTL, and run re-checks through the existing 8-worker warmer.
**Saving:** converts a recurring 77s spike into ~10s/run background noise.

### P4 — Radarr orchestration is O(N²): every pull loops all instances, then rewrites full-library JSON

`RadarrOrchestrationManager.run` iterates instances (`orchestration/__init__.py:607`), but **every** `run_*_data_pull` ignores its `instance` argument and re-loops all instances internally (`:91-99`, `:103-138`, `:142-156`, `:160-168`, and 7 more). 3 instances → 9 executions of each pull — the log's triple "Pulled 24848 movies" block. The HTTP layer is memoized 900s (`base_instance_manager.py:74-78`) *but* mid-run PUTs invalidate it, and the disk layer rewrites `radarr.movies.standard.full` — **24,848 movie dicts as `indent=2` JSON — nine times per run** (`orchestration/__init__.py:98` → `json_handler.py:107-108`).

**Fix:** honor the `instance` argument (one-line change per method), and write the disk snapshot once per instance per run without `indent` for multi-MB payloads.
**Saving:** ~10–15s/run + large disk-churn reduction.

### P5 — Free-space calls: uncached, repeated, and capable of 30s stalls

`_do_acquire_next_episodes` gates correctly *before* per-episode work, but the gate itself costs 4 uncached HTTP GETs — `_get_free_space_gb` + `_get_total_space_gb` each hit `rootfolder` + `diskspace` (`episode_files.py:2507,2515`, `:4081-4082`); neither endpoint is in `_CACHEABLE_GET_ENDPOINTS` (`base_instance_manager.py:77`). The retry ladder (8 retries, exp backoff, `base_instance_manager.py:432-460`) makes ~30s reachable per GET when the array is busy — today's 51.2s "acquire" that did nothing was exactly this. JIT, pilot search, storage checks, and space pressure each re-pay the same calls.

**Fix:** run-scoped memo for `diskspace`/`rootfolder` per instance (fetch once, share across all consumers; they're called a dozen+ times per run).
**Saving:** removes multi-second-to-50s stalls; typical 5–10s/run.

### P6 — Parquet full rewrites: 5–7× per instance per run (Sonarr), 4–8× (Radarr)

`episode_files.save()` (`episode_files.py:528-557`) sorts and rewrites the whole ~12k-row frame every call; pipeline calls it from pilot batch, sync, JIT, refresh_scores, refresh_enrichment (see call sites `:3479, :5359, :4331, :615, :682`). Radarr's `movie_files.save()` same pattern (`movie_files.py:523-540`; sites at `:938`, `space_pressure.py:1206`, `universe.py:558`, +conditionals). No dirty flag.

**Fix:** dirty-flag + one consolidated save at pipeline end (context-manager or explicit `flush()` at the last stage); intermediate stages mark dirty only.
**Saving:** a few s/run, but the bigger win is crash-consistency clarity and SSD churn.

### P7 — Cache TTL stampede by construction

`GlobalCacheManager.get_or_generate_cache` compares bare mtimes (`factories/cache/__init__.py:112-116`); no jitter exists anywhere in the cache layer. Keys written together expire together — today's run regenerated `tautulli/history/all`, `metadata/index`, `trakt/history/movies`, and 5 per-user histories in one shot. Serve-stale keys (`regenerate_on_expiry=False`) never rewrite, so their mtimes freeze and logged "ages" (1.27M s) grow unboundedly — cosmetic but misleading.

**Fix:** ±10% deterministic jitter on the TTL comparison (hash of key), and log serve-stale keys distinctly.
**Saving:** flattens the periodic regeneration spike; honest logs.

### P8 — Sonarr full series fetch: 24h gate exists but likely never engages

`refresh_all_series` has a proper 24h timestamp gate (`fetch.py:192-214`) — but the timestamp is only stamped on the persistence path (`:269-272`), which is skipped when `self.manager` is the default `{}` (`:18`, early-return `:236-240`). Gate reads one object (`self.sonarr_cache.series`, `:203`), persistence uses another (`self.manager.series_cache`, `:196`) — a split-brain: the count check passes, the stamp never lands, so every run pays the 30–60s full fetch for a +16/−1 delta.

**Fix:** unify the two references (or stamp on the gate-read object). Verify with two consecutive runs: second should log a cache-hit skip.
**Saving:** 30–60s on all but one run per day.

### P9 — Pilot search: dry-run never persists its own throttle

`run_pilot_search` saves only when `changed and not dry_run` (`episode_files.py:4060`), so `pilot_last_searched_at` is never written in dry-run — the 24h interval guard can't engage and the full loop re-runs every time (`0 skipped recent` in every archived log). Combined with per-stub scoring, this is 26.6s/run in the last 20 runs and 3,492s all-time — the single largest named sink.

**Fix:** persist search stamps in dry-run too (they're bookkeeping, not a write to Sonarr), or short-circuit grading for stubs already searched <24h ago. The live repo's watchability gate (holds 7,460 stubs) helps the *search* count but the grading loop still walks everything each run — grade incrementally (only stubs whose inputs changed).
**Saving:** 15–25s/run.

### P10 — Show scorer: 211k cache gets per 20 runs inside the per-row loop

`_build_show_score_map` → `_score_row` (219k calls all-time) with `get_people` cache lookups inside the row loop (`_build_show_score_map>get_people`: 211k calls / 20 runs). 13–14s/run of pure Python.

**Fix:** prefetch people/affinity into plain dicts before the loop; vectorize the arithmetic signal groups over the DataFrame where possible (the roadmap already lists "vectorized batch scoring" — this is the concrete target).
**Saving:** ~10s/run.

### P11 — Boot: config parsed 3×, keyring walked 3×

`main.py:647` (onboarding's throwaway loader), `main.py:655` (`ConfigManager`), `main.py:45` (`reload()` — unconditionally redundant). Each `load()` re-walks the secret tree hitting Windows Credential Manager per secret (`config_loader.py:30-58,64-74`).

**Fix:** hand onboarding's result to `ConfigManager`; delete the `reload()`. Trivial.
**Saving:** seconds at boot; less DPAPI noise.

### P12 — Enrich daemon: backlog math and scope

650 calls/306s, 7 endpoints/movie (`daemon_paths.py:85,97,100`) ⇒ ~93 fresh movies/cycle ⇒ the 24.8k-title library is a ~22-hour tail even before shows — and the daemon fully pauses during main runs (`enrich_daemon.py:1252-1261`). Watched-first tiering is already on (`:823,:892-894`), good. The run-log ETA ("~2.1h") counts only owned movies (`orchestration/__init__.py:446-450`) while the "24,848 unenriched" line counts the whole library — two different populations in one table.

**Fix:** trim scope for *unowned* titles to `["summary","people"]` (the two the scorer actually consumes) — 3.5× faster tail coverage; label the ETA row "owned".
**Saving:** days→hours on cold-start enrichment; unblocks the 1,656 "deferred: Trakt credits not cached" decisions sooner.

### Instrumentation gap (meta)

`ROOT>run` self-time totals 5,896s all-time — I/O sitting in `run()` bodies with no timed child. Wrap the remaining inline API calls (notably `_make_request` consumers in run bodies) so future audits attribute this residue.

---

## 4. Order of attack

**Implementation status (2026-07-25):** items 1–5 below are DONE in this tree — P1 (index in `cache/series.py` + delegation in `retrieval/fetch.py`), P8 (unified gate/persistence object in `refresh_all_series`), P5 (`_SPACE_GET_ENDPOINTS` 60s tier in `base_instance_manager.py`), P4 (`_pull_targets` honoring the instance arg + `pretty=False` on the two whole-library JSON writes), P9 (`pilot_last_planned_at` column, dry-run-only throttle + persisted save). P11 was investigated and intentionally skipped: onboarding/secret migration may legitimately rewrite `config.json` between the two loads on a first boot, so the redundant `reload()` stays until that path is provably inert. Port note for the live repo: P9 touches `run_pilot_search`, which has drifted upstream (watchability gate) — re-apply the three small hunks by hand there.

| # | Change | Effort | Typical-run saving | Herd-run saving |
|---|---|---|---|---|
| 1 | P1 id/title indexes | S | 25–35s | 60s+ |
| 2 | P8 fix series-fetch gate wiring | S | 30–60s | 30–60s |
| 3 | P5 run-scoped diskspace memo | S | 5–10s | up to 50s |
| 4 | P9 persist pilot-search stamps in dry-run | S | 15–25s | 15–25s |
| 5 | P4 honor instance arg + single JSON write | S | 10–15s | 10–15s |
| 6 | P3 cap+jitter+parallelize stub re-checks | M | — | ~70s |
| 7 | P2 shared episode session cache + parallel rebuild | M | 25–30s | 60s+ |
| 8 | P6 dirty-flag saves | M | 3–5s | 5–10s |
| 9 | P10 vectorize show scorer | M/L | ~10s | 30s+ |
| 10 | P7 TTL jitter | S | smooths spikes | smooths spikes |
| 11 | P11 config boot dedupe | S | ~2s | ~2s |
| 12 | P12 daemon scope trim | S | (background) | (background) |

Items 1–5 are an afternoon and should roughly **halve the steady-state run (45–65s → ~20–30s)** and cut herd runs from 5–12 min to ~2 min. Items 6–9 get steady-state under ~15s.

**Measure:** the timings archive is already perfect for before/after — compare `summary.top_10_by_wall_s` and total wall across 5 runs pre/post each change. Watch for the run-127-style blowup (728s, 91% CPU in `run_full_enrichment`) disappearing once P1+P2+P9 land.

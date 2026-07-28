# Performance baseline — 2026-07-26

Compact record of the optimisation campaign, extracted before the raw profiler
dumps were deleted. The dumps are ~25 MB each (a full call tree per run); these
numbers are the part worth keeping. Regenerate any row with
`scripts/support/logs/timings*.json` after a run.

## Campaign: 244.9s → 39s (6.3x)

| stage | total | cpu | refresh_scores | show scoring (self) | plex phase |
|---|---|---|---|---|---|
| pre-fix baseline | 244.9 | — | — | — | 29–32 |
| after quick wins (P1/P4/P5/P8/P9) | 71.6 → 57.4 | — | — | — | ~23 |
| after P2 (incremental owned-episodes) | 55.6 | — | — | — | 28–32 |
| after dry-run search deferral | 39.3 | 36.7 | — | — | ~23 |
| collection fix + memo reseed (worst) | 211.2 | 205.2 | 193.6 | 175.8 | 19.5 |
| fingerprint keys (memo finally hits) | 72.7 | 70.3 | 57.0 | 46.1 | 23.7 |
| **batched keys (steady state)** | **39.3 / 38.8** | 36.7 | 5.5 | **0.0** | 21.8 |

Steady-state profile is FLAT — no single sink above ~10s. Main root ~39s splits
across relational pull ~7.6, repair scans ~9.8, scoring ~5.5, Radarr data pulls
~9; the Plex phase (~22s, ~10s cpu) runs in parallel and is mostly API wait.

## What moved the needle, in order

1. **Show-score memo keyed on cache *contents*** — the three per-series Trakt
   payloads (people/ratings/related, one gzip+JSON file each) were READ to build
   the key, before the memo check. ~36k decompressions per run regardless of hit
   rate; the memo could never save the I/O it was keyed on. Fixed with
   `TraktShowCacheManager.fingerprint()` (stat-only: size, mtime_ns, freshness)
   and by deferring the reads to misses. 46.1s → 0.0s self-time.
2. **Key construction at 12k series** — ~36k individual `stat()` calls (brutal on
   Windows/Defender) plus 12k `hash_pandas_object` calls. Batched to one
   `os.scandir` per bucket + one vectorised hash. NOTE: `list(series.values)`
   keeps numpy scalars so the JSON key renders identically; `.tolist()` emits
   bare ints and silently invalidates every memo entry.
3. **Row hash covered the whole row** — the pilot batch rewrites `date_added` on
   ~7.7k stale stubs every 48h, so most series looked "changed" while their score
   inputs were identical. Hash only the 9 columns `show_features.py` reads.
4. **Plex builders re-walked collections** — the combined builder repeated the TV
   and movie builders' universe scaffolding. Shared the I/O (collection listings,
   children, mdblist lists) per run while leaving local computation per-call, so
   each builder still sees its own current state. Plex HTTP 134 → 58 calls.
5. **O(N) series lookups, O(N²) Radarr pulls, uncached free-space GETs, a
   freshness gate that never engaged** — the original quick wins.

## Gotchas worth not relearning

* The profiler writes ~25 MB per run. Five retained dumps = 125 MB, the largest
  consumer under `scripts/support/`. Consider capping retention.
* `run_reconcile` in the profile is the WHOLE post-*arr Plex phase (three
  playlist builders + shelf + write-back), not the cheap set-diff reconcile.
* Profiler roots overlap (parallel threads): wall total ≠ sum of self-times.
* Per-series show scoring rose ~1.8ms → ~18ms once the enrich daemon populated
  credits/ratings — the work got more expensive because there was finally real
  data to score, which is what makes the memo valuable.

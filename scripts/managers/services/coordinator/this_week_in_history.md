# "This Week in History" discovery

**Goal.** Surface anniversary content — movies and TV episodes RELEASED/AIRED during the current
Sun–Sat calendar week in ANY past year — acquire the most watchable into a rotating, self-replenishing
**weekly trial shelf**, and learn from what the household actually watches. It introduces titles people
wouldn't otherwise find; when they engage, acquisition + recommendations pivot toward it. The shelf is
ephemeral by construction, so the disk footprint self-cleans every week.

**Status.** Designed, NOT built. Several capabilities this leans on are net-new rather than reuse (flagged
below), and the destructive paths need safety gates before anything grabs or deletes — the phasing
front-loads both.

## Concept
- **Window.** today → its Sun–Sat week → seven concrete dates → match a title when its release/air
  `(month, day)` is in that SET in ANY year (see Window logic — this is the most bug-prone primitive).
  Filter to ALREADY-RELEASED (the title's real historical date ≤ today; this-year FUTURE anniversaries excluded).
- **Two COMBINED playlists, per user, OPT-IN (default OFF).** `Released This Week in History` = every
  movie library merged; `Aired This Week in History` = every show library merged. Generated only for users
  who opt in (or show a real consumption signal) — not N×2 always-on playlists for passive accounts.
- **Net-new FIRST; owned is free fallback.** The shelf prioritizes UNOWNED candidates (the point is net-new
  finds). Owned titles that also aired this week fill only the slots net-new can't, and are FREE (no grab,
  no budget spend, no purge) — but pass the SAME per-user age/library gate as acquisitions.
- **Trial, not a library add.** Net-new grabs live for ONE week; unwatched ones are reclaimed at week-end;
  only earners graduate (watched, saga-absorbed, watchability-promoted, or user-SAVED).

## What's reusable vs net-new
**Genuinely reusable:** the acquisition scorer scores an UNOWNED candidate with no change
(`acquisition/scorer.py`); the cert/age gate (`playlists/cert_gate.py:cert_allowed`, fail-closed); the
destructive gates (`backup_gate.effective_dry_run` degrade-to-dry-run, `space_targets.deletions_enabled`);
`storage/space.disk_free_gb` + `space_targets` (free + reserve); the universe-acquire deferred signal
(`plex/playlists/universe_acquire_preview`).

**Net-new — must be built:**
- **Two playlists** — writeback's `_UP_NEXT` is *precedence* (one playlist/user). Need two NEW writeback
  families + plan-cache keys + a builder (`writeback._all_families` shows the pattern — mood/Fresh Arrivals).
- **Per-user LIBRARY-access scope + a PRE-grab gate** — only a cert/age gate exists, applied POST-build on
  the OWNED set; `ensure_owned_and_grab` does no cert check. Both the allowed-library dimension and the
  pre-acquisition gate are net-new.
- **An ISOLATED discovery-affinity layer** — the affinity/people matrices are pure tallies into ONE shared
  model (`tautulli/affinity`, `people_matrix/affinity`) that only ADDS non-negative weight and is ALSO read
  by `space_pressure` for deletions. No injection point for a weighted hit, no representation for a negative.
- **A foreseen-committed-GB accumulator** — `SPACE_ESTIMATES` is just the rootfolder list; the deferred
  backlog is never GB-summed.
- **Week-boundary detection + idempotent rollover** — the app is stateless, run by an EXTERNAL scheduler;
  no weekday/cron/last-rollover logic exists.
- **GB conversion of franchise demand** — it's exposed as a TITLE count (and only when the universe feature is on).

## Locked decisions
- **Adaptive budget (GB), franchise-first with a RESERVED discovery floor.** `budget = clamp(min(C_consume,
  C_space) × Q × D_backlog, 0, HARD_MAX)`; `discovery_cap = max(floor_min, budget − franchise_demand)` so
  franchise can claim at most `(1 − floor_share)` and discovery is guaranteed ≥1 when `budget ≥ 1` and
  `Q > 0`. Charge only THIS-RUN franchise grabs (`max_per_run`), not the whole deferred backlog. Log when
  discovery is forced to 0 so the inert state is observable.
- **Rolling shelf; occupancy decoupled from completion.** `cap` = shelf SIZE; a ranked queue sits behind it.
  Refill a freed slot for ANY terminal reason — completed, dismissed, a per-title max-dwell elapsed, or a
  never-touched slot stale > K days — NOT only on ≥-threshold completion (else partial-watch/abandon ratchets
  the shelf empty). The completion event still drives the LEARNING signal; a separate occupancy controller
  drives footprint.
- **Saturday-midnight rollover = purge + graduate, idempotent + seed-aware.** At the TZ-aware week boundary:
  clear the queue, DELETE unwatched trials to reuse the space — under the backup / deletions-armed / dry-run
  gates, only on TAGGED discovery items, with the purge-correctness checks below. Graduates (kept): watched
  ≥ threshold, in-progress (any playback > 0), user-SAVED, saga-absorbed, or watchability-promoted past the
  keep-bar. Persist `last_rollover (year, ISO-week)`; catch up a missed run; if the gate degrades to dry-run,
  record the purge as OWED.
- **Watched / saved discovery = strong signal, ISOLATED.** A hit feeds a SEPARATE `discovery_affinity` layer
  (own cache key) read only by shelf ranking, and nudges `discovery_share`. The PRIMARY shared model is fed
  ONLY by real ≥-completion watches (which already flow through Tautulli history at weight 1) — NEVER by a
  skip or any discovery-derived negative, which must never reach the deletion path.

## The adaptive budget formula
```
budget        = clamp( min(C_consume, C_space) × Q × D_backlog,  0,  HARD_MAX )   # in GB
discovery_cap = max(floor_min, budget − franchise_demand_GB)                      # franchise-first + floor
```
| term | meaning | signal |
|---|---|---|
| `C_consume` | `weekly_MOVIE_watch_rate × discovery_share` | **Tautulli completions**, NOT Sonarr `is_watched` (tracking-sparse → fake backlog → ships dead). Movie-specific rate. |
| `C_space` | `(free − reserve − foreseen_commit) / avg_title_GB` | `disk_free_gb` + `space_targets` (reuse); `foreseen_commit` = **sum of deferred-backlog GB + this-run pending grabs** (build). `avg_title_GB` is media-specific (movie ≠ episode). |
| `Q` | candidates clearing the watchability floor ÷ target pool | scorer over candidates; floor is a HARD precondition (below) |
| `D_backlog` | weeks-of-unwatched throttle | **Tautulli play history**, never `is_watched` |
| `HARD_MAX` | absolute slot-count safety ceiling | const |
- Pick ONE currency: the budget is GB; at fill time draw candidates until EITHER the GB budget OR the
  `HARD_MAX` slot count binds (`min()` of both). "Footprint ≤ cap" means ≤ the GB budget.
- Fail-safe: any single ceiling → 0 (disk full, nobody watching, weak week). Add a self-test proving the
  budget does NOT collapse to 0 on the current sparse `is_watched` data — i.e. that it reads the Tautulli source.

## Anti-staleness — exposure decay + novelty
A per-`(user, key)` exposure ledger keeps the queue-top from silting up with titles that roll over un-watched.
- **Decay only the SKIPPED-while-VISIBLE, and only on REPEATED skips.** A title needs ≥2 visible weeks before
  any penalty — a single skip at a ~3-of-370 watch rate is overwhelmingly "didn't get to it", not "disliked".
- **Attenuate decay when `watch_rate << shelf_size`** (the household physically can't watch them all, so most
  skips aren't signal) — otherwise decay becomes an anti-EVERYTHING mechanism and oscillates.
- **Separate STABLE quality (scorer output) from the TRANSIENT exposure penalty**, so a good-but-bandwidth-skipped
  title doesn't sink below mediocre novelty.
- **Re-grab COOLDOWN gates the GRAB, not just the sort.** Persist `last_grabbed_week` / `grab_count`; HARD-suppress
  re-acquisition of a purged-unwatched title for a cooldown measured in YEARS (anniversaries recur annually);
  a title purged-unwatched ≥2× becomes owned-fallback-only (never auto-grabbed again). Rank recovery must NOT
  lift this suppression.
- **Series-level for TV — but surface the PILOT** (or next-unwatched) as the entry point, not a random
  out-of-context mid-run anniversary episode; decay then reflects a fair "want to start this show?" decision
  (reuse the pilot-search infra). **Title-level for movies.** **Per-USER.** Floored + slowly recoverable.
- **Novelty + CLOSED-LOOP diversity.** A never-shown candidate gets a boost; the diversity term is a RANK-level
  penalty on over-represented genres (not just a shelf-slot cap) and auto-raises when recent-shelf genre
  ENTROPY drops — so it can actually counteract the compounding affinity signal instead of losing to it.

## Window logic (most bug-prone primitive — explicit policy)
- Build the seven concrete Sun–Sat dates in ONE pinned household TZ (configurable, default PMS/local). Match a
  candidate by `(month, day)` SET membership against those seven pairs — year-agnostic, so month- and
  year-boundary WRAP is free (no tuple range compares).
- **Released check** = the title's REAL historical air/release date ≤ today. NEVER construct
  `date.replace(year=today.year)` (it wrongly excludes a Jan-2 title in a Dec→Jan straddling week).
- **Feb 29**: map Feb-29 anniversaries onto Feb 28 in non-leap windows so they can surface every year.
- Convert each candidate's `air_date_utc` to the household TZ BEFORE extracting `(month, day)`. Rollover is a
  TZ-aware instant via `zoneinfo` (DST-safe), and window-open + purge use the SAME "now".
- EXCLUDE null/sentinel `air_date_utc` and season-0 specials (the widened Sonarr pull resurfaces TBA
  placeholders that parse to a bogus Jan-1 and flood the New-Year window).
- Tests: May 28–Jun 3, Dec 29–Jan 4 (today=Dec 30, a prior-year Jan-2 title MUST surface), Feb 26–Mar 4
  (non-leap), Feb 26–Mar 3 (leap), an exact-Feb-29 title, a UTC-12…UTC+14 household, a DST-transition Saturday.

## Candidate generation
- **Movies** — Radarr DB (~24k, ~370–470 unowned/week). **Owned + monitored TV** — `owned_episodes.parquet`
  `air_date_utc`; widen the Sonarr pull past `has_file`. **Unowned-TV — deferred** (TVDB/Trakt episode-calendar harvest).
- **Ownership key** = `tmdbId` (movie) / `(tvdbId, season, episode)` (episode) — NEVER a title-string join
  (remakes/same-name collisions). "Owned anywhere" is a GLOBAL set across all instances.
- **Watchability floor is a HARD precondition, fail-CLOSED.** Score candidates BEFORE shelf eligibility;
  exclude any that can't be scored above the floor. Anchor a minimum-popularity prior (TMDb votes/rating)
  distinct from personalized watchability, so an obscure title nobody anywhere watched never surfaces. Cache
  scores (no cold ~400-title pass per user per week).

## Safety
- **Per-user appropriateness (load-bearing once libraries are combined).** Build each viewer's candidate set
  from an ALLOWLIST of that viewer's permitted libraries (filter at GENERATION — an adult title is never even
  a candidate for a kid), age-gate fail-CLOSED, plus a pre-grab assertion against the most-restrictive user in
  scope. Owned FILLERS pass the identical gate. (The age-gate has a prior fail-open history — so defense in depth here.)
- **Affinity isolation from the DELETE path.** The discovery signal lives in its own layer; the primary
  `tautulli/affinity` (which `space_pressure` uses to choose deletions/downgrades) NEVER receives a
  discovery-derived delta — especially never a negative. A transient "skipped an obscure sitcom" must not make
  an OWNED sitcom deletion-eligible.
- **Seeding/ratio + pipeline.** Before purging a torrent-sourced trial, check the download client for
  min-seed-time / ratio not-yet-met → DEFER that delete (hit-and-run = bans); prefer usenet for discovery
  grabs. Add a shared pipeline-throughput ceiling (indexer rate / download-client queue depth) so discovery
  doesn't saturate real-library + franchise grabs.
- **Purge correctness.** (a) In-progress exemption — any playback > 0 on a trial item → HELD, not deleted
  (a graduation path), capped to ~2 grace weeks. (b) Atomic check-and-delete with a freshness/grace margin —
  quiesce active sessions, re-read watch state immediately before each delete, and treat any play in the
  trailing N minutes as a graduate (defends the Tautulli-flush-lag + boundary race). (c) Competing-claim check
  — tag is necessary-not-sufficient: skip if the item is now in a monitored saga/backfill set, referenced by a
  pending user request, or present in a real (non-trial) library path / carrying a non-discovery tag. (d)
  Never-completed download is a THIRD state — cancel cleanly from the *arr queue, do NOT apply exposure decay
  (it was never playable/visible). On any ambiguity: downgrade-not-delete (per `catchup_retention`) or skip
  and re-evaluate next rollover.
- **Discovery OUT of the shared deferred backlog.** A discovery grab that can't go now is just skipped (it's a
  trial) — never persisted into the single 500-cap `acquisition/deferred_search` (where it could evict
  real-library wants, and `_flush_deferred` could later re-grab an already-purged item). If a discovery backlog
  is wanted, it's its OWN bounded queue that the rollover scrubs.
- **Rollover idempotency.** Persist `last_rollover (year, ISO-week)`; on startup run a catch-up if the week
  advanced; delete per-item (resume cleanly after a crash); if the destructive gate degrades to dry-run, record
  the purge as OWED so the next armed run completes it.

## UX
- **Default OFF, per-user OPT-IN** (auto-offer to users above a consumption floor); skip generation + writeback
  entirely for opted-out / zero-consumption accounts.
- **A one-click SAVE/KEEP** that promotes a shelf item to real-library retention before the purge — user-driven
  graduation, the STRONGEST engagement signal, and the direct answer to "you grabbed it then deleted it" loss
  aversion. Saving feeds affinity like a watch.
- **Forewarn churn:** an "expiring Saturday" label on items in their final ~48h, and a metadata-only
  "rolled off last week" list so a missed title is re-findable (no disk).
- **Make it inviting + show the hook.** Rename to something enticing/unambiguous (e.g. "Anniversary Picks for
  <name>" / "On This Week: <year> Throwbacks") and surface the per-item hook ("aired 15 years ago this week") —
  free metadata, the feature's whole charm. Keep the shelf small/curated (5–7), ordered by watchability desc.
- **TV entry point = the pilot** (or next-unwatched), not a random S6E14 anniversary.

## Phases (front-load net-new infra + safety)
| # | Phase | Effort |
|---|---|---|
| 1 | **Window engine** (TZ-aware, set-membership, Feb-29 / year-boundary / null-date handling, tested) + movie/owned-TV candidate gen + ownership-key dedup + fail-closed scoring/popularity floor | medium |
| 2 | **Per-user gating** (allowed-library allowlist + pre-grab age gate) + the **two new writeback playlist families** + the SAVE action; render the shelf with a FIXED conservative cap, NO acquisition/delete yet (read-only preview of net-new picks) | medium |
| 3 | **Bounded acquisition** (own discovery queue, NOT the shared backlog; pipeline ceiling) + the rolling shelf occupancy controller | medium |
| 4 | **Saturday rollover** — week-boundary detection + idempotent purge under the destructive + **seeding-aware** + purge-correctness gates | medium |
| 5 | **Adaptive budget** (Tautulli-sourced C_consume/D_backlog, GB+foreseen-commit, franchise floor) + stress-test (disk full / zero rate / all-weak / huge deferred → all → 0) BEFORE it governs grabs | medium |
| 6 | **Learning loop** — ISOLATED discovery-affinity, `discovery_share` (floored + EMA + exploration, opportunity-normalized), exposure-ledger re-grab cooldown, closed-loop diversity, cold-start seed | medium |
| 7 | **Unowned-TV** episode-calendar harvest (TVDB/Trakt) | large |

## Risks / still-open
- Calibrating the budget signals against the household's REAL Tautulli consumption (not `is_watched`).
- Tuning decay × recovery × novelty so the fixed point is a stable shelf, not a limit cycle.
- The seeding-aware purge depends on download-client introspection that may vary by client.
- Cold-start households (no history) get a popularity-prior bootstrap, not personalization.

## See also
- `universe_acquisition.md` (the franchise backfill this yields to AND feeds via engagement),
  `catchup_retention.md` (retention graduates fall under; downgrade-not-delete), the acquisition scorer,
  `cert_gate` (rating gate), `space_pressure` / `space_targets` (free/reserve + the shared affinity the
  isolation protects), `backup_gate` (destructive gates), plex playlists writeback (the per-user shelves).

# Glidearr Enhancement Register

**Status** — Living document. The single index of every improvement identified during the documentation sweep.
**Scope** — `scripts/**`
**Last updated** — 2026-08-13, cache/restore/seed sweep (✅ **`GLD-EPF-14`** file facts froze at row insertion — `sync_from_tautulli` re-read watch stats and stamped `last_synced_at` but never re-resolved the FILE, so Loki read 6.7 GB at 480p against 67.4 GB of 2160p with zero id overlap · ✅ **`GLD-CACHE-14`** `expiration_time` only ever logged; every `by_series` payload frozen at first write since 08-08 · ✅ **`GLD-TVQ-01`** a delete-scoped keep tag also vetoed downgrade · ✅ **`GLD-RST-01`/`-02`/`-03`/`-04`/`-05`/`-06`** restore identity + the hardlink-pinned reclaim split and seed gate; `GLD-RST-03` was **P-I, built-caller-unwired** and is now wired into `_targeted_restore`. Previous: 2026-08-10, cache singleton hardening (**`GLD-CACHE-13`**) and the registry diagnostics sweep)

> **Read §8 first if you are continuing the sweep.** It records the *recurring
> defect patterns* found so far — the shapes that keep reappearing in different
> folders. Knowing them turns the remaining sweep from discovery into
> confirmation. **P-A through P-G are defect patterns; P-H is a design instinct
> worth preserving; P-I predicts where other defects hide; P-J is the single most
> productive predictor in this register.** ⚠️ **And read the Calibration note at
> the end of §8 before triaging by severity** — nine findings filed on an
> *absence* have been downgraded after a deeper read, and none has been upgraded.
> **§8 P-G in particular describes a failure mode this sweep has already committed
> twice** — read it before repeating any figure from another document.

---

## P-J — the twin asymmetry

**Two pieces of code do the same job for different media (movies/TV), different
services (Radarr/Sonarr), or different halves of one pair — and only one of them
got the fix.** Eight confirmed instances, every one a live defect:

| # | Twins | The one that was wrong |
|---|---|---|
| 1 | `warm_cache` Radarr / Sonarr | Sonarr passed the raw `<instance>` template — cache never read (`GLD-STO-01`, ×2 files) |
| 2 | `trakt/history`'s three fetchers | `get_full_watch_history` conflated failure with end-of-data; its two siblings already returned `None` (`GLD-THY-01`) |
| 3 | `dry_run` capture, 5 managers | `writeback`, `calendar`, `radarr.selector`, `radarr/repair/anomaly`, `radarr/storage/space` all clobbered the parent value after `super()` (`GLD-ORCH-02`) |
| 4 | `auto_rate_watched_shows` / `_movies` | The shows loop guarded a null progress entry; the movies loop did not |
| 5 | `_NUMERIC_COLUMNS` / *(no string twin)* | An all-null Parquet column returns `float64`; assigning an ISO string **raises**. Guarded at 4 write sites, missed at 3 (`GLD-EPF-01`) |
| 6 | `score_movie(credits=…)` callers | `repair/anomaly` passed `{}` per the documented contract; `trakt/ratings` passed `None` (`GLD-TRT-01`) |
| 7 | `THRESHOLD_SPECS` / `likelihood.untouched_base` | A re-anchor pass walking only the specs misses the constant on the other scale |
| 8 | Sonarr / Radarr `anomaly.py` | Same filename, unrelated modules — one a set-diff scanner, one the delete engine |

### Why it is the most productive pattern here

The others describe defects **already found**. This one **predicts**: given a
failing method, the first question is *"does a sibling do this job for the other
media type, and does it do it differently?"* That question found #4, #5 and #6 —
and #6 had been silently disabling an entire feature (`auto_rate_watched_movies`
died on its first movie, **every run, on every instance, for months**) while its
shows twin logged normally in the same pass.

**Check the sibling before reading the failing code.**

---

## Instrument errors — three ways I read the wrong thing

Not defects in the codebase. Mistakes in *observing* it, each of which produced a
confident and wrong conclusion:

| Error | What happened | Rule |
|---|---|---|
| **Grepped the tail, called it the file** | "Zero errors" across four runs. There were **nine**, in every one — I had only ever read the last ~40 lines | Count over the WHOLE file before claiming a clean run |
| **Read a stale artifact** | Called a run's results from a 2 KB onboarding log and a `relocation.log` written 16 minutes earlier | **Check the artifact's mtime belongs to the event** before reading anything into it |
| **Pattern that could not match** | `Select-String 'Stale-owned prune'` returned empty because log titles pad with **U+00A0**, not spaces — nearly concluded the delete path was disabled | Absence of a log line is not absence of the event |

### And the method that actually worked

For `GLD-TRT-01` I guessed twice from reading and was wrong once — shipping a
plausible fix on the wrong line. What settled it in a single run was a
**diagnostic scaffold**: wrapping each stage so the exception named its own
location, because the decorator logged a message with no traceback.

**When a log gives you a message but not a location, make the code say WHERE
before you reason about WHY.** One run of scaffolding cost less than two rounds of
inference, and inference produced a fix that looked right and was not.

### For parallel data, check every WRITER — not the constructor

`GLD-MVF-01` was deferred for two batches on the reasoning that the write site for
`cast_names` / `cast_characters` / `cast_order` was buried in 2,380 lines and I would
be guessing. The reasoning was sound and the conclusion was wrong:

* `_extract_people` — the CONSTRUCTOR — builds all three from **one** `sorted_cast`
  list. Aligned by construction. Perfectly safe. Reading it would have closed the
  item as a non-issue.
* `refresh_enrichment` — 400 lines away, in a different method — rewrote **only
  `cast_names`**, from Trakt, leaving Radarr's characters and billing order in place.
  Misaligned on every run, and `people_matrix`'s billing decay consumed the result.

**A correct constructor tells you nothing about the updaters.** Parallel arrays,
mirrored caches, paired thresholds, rows-and-descriptions: the invariant lives across
every site that writes any member, and the one that creates them is usually the one
that gets it right. Enumerate the writers, not the origin.

This is the same shape as P-J one level down — there the twins are two methods doing
one job for different media; here they are several sites maintaining one invariant.
In both, the correct instance is the distraction.

---

# §0 — SWEEP STATUS (session 83) — READ THIS FIRST TO RESUME

**The sweep is PAUSED at session 83 to move into remediation.** It is not
finished. This section records exactly where it stopped, what has already been
*changed in code*, and what is still unread — so work can resume cold without
re-deriving any of it.

## 0.1 ✅ Code already CHANGED (do not re-apply)

Fifty-five edits have been made. **Everything else in this register is documentation
only.**

| # | File | Change | Item |
|---|---|---|---|
| 1 | `sonarr/storage/__init__.py` | `warm_cache` now substitutes `<instance>` before writing | `GLD-STO-01` |
| 2 | `sonarr/storage/space.py` | Same fix — a second copy, found only by grep | `GLD-STO-01` |
| 3 | `tautulli/users/__init__.py` | `_affinity_half_life` docstring — full axis-translation warning + 3-step pre-flight | `GLD-TUS-01` |
| 4 | `machine_learning/affinity/genre_affinity.py` | Module docstring — same warning at the brain-side knob | `GLD-TUS-01` |
| 5 | `machine_learning/thresholds/registry.py` | Delete block now **names the three axis-translation triggers** and warns that a `THRESHOLD_SPECS`-only re-anchor **misses `untouched_base`** | `GLD-TUS-01` |
| 6 | `machine_learning/thresholds/drift.py` **(NEW)** + `test_drift.py` **(NEW)** | The axis-drift detector — pure, stdlib-only, scale-agnostic, 13 tests | `GLD-LIK-01` |
| 7 | `machine_learning/ledger/plan_summary.py` | `log_axis_drift()` added, called after `log_thresholds()` | `GLD-LIK-01` |
| 8 | `sonarr/series/retrieval/fetch.py` | Short-list guard in `refresh_all_series` (pre-sync count is the only independent reference) | `GLD-SRF-02` |
| 9 | `factories/base_manager.py` **+ 4 subclass deletions** (`writeback`, `calendar`, `radarr/quality/selector`, `radarr/repair/anomaly`) | `dry_run` resolved **once** in `BaseManager`, 5-rung precedence | `GLD-ORCH-01`/`02`, `GLD-RAN-07` |
| 10 | `sonarr/cache/episode_files.py` | `_recycle_to_fund_acquisition`'s per-series refusal reasons (`_why`) now emit **unconditionally when non-empty** — was gated under `if not funded:`, so a partial fund discarded all of them. ✅ Verified 2026-08-06 23:13 — first partial fund emitted all 4 reason lines (See, AoT, TBBT, Abbott & Costello) and localized the recycle blocker to the protected set in one run | `GLD-ACQ-21` |
| 11 | `machine_learning/classification/guards.py` + `sonarr/cache/episode_files.py` (×2 sites) + `test_episode_files_guards.py` | **Household guard → ACTIVE WATCHERS ONLY** (`GLD-ACQ-18` decided + executed 2026-08-06): fires only when `all_household_watched` is falsy AND row `retention_hold` is truthy — raw mandate ∩ retention at all three guard sites; var + sync computation untouched. Guard tests extended; brain branch verified in isolation 5/5 | `GLD-ACQ-18` |
| 12 | `machine_learning/classification/guards.py` (restructured) + `sonarr/cache/episode_files.py` | **`GLD-ACQ-22` attribution live**: `build_protected_file_reasons()` — per-guard `{name: fids}` from ONE mask source; `build_protected_file_ids` is now its union (cannot disagree). Recycle builds reasons once, derives the flat set, and appends `protected-by: {guard: n}` to every empty-pool refusal line. Verified in isolation: household regression 5/5, per-guard membership, union==flat, absent-column shape. First live line names the TBBT blocker | `GLD-ACQ-22` |
| 13 | `sonarr/cache/episode_files.py` | **`GLD-ACQ-24` cold-TV reclaim, DEFAULT OFF** (`cold_tv_reclaim.enabled`): `_ingest_cold_inventory` — for sub-floor-score series with files, owned-unwatched episode files older than `min_owned_days` become parquet rows flagged `row_origin='cold_scan'` (built by the SAME `_resolve_episode_file`+`_normalise` path; series score stamped so the pool never defers them; `available_until = now+grace_days` visibility window). Self-contained MARK-on-expiry / RELEASE-on-score-recovery lifecycle (deliberately NOT threaded through the watched-anchored `episode_grace_decision` — P-B avoidance); cleanup exempts cold rows (would otherwise delete the feature's state every run); `row_origin` added to SCHEMA + string-cast lists; scan capped `max_series_per_run` and keep-tag-gated. Coordinator remains the sole delete decider. Verified: compile + structural checks + lifecycle simulation on the extracted method (off→no-op byte-identical, expired→marked, unexpired→held, recovered→released+window-cleared, keep-tagged→never scanned). ⚠️→✅ **Self-caught gap fixed before first enabled run**: the release path cleared `available_until` and nothing re-stamped it, so a released-then-re-cooled row was PERMANENTLY exempt — the documented "window restarts from scratch" was unimplemented (a P-A-shaped doc/code divergence). Mark pass now RE-WINDOWS cold unmarked rows with a null window (`rewindowed` stat + log field); simulated: re-cooled→fresh window, and at `grace_days=0` marks the following run. Operator expedited for testing 2026-08-06: `min_owned_days 90→30`, `grace_days 30→0` (restore after test). 🔴→✅ **FIRST ENABLED RUN (00:51): `0 ingested across 25 series` — the cap was consumed by PILOT-ONLY series.** The pilot-fid has-a-file proxy selects series with A file, but cold sub-floor series are exactly the pilot sampler's one-file grabs, so every slot fetched a series with nothing reclaimable (a P-B shape: the filter looked like "has reclaimable files" but meant "has any file"). Fixed same session: selection now uses the series cache's `statistics.episodeFileCount` — require ≥2 and walk FATTEST-FIRST so the cap goes to DBZ-shaped piles; falls back to the old proxy when statistics are absent. Sim-verified: cap-2 over {fc:1, fc:100, fc:10, no-stats} fetches [100-series, 10-series] only; fallback engages when stats wholly absent | `GLD-ACQ-24` |
| 14 | `sonarr/cache/episode_files.py` | **Cold-scan selection → file-bulk, fattest-first** (`GLD-ACQ-24` first-run fix): `episodeFileCount ≥ 2` from the series cache replaces the pilot-fid proxy; cold sids sorted by file count desc before the per-run cap, so the scan reaches the piles the feature exists for instead of burning the cap on one-file pilot samples | `GLD-ACQ-24` |
| 15 | `factories/base_instance_manager.py` + `radarr/quality/space_pressure.py` (×2 sites) + `radarr/quality/universe.py` + `sonarr/cache/episode_files.py` (recycle) | **DELETE result is now checkable and CHECKED.** Base contract: a successful DELETE returned `None` — indistinguishable from a swallowed failure returning the default fallback (`None`), so no call site could ever verify a delete and none did (every existing try/except around deletes was DEAD protection). Success now returns `True`. All four destructive sites check it: universe realize + space-pressure step-down skip the grab and back off when the delete fails (file kept, loud warning); the coordinator delete stage keeps the mark, counts `failed`, and no longer books phantom `bytes_freed`/`deleted_tmdbs`; the recycle abandons the series' funding (nothing acquired against unfreed space). Incident evidence: 2026-08-07 apply — 76 delete 500s ('Unable to delete movie file', MediaFileDeletionService), 30 movie deletes counted as freed, all 6 recycle deletes silently failed with the acquisition funded anyway, and 'file deleted, grabbed Scorpion.King.4' printed around a failed delete | `GLD-ORCH-03`, `GLD-RAD-30` |
| 16 | `radarr/quality/space_pressure.py` (+ threading in `universe.py`) | **Release identity + audio-language gates in `_pick_stepdown_release`** (`GLD-RAD-30`): `_release_matches_movie` (token-normalized contiguous match with join-flex 'spider man'≡'spiderman', roman≡arabic, optional leading article, sequel-number boundary guard, ±1yr year check, alternate-title support) + `_release_language_ok` (Radarr `languages` authoritative; filename foreign-audio fallback; subs tags stay eligible) + fatal 'unknown movie / unable to parse' rejection scan. Universe threads title/year/alternateTitles from the movie payload; the space-pressure step-down threads title/year from the frame. `movie_title=None` (legacy/tests) ⇒ byte-identical. Verified against the incident's real damage list: 8/8 wrong-movie rejects (incl. Spiderman.2002-for-Spider-Man-2 and Scorpion.King.4), 12/12 right-movie accepts (incl. Alien-4-via-alt-title, JKP romaji via alt, ±1yr Eras Tour), 9/9 language cases | `GLD-RAD-30` |
| 17 | `radarr/quality/universe.py` + `radarr/quality/space_pressure.py` | **Missing `movie_file_id` ⇒ grab SKIPPED** (`GLD-RAD-31`): both step-downs previously bypassed the delete when the row carried no fid and grabbed anyway — 19/21 realize rows on the first apply took that path. Now: `fid_missing` stat, ledger backoff, loud warning, no grab; profile columns still updated so the row re-probes normally | `GLD-RAD-31` |
| 18 | `routing/uhd_reconcile.py` + `radarr/quality/universe.py` + `radarr/quality/space_pressure.py` | **Ultra is 2160-only — enforced** (`GLD-RAD-32`, operator ruling): universe defers sub-2160 downgrades on the 4K instance pre-PUT; the space step-down defers entirely there; `_demote_overqualified_4k` rehomes SUB-2160 RESIDENTS score-independently through the existing survivor/make-before-break machinery (pins + same-path guard honoured; shells recover to proper 2160s); demote delete result-checked with re-monitor-on-failure. Sim-verified 6 shapes + failure paths; sim caught + fixed an UnboundLocalError on `grabbed` | `GLD-RAD-32`, `GLD-ORCH-03` |
| 19 | `acquisition/scorer.py` + `acquisition/__init__.py` | **Household taste profile → all four people roles** (`GLD-ACQ-25`, operator request "Make the movies match the shows, adding in cast, directors, composers, producers"): `taste_profile()` now surfaces `composers` + `producers` alongside directors/actors — `aggregate_affinity` had tallied all four roles into `tautulli/affinity` all along; they were just never read. Breakdown block prints `top composers` / `top producers` when non-empty (absent roles print nothing). Evidence finding folded in: the block was never movie-gated — one mixed batch, one block at the end (07:56 log: 9 movies + 1 show, single block at line 985); the operator's two excerpts were mid-batch vs end-of-batch from different runs. Verified: extracted-method sim (roles surfaced, absent-role + no-cache graceful) | `GLD-ACQ-25` |
| 20 | `support/tools/people_billing_experiment.py` (NEW) | **The billing/role-weight measurement harness** the DESIGN pre-registered (`GLD-PPL-01`/`03`/`04`, operator green-light "run tests across our setups … datamine breakpoints to set the billing numbers at"): offline + read-only; replays the PRODUCTION `aggregate_person_affinity` (its `role_weights`/`billing_decay`/`engagement` params exist for exactly this) under swept constants. INSPECT stage first (per-role id coverage — Q3 gates Q1 — + watched∩forward overlap + date/engagement availability); SWEEP: temporal holdout (random fallback, labelled), decay ∈ {0, .10, .25ₑ, .40, .60, 1.0} × cast_limit ∈ {3,5,10,15,20}, negatives = never-watched forward-map titles, metrics AUC + lift + split-half top-25 stability, per-role ablation for the role-ORDERING claim, recommendation block (best decay, cast-depth knee, measured vs asserted ordering). Machinery smoke-tested via fake-module injection (key-norm both serialized shapes, cast-cap billed-only, end-to-end variant AUC=1.0 on planted signal, tie-handling). Watched-set source = `movie_files.parquet` (schema-probing columns); iterate on the operator's INSPECT paste if the probe misses | `GLD-PPL-01`, `GLD-PPL-03`, `GLD-PPL-04` |
| 21 | `affinity/genre_affinity.py` + `acquisition/scorer.py` + `acquisition/__init__.py` | **Tautulli people tallies un-flattened + a dead Group-B signal resurrected** (`GLD-AFF-10`/`11`, operator request "make the tautulli/affinity not flat … same semantics for both"): actors now billing-tiered with the SHARED `PERSON_BILLING_DECAY` constant (one semantics for profile + both scoring paths; scale-safe because `affinity_topk` max-normalizes; `person_billing_decay=0` = flat legacy); role weights deliberately NOT applied in the tally (Group B's 8/6/4/4/3 caps tier at consumption — double-tier avoided); `writers` map ADDED — `movie_scorer`'s B3 (cap 4.0) consumed a map that was never built, permanently 0.0 until now (P-A live instance); `taste_profile()` + breakdown surface `top writers`. Verified: repo's pinned genre tests reproduced green against the new code; lead-in-one-title outranks rank-5-in-two (1.0 > 0.889); flat mode byte-faithful. `GLD-AFF-12` filed: composers/producers built-never-scored — adding B6/B7 terms is an AXIS expansion needing the re-anchor treatment, operator decision | `GLD-AFF-10`, `GLD-AFF-11`, `GLD-AFF-12` |
| 22 | `config.json` + `sonarr/cache/episode_files.py` + `support/tools/people_billing_experiment.py` | **Pilot gate 20 → 15 + experiment-tool join iteration** (`GLD-ACQ-26` + `GLD-PPL` tooling): pilots now pass at watchability ≥ 15 — deliberately BELOW the net-new/cold 20-tier per the operator's exploration rationale (a slice of the 7,497 held stubs re-enters next run; gate stays no-dead-zone). Experiment tool first live run found `watched ∩ forward = 0/213` with the sweep degenerating to NaN theater — tool now prints key SAMPLES from both sides, computes the id-space overlap, SELF-HEALS a medium-token mismatch by adopting the forward map's dominant medium (loudly), reports dated counts raw + in-join separately (the old line hid dates behind the broken join), and hard-aborts the sweep at zero overlap instead of printing a bogus recommendation | `GLD-ACQ-26`, `GLD-PPL-03` |
| 23 | `services/trakt/people_matrix.py` + tool guard | **THE C4 PEOPLE SIGNAL WAS DEAD SYSTEM-WIDE — published forward had ZERO movies, published affinity was `{}`** (`GLD-PPL-11`, found BY the billing experiment's 0/213 join + key samples showing all-`('show',…)` keys): `_movie_forward`'s missing-parquet `return {}` early-returned past the AUTHORITATIVE bucket supplement (textbook P-B — the docstring literally says "buckets WIN"), and the relational parquet has never existed here (only `studios.parquet`) — so every build shipped shows-only, `aggregate_person_affinity` computed over a movie-less forward → `{}`, and the scorer's `if fwd_raw and aff_raw` gate zeroed C4 for EVERY title while 1.5MB of fresh movie graph (sidecar) + populated `trakt/movies/` blobs (radarr_credits_sync thousands done) sat unused. Fixed: no-parquet ⇒ buckets ARE the movie half; parquet path unchanged when present. Rebuild required (fingerprint would republish stale): operator deletes `cache/trakt/people_matrix.state.json`. Tool also gained a <20-overlap sweep abort (1 coincidental id slipped the zero-guard and printed AUC-0.5 theater). Discovery chain worth noting: taste-profile request → billing question → experiment harness → join failure → dead production signal | `GLD-PPL-11` |
| 24 | `people_matrix/build.py` + `scoring/movie_scorer.py` + `scoring/show_scorer.py` | **Role weights re-set by operator ruling** ("producer/writer affinity less impactful, casting higher — more RDJ in Tropic Thunder, less Russo brothers for another superhero movie"): `PERSON_ROLE_WEIGHTS` directors 1.0→0.7, writers 0.6→0.3, producers 0.3→0.15 (cast 1.0 anchor; single shared table ⇒ C4 build + candidate scoring reshape together); Group-B caps redistributed SUM-PRESERVING in both scorers (actors 8→10, writers 4→2; 25→25, no axis change; directors' name-cap untouched — the Russo trim applies once, id-side). Rationale at the constant: the measured producer/writer dominance (`GLD-PPL-01`) is largely franchise continuation + graph density, already monetised by saga/universe — double-counting avoided; this table expresses PEOPLE-following. Quantified through the production top-3 formula: RDJ-cast title C4 10.0→10.0 (unchanged — cast path untouched), crew-only-overlap superhero title 2.93→1.93 (−34%). Validation against decontaminated data remains `GLD-PPL-12`. Note: second score reshuffle today (C4 was resurrected this morning) — expect ladder movement | `GLD-PPL-01`, `GLD-PPL-12` |
| 25 | `support/tools/people_billing_experiment.py` + register | **`--decontaminate` mode + the squared-weights finding** (`GLD-PPL-12` implemented, `GLD-PPL-13` filed): the experiment can now drop test titles AND negatives sharing a Radarr `collection.tmdbId` with any train title (`radarr.movies.*.full.json` loader, envelope-tolerant, unit-checked) — built in direct answer to the operator's "should we actually favor writers over actors?" question, which the decontaminated ablation decides from household data. Riding along, a structural discovery: role weights apply at BOTH aggregation and candidate scoring, so cross-role ratios are `rw²` end-to-end — writers sit at ~0.09 vs cast 1.0 under the new table (0.36 before), far steeper than the table reads; intentional-vs-double-count decision gated on the PPL-12 result | `GLD-PPL-12`, `GLD-PPL-13` |
| 26 | register + tool print | **PPL-12 decontaminated RESULTS recorded**: ordering survived (producers 0.814 > writers 0.766 > cast 0.747 on 30 clean / 90 neg) but writers-vs-cast is a statistical tie (Δ ≪ 1σ at ±~0.05 SE) and producers' lead rides studio-stable continuity (collections can't see it) + graph density. Table STANDS by parsimony; writers 0.3→0.45 nudge on offer; definitive at ≥60 clean positives. Decay 0.25 + depth 10 reconfirmed; stability metric coarse-not-broken (flat 0.042 vs 0.087). Tool: split line now prints effective positives after filters (was showing pre-decontamination count) | `GLD-PPL-12` |
| 27 | `people_matrix/build.py` (operator) + `people_matrix/README.md` (operator) + `people_matrix/DESIGN.md` §7/§9/§10 (operator + assistant) + `affinity/genre_affinity.py` + `scoring/_shared.py` | **PEOPLE-SYSTEM THREAD CLOSED** (operator: "call this resolved … stash the logic away. Nudge the writer to 0.375 — split the difference"): `PERSON_ROLE_WEIGHTS` writers finalised **0.375** (effective ~0.14 under the both-stages regime) — operator applied the table + FINAL-TABLE comment directly, then consolidated §7 constants, §10 questions (Q1/Q2 answered, Q5 minted for PPL-13), the full README (weights table with standings, ruling quote, tie result, re-run command, squared warning, PPL-11 saga), and closed the PPL-12 row RESOLVED. Assistant completed: the squared-application cross-references stashed in BOTH `.py` application-site docstrings (`aggregate_person_affinity` + `person_affinity_score` — "change either site and the table's numbers change meaning"), the stale header claim ("weights encode an unmeasured claim" → measured + finalised), and this record. Standing state: decay 0.25 + depth 10 measured; role table set by ruling + decontaminated measurement; `GLD-PPL-13` (squared application: intentional vs double-count) is the people system's sole open item; writers-vs-cast re-measures free at ≥60 clean positives | `GLD-PPL-01`, `GLD-PPL-04`, `GLD-PPL-12`, `GLD-PPL-13` |
| 28 | `sonarr/series/space_pressure.py` + sonarr DESIGN §9 | **WRONG-SHOW GRABS on the sonarr step-down — identity gate shipped** (`GLD-ACQ-27`, the RAD-30 twin, live evidence: Space Brothers S01E06→'Property.Brothers.S11E06', S01E05→'Super.Giant.Robot.Brothers', S01E20→'Space.Brothers.E81'): raw `release?episodeId=` results fed an identity-blind shared picker, POST forced the stranger into the slot, failed imports re-opened episodes → the delete→wrong-grab→missing→regrab→step-down churn that ate the pipeline as "the same few shows". `_release_ok_for_episode` gates title (shared matcher + masked-episode retry defusing the movie-side sequel-number boundary on 'Space Brothers - 20' forms), episode evidence (SxxEyy/range/NxM; bare-number only season-1-exact — kills E81), language; conservative no-evidence⇒kept; `identity_rejected` stat; truth-tabled 12/12 incl. all three live failures. Same investigation: pilot drought DECOMPOSED — gate-15 working (6,716 held vs 7,497), 07:09 wave probed 791 stubs / EpisodeSearch'd 684, 16:33's `searched 0` = correct 24h cooldown; daemon liveness suspect filed `GLD-ACQ-28` (log mtime frozen 13:05, spawn claims dead pid 33692, queue empty — operator process-check pending) + `GLD-ACQ-29` (legacy 0/0/0 unaccounted skip counters, same 67 re-spilled) | `GLD-ACQ-27`, `GLD-ACQ-28`, `GLD-ACQ-29` |
| 29 | `factories/daemons/supervisor.py` + sonarr DESIGN | **`GLD-ACQ-28` SOLVED + fixed — daemons murdered by the IDE job object, logs buffered into oblivion**: operator's process check = EMPTY (dead, not hung); the 16:34 child really spawned (Windows recycled pid 33692), processed the legacy job, idled with everything in block-buffered stdout, and was hard-killed at run-window close — buffer destroyed, zero bytes to disk, mtime frozen at the prior session's clean exit. The supervisor's own docstring predicted it. Fixes: `PYTHONUNBUFFERED=1` child env (deaths visible), 1.5s post-spawn liveness poll (instant deaths log ERROR + pidfile cleanup instead of fake success), breakaway-rejected warning states the kill-at-run-close consequence. Guidance: big-spree runs from terminal/Task Scheduler. Also from the operator's Sonarr history paste: PILOTS CONFIRMED FLOWING (wall of 1x01s through the T's, the wave landing) — the original drought concern closes empirically; NEW pathology spotted: same-episode re-grab churn (Tracker 1x02 ×15+/hour, Witcher 1x01 descending 720p→SDTV through ~20 releases) = grab→fail→redownload loop suspect, evidence requested (unfiltered history between grabs); 'Anime Web Tier' CF over-matching non-anime releases noted | `GLD-ACQ-28` |
| 30 | sonarr DESIGN §9 | **The afternoon UNIFIED (`GLD-ACQ-30` filed 🔴)**: operator's df + SAB warnings closed it — 729G aggregate ≈ back under the ~200G/disk min-free wall from the MORNING incident; SAB `complete_dir not writable` ⇒ every download failed ⇒ Sonarr FDH ladder-burn = the re-grab churn + 502 storms. Today's successes (pilot imports, replacements, next-ep grabs) consumed the purge headroom while acquisition lanes grabbed floor-lessly into a 98% array — upgrades check space, pilots/next-episode/legacy don't. Design filed: shared `acquisition_space_ok()` + `acquisition.space_floor_gb` across all grab lanes; implementation on operator go. Remediation guidance issued: purge, SAB incomplete cleanup, **Sonarr blocklist clear** (storm blocklisted the best releases), SAB pause-on-low-space ~1TB | `GLD-ACQ-30` |
| 31 | `acquisition/__init__.py` + `routing/uhd_reconcile.py` + `config.json` | **UHD headroom gate SHIPPED** (`GLD-ACQ-30` UHD sub-policy, operator ruling post-1.9TB-manual-4K-wipe: "4k spacing 25% higher than the floor as free before we try and acquire 4k"): `routing.movies.uhd_headroom_multiplier: 1.25`; new `_uhd_space_ok` (adder) swaps into the `plan_uhd_companion` `space_ok` callback — the 2160p bonus copy needs `free ≥ U × 1.25`, general adds keep the band; new `_uhd_headroom_ok` (reconcile) gates SHELL RECOVERY the same way (deferred loudly, shell stays ledgered — zero loss); demotes/rehomes deliberately NEVER gated (they free space). Both compile; boundary math sim-verified (band-pass-but-4K-fail case). ⚠️ WIPE AFTERMATH flagged to operator: manually-wiped titles are still MONITORED in Radarr ultra — Radarr-native missing-search refills them regardless of any glidearr gate; bulk unmonitor-or-delete the fileless ultra entries after rescan is REQUIRED. General-lane floor (pilots/next-ep/legacy) remains the open ACQ-30 half. ✅ **Incident remediation VERIFIED 2026-08-07 evening: operator's 1000MB SAB test download COMPLETED — the array accepts writes again; the ENOSPC-on-mkdir chain is closed** | `GLD-ACQ-30` |
| 32 | `sonarr/sync/tags.py` + `series/sync/__init__.py` + `series/sync/synchronize.py` + `series/sync/async_tasks.py` | **Fresh-start first run flushed a QUADRUPLICATE latent bug + a false-success pattern** (`GLD-ACQ-31`): the cold-seed path sent the 'keep' LABEL string inside Sonarr's integer tag-id array → every keep-tagged series PUT 400'd (`$.tags[i] → Int32`) — and `run_sync_jobs` logged each as `✅ Synced` because `_make_request` returns None on HTTP errors without raising (P-D, the DELETE twin). Root: `ensure_keep_set` resolved the tag ids and DISCARDED them; with no accessor, `updated_tags.add("keep")` grew in four places (P-E). Fixed: ids persisted + `ensure_keep_tag_id()` accessor (persisted → GET → POST-create → None⇒skip, never a string), all label sites converted, int-coercion belt + PUT result-check in run_sync_jobs. 4/4 compile; accessor ladder sim-verified. Operator: re-run the sync — the 44 seeds now apply for real | `GLD-ACQ-31` |
| 33 | `plex/playlists/writeback.py` + `test_writeback.py` | **The "last resort" recreate path was the DEFAULT path — every managed playlist was being deleted and re-minted with a fresh ratingKey on most runs** (`GLD-PLY-13`). `_diff` returned the ENTIRE survivor list as `move` whenever the order differed at all, so `n_changes` came to `len(desired) + removes` and `_RECREATE_RATIO = 1.0` tripped whenever **a single item was removed** — which for an "Up Next" list is the steady state (items get watched, they drop out). Measured in `support/logs/playlists-4/5.log`, reproducible across rotations: **4 of 6 "Up Next" lists RECREATED per run**, the other two sitting exactly on the threshold (`+22/-0/~78` = 100 vs D=100, clearing by a margin of zero). Fixed: `move` is now the MINIMUM displaced set (LIS complement, `_min_moves`), verified exhaustively vs brute force for every permutation to n=7 + randomised replay to n=60. Re-simulated on the live shapes: `+22/-3` moderate rerank 103→**52**, even a FULL reshuffle 103→87 (all in-place); a genuine `+60/-40` overhaul still recreates. P-B twin — a FALLBACK broader than it appears, the mirror of "guard narrower than it appears" | `GLD-PLY-13` |
| 34 | `plex/playlists/writeback.py` + `test_writeback.py` | **titleSort silently lost on every recreate, permanently** (`GLD-PLY-14`). `_recreate` mints a new ratingKey, repoints the anchor and force-re-brands the poster — but never called `_ensure_title` on the new list, and the title gate is keyed on `anchor_id` (`safe` / `safe::suffix`), which **survives a recreate**. So the fresh list inherited a stale "already titled" marker and never got its `!` front-pin; `create_playlist`'s POST cannot set `titleSort`, which is precisely why `_ensure_title` exists. Exact shape of the already-fixed `_BRAND_KEY` bug (404 cached as done) — branding got `force=True` and a regression test, the title half was missed. Fixed: `_ensure_title(..., force=True)` in `_recreate` + `force` param + `test_title_reapplied_to_recreated_playlist` mirroring the branding one. **P-C, ninth instance** — a gate keyed on the wrong identity, so a NEW object inherits an OLD object's "done" marker | `GLD-PLY-14` |
| 35 | `plex/playlists/writeback.py` + `test_writeback.py` | **Once-a-day rewrite cadence with a churn escape hatch** (`GLD-PLY-15`, operator request: "regenerate ONLY one time a day unless there is massive overhaul"). Item writes are now rate-limited per playlist against `_LASTWRITE_KEY` (epoch stamp, set **only when armed** — a disarmed preview must not start the interval). Knobs, both config-derived: `min_interval_hours` (default **20**, not 24, so a daily-scheduled run is never blocked by clock drift; `<= 0` disables) and `churn_override_ratio` (default **0.40**, deliberately ABOVE this install's observed steady-state add churn of ~0.22 so routine re-ranking defers). The override is measured on **adds and removes only, never moves** — a pure re-order is exactly the noise the gate exists to absorb, so it must not be able to override it. Title/poster stay OUTSIDE the gate (one-time version-gated migrations). Also closed the detector gap: `stats["recreated"]` + `stats["deferred"]` added to the banner, which previously counted a full recreate and an in-place diff both as "update" — so the recreate rate was invisible (P-D). 51/51 tests pass | `GLD-PLY-13`, `GLD-PLY-15` |
| 36 | `support/tools/posters.py` + `plex/playlists/poster_render.py` + `plex/collections/posters.py` | **Every generated poster re-uploaded on EVERY run — the producer was invalidating its own consumer's cache key** (`GLD-PLY-16`). `poster_sync._asset_version` is `size + mtime_ns`, a deliberately cheap content token; all three render sites called `write_bytes` UNCONDITIONALLY, so each pass gave every PNG a fresh mtime, every token changed, and the gate re-uploaded art that had not changed. Measured live: `45 branded` on an armed run that changed nothing. The gate was correct throughout — nothing was wrong downstream of the write. Fixed: `posters.write_if_changed` byte-compares before writing; identical values render to identical bytes because rasterisation is deterministic by construction (vendored OFL fonts + `skip_system_fonts`, originally for cross-platform fidelity — determinism turns byte-compare into a correct cache key). A non-deterministic backend degrades to always-write, i.e. today's behaviour, so there is no regression path. Verified with `poster_sync`'s exact token scheme across three passes: new→wrote, same values→skipped with the version PRESERVED, count 6→7→wrote. Next armed run measured **45 → 4 branded, 41 unchanged (gated)**. New shape worth naming: a WRITER that defeats a downstream content gate — the mirror of P-D, where the failure path exists but is undetected; here the success path exists and is destroyed | `GLD-PLY-16` |
| 37 | `plex/playlists/tonight_builder.py` | **NaN is truthy, so `universe_name or collection_name` fused every unplaced movie into ONE franchise** (`GLD-PLY-17`). The movie_files parquet stores a missing universe as float NaN; `NaN or X` short-circuits on the NaN, `str(nan)` is `"nan"`, and every movie without a universe collapsed into `franchise:nan` — which then outweighed every real franchise in the weekday model and became two profiles' entire Tonight list, both handed the same wrong film. Visible in the live log as `franchise:nan rk=1008` for two different people. The repo ALREADY had the guard: `models.PLACEHOLDER_AFFINITY` exists for exactly this class (its own comment records a bare `"universe"` label once fusing ~220 unrelated movies into one mega-group). Fixed by REUSING it via `_affinity()` rather than writing a second junk-token list. **P-C** — absent conflated with present — plus the meta-failure of not searching for an existing guard before writing a new one | `GLD-PLY-17` |
| 38 | `plex/collections/posters.py` + `plex/playlists/smart_shelves.py` | **`12 collection(s) not found` on every run since the feature shipped — a lookup that could not succeed** (`GLD-PLY-18`). `CollectionPosterManager` kept a hand-written map of twelve collection TITLES and scanned every section for them. TEN of the twelve (Up Next, Tonight, Hidden Gems, The Long Glide, Touch & Go, Franchise Run, Household Picks, Kids Safe, Because You Watched) name per-user PLAYLIST families that are never collections at all; the two that can be are created by `smart_shelves` as `"Just Landed — Movies"`, never the bare title searched for. The message read as "the collections are absent" when the truth was "this can never match". The same scan also warned on every Kometa duplicate — ~70 lines of noise per run about per-section collections, which is simply how PMS works. Fixed by inverting the direction: `smart_shelves` publishes `{rating_key: {title, section, family, kind, library, count, slug}}` under `plex/collections/managed` at the moment of creation (it already holds the ratingKey), and the poster pass walks it — no title resolution, no section scan, no Plex reads at all. Live: `0 of 12 not found` → `8 of 8 managed`. **P-E** — two independent definitions of "which collections exist and what art they wear", which drifted the instant either side was renamed | `GLD-PLY-18` |
| 39 | `plex/playlists/poster_render.py` + `tonight_builder.py` | **Poster tokenisation was built, tested, wired and never FED — every live poster showed its SPEC default** (`GLD-PLY-19`). `generate_posters` called `render(slug, kind)` with no values, so eight real playlists shipped `"next up in your series"`, `"12 just landed this week"`, `"continuing your sagas"` — one of them announcing "12 just landed" while holding 3 days 14hr of content. Textbook **P-A**: signal computed, never consumed, in code whose entire purpose was to carry values. Fixed with `PlaylistPosterRenderManager`, running BETWEEN the builders and the write-back (both halves load-bearing: earlier renders from stale plans, later and the upload has already happened). Counts now come from `len(plan["items"])`; labels from the plan's own `group_key`/`reason` so the poster cannot disagree with the playlist. Posters became PER-USER — five of eight families are personal — which cost nothing structurally because `_apply_branding` already keys its version gate on the anchor id. Two follow-on defects fixed in the same pass: parquet `genres` arrive as the JSON STRING `'["drama"]'` and a comma-split treated it as one label, leaking `["drama"]` onto a live poster (`_labels()` now parses JSON first); and every family was labelled with SERIES TITLES, so "Touch & Go" — whose copy reads `one-offs in {GENRE}` — shipped `"one-offs in The West Wing · Suits · rocky"` (`_LIST_SPEC` now declares which SOURCE each family's copy asks for, and omits the key entirely when that source is empty so the template falls back rather than printing the wrong category) | `GLD-PLY-19` |
| 40 | `plex/playlists/tonight_builder.py` | **THREE invented cache keys — movies would have been silently absent from Tonight forever** (`GLD-PLY-20`). `_movie_maps` read `plex/movies/genres_by_tmdb`, `ml/movie_scores` and `plex/collections/membership_by_tmdb`. The first two do not exist; the third is only written by a default-OFF capability. All three returned `{}`, so Tonight would have worked perfectly for TV and never surfaced a single movie — no error, no warning, just an absence. Caught by directory inspection BEFORE the first run, then repeated an hour later (`plex/movies/genres_by_rating_key`, `plex/series/genres_by_rating_key`) and caught again the same way. Fixed both times by using the source already in hand: `_load_owned_movies()` carries `tmdb_id`, `genres`, `watchability_score`, `universe_name`, `collection_name` on one Radarr row (`_OWNED_MOVIE_COLUMNS`), and the genre map is now PUBLISHED by its builder for `poster_render` to read rather than re-derived a third time. **Rule this produced: a consumer must never name a cache key it has not verified a writer for** — the failure is invisible because an empty map and an empty result are indistinguishable (P-C) | `GLD-PLY-20` |
| 41 | `support/tools/posters.py` + both render managers | **The engine rendered from artefacts only an operator CLI produced** (`GLD-PLY-21`). Poster templates are GENERATED from `poster_templates.py` and were built solely by `generate_posters`. That was fine while rendering was an operator step and became wrong the moment the engine started rendering per-user and per-section posters: a design change then shipped code referencing templates nothing had written. Live failure: eight × `cannot read template … shelf_this_week.template.svg`. The version-stamped rebuild already existed — it had been placed only in the CLI. Fixed: `posters.ensure_templates()` at the top of both render paths, idempotent via the version stamp inside each file. Extended the same session to the shared DEFAULT PNGs (`_ensure_base_posters`), which are write-back's fallback and were equally CLI-only — and which must re-render DAILY because four families print a live date. Marker records date + template version; a mismatch on either re-renders. Proved from an EMPTY assets directory: 28 templates + 8 defaults, zero operator commands | `GLD-PLY-21` |
| 42 | `plex/playlists/writeback.py` | **The titleSort gate trusted a local marker instead of the server, so drift could never be repaired** (`GLD-PLY-22`). `_ensure_title` skipped when `cache[key] == sort` — but that marker records what we last SENT, not what Plex holds. A hand-rename, a restored database or any server-side reset drops the titleSort while the marker still reads "done", and the list sits in plain alphabetical order permanently, because the one thing that could fix it has convinced itself there is nothing to fix. Note the gate was ALREADY outside the cadence gate (`GLD-PLY-15` put it there deliberately) — the deferral was not the cause, which is what the first diagnosis assumed. Fixed: verify against the live `titleSort` from one token-scoped playlist listing, memoised per token for the run (6 calls, not 48). `None` from an unreadable listing falls back to the marker — treating it as "empty" would rewrite every title on any transient API blip (P-C). A server that is already correct RE-SEEDS a missing marker rather than rewriting. Banner reports `N sort key(s) REPAIRED after server drift` so recurrence is visible rather than silently self-correcting | `GLD-PLY-22` |
| 43 | `machine_learning/playlists/caps.py` + `ordering.py` + `models.py` + `combined_builder.py` | **"Touch & Go", the ONE-OFFS list, shipped five consecutive Suits episodes at positions 3–7** (`GLD-PLY-23`, operator: "set a hard limit of 1 per series"). Not an ordering bug — group contiguity is the engine working correctly, and for Up Next following one show run-on is the entire point. The same engine serves the opposite intent here, so this is a per-FAMILY policy expressed as an argument, not a global. `caps.limit_per_group` trims each group to its first N, applied BEFORE `apply_size_cap` so the size cap budgets against trimmed groups (cap-then-trim would spend the budget on members about to be discarded) and taking the FIRST n so spoiler-safe `(season, episode)` order is preserved. The Long Glide deliberately has NO cap — it is the in-progress list. `PlaylistPlan.per_group_dropped` is a SEPARATE field from `truncated`: "the playlist was full" and "this series may only contribute once" are different facts, and one number covering both hides which policy acted — the same ambiguity that made `0 labelled · 98 cleared` unreadable earlier the same day | `GLD-PLY-23` |
| 44 | `machine_learning/discovery/shelf.py` + `plex/playlists/tonight_builder.py` + `labels/recommendations.py` | **Stable identity was in hand at build time and DISCARDED, so the recommendation ledger could never record a show** (`GLD-PLY-24`, operator: "it would make more sense to have a parquet that stores the data at build time than to try and invert the inventory after the fact — given rating keys can change"). `gated_plan` builds two branches from the same candidate: the `net_new` branch keeps `tmdb_id`/`tvdb_id`, the OWNED branch — the one that becomes the shelf — kept only `rating_key`. Anniversary publishes 23 movies + 97 shows per profile; all 97 shows would have been skipped by `build_events`, reported as an absence indistinguishable from "no TV was surfaced". The operator's reasoning is the same reasoning already encoded in `watched_episode_keys`' three-identity matching: **a Plex re-scan RETIRES ratingKeys — measured at 11/117 on one series** — so resolving tmdb/tvdb back from a ratingKey later either fails or, worse, succeeds against a DIFFERENT title that inherited the number, which is a silently-wrong ledger row. Fixed: both builders capture identity at selection time (Tonight's episode identity is free — the pool item IS the `tvdb:s:e` join key), and `recommendations.entity_of` widens the ledger to movies, shows and episodes. Movies keep their BARE tmdb `entity_id` forever (`_DEDUP_KEYS` joins on it; re-keying would orphan every pick still inside its window); TV ids are PREFIXED because `media` is not in the dedup key and tmdb/tvdb number-spaces overlap, so a bare `"550"` could be Fight Club or a series and the two would collide into one row. `build_events_ex` returns `(rows, skipped)` — a silent skip is how 97 shows a night disappear | `GLD-PLY-24` |
| 45 | `factories/registry/cli.py` + `factories/registry/core.py` + `factories/registry/config_sync.py` + `factories/base_manager.py` | **THE REGISTRY'S OWN DIAGNOSTICS WERE THE LEAST TRUSTWORTHY CODE IN THE TREE — four defects, none of which a run could reveal.** (1) `GLD-REG-11`: the anomaly column flagged any source containing `pycharmprojects` — the canonical checkout **is** `…/PycharmProjects/glidearr`, so every healthy row got ❌ and a stale-mirror import read clean; now the reference root comes from `cli.py`'s own `__file__` and `_is_expected_path` is wired as a secondary ⚠️ behind a three-state `_expected_subpaths()` (rules exist for four service prefixes only — `False`-for-no-rule would have flagged every brain manager) with separator tolerance (`space_pressure.py` ≠ `space/pressure.py`). (2) `GLD-REG-03`: `print_tree_view` never existed AND its call sat inside the parent-linking `try`, so the debug flag would have cost every manager its inherited `dry_run`; implemented + moved out. (3) `GLD-REG-13`: `register()` writes a wrapper dict and `set()` writes a bare object — `find_by_attr` has **always returned `[]`**, `load_config_and_propagate` propagated to **nothing**, `get_all` would raise on a `set()` row, and the CLI dump read keys nothing writes; one `_unwrap()` for all four. (4) `GLD-REG-14`: `factories/mixins/` missing from `utility_keywords`, so the mixin's `register()` **overwrote** the correct origin with its own path — found only because it would have made the new ⚠️ fire on every service component. `GLD-REG-12` filed OPEN (decision): `auto_hot_swap_from_config` receives the config **dict** and is a no-op on every init, with its own warning branch guarded on an attribute `RegistryManager` never defines. All four files compile; sim: 9 sections / 38 assertions incl. the old heuristic replayed on both row types, mutual-cycle and self-parent trees, and both stored entry shapes | `GLD-REG-03`, `GLD-REG-11`, `GLD-REG-12`, `GLD-REG-13`, `GLD-REG-14` |
| 46 | `factories/cache/__init__.py` + `factories/cache/test_cache_key_collision.py` | **Singleton re-init hazard CLOSED — a second `GlobalCacheManager(...)` can no longer wipe `memory`/`run_summary` mid-run** (`GLD-CACHE-13`, operator: "close off the possibility of a second instance for good"). `BaseManager.__new__` singletons on `(cls, singleton_key)` and nothing passes a key, so a second construction returned the SAME object and re-ran `__init__` — rebuilding `memory` (in-RAM cache wiped) and `run_summary` (every per-title row orphaned; end-of-run tables render empty/partial with no error). LATENT P-D, no live trigger: the call-site audit found exactly three constructors, one per process — `main.py __main__`, the guarded `Main.__init__` fallback, `main_trakt.py`; both daemons deliberately use `CacheKeyBuilder` shims instead of the class. Fixed: idempotent `__init__` — guard FIRST, flag set LAST (a first init that raised partway stays retryable, never frozen half-built), repeat construction WARNS + no-ops; `_reset_singleton()` classmethod is the test-only escape, wired into `test_cache_key_collision._gc` (which otherwise inherits the previous test's tmp_path-pointed handlers — strictly MORE hermetic than the old accidental re-init). Verified: both files compile; mechanics simulated against a faithful `__new__` replica — identity preserved, state preserved, config NOT swapped by re-init, warn-once, an unguarded control class confirms the wipe the guard prevents, reset yields a fresh instance, mid-init failure retryable, explicit `singleton_key` instances unaffected | `GLD-CACHE-13` |
| 47 | `sonarr/series/space_pressure.py` | **A keep tag vetoed QUALITY CHANGE as well as deletion, silently removing 78 series from the step-down pool** (`GLD-TVQ-01`, operator: "keep-series is to never delete the series episodes … but we still downgrade to floor when needed"). `_resolve_keep_policy_map`'s own docstring scopes the tag to deletion — *"No episode from this series will ever be marked for deletion"* — but the same value was passed into `plan_series_downgrades(keep_tags=…)`, which skips on `keep_policy in keep_tags`. A pinned series was therefore permanently immune to the ladder, the opposite of the intent: it should hold its EPISODES (playlist order, saga completeness) while still shrinking toward the floor under pressure. The movie twin gates on keep tags for an unrelated reason it documents — universe titles have their quality owned by the credit-gated ladder in `plan_movie_downgrades` — and there is NO second owner on the TV side, so the skip was a permanent exemption rather than a handoff (**P-J**: same job, two media, only one correct). Split into `KEEP_TAGS` (delete-scoped, documentation) and `DOWNGRADE_KEEP_TAGS = frozenset()` used at the call site; the pure planner keeps its parameter, which the movie path and `test_downgrade_planner.py:262` both already exercise. Also dropped `keep_universe`/`keep_forever` from the TV set — `_resolve_keep_policy_map` only ever emits `keep_series｜keep_season｜None` for a TV row, so those two branches could never match. Live effect smaller than predicted (4 series entered the pool, not 78) because keep-tagged series are overwhelmingly already AT the 720p floor — the guard was redundant for most of them, which is why it survived unnoticed | `GLD-TVQ-01` |
| 48 | `sonarr/cache/episode_files.py` | **A 24h TTL that only ever logged, so every per-series Sonarr payload froze at first write** (`GLD-CACHE-14`). `get_or_generate_cache` defaults `regenerate_on_expiry=False`, and its own docstring admits the consequence in prose while the parameter name hides it: expiry emits a debug line and serves the stale copy anyway. All three call sites here omitted the opt-in. Observed live: every `by_series` payload stamped **2026-08-08 17:33**, read daily, rewritten never, still being served on 08-13 — while `radarr.movies.*.full` re-pulled 27,469 movies every run, so the gap was Sonarr-only (**P-J** again). Every space decision reads these cells via `_build_row`. Fixed: config-driven TTLs (`sonarr_episode_list_max_age_s` / `sonarr_episode_files_max_age_s`, both 900 s, mirroring Radarr's `radarr_movie_library_max_age_s`) with `regenerate_on_expiry=True` on all three. **The first attempt split them 86400/900** on the reasoning that a season list "moves when a season airs" — wrong, because `episodeFileId` lives in the EPISODE record and moves on every grab; a stale episode half yields retired ids that match nothing in the fresh file half. Corrected to 900/900 by the operator, and `_episodes_ttl_s` now CLAMPS to `min(configured, file_ttl)` so the pairing is structural rather than conventional. Second defect fixed in the same pass: turning regeneration on EXPOSED a latent **P-C** — the generators used `fallback=[] or []`, and `get_or_generate_cache` serves last-good on `None` but CACHES `[]` as real data, so one Sonarr blip would have overwritten a good payload with an empty list. Now `fallback=None`, the house idiom (`base_instance_manager` ~542: *"lets us tell a FAILED fetch from a genuinely-empty list"*) | `GLD-CACHE-14` |
| 49 | `sonarr/cache/episode_files.py` | **File facts froze at row INSERTION — an upgrade after the row existed was invisible forever** (`GLD-EPF-14`). `sync_from_tautulli` resolved the episode file ONLY on the branch that CREATES a row; the `if key in existing_key` branch refreshed watch stats, stamped `last_synced_at` with now, and never re-read `episode_file_id`/`resolution`/`size_bytes`/`quality_name`/`video_codec` again for the life of the row. Textbook **P-C**: the row's PRESENCE treated as equivalent to its CONTENTS being current. Observed live on Loki 2026-08-13 with BOTH caches verified correct at the time: Sonarr held 12 files, all WEBDL-2160p, 67.4 GB, imported 08-12; the parquet held 6 rows at 480p/720p/1080p totalling 6.7 GB whose ids (35843, 35845, 35848, 41044, 41052, 51457) had ALL been deleted — **zero overlap, 10x understatement, wearing a four-minute-old sync timestamp**. Cost the largest single TV reclaim in the library: Loki is `keep_series` (no longer a downgrade veto after `GLD-TVQ-01`), scores 53 against a 75 UHD cutoff, sits at 2160p, and was absent from the plan entirely. Fixed: `_repoint_file_fields` re-resolves on the update branch and copies an explicit 25-name `_FILE_DERIVED_COLUMNS` whitelist — deliberately NOT the whole `_normalise` row, which builds a CREATION row setting `marked_for_deletion=False`/`available_until=None` and would have released every grace mark in the library on first run. No-op when the id already matches; `stats["repointed"]` makes recurrence visible. **Diagnosis cost two rounds**: TTL staleness was a real bug and was blamed for this one, because the symptoms all pointed at the cache (fresh timestamp, fresh payloads, and rows created AFTER an upgrade — See, Silo, Yellowstone — showing 2160p correctly, so the library read as partially right rather than uniformly wrong). The tell missed twice: **the values were byte-identical across three runs**, which is a write that never happens, not a read returning old data. Sim: Loki's six real stale rows replayed against the real refreshed episode cache — 6/6 re-pointed, ids resolve out of sequence (S1E1→67681, S1E2→67680) proving the pointer is read not assumed, 5.1→37.6 GB, nine accumulated fields incl. a `marked_for_deletion=True` row preserved, second pass re-points 0 | `GLD-EPF-14` |
| 50 | `machine_learning/lifecycle/restore_policy.py` | **`video_codec` was the one identity field the parquet already carried that the release record ignored** (`GLD-RST-01`). An x265 and an x264 encode at the same quality string and resolution are the same row to `match_release` but different playback (direct play vs transcode). Added to `RELEASE_FIELDS` and scored +1, matched against the release TITLE and custom-format labels since a `/release` row carries no mediaInfo, with alias folding (`x265`/`h265`/`hevc` are one family; unknown codecs fall through to a literal match so an unrecognised value never matches a different family). Additive and v2-safe — `release_record` skips absent fields, so pre-existing entries score without it. Sim on a real Loki row: given two candidates identical in group/quality/resolution, the recorded codec now decides instead of a size guess | `GLD-RST-01` |
| 51 | `sonarr/series/space_pressure.py` | **The step-down was the only path that destroyed a file without recording what it was** (`GLD-RST-02`). The delete pass writes `sonarr/{inst}/deleted_episodes` via `release_record`; this pass wrote nothing, so a downgrade was strictly one-way — once the file was gone nothing knew it had been a 2.18 GB x265 NTb file, and nothing could identify it in a recycle bin or an indexer search afterwards. Archive added on its OWN key (`stepdown_releases`), deliberately not `deleted_episodes`: that ledger is an INPUT to `restore_recovered_episode_deletions`, which re-grabs on score RECOVERY, so filing step-downs there would make the restore pass fight the space pass — every series shrunk under pressure queued to grow back the moment its score ticked up. Same entry SHAPE, so `ledger_releases`/`merge_ledger_entry`/`match_release` read it unchanged. Captures the `release/push` descriptor (title, protocol, publishDate, indexer, downloadUrl, torrentInfoHash) at the moment of destruction, since Sonarr history is finite. Secret-bearing query params are REDACTED on the way in — this ledger is plaintext JSON that `safe_cache_clear` deliberately PRESERVES, and an unredacted `apikey=` would be a credential on disk forever; a URL that cannot be confidently parsed is dropped rather than half-scrubbed. Sim: usenet + torrent payloads, `dry_run` writes nothing and makes zero API calls, all history-failure paths still archive the identity and drop only the descriptor | `GLD-RST-02` |
| 52 | `machine_learning/lifecycle/restore_policy.py` + `sonarr/cache/episode_files.py` | **Built, caller unwired — now WIRED** (`GLD-RST-03`). `HISTORY_GRAB_EVENTS`, `codec_from_title`, `history_release_record` and `merge_release_records` were correct, well-scoped and had NO consumer anywhere in the tree (**P-I**). The gap they close: the ledger identity is lifted off the parquet row at delete time, and `scene_name` is absent from EVERY entry on this library — so `match_release` scored at most 3 (group + quality + resolution) when the release TITLE alone is worth +3 exact / +2 substring. Sonarr's grab history knows that title. Fixed: `_enrich_recorded_from_history` reads `history?episodeId=` once per episode and merges via `merge_release_records` BEFORE the interactive search, so the enrichment strengthens the KEY rather than replacing the search. Ledger stays authoritative field-by-field — it describes the FILE that was on disk, history describes what was GRABBED, and a repack / manual import / external replacement makes those differ. Deliberately NOT a grab source: no guid is carried, because caches roll over in hours, a restore fires months later, and `_make_request` returns `None` on a soft reject — a dead guid fails QUIETLY and is worse than no guid because it looks actionable. Grabs only, so an import/delete/rename row can never be mistaken for the release taken; unparseable dates sort last rather than being dropped. Sim on the real ledger shape (no `scene_name`): given two candidates identical in group/quality/resolution, the search picked the x264 on size before and the recorded x265 after; all five degraded paths (endpoint raises, `None`, empty envelope, bare list with no grabs, garbage payload) return the ledger UNCHANGED so a restore can never be COST by the optimisation added to improve it; a ledger disagreeing with history keeps its own quality/resolution and takes only `scene_name` | `GLD-RST-03` |
| 53 | `machine_learning/lifecycle/restore_policy.py` | **The only permanent restore handle in a grab record was being discarded** (`GLD-RST-04`, operator: "I just added qbit to my *arrs"). Every other identifier decays — a `/release` guid dies when the indexer's search cache rolls over (hours), a usenet `downloadUrl` dies when the indexer drops the release, a SAB `nzo_id` dies with the queue entry. An infohash is CONTENT-ADDRESSED, so it stays valid as long as a swarm exists and needs no indexer, account or secret. `magnet_from_hash` rebuilds a magnet from 40-char hex or 32-char base32 (anything else returns None rather than emitting a magnet that would silently never resolve); `push_payload` builds the `POST /api/v3/release/push` body, preferring `magnetUrl` when an infohash is present and refilling redacted `apikey` placeholders from config so the secret lives in config and never in the ledger. A descriptor still holding a placeholder with no key supplied is DROPPED, not pushed — Sonarr would accept the URL, fail the fetch, and surface it as a dead release, which reads as a bad indexer rather than the misconfiguration it is. **Private trackers still need the passkey URL**: a bare magnet resolves via DHT/PEX and private trackers disable both | `GLD-RST-04` |
| 54 | `sonarr/series/space_pressure.py` | **Under TRaSH hardlinks a step-down freed ZERO bytes and booked the reclaim as real** (`GLD-RST-05`). Download dir and media root share a filesystem and *arr hardlinks on import, so a seeding torrent's data and the library file are two links to ONE inode; `DELETE episodefile/{fid}` unlinks the library path, qbit keeps the other link, the inode survives. `movie_files._mirrored_tmdb_ids` already states the rule — *"unlinking one of two hardlinks frees nothing"* — but scoped to cross-instance mirrors, with qbit as an unguarded second link holder (**P-J**). Worse than mis-reporting: `realized_reclaim_gb` feeds the exhaustive stop condition, so the pass believed it was gaining space, kept going, AND grabbed replacements — real new bytes against a reclaim that never landed, i.e. the 2026-08-08 phantom-headroom failure with a different cause. Split into `pinned_reclaim_gb`/`pinned_files`; `realized_reclaim_gb` stays the honest "what we unlinked" figure for reporting and only the CONFIRMED remainder moves `_net`. Detection is free — `_archive_stepdown_release` returns the descriptor it already fetched and `info_hash` is the torrent signal. Conservative in one direction only: a torrent qbit already removed still reads as pinned, so `pinned_reclaim_gb` is an upper bound, matching `bin_forecast`'s rule that uncertainty may only make the system LESS aggressive. Sim: overstatement 10.25 GB → 0.00 on an 8-file batch; near the target the old accounting stopped after 10 files believing it had arrived | `GLD-RST-05` |
| 55 | `machine_learning/space/seed_gate.py` **(NEW)** + `services/qbittorrent/client.py` **(NEW)** + `sonarr/series/space_pressure.py` + `config.json` + `reference_keys.json` | **Stepping down a pinned file is NET NEGATIVE — it frees nothing and downloads a replacement** (`GLD-RST-06`). `GLD-RST-05` stopped the ledger lying about it; this stops the pass doing it. The gate does NOT recompute seed criteria — that would duplicate, badly, a decision Sonarr already makes (Completed Download Handling → Remove drops the torrent once the indexer's `seedCriteria` are met), and a duplicate ladder can drift out of agreement (**P-E**). The reliable question is one boolean: **is the infohash still in the client?** Gone ⇒ the library link is the last one ⇒ net positive. `states_for` returns `(records, reachable)` because "the client says no" and "the client did not answer" are opposite conclusions (**P-C**); login checks the response BODY since qBittorrent answers a bad credential with HTTP 200 and `Fails.`. Gate placed BEFORE the interactive search and the budget slot REFUNDED on defer — a deferral performs no search, so charging it a slot would let pinned files starve the pass of actionable ones. **`min_seed_time_hours`/`min_seed_ratio` are ADVISORY ONLY and documented as such**: physical link presence dominates both, so wiring them into the decision would have been a **P-A** — computed every candidate, appearing to gate, consuming nothing. `max_defer_days` bounds the guard's own failure mode (unset seedCriteria ⇒ defer forever ⇒ the file silently leaves the reclaim pool); `unbounded_seeding` names the config that causes it. Broken/missing/garbage config stays CLOSED. Sim: 60-file batch two-thirds pinned — same 20 searches, same 20 actions, **+3.00 GB → +31.00 GB net**; every gate branch incl. paused (pins as hard as uploading) and `missingFiles` (no second link ⇒ proceed) | `GLD-RST-06` |
| 56 | `services/acquisition/breakdown.py` **(NEW)** + `services/acquisition/__init__.py` + `test_elevation_breakdown.py` **(rewritten)** | **Elevation breakdown is now records-first, not prose.** `_log_elevation_breakdown` printed a 4-5 line free-form stanza per acted-on title; it now delegates to a pure `breakdown` module that builds ONE canonical record per title (`SCHEMA`, 41 cols, `SCHEMA_VERSION=1`) and projects it into seven boxed tables — evidence, **score decomposition** (per-signal contribution in score points, summing back to the score, with the dynamic denominator per row), profile rationale key, cohort roll-up by source, signal coverage, genre affinity legend, taste profile. The prose repeated two RUN-CONSTANTS on every row — the household genre weight (`adventure(1.00)` is 1.00 for every candidate, always; 79 mentions across 14 distinct weights in one sample) and the ~110-char profile rationale (3 distinct strings printed 22×) — both now emitted once as legends that are **pure aggregations of the same records**, so there is no second source of truth. The decomposition table is strictly NEW: the matrix was previously only a `log_debug` dump (⇒ `GLD-ACQS-06` partly closed). Frame also persisted to global-cache `acquisition/breakdown` for the website generator. 🔴 **The conversion forced a P-C**: `scorer.score` only sets `evidence["people"]` when the title resolves in the people-matrix, so *absent* (never computed) and *`matched=0`* (computed, nobody) are different facts the prose distinguished only by line-presence — a table cell has no absent rendering, and a blank/0 would assert a check that never happened. `people_scored` carries it explicitly, absent renders `not scored`, and `to_dataframe` pins nullable dtypes so a stray `fillna(0)` downstream cannot reintroduce it. Also removed the manager's now-orphaned `_fmt_votes`/`_fmt_feed` statics (the prose emitter was their only production caller — leaving them beside the module's copies would have been a self-inflicted P-E). Verified: `py_compile` on every edited file; a `simroot` harness with `_box_table` extracted **verbatim** from `logger.py` (real widths/NBSP, not an approximation) replaying old prose vs new tables over a 25-title fixture and asserting every title/score/genre-name/genre-weight/feed/rating/vote/year/cast-crew figure and taste-profile name survives, both P-C states render distinctly, contributions sum to score, legends are faithful aggregations, schema complete — all pass; rewritten test module 12/12 | `GLD-ACQS-06`, `GLD-ACQS-10`, `GLD-ACQS-11`, `GLD-ACQS-12` |
| 57 | `machine_learning/sizing/anomaly.py` + `sonarr/cache/episode_files.py` + `test_anomaly.py` + `test_size_anomaly_remediate.py` | **Size-anomaly remediation was a no-op loop that re-fired forever** (`GLD-SON-13`/`-14`). Two independent causes, measured on consecutive runs: 496 anomalies, 232 oversized, ~433 GB "reclaimable", **the same 38 Dragon Ball Z episodes searched both times, every figure byte-identical**. (a) **Wrong route.** `_JUNK_OR_SD_GRADES` stopped at `Bluray-576p`, so `HDTV-1080p` at 5x expected bitrate went down the RE-GRAB path — but broadcast bitrate is capped far below disc bitrate, so an HDTV file that big is a mis-GRADED disc source, and a re-grab cannot help it: the search asks for an UPGRADE and a smaller file is never one. Renamed to `_MISGRADE_WHEN_BLOATED` (the old name stopped describing the set) with `HDTV-720p`/`HDTV-1080p` added and a back-compat alias; `HDTV-2160p` deliberately EXCLUDED because UHD broadcast is genuinely high-bitrate and has no higher HDTV tier to be mistaken for. Live cohort: 39 fruitless searches -> 1. (b) **No bound, no detector** (*P-D*). Added a pure attempt ledger (`attempts_key`/`should_attempt`/`record_attempt`/`prune_attempts`, config `max_regrab_attempts=3`, `regrab_retry_days=7`): after 3 attempts with the size unchanged a file is ABANDONED; a size CHANGE resets the budget (the file moved, so its history no longer describes it); entries are pruned once a file stops being anomalous. `size_bytes` added to the anomaly row — the 2dp display GB is too coarse for change-detection. **P-C at the core of it**: an absent/unparseable size is UNKNOWN, and reading it as "changed" would reset the budget every run and restore the exact churn the ledger exists to stop — `_as_int` returns None rather than 0, and the ABSENT-entry case is documented as safely conflated with zero-attempts *because the answer is genuinely the same*, unlike the size. Ledger write is dry-run gated. ⚠️ **The pre-existing test file was already stale** and would have masked this: it still asserted `episodefile/20 DELETE` (removed by `GLD-SON-18`) and exact-equality stats predating `GLD-SON-19`'s counters — brought current and extended. Verified: `py_compile` on all four files; a simulation over the live cohort shapes reproducing the 39->1 routing shift and the run-by-run convergence (search, search, search, abandoned, abandoned, abandoned); the sim CAUGHT a real defect mid-build (non-numeric `size_bytes` raised instead of degrading) which was fixed and re-verified; 21/21 pure tests pass | `GLD-SON-13`, `GLD-SON-14`, `GLD-SON-15`, `GLD-SON-16`, `GLD-SON-17` |
| 58 | `machine_learning/acquisition/space_budget.py` **(NEW)** + `services/acquisition/__init__.py` + `breakdown.py` (SCHEMA v2) + `test_space_budget.py` **(NEW)** + `test_space_budget_selection.py` **(NEW)** + `config.json` | **Acquisition is now governed by BYTES, not counts** (`GLD-ACQS-13`/`-14`, operator ruling: "I don't care about max adds per run if we are staying below the free space limit"). The count cap was the ACCIDENTAL safety rail: `_space_band` memoises one free-space snapshot per instance per run, `cap=0` already meant uncapped, and `expected_size_gb` — the one field a byte budget needs — was computed for every candidate and consumed by exactly one display formatter (**P-A**, now closed by consumption). Selection now funds candidates in priority order out of `max(0, free-U)` minus IN-FLIGHT bytes, skip-and-continue (a 40 GB refusal does not strand the cheap shows behind it; the big title keeps first claim on next run's refreshed budget). **The committed ledger is the anti-phantom-headroom half**: run N reads free=3800 and commits 400 GB of grabs; run N+1 two hours later reads the SAME 3800 (downloads still queued) — without netting, it commits the same 400 again, `GLD-RST-05` with the sign flipped. Entries commit on `added` only (never `would-add`); DEFERRED adds commit at FLUSH time — that is when their bytes become in-flight — with the price riding the queue record's new `gb` field; TTL-reconciled (72h) at snapshot; pruned-on-write; write failure warns LOUDLY (a silent miss = over-commit next run). **The 4K companion is priced LIVE inside the add loop** — it is planned after selection and is the largest file class in the system, so an upstream-only budget would govern everything except the biggest bytes; refusals render as `skipped [4k] (space budget)`. **FAIL DIRECTION DELIBERATELY INVERTED** (§8 P-C entry): every other space gate fails OPEN because a count cap bounds the damage at N; under an uncapped byte budget "open" means unlimited-with-no-budget, so unreadable free space / corrupt ledger / raising cache collapse to the BOUNDED count-cap fallback, and a configured cap of 0 falls back to 10, never unlimited. Unknown candidate sizes price at conservative defaults (movie 15 GB, episode 2 GB × ONE pilot), never 0. `shared_pool` default TRUE: one Unraid array behind TRaSH hardlinks — per-instance pools would double-spend the same free space; budget = MIN headroom across routed instances. SCHEMA v2 appends `space_charge_gb`/`space_pool` so the website frame carries the price. Module default `enabled:false` (bare-config testers keep legacy byte-identically); THIS deployment's config enables it. Verified: pure-module sim (skip-and-continue exact sets, companion charge/refuse, cross-run netting 500→410, TTL release, P-C hard cases, unknown-pool pass-through, hard_max, two-run convergence trace); the REAL `_space_budget_context` source exec-extracted and tested against stubs across all 8 mode/failure paths; breakdown fidelity sim + 12/12 tests re-green on SCHEMA v2; 14/14 new pure tests; `py_compile` on all six files; config JSON re-parsed valid. Open follow-ups: `-15` hasFile reconcile, `-16` out-of-view commit paths (saga/universe/rehome), `-17` fairness-under-budget (moot while `demand.enabled=false`); `GLD-ACQS-10` superseded per the ruling; supply now binds at `recommendation_limit` (left at 20 — raising it multiplies GLD-PERF-01 metadata probes, operator call) | `GLD-ACQS-13`, `GLD-ACQS-14`, `GLD-ACQS-15`, `GLD-ACQS-16`, `GLD-ACQS-17` |
| 59 | `factories/onboarding/schema.py` + `env_map.py` + `test_schema_env_map.py` + `config.json` | **Onboarding now generates every knob sessions 91's features added — and its stale prose is corrected.** `empty_config()` seeds `acquisition.space_budget` (module defaults, `enabled:false` so a fresh install keeps the legacy count-cap slice byte-identically — a new test pins `schema == space_budget.DEFAULTS` so the two cannot drift) and `size_anomaly.max_regrab_attempts`/`regrab_retry_days`; `people_cooccurrence` added to the seeded `sources` (the live config had it, generated configs never exposed it). `_DOC_LEAVES` gains four rows (`space_budget.enabled`/`.hard_max_adds`, both ledger knobs) so `--print-env-template` emits them headlessly. ⚠️ **Two stale descriptions corrected**: the schema comment and the `size_anomaly.remediate` doc leaf both still described the pre-`GLD-SON-18` behaviour — "re-grab … (delete + research); DESTRUCTIVE" — when the shipped path is SEARCH-ONLY (file kept until the replacement imports, retries bounded by the attempt ledger); a tester reading either would have declined a feature on the strength of a hazard that no longer exists. Exact-equality schema test updated (it pinned the six-key `size_anomaly` dict and would have failed the seed). Operator `config.json` gains the two ledger knobs explicitly (space-budget block already present from #58). Verified: `py_compile` on all four; sandbox exercise of the REAL `empty_config`/`deep_merge`/`_DOC_LEAVES`/`generate_env_example` — seeds exact, overlay merge preserves operator overrides while defaults fill in, schema==module DEFAULTS, env template emits the new vars, no stale remediate wording survives; live config re-parsed | `GLD-ACQS-13`, `GLD-SON-13`, `GLD-SON-18` |
| 60 | `services/acquisition/__init__.py` + `test_space_budget_selection.py` | 🔴→✅ **FIRST LIVE RUN OF THE BYTE BUDGET: it fell back on a healthy system and could never have armed itself** (`GLD-ACQS-18`). Log line: *"space budget: committed ledger is corrupt (not a list) — falling back to the count cap"* — on a ledger key that had NEVER been written. `GlobalCacheManager.get` returns **`{}` for a missing key** (its own compat wrapper documents `{}` as the missing sentinel), and the context builder's `raw is not None and not isinstance(raw, list)` read that sentinel as corruption. Worse than one bad run: fallback mode never persists the ledger, so the first read repeats forever — **a deadlock disguised as a safety fallback**, found only because the fallback is LOUD (had it been silent, the budget would simply have never existed). Misdiagnosis mechanics per convention: (a) the tell in the code — the deferred-queue read six lines ABOVE the new block does the tolerant `isinstance` coercion for exactly this cache behaviour, as does the size-anomaly ledger read; the siblings knew the sentinel and the new code did not look; (b) the tell in the harness — the exec-verification GC stub returned `None` for missing keys, so all 8 mode paths passed against the WRONG sentinel; stub fidelity to the real dependency's edge semantics is part of the harness's job. Fixed: falsy (`None`/`{}`) = first-run empty; only a TRUTHY non-list is corrupt; raising reads unchanged (fallback). Re-verified by exec-extracting the fixed method against a stub with the REAL `{}` semantics — first-run arms with headroom 4010.9−3300≈711 GB, truthy-dict and raising reads still fall back, in-flight netting intact; both files `py_compile`; the manager test's absent-key case now covers `{}` explicitly | `GLD-ACQS-18` |
| 61 | `services/tautulli/users/__init__.py` + `test_family_scope.py` **(NEW)** + `factories/onboarding/schema.py` + `env_map.py` + `test_schema_env_map.py` + `config.json` | **Non-family viewers no longer steer the household taste maps** (`GLD-TAUT-12`, operator: "if a user is NOT in the family, they do NOT affect the family genre mapping — but build their own gradings"). One choke point does it: `compute_genre_affinity` (the sole feeder of `tautulli/affinity`, confirmed at the parent's single call/write site) now scopes its history through `_family_scope` when `household_affinity.family_only` is on — every downstream consumer (acquisition genre signal, breakdown taste profile, playlists' household component, watch-likelihood affinity branch, movie-scorer people maps) inherits the scoped aggregate for free; `compute_per_user_genre_affinity` receives the FULL history independently, so the outsider's own matrices keep building untouched. **Family = the `plex/identity_map` the Plex users pass already persists** (zero new Plex code — `_persist`'s own comment says it exists because that manager runs after its consumers); measured run order (Tautulli aggregates at log-line 29, PlexUsers at 261) means the map read is the PREVIOUS run's, so the first run after enabling degrades OPEN with a loud `UNSCOPED` warning — the household aggregate feeds the whole system and emptying it would be far worse than one run of drift. `{}` treated as the missing sentinel from the start this time (the `GLD-ACQS-18` lesson, one session old). Unknown-owner plays kept and counted (stated trade: family plays dominate and Tautulli reliably stamps `user_id`; dropping unknowns would starve the family to guard a rare leak). Scoped runs append `[family-scoped: K/N plays … excluded M play(s) across U non-family account(s)]` to the affinity summary. Onboarding seeds `household_affinity.family_only=false` (fresh installs byte-identical) + doc leaf `GLIDEARR_HOUSEHOLD_AFFINITY_FAMILY_ONLY` + parity-test row; operator config set `true`. ⚠️ **Deliberately out of scope, filed**: the parquet `is_watched` flags stay household-agnostic (`GLD-TAUT-13`) — an outsider's stream still marks titles watched for engagement floors, retention gates and the people-matrix watched-set; that is per-consumer provenance surgery and the delete-guard half of `GLD-ACQS-19`. Verified: `py_compile` on all five; the REAL edited manager + REAL brain exercised in a sandbox package against the true `{}` sentinel (default passthrough byte-identical; Stacee-shaped outsider's 5 plays excluded while her per-user matrix still builds; note fragments exact; absent/id-less/raising map all degrade open with warnings; str/int id equivalence); repo test file itself run 7/7; onboarding seed+overlay+doc-leaf+env-template re-verified with earlier seeds intact; live config re-parsed | `GLD-TAUT-12`, `GLD-TAUT-13` |
| 62 | `services/tautulli/users/__init__.py` + `test_family_scope.py` + `README.md` + `DESIGN.md` | **The scoping claim is now evidenced, not asserted** (`GLD-TAUT-14`, operator: "display the results for the non-family accounts separately as well so we can see they are being graded"). §0.1 #61 shipped the behaviour — outsiders lose the household vote, keep their own grading — but a run could not CONFIRM the second half: the log printed a household total and a bare COUNT of per-user matrices, so *an outsider graded* and *an outsider silently dropped* rendered identically (both invisible). New boxed table `[TautulliUsers] per-account affinity gradings`: `Account \| Scope \| Genres \| Actors \| Directors \| Top genres`, family rows first, then outsiders, richest grading first. **Classification cannot contradict the aggregate**: `_family_ids()` extracted as the SINGLE non-logging resolver both `_family_scope` and the table read (two copies could drift and let a row print `family` for an account the aggregate excluded — a table lying about the exact thing it exists to prove); it never logs because two consumers would fire one condition's warning twice, so each caller renders `reason` (`unreadable: <err>` / `no-ids`) in its own voice. **Refuses to guess**: unresolvable id set, or `family_only` off → Scope renders `-` and the caption states `scoping is INACTIVE` — mislabelling a family member as an outsider would be worse than showing nothing. **Three states, not two** (P-C): `no history in window` (brain omits zero-entry users) vs `none` (graded, matched nothing) vs a row that never appears (never offered to the grader) — which ANSWERS the long-open `GLD-TUS-05`/Q3 ("omitted or zero-valued?" → omitted) and makes the answer visible at run time for the first time. ⚠️ **The stub-fidelity gap from `GLD-ACQS-18` recurred one session later**: the existing `_Log` stub had no `log_table`, so the first sandbox run died on `AttributeError` — caught here only because the new code calls it on every run; the stub now mirrors the real logger's surface and RECORDS tables, which is what made content assertions possible at all. Verified: `py_compile` on both files; 14/14 tests run against the EXACT repo sources (7 pre-existing family-scope green after the `_family_ids` refactor + 7 new: outsider-graded-while-scoped-out, an exact cross-check that every row's Scope matches `_family_scope`'s own kept-id set, the three-state P-C rendering, both refuse-to-guess paths, ASCII-safety + ordering, empty-user-list no-ops); table rendered through `_box_table` extracted VERBATIM from `logger.py` — uniform 95-char widths, cp1252-encodable; an unused `graded` counter caught and dropped before the repo write (a self-inflicted P-A) | `GLD-TAUT-14`, `GLD-TUS-05` |
| 63 | `services/sonarr/cache/episode_files.py` | 🔴→✅ **The JIT restore pass wrote during a DRY RUN, called four rejected PUTs successes, and deleted the evidence needed to retry them** (`GLD-SON-20`). Caught in the 2026-08-20 14:14 log: the boxed table said `restored: 4, failed: 0` four lines below four `PUT /episodefile/... 400 Bad Request` warnings. The log itself proved the contract — if `_make_request` raised, the method's `except` would have counted them; it did not, so the call LOGS and RETURNS. Four defects, one method: **no dry-run gate at all** (it PUT to Sonarr on a `dry_run=True` run while every comparable pass in the file gates on `effective_dry_run`); **the PUT result was never checked** (P-D — `restored` incremented unconditionally and a success line printed for a write the server refused); **the same path then cleared `pre_upgrade_quality` + `upgraded_for_watching` and SAVED the parquet**, so the cache claimed a rollback that never happened and discarded the only snapshot a retry needs — the failure destroyed its own remedy; and the pilot-floor SKIP branch counted as `restored` too. The 400's cause was the payload: the snapshot's `resolution` comes back off the parquet as JSON and can be `"720"`/`720.0`/numpy int, which Sonarr rejects for `System.Int32` — now `int(float(...))`-coerced, falling back to the CURRENT value rather than sending junk. Fixes: `effective_dry_run` gate with withheld rollbacks counted as `would-restore` (snapshots kept); falsy PUT → `failed` + warning with snapshot AND flag left INTACT so next run retries; `skipped-floor` counter split out; `checked` surfaced in the table (tallied every pass, never displayed — small P-A); every row now carries a description saying what it IS. Verified by exec-extracting the REAL fixed method and replaying the production shape (4 Blue Bloods episodes, API stub mirroring the log-and-return contract): rejection → restored 0 / failed 4 / snapshots intact / no success line; dry run → **zero PUTs issued**, 4 would-restore, snapshots intact; acceptance → 4 restored, snapshots cleared, quality columns rolled back; resolution coercion asserted across `"720"`, `720.0`, `"720p"`, `None`; `py_compile` clean | `GLD-SON-20` |
| 64 | `services/tautulli/users/__init__.py` + `test_family_scope.py` + `README.md` + `DESIGN.md` + `factories/onboarding/schema.py` + `config.json` | **Two logins, one viewer** (`GLD-TAUT-15`, operator: "Are we able to build a join between accounts … have both playlists show the same thing, have it grade all together?" — `Mom` and `mirandan75` confirmed the same person). §0.1 #62's gradings table is what surfaced it: the two accounts sat side by side with 16 genres/820 actors against 5/236, one person's taste split across two thinner matrices. New top-level `account_links` (list of groups, first member PRIMARY, members may be usernames or `user_id`s, empty = off and byte-identical): `_link_history` rewrites every member's plays onto the primary BEFORE the brain groups them, so the union is graded once; `_fan_out_links` then gives the merged matrix back to every member. **The fan-out is the load-bearing half** — without it the secondary's plays are rewritten away, it matches nothing, drops out of the result and falls back to household defaults, so linking would leave that account WORSE than not linking; there is a test pinned on exactly that. Three invariants, each tested: entries are SHALLOW-COPIED because the same list is handed to `compute_genre_affinity`, and rewriting `user_id` in place would re-route plays through the family filter and corrupt the household aggregate; each member receives its OWN copy of the matrix, so a consumer annotating one account cannot write through to the other; and a link NEVER widens the family aggregate — family means Plex HOME membership, and letting a per-viewer grading knob admit a non-Home account would reopen the exact leak `family_only` closes. Table gains a `Link` column (`primary` / `-> primary`) so two accounts showing identical numbers read as the declared join rather than a coincidence. ⚠️ **A latent bug surfaced while writing the env contract**: `account_links` is a list of LISTS, which the comma-separated `_DOC_LEAVES` convention cannot express — and a flat `["a", "b"]` (exactly what such an overlay would produce) would have iterated the STRINGS as characters and built aliases out of single letters. Rather than ship a headless path that silently produces a wrong config, the shape is now rejected with a warning and no env leaf was added; the schema seeds `[]` with the shape documented inline. Verified: `py_compile` on both code files; 26/26 tests against the EXACT repo sources (14 pre-existing + 12 new: merged-union grading, both members identical, the fan-out failure case, caller-list immutability, per-member copy isolation, case/id insensitivity, byte-identical default, unknown-primary warns, one-member no-op, flat-string rejection, five garbage shapes, Link labels, and family-aggregate non-widening); table re-rendered through `_box_table` extracted verbatim from `logger.py` at a uniform 117 chars, cp1252-clean; live config re-parsed. Table assertions rewritten to resolve cells by HEADER NAME — the table has gained a column twice now and positional asserts broke silently both times. ⚠️ Half the operator's ask: identical GRADINGS ship here, identical WATCHED-SETS are `GLD-TAUT-16` | `GLD-TAUT-15`, `GLD-TAUT-16` |
| 65 | `machine_learning/sizing/quality_caps.py` **(NEW)** + `size_model.py` + `test_quality_caps.py` **(NEW)** | **A 50.9 GiB "Bluray-720p" — and the second-order damage nobody would have seen.** *arr grades on FILENAME, never bytes: `The.Godfather.3.1990.720p.BluRay...` parses as `Bluray-720p` whether the payload is an 8 GiB encode or a disc image, so nothing refuses it at grab time and `anomaly.py` only catches it after download+import+hardlink. Two fixes. **(1) `GLD-SIZ-12`, the ratchet** — found by checking the calibration path rather than assuming: `measured_stats` filters only to `[0.5, 900]` MiB/min, which its OWN docstring calls a guard against corrupt runtimes. 321.7 passes it comfortably, lands in the tier's plain `.mean()`, raises `expected_size_gb`, and therefore raises the `over_ratio x expected` threshold meant to catch the next one — **the detector calling a file an anomaly while the calibrator averages it in as a legitimate sample**. Demonstrated with the real function on an n=18 tier: `65.00 → 78.51 MiB/min (+20.8%)`, threshold `195 → 236`. Fat tiers are near-immune (n=1170 moves +0.4%); THIN tiers are where it bites, and thin tiers are exactly where a positive measured entry wins outright at resolution. Fixed with median-anchored rejection — median because the mean is what the outlier is already corrupting — gated at `n >= 5` because an outlier cannot be identified in a sample of one; OFF by default, `dropped` reported per tier so a rejection is never silent. **(2) `GLD-SIZ-13`, the ceiling** — Glidearr does not pick releases (it triggers *arr's own search), so the cap must live where the picking happens: `qualitydefinition.maxSize`, in MB/min, the same shape as this package's MiB/min model. Multiplier is `size_anomaly.over_ratio` so the two enforcement points cannot drift: *if we would flag it after the grab, refuse it before*. Replayed against the real search page (runtime 162, `Bluray-720p` 52.4 MiB/min n=1170, cap **164.8 MB/min = 24.9 GiB**): the 50.9 GiB CyTSuNee (6.14x), the 49.2 GiB BD50 (5.93x) and the 28 GiB split archive (3.38x) are refused; 19.9 / 18.2 / 13.9 / 12.2 / 11.9 / 9.9 / 8.6 / 5.9 GiB all pass. Four invariants: **caps may only TIGHTEN** (a poisoned rate would otherwise widen the ceiling — second guard on the ratchet, tested with a 321.7 rate against an existing 165 cap); **thin tiers get no cap** and that check precedes the tighten check (too LOW a cap starves a tier silently, with no error — just an *arr that mysteriously stops grabbing); `MIB_TO_MB` applied (skipping it over-tightens by 4.9%); **unlimited (`None`) is never read as 0** (P-C — 0 would compare as tighter than any proposal and suppress every cap). Every definition lands in proposals or skipped WITH a reason — none dropped silently. Context: the operator's own collections run validated the model at 344 GB predicted vs 359.4 GB actual (**4%**) once Godfather was pulled — but mid-run, with that one release at 28% of the queue, a 1.4x inflation factor was nearly coded into the estimator permanently. Verified: `py_compile` both; 17/17 tests against the EXACT repo sources; one test-expectation error caught and corrected (thin-sample must precede tighten — the CODE was right). ⚠️ Pure planner only; nothing calls it yet — wiring is `GLD-RAD-34`, and `radarr/cache/quality.py` already fetches the definitions and uses them for a COUNT (P-A) | `GLD-SIZ-12`, `GLD-SIZ-13`, `GLD-RAD-34` |
| 66 | `machine_learning/sizing/quality_caps.py` + `test_quality_caps.py` | **The ceiling's write path, built ONCE for both services** (operator: "Sonarr as well"). MiB/min is a BITRATE, so the arithmetic is identical for a 162-minute feature and a 42-minute episode — the earlier worry that "Sonarr's per-episode rates make the runtime math different" was wrong, and the only things that legitimately differ are the source parquet (`episode_files` vs `movie_files`) and a presentational runtime label. So `render_rows` and `push_caps` live in the brain with `put`/`logger`/`runtime_minutes` INJECTED, and each service contributes a thin adapter. That choice is defensive, not tidy: Radarr and Sonarr already keep two of everything around quality definitions and the pairs have **already drifted** — different cache-key FORMATS (`radarr.{i}.quality.definitions` vs `sonarr/{i}/quality_definitions.json`), different fetch calls (raw `_make_request` vs a named API method), different summary wording. A second copy of a diff table and a write loop would have drifted identically (P-E). `push_caps` carries the `GLD-SON-20` lesson explicitly: `_make_request` LOGS and returns a falsy fallback rather than raising, so a falsy return is checked and counted as FAILED with a warning, never as an applied cap; and because this writes *arr CONFIGURATION — which outlives the run and governs every future grab, including ones Glidearr never initiates — the dry-run gate is checked before any write and withheld writes are REPORTED as `would`, never silently skipped. Verified: `py_compile`; 23/23 against the EXACT repo sources (17 prior + 6 new: dry run issues zero writes and marks the title, an armed write round-trips `id`/`minSize` and only changes `maxSize`, a rejected write counts failed + warns, one renderer serves both services with only the runtime label differing, every definition reaches the table carrying its skip reason and is cp1252-safe, an empty plan logs no table). ⚠️ Still nothing CALLS it — `GLD-RAD-34` and `GLD-SON-21`. Found while tracing Sonarr's write primitive: `sonarr/api/client.py` is a **zero-byte file**; the real primitive is the shared `BaseInstanceManager._make_request`, same as Radarr | `GLD-SIZ-13`, `GLD-RAD-34`, `GLD-SON-21` |
| 67 | `machine_learning/size_calibration.py` + `sizing/quality_caps.py` | **The ratchet fix was shipped DORMANT, and the caps needed no parquet after all.** §0.1 #65 added `outlier_ratio` to `measured_stats` with a default of `None` — correct for a byte-identical pure module, but it meant `GLD-SIZ-12` was fixed and *not running*: nothing passed the argument. `SizeCalibrator` now reads it from **`size_anomaly.over_ratio`** and passes it at BOTH `measured_stats` call sites (the Sonarr episode parquet and the Radarr warm movie snapshot). Sourcing it from `size_anomaly` rather than a new knob is the whole point — the calibrator and the detector cannot disagree about the same file. Degrades to OFF when `size_anomaly` is absent, empty, zero, negative or unparseable: a deployment that never opted into anomaly detection has not said what it considers anomalous, and inventing a number there would silently reshape every size estimate it makes. ⚠️ **Known coupling, stated rather than discovered later**: lowering `over_ratio` to manufacture test anomalies ALSO starts rejecting legitimate files from the calibration sample. That is the price of one threshold governing two enforcement points; a test-only threshold needs its own key (see §5 outstanding). Second finding: the cap adapters need **no parquet access at all** — an assumption corrected by reading `SizeCalibrator`, which already measures the live library every run and persists BOTH halves to `size_model/calibration` (`table` = rate, `counts` = sample size, two separate maps). New pure `measured_from_calibration` reassembles them, so each adapter is one cache read. P-C in that bridge: a tier present in `table` but ABSENT from `counts` gets **n=0**, never an assumed-large sample — verified end-to-end that such a tier is refused by the thin-sample guard and never capped, because a ceiling from an unknown sample size starves a tier silently. Verified: `py_compile` both; `_outlier_ratio` exec-extracted with the REAL `_cfg_get` and checked across 8 config shapes; bridge checked against the real persisted payload shape plus 7 garbage inputs; 23/23 still green | `GLD-SIZ-12`, `GLD-SIZ-13` |
| 68 | `machine_learning/ledger/pending_plan.py` **(NEW)** + `test_pending_plan.py` **(NEW)** + `ledger/plan_summary.py` + `services/acquisition/__init__.py` + `main.py` + `machine_learning/acquisition/space_budget.py` | 🔴→✅ **The change-plan grid omitted the largest byte flow in the run, while titling itself "every planned action this run"** (`GLD-SPC-01`). The 2026-08-20 19:23 log reported `acquire 19 / -25.2 GB` (every row a Sonarr next-up episode; **no `> movies` sub-row at all**) and `TOTAL +242.7 GB` — which reads as a run that FREES space — while the acquisition pass in the SAME run reported `97 funded (~312 GB)`. True net: about **-68 GB**. A 310 GB swing in the one table an operator uses to sanity-check a run before arming it. **Not a forgotten call — a missing mechanism**: `decision_ledger.stamp(df, idx, …)` writes `planned_action`/`plan_reason`/`plan_reclaim_gb` onto PARQUET ROWS and `plan_summary.summarize()` builds the entire grid by grouping those columns, so a title being ADDED for the first time is structurally invisible — it has no row to stamp because it is not in the library yet. Fix is the rowless half: new pure `pending_plan` (cache-backed, keyed so a re-entrant pass overwrites rather than double-counts — the `reclaim_ledger` discipline), folded into the SAME two accumulators `summarize()` builds, preserving the property it maintains deliberately (**every parent row equals the sum of its subtotals**, asserted in test). Sign convention inherited verbatim from `decision_ledger` (+GiB freed / -GiB consumed), so an acquisition is negative; an unparseable GB is DROPPED, never stored as 0 — a silent zero would understate consumption in the very table that exists to catch understated consumption (P-C). Recorded for `would-add` too, because the grid is a PREVIEW and a dry run showing nothing here is exactly how the omission stayed invisible. `reset_pending` placed beside `reset_planned_reclaim` in `main.py`, with the OPPOSITE failure direction noted in the comment: a stale reclaim entry makes passes think the deficit is covered and reclaim nothing, while a stale pending entry ADDS last run's consumption to this run's total — and the natural reaction to an over-reported fill rate is deleting media that did not need deleting. **Verified in production the same evening**: the 20:23 run rendered `acquire 29 / -58.2` with `> movies 6 / -31.2` and `> tv shows 23 / -27.0`, reconciling exactly against `10 funded (~33 GB)` and `10 acted on (4 show / 6 movie)` — 6 movies + 4 shows, and 31.2 + ~1.8 ≈ 33 GB. Also bundled: `GLD-ACQS-20`, found by reading that same run. 11/11 new tests against the EXACT repo sources; 14/14 space_budget still green; `py_compile` on all five; a CJK character (`真`) that slipped into a `plan_summary` comment was caught on readback and removed — the encoding residue the register's own integrity check exists for | `GLD-SPC-01`, `GLD-ACQS-20` |
| 69 | `services/backup/__init__.py` + `test_backup.py` + `machine_learning/space/routing_targets.py` + `test_routing_targets.py` + `services/routing/__init__.py` + `services/routing/uhd_reconcile.py` + `services/radarr/quality/universe.py` + `plex/playlists/{builder,movie_builder,combined_builder}.py` | **Five defects found by reading ALL the logs for the first time.** Until this session only `default.log` was ever read; the run also writes `routing`, `playlists`, `franchise_regen`, `pilot_search_daemon`, `enrich_daemon`, `audit`, `relocation` and a 36.9 MB `timings.json`. **(1) `GLD-BKP-10`** — a LIVE run degraded over a backup that had SUCCEEDED: the *arr lists a zip only after flushing it, so the immediate `_list_backups` is a race lost in proportion to DB size; radarr:standard's 218.8 MB file was stamped `20.34.00`, seconds after the manager gave up and moved to the next instance. Now polls the listing (`LISTING_SETTLE_S`, 90s) — not a longer command timeout, because the command HAD completed. **(2) `GLD-BKP-11`** — the precise failure `detail` was computed on every path and discarded by a warning built from result KEYS alone (P-A), in the gate that blocks every live run. **(3) `GLD-BKP-12`** — stale-backup fallback per operator ruling: a valid 24-72h backup arms the gate rather than costing a run, validated identically to a fresh one (a recent 0-byte file still disarms) and warned with its age. **(4) `GLD-ROU-07`/`-08`** — `reorg_mode` forced a choice between two INDEPENDENT axes, so an operator with BOTH consents armed still ran one log-only: 121 same-instance misplacements re-planned every run forever. New `all` mode arms both, each still gated by its own consent; the redundant second `mode == "same_instance"` test removed so one gate lives in one place; the routing log header now states capability instead of intent. **(5) `GLD-PLX-11`** — all three playlist builders labelled output `[dry-run]` on a run where writeback ARMED and updated 28 playlists. ⚠️ **THREE self-corrections this session, all the same shape**: I called `cross_instance` "no apply path", then "inert", then "nothing reads it" — it is 1,262 lines in `uhd_reconcile`, named in a table at `routing/__init__.py` line 24 that I grepped past twice; I nearly shipped a DUPLICATE gate (`cross_instance_relocation_enabled`) beside the existing `cross_instance_move_enabled`, caught on an import line; and I reported `audit.log` had "zero entries today" from a date grep against a file containing **no timestamps at all**. The tell each time: inferring from one module instead of reading the one it points at. ⚠️ **My universe fix also shipped a bug that its own test caught** — counting 46 of 51 rows because `None` survives `.astype(str)` as a FLOAT nan (the `.str` accessor propagates rather than converts) and `nan != "nan"` is True; `fillna("")` first. Verified: `py_compile` on all eight; 21/21 backup (11 prior + 10 new, incl. a scripted late-listing race), 50/50 routing_targets (45 prior + 5 new `all`-mode, incl. per-consent bypass attempts), universe audit re-checked across named/bare/empty/literal-sentinel shapes | `GLD-BKP-10`, `GLD-BKP-11`, `GLD-BKP-12`, `GLD-ROU-07`, `GLD-ROU-08`, `GLD-PLX-11`, `GLD-RAD-35` |
| 70 | `machine_learning/affinity/account_links.py` **(NEW)** + `plex/playlists/builder.py` + `tautulli/users/__init__.py` + `test_family_scope.py` | **Linked accounts now share a WATCHED-SET, not just a grading** (`GLD-TAUT-16`, TV half). §0.1 #64 merged the gradings; the 2026-08-20 live run proved that was only half the promise — `Mom` and `mirandan75` graded IDENTICALLY (14 genres / 816 actors / 16 directors) and still produced *The Long Glide* at **24 vs 16 items** with completely different TV Up Next sets, while their MOVIE Up Next matched for 25 straight rows. That pattern IS the diagnosis: identical affinity, different watched-sets — the movie shelves agreed because neither profile had watched those films. New pure `affinity/account_links` (`parse_links` / `account_keys` / `linked_id_map` / `expand` / `malformed_groups`), and `TautulliUsersManager._account_links` now DELEGATES to it. One parser on purpose: a builder that disagreed with the grader about who is linked would merge one half of a viewer and not the other — the half-joined state the join exists to remove, and it would read as a data problem rather than a config-parsing one (P-E). Both TV halves merged, and both were needed: filtering an episode as watched while its recency timestamp went missing would leave resume ordering with nothing to sort on for exactly the series the viewer is mid-way through; latest timestamp wins on collision. Three deliberate calls, each tested: `None` expands to **EMPTY, never `{None}`** (a null id makes Tautulli return HOUSEHOLD history, which would hand one profile everyone else's watches); one member's history failing degrades to a PARTIAL union rather than none; ids keep their ORIGINAL type, since a `"555"` where `555` was expected returns nothing silently. ⚠️ **Two self-inflicted defects, both caught by tests.** (1) Extracting the parser REMOVED the malformed-config warning — a misshapen `account_links` would have no-op'd silently, the exact computed-and-discarded pattern this session has been removing. Fixed by splitting detection (`malformed_groups`, pure) from reporting (the manager logs), the same split `_family_ids` uses; it now warns once per RUN instead of three times, since the parser is called three times per run. (2) My replacement test then asserted ONE warning where the fixture has TWO malformed entries — the code was right. Verified: `py_compile` on all four; 26/26 family-scope against the EXACT repo sources; the pure module checked across roster-field variance (5 name fields), id-declared and case-insensitive links, id-type preservation, and 5 garbage config shapes; both builder methods exec-extracted and replayed (union identical across the group, latest-ts recency, unlinked byte-identical, partial-union on one member's failure, None queries nothing). ⚠️ Movie/combined builders (`_watched_film_tmdbs_for`) and the Plex per-profile `viewCount` union (each profile's own TOKEN) still open | `GLD-TAUT-16` |
| 71 | `plex/playlists/{builder,movie_builder,combined_builder}.py` | **The rest of the linked-viewer merge — and the run that isolated what was left.** §0.1 #70 merged the TV watched-set; the 22:04 live run turned the diagnosis into a measurement. *The Long Glide* went **24 vs 16 → 25 vs 25**, and Up Next / Touch & Go / Fresh Arrivals came out byte-identical row for row. What remained was ONE difference, and it named a THIRD input I had not identified: the same 25 titles in a DIFFERENT ORDER, because Blue Bloods scored `0.99 watching now` for Mom and `0.45` for mirandan75. Three things landed here. **(1) The movie paths** — `_watched_movies_for` and `_watched_movie_recency_for`, same union + latest-ts-wins as the TV pair. Not obviously needed and needed: their movie shelves already matched, but only because NEITHER profile had watched those films; the first time one finished a movie the other would have been recommended it again. **(2) `_watched_film_tmdbs_for`** — found by grepping for leftover single-id history reads rather than trusting that the two obvious methods were the whole surface. This one is not a playlist path at all: it feeds the movie DELETE SHIELD, so counting a linked viewer's films against only one login made the shield protect the pair LESS than it protects a single account — and the failing direction is worse here, since an under-counted watched-set makes a title eligible for RECLAIM rather than merely mis-ranked. **(3) `_jit_series_by_user`** — the "watching now" signal, unioned across the group: one person mid-series is one person mid-series whichever login they pressed play on. The union computes into a SEPARATE dict rather than mutating in place, because updating while reading would let the first member's merged set feed the second's — harmless for a pair, wrong for a group of three, and invisible until someone links three accounts (tested with a three-way group). All three builders build `_linked_ids` in their OWN `run()`: they are separate manager instances, so inheriting the method does not inherit the state. Verified: `py_compile` on all three; 26/26 family-scope still green; movie methods and `_jit_series_by_user` exec-extracted and replayed (union identical across the group, latest-ts recency, unlinked byte-identical, three-way completeness, `None` queries nothing); `grep` confirms **zero** remaining single-id `get_all_history_cached(user_id)` calls across the three builders. ⚠️ Last piece open: the Plex per-profile `viewCount` union, which is different in kind — read with each profile's own TOKEN, so it needs token plumbing none of this touched. Its practical size is measurable rather than assumed: it only covers titles marked watched in Plex but NOT seen by Tautulli (direct play outside Tautulli's view, or a manual mark-as-watched), so if the next run's Long Glide is identical it is immaterial for this household | `GLD-TAUT-16` |
| 72 | `services/routing/test_routing_manager.py` + `services/routing/DESIGN.md` §4/§9 | **The handoff's one mandated investigation, and the diagnosis it was pointed away from.** `COMMIT_HANDOFF.md` §6 flagged two failing routing tests and hypothesised that §0.1 #69's `apply` rewrite had broken the consent gate — that `relocation_enabled` was returning False on a config a standalone check says returns True, with a set `RELOCATION_CONSENT` env var as the likely cause. **Both halves were wrong and the check that settles it is three lines**: no such env var is set, and calling `relocation_enabled()` on the test's exact config returns **True**. The gate was never the refuser. The PUTs were missing because `apply_here` — not `apply` — was False: `GLD-RT-01`'s new cross-root guard fetches `rootfolder`, the fake instance-manager keys responses on the instance NAME and ignores the endpoint, so it answered with the ITEM list, whose dicts have no `path`. Empty `allowed_roots` ⇒ planning-only, **which is the guard working**. Proved by serving the endpoint and watching both PUTs appear with the right payloads (`/m/anime`, and `/t/anime` + `seriesType`), so the production code was confirmed correct before a line of it was touched. ⚠️ **The tell that should have caught the misdiagnosis without any code being read**: every NEGATIVE test passed (no-consent refuses, dry-run refuses). A gate that has genuinely stopped working fails OPEN — it moves when it should refuse — so all-negatives-green plus only-positives-red cannot be a loosened gate, and points at something downstream of it. §6 recorded that fact and still drew the opposite conclusion from it. Second lesson, same shape as #62's `_Log`/`log_table` and `GLD-ACQS-18`'s stubs: **adding a new outbound call is a change to the double's contract**, and this is the third session running in which a stub behind its real dependency has been read as a defect in live code. Also closed the doc-code drift the handoff missed — `routing/DESIGN.md` §4 and §9 still described the guard failing toward ACTION, the very behaviour #69 reversed. Verified: `py_compile`; 8/8 `test_routing_manager` (was 6/8); full suite **53 failed/2658 passed → 51/2660**, i.e. exactly the two, nothing else moved | `GLD-ROU-09`, `GLD-RT-01` |
| 73 | `services/radarr/quality/space_pressure.py` + the 2026-08-20 commit sweep | **The working tree committed, and the handoff's central claim disproved by testing it.** `COMMIT_HANDOFF.md` §2 established that HEAD did not collect (tracked tests importing untracked modules) and prescribed a ten-file first commit that would "alone make HEAD collect again". **It does not, and the way to find out costs nothing**: `git worktree add --detach <tmp> HEAD` gives a HEAD-only checkout to run collection in WITHOUT stashing, which §7 rightly warns against — the prior session verified by `git stash push --include-untracked`, the one method that puts nine load-bearing untracked modules at risk to answer a question a worktree answers for free. After that commit HEAD reported **9** collection errors, not 0 and not the documented 8; after the rest of Bucket A it reported **44**, because committing files that import still-uncommitted ones makes the gap WIDER before it closes. The prescription was incomplete rather than wrong: `playlists/coverage.py` (imported by the tracked, unmodified `affinity_builder.py`) and `lifecycle/restore_policy.history_release_record` (imported by `sonarr/cache/episode_files.py`) were both needed and both sat in Bucket B, the bucket the doc marked "do not commit without reading". HEAD collects only with the whole tree committed: **50 failed / 2652 passed / 9 skipped**, the 9 being `test_installed_config_axis` correctly skipping with no installed `config.json` — so HEAD and the working tree now agree exactly. ⚠️ **`GLD-RAD-36`**: committing surfaced a real defect in the uncommitted work — the pass-ledger read was the file's only unguarded `global_cache` access and crashed `run_downgrades`. ⚠️ **§6's remaining triage, now settled rather than assumed**: the `_Api has no disk_free_gb` group and the residual `'_GC' object has no attribute 'get'` are CONFIRMED test-double drift (`_stepdown_ledger` guards on truthiness, which a stub without `get` passes); the sonarr `space_pressure_realize` failures are call-ORDER assertions, not the `stepdown_cooldown` crash §6 grouped them under. ⚠️ Also corrected: §4 asked for a gitignore decision on `scripts/support/logs/` and `*.parquet`, **both already ignored since before this session**, and §5 step 14 asked whether to track `scripts/support/config/config.json`, which is **untracked and matched by `**/config/config.json`** — two of the three deferred operator questions were already answered in `.gitignore`. Verified: `py_compile`; suite 53/2658 → 50/2661 across the sweep, every delta accounted for | `GLD-RAD-36`, `GLD-ROU-09` |
| 74 | `sonarr/series/space_pressure.py` + `radarr/quality/{test_universe_realize_downgrade,test_exhaustive_downgrade}.py` + `sonarr/series/{test_space_pressure_realize,test_exhaustive_downgrade}.py` | **Nineteen failures in the two step-down realize paths, worked heaviest-first — eighteen stale tests and one real defect.** Three contracts the tests predate, each verified against the live code BEFORE a test was touched. **(1) The identity gates.** `GLD-RAD-30` / `GLD-ACQ-27` refuse a release whose TITLE does not name the title/episode, and every fixture used bare labels (`"hd"`, `"r1"`, `"e1101"`) against movies called `x`/`M1` and a series called `S` — so every pick returned None and no delete ever ran. **That is the gate working**: the pick PRECEDES the delete, so grabbing a release that does not name the film is how the file is lost and replaced with something else. ⚠️ `test_no_smaller_release_keeps_file_lowers_profile_and_reprobes` was **passing for the wrong reason** — refused by identity, not by the 300 MiB sanity floor it exists to test; it still passes, now on the floor. **(2) The DELETE contract** — success returns True, and doubles answering `{}`/`None` read as a failed delete. **(3) pandas 3.0.3** — `None` into a `str` column stores NaN, so `quality_action is None` no longer holds; every consumer reads it with `.notna()`, so `pd.isna` IS the "cleared" contract and `is None` only ever worked on object dtype. ⚠️ **`GLD-SON-23`, the one real defect, and it was found by asking why a test double mattered**: the sonarr delete relied on an exception `_make_request` never raises, so a failed delete counted as realized, inflated `realized_reclaim_gb` and grabbed against a still-present file. ⚠️ **Self-correction**: I read `_do_call`'s `raise raw._delete(...); return None` and was about to report the base contract as unimplemented — the conversion lives in the OUTER wrapper (`if method_upper == "DELETE": return True`) twenty lines below. The tell: I diagnosed from the inner helper without reading the function that returns to the caller. Also replaced an exact-call-list assertion with a relative-ORDER one — search → delete → grab is the contract; the exact list had frozen `GLD-ACQ-27`'s per-series metadata GETs and `GLD-RST-02/-06`'s history reads into an unrelated test. Verified: `py_compile`; sonarr/series 48/48; suite **50 failed/2661 passed → 31/2680** | `GLD-SON-23`, `GLD-RAD-30`, `GLD-ACQ-27` |
| 75 | `scoring/golden_scores.json` + the role-weight, tvfran, floor-gate and stub tests | **The remaining 41 failures closed, worked down by severity — 40 stale tests and one more real defect.** ⚠️ **`GLD-SON-23`** (§0.1 #74) was the only production bug among the 53. Everything else was a test asserting a contract the code had deliberately moved past, and in three cases the test was **passing for the wrong reason** or **failing in a way that named the wrong thing**: `owned_restore_score_threshold` reported ZERO readers for a key that still has exactly one — the grep matched `get("key"` and the read had moved behind a `_cfg_int(...)` helper, so a stale DETECTOR read as a dead FEATURE; `test_dual_emit` selected `tables[-1]`, so a newly appended table silently moved the index and the failure read as "the saga column is gone"; `test_no_smaller_release...` was refused by the identity gate rather than by the 300 MiB floor it exists to test. **Deliberate behaviour changes the tests had frozen**: `cluster_same_stem` now defaults FALSE (Layer-1 grouped by title SHAPE and minted a "Blue" family from Blue Bloods + Blue Planet II + six anime — credit that feeds the watchability score AND the universe DELETE guard); **no last-resort GB floor remains** (four callers declared one, disagreeing 25/25/1000, one under a DIFFERENT NAME so a grep for the common one missed it; a wrongly-low floor does nothing, a wrongly-high one reclaims on a healthy disk); `GLD-SON-18` made remediate search-only, so an unmonitored sibling no longer VETOES a multi-episode file — the orphan risk is gone structurally rather than conditionally. `restriction_profile` entering the PII allowlist was that test doing its job: a coarse parental-controls tier, no email, no token, approved explicitly with the match kept EXACT so the next new field is looked at too. **The golden fixture** was regenerated only after proving the 649 mismatches were 100% attributable: `PERSON_ROLE_WEIGHTS` never reaches `score_movie` (it takes the per-person affinity map), so the drift is §0.1 #24's SUM-PRESERVING Group-B cap redistribution — actors ×1.25 (8→10), writers ×0.5 (4→2) — confirmed by temporarily restoring the old caps, watching the fixture pass byte-identically, and restoring the file. Verified: **2711 passed, 0 failed**, from 53 failed/2658 passed | `GLD-SON-23`, `GLD-RAD-30`, `GLD-ACQ-27`, `GLD-SON-18` |
| 76 | `services/sonarr/cache/episode_files.py` | **A fourth outcome bucket was invisible in the legacy re-grab summary** (`GLD-SON-24`). `legacy_regrab` counts a zero-release indexer response as `empty_search` and refuses to persist it as `no_release`, because that costs a 14-day cooldown (`GLD-SON-02`, measured: 814 of 881 files benched that way). Correct policy, unreported outcome — now in the line, with a WARNING when `empty_search == checked` pointing at indexer health rather than the library. Partial empties count without warning; an absent key stays silent so older callers are unaffected; `empty_search` added to both return dicts including the `skipped_space` early return. Verified: `py_compile`; the summary block exec-extracted and replayed across four shapes (observed-run, healthy mixed run **byte-identical to before**, partial empties, absent key). 🔴 **THE DIAGNOSIS THIS ROW WAS BUILT ON WAS WRONG — RETRACTED, see `GLD-SON-24` and `GLD-SON-25`.** I attributed the 2026-08-20 run's three zero counters to `empty_search`; measurement showed `empty_search == 0` and **zero searches issued**, with the real cause being stale `episode_file_id` pointers resolving to nothing and returning silently. **Two tells were available and neither was used**: the timing (69 files in ONE second, when a single search takes four — arithmetically impossible if searches ran), and the fact that I read `_one` from the `empty_search` branch downward without ever tracing the episode-resolution step above it. This is the second wrong mechanism filed for the same symptom in one session, and the register row for the first one was being written at the same moment as the second. **The reporting fix is independently correct and stays**; only the attribution is withdrawn | `GLD-SON-24`, `GLD-SON-25`, `GLD-SON-02`, `GLD-ACQS-20` |
| 77 | `machine_learning/space/jit_backoff.py` **(NEW)** + `sonarr/cache/jit_search.py` + `sonarr/cache/episode_files.py` | **Three hours a night spent on five series that could not be satisfied** (`GLD-SON-26`). Found by asking why the JIT log showed so many reverts — the revert is correct (Sonarr has no per-episode profile, so JIT bumps the SERIES, searches, and must put it back), but quantifying the window exposed the real shape: **146 step-downs → 14 grabs at ~112s each, and 116 of the 146 went to five series that grabbed NOTHING** (Johnny Bravo alone walked 37 profiles). Every one ended `queued for retry next run`, and `_reconcile_failed_jit` then reset their flags and deleted its key — no memory, so the identical waste repeated nightly. **Two limits, both degrading ORDER rather than eligibility.** A step budget stops a ladder after 4 consecutive misses (the tail of a ladder is where releases are rarest, so the marginal profile is the least likely to grab and exactly as expensive as the first) and RESETS on any grab, so a productive ladder is never truncated. A demotion ledger sorts an exhausted series to the BACK of the next run's queue, and any grab clears it. ⚠️ **Deliberately a QUEUE, not a cooldown.** `legacy_regrab` benches for 14 days and `GLD-SON-02` records that failing at 814-of-881 scale, because a disabled or rate-limited indexer is indistinguishable from "no release exists" — and tonight's five titles are old/obscure/anime, exactly what lives on the **13 torrent indexers currently disabled on this deployment**. A bench would survive re-enabling them; a demotion self-heals on the first grab. ⚠️ **Deliberately NOT configurable** (operator call): a knob here would be a knob to re-enable the defect, and hard-coding is safe precisely because a wrong constant costs "a bit more or less search per run", never a lost title. The two ledgers are kept SEPARATE and the reason is written into both docstrings — `jit/failed_upgrades` ("is this EPISODE still owed a grab?" → retry) and `jit/backoff` ("how often has this SERIES come up empty?" → sort last) — because merging them, or clearing the second in the reconcile, would wipe the counters every run and restore the loop. Verified: `py_compile` all three; the pure module replayed against the measured window (grabbers lead, Johnny Bravo reaches only 4 exhaustions in 8 runs, always comes due again, one grab clears it, unseen series sort with the fresh, 4 garbage-input shapes); the step budget exec-extracted and replayed (barren 37-ladder 37→4 searches, a grab at step 2 gives 7 not 4, immediate grab gives 1, cap 0 = legacy); the wiring block exec-extracted and replayed on the real candidate frame; three cache keys proven distinct. ⚠️ **My own two errors this pass, both caught before shipping**: a test asserting "grab at step 6 with cap 4" should reach step 6 — four consecutive misses fire first, the CODE was right — and a register edit whose anchor was a PREFIX of `GLD-SON-24`, which spliced two new rows into the middle of it and destroyed that row until repaired. ⚠️ Also filed here: `GLD-SON-25` had **no table row at all** — referenced twice, defined nowhere — the same orphan pattern that has now recurred five times this session | `GLD-SON-26`, `GLD-SON-25`, `GLD-SON-02` |
| 78 | `factories/base_manager.py` | **A warning I added last week fired for the first time, and I misdiagnosed it twice** (`GLD-MGR-12`, `GLD-MGR-13`). The 02:38 run surfaced `global_cache exposes no 'memory' MemoryManager — that is a CONTRACT CHANGE`, from `SonarrCacheOwnedEpisodesManager`. **The guard fix is right and stands**: a real `GlobalCacheManager` missing `.memory` still WARNS, anything else gets a DEBUG line naming its type, on a separate class flag so one can never consume the other's once-per-process budget; duck-typed because importing `GlobalCacheManager` into `BaseManager` is a factories→factories cycle. Verified on 6 paths against the real extracted method, including that an EMPTY MemoryManager still returns a silent `[]` — preserving `GLD-MGR-11`'s guarantee. 🔴 **Two wrong claims, both retracted.** (1) I attributed it to the daemon's `LedgerCache`, reading a `ConfigLoader`/`SecretBootstrap` pair as a process boundary. There was no second process — `pilot_search_daemon` contains **no reference** to that manager, one grep, available before I wrote the row. It is a second MANAGER STACK inside the same run, the end-of-run threshold shadow pass. (2) I then claimed that stack had overwritten `owned_episodes.parquet` with 2 rows and said to stop running. The file was intact at 15,021 rows and its **mtime proved the good build was the last writer** — one command disproved it. ⚠️ **Same shape both times, and the same shape as §0.1 #76**: a confident attribution published while the disproving evidence was already in hand — there the timing arithmetic, here a grep and a file mtime. Three retracted causes in one session is a pattern, not bad luck: the common factor is asserting a mechanism from a plausible story instead of the one check that would falsify it. ⚠️ The real defect is filed as `GLD-MGR-13` — the missing `.memory` was a symptom of a shadow stack that also sees 2 series where the run sees 12,133 | `GLD-MGR-12`, `GLD-MGR-13`, `GLD-MGR-11`, `GLD-CACHE-13` |
| 79 | `services/routing/__init__.py` + `factories/onboarding/schema.py` + `support/daemons/pilot_search_daemon.py` + `sonarr/cache/episode_files.py` + `radarr/quality/test_identity_gate_rejects.py` **(NEW)** + `support/config/config.json` | **"Is the routing flip firing?" — no, and it never had.** The operator set `reorg_mode: all` with every consent granted and `dry_run: false`, and nothing happened for hours. `RoutingManager.run()` returns on its FIRST line when `routing.configured` is unset, before it reads `reorg_mode` — so the 123 same-instance misplacements were never even classified, and `relocation.log` (a DIFFERENT axis, `uhd_reconcile`) hadn't been written since 08-19. `GLD-ROU-10`: the gate is right, its silence was the defect; every other refusal on that path announces itself. Now warns when the config contradicts itself, reusing `relocation_enabled()` so the warning cannot drift from the gate. With `configured: true` + `log_only` the router produced its first plan: **123 misplacements, 29 movies / 94 shows, all within-root**. ⚠️ **`GLD-ROU-11`**: the plan demoted five titles OUT of `movies/kids` and the number responsible — the Common Sense age ceiling — was hard-coded at 11 and never passed by the router. Now `plex.playlists.kids_age_max`, asked during Plex onboarding beside `profile_ages`, refused rather than clamped when out of range (clamping a mistyped `25` to `17` would silently WIDEN a parental gate). ⚠️ **A P-B found while filling the classifier config**: `_as_set` does `cleaned or set(default)` — a config list REPLACES the default, it does not extend it. `realityGenres: ["reality"]` and `documentaryGenres: ["documentary"]` were therefore discarding four and two values respectively, so game shows, talk shows, biographies and nature docs had never been classified at all. All eight classifier lists now written explicitly and verified set-by-set against the defaults (behaviour-neutral for the six that were absent; the two partial ones are widened back). Also: the daemon's own legacy summary got `GLD-SON-24`'s outcome buckets (a second site with the same omission); a `try/except: pass` audit of all 34 sites found one real defect — a corrupt `available_until` left a row PERMANENTLY unmarkable with no signal, now counted and surfaced as its own warning; and `GLD-RAD-30` regained the rejection coverage §0.1 #74 removed when it relabelled fixtures to satisfy the gate (10/10 against the real extracted gate). ⚠️ **Seventh misread of the session, same shape**: I reported 11 cross-media-root moves (`movies/anime -> tv/series`) from a truncating grep — there are NONE, every move stays within its root. Verified: suite 3065 throughout | `GLD-ROU-10`, `GLD-ROU-11`, `GLD-SON-24`, `GLD-RAD-30` |
| 80 | `machine_learning/classification/library_classifier.py` + `its test` + `services/routing/__init__.py` + `support/config/config.json` | **Reading the routing plan changed the classifier twice** (`GLD-ROU-12`). The `log_only` pass exists so a 123-move plan is READ before it actuates, and it earned its keep immediately: two clusters looked wrong, and both were precedence rather than genre lists. **(1)** Widening `realityGenres` swept news magazines and satire (`20/20`, `Axios`, `You Can't Ask That`) out of documentaries into reality, because TVDB gives them the same `Talk Show` tag it gives `Maury` — and reality is checked BEFORE documentary. `newsGenres`, checked ahead of reality, routes news to the documentary bucket; kept as its own set because documentary genres COMPETE while news WINS. Operator ruled against a separate news library: not enough titles to justify a folder. **(2)** `nonKidsGenres` was vetoing the kids-NETWORK route, so `Take Two with Phineas and Ferb` (Disney Channel) and `Crashbox` (HBO Family) routed to REALITY — the veto doing the opposite of its job, since it exists to stop a GENERAL network's talk/cooking output being called kids and a children's channel is the one case with no such risk. Lifted for that route only; the AUDIENCE guards (adult cert, CSM ceiling) still refuse. ⚠️ **`pop` and `nick at nite` were REFUSED from the network list**: matching is `tok in netw`, a SUBSTRING test, so a three-letter entry matches any network containing it — and Nick at Nite is Nickelodeon's ADULT block, a name that looks right with the wrong audience. ⚠️ **A test asserted the old policy in as many words** (`"lifestyle veto wins"`) and failed — correctly. Split in two so the audience-vs-format distinction cannot drift back, with the reversal, its date, and the two real titles recorded in the docstring rather than the assertion silently flipping. ⚠️ **My own assumption, flagged rather than buried**: I had widened `realityGenres`/`documentaryGenres` on the theory that a narrow value was an oversight (P-B). It may equally have been the operator's own correction for exactly this news problem — I read a narrow config as accidental without asking, which is the same species of error as §0.1 #76/#78. ⚠️ **Seventh misread of the session**: I reported 11 cross-media-root moves from a truncating grep; there are none. Verified: 13/13 + 11/11 against the real classifier, 32/32 in the classifier test file, suite 3066 | `GLD-ROU-12`, `GLD-ROU-11` |
| 81 | `scripts/ENHANCEMENTS.md` §4.52 **(NEW SECTION)** + `factories/onboarding/tier_pairing.py` **(NEW)** + `support/config/config.json` + `factories/onboarding/env_map.py` | **The tier map is 90% decorative, and the deployment just went from two tiers to three** (`GLD-INS-01`..`-09`). Traced rather than assumed: `gateway.categorized_instance` has exactly **two callers, both in `uhd_reconcile`**, so `radarr_instances_categorized` answers one question — which instance holds the 4K copy — and nothing else. Every other pass is instance-AWARE but tier-BLIND. `sonarr_instances_categorized` is declared, documented, and read by **nothing** *(P-A)*. Nine items filed covering all three shapes the operator needs switchable from config alone: single (the shipped default, must stay byte-identical), two (`standard`+`ultra`, `uhd_reconcile`'s native shape), three (720/1080/2160 both services). Sized honestly — `GLD-INS-01` (tier-aware acquisition) is the small change that makes the config functional at all; `GLD-INS-02` (a promote/demote LADDER, with make-before-break holding at every rung, not just the top) is delete-path work; `GLD-INS-03` (Sonarr cross-instance) is project-sized and should not start until the Radarr side proves the model. **Also delivered**: `tier_pairing.py`, a pure module that infers a tier per instance from ROOT-FOLDER PATHS (the arr reports them and they cannot drift from reality), falls back to name/URL, treats CONTRADICTORY evidence as unknown rather than voting, and **refuses to guess house style** — `4k`/`uhd` are unambiguous, `ultra`/`standard`/`hd` are not, because one deployment's "ultra" is another's 1080 and a wrong pairing routes upgrades into the wrong library silently. Verified against this deployment: it paired 2160 from `/movies/4k/` alone despite the key saying `ultra`, and correctly reported 720/1080 as unpaired rather than inventing partners. Config migrated to container-name keys with all ports moved to the new 19xxx tunnels; `port` had been left stale while `base_url` was updated, so every instance carried two contradictory answers to "where do I connect" — harmless only because `base_url` wins. ⚠️ **`rootFolders` was stale in a way that silently disabled TV routing entirely**: the operator's restructure moved TV to `/data/media/tv/720/…` and the config still named `/data/media/tv/…`, so `GLD-RT-01`'s cross-root guard REFUSED every TV move — which read as "the classifier fix worked" when it was "the plan could not be made". ⚠️ Onboarding was missing `rootFolders.kids` entirely while `CATEGORY_ORDER` has five categories — a fresh install would classify shows into `kids` and have nowhere to put them | `GLD-INS-01`..`GLD-INS-09` |
| 82 | `services/routing` (live run) + `support/config/config.json` | **The relocation axis executed for the first time, and the tiering plan it was built for was retired the same night** (`GLD-ROU-13`, `GLD-INS-10`). `same_instance` moved **235/235** — Radarr 31, Sonarr 204 — every line `SUCCESS`, reconciling exactly against the plan the operator had read under `log_only`. That is also the production proof of `GLD-ROU-12`: 102 reality moves carrying the talk/game shows the widened `realityGenres` was meant to catch, and 6 `reality→documentaries` rescued by the new `newsGenres` precedence, with `20/20` and `Axios` correctly staying put. ⚠️ **The tiering question was answered by measuring instead of building.** Asked whether a parquet crawl should move 1080/2160 files into the new instances, the data said no twice over: **105 of 5,249 series mix resolutions**, and Sonarr owns the SERIES — so a crawl would split them across instances leaving neither complete. And more fundamentally, 4K earns cross-instance because it is a **DUAL** (two real files, a baseline plus a UHD copy) while 720↔1080 is ONE file at one quality, which a quality PROFILE decides. ⚠️ The clinching evidence came from the backlog tiering would have chased: **5,676 episodes below the floor, median series watchability 10, ZERO reaching the upgrade tier** — the passes are not failing to reach them, they are correctly declining to spend on cold content, and 90 are already marked for deletion. **Config retired**: `radarr-1080` dropped and `1080p` re-pointed at `radarr-720`, which had been a LIVE MISROUTE sending 1080p titles to an empty instance. ⚠️ **Eighth misread of the session**: I reported "nothing moved" from grepping `routing.log` for `moved` / `applied` / `PUT` — the log says `relocated`, and `routing.log` is the PLAN written before actuation; the SUCCESS lines were in `default.log`. Same shape as the truncating-grep cross-root claim: searching for the word I expected rather than reading what the code emits | `GLD-ROU-13`, `GLD-INS-10`, `GLD-ROU-12` |

✅ **EDITS 1–11 RAN CLEAN; edit 11's unblocking effect was SHADOWED** — the 23:36 UTC run
imported the narrowed guards (mtime-proven) and TBBT stayed 37/38 protected: household was
not the live blocker (`GLD-ACQ-22` → universe-credit suspect; `GLD-ACQ-23` decision filed). Six `dry_run` sessions between 2026-08-05 14:16 and 08-06
00:15 exercised edits 1–9; edit 10 verified 23:13 the same day — the first partial
fund after the fix emitted all 4 `[Recycle]` reason lines and localized the recycle
blocker in one run (`GLD-ACQ-21` → `GLD-ACQ-22`). Edits 6–7 (the drift detector) bootstrapped and
then produced a real comparison; edit 9 (`BaseManager`) held across ~40 managers
with no `AttributeError`; the `dry_run` gate held **four times** (proven
negatively — `audit.log` unchanged while playlists were built each run). Final
run: **0 non-banner errors**, down from a long-standing 9. See §0.1.0–§0.1.6.

### 0.0 🎯 SESSION 90 — THE SPACE PASSES NOW FORM A TREE, NOT A RACE

**The most consequential finding of the sweep, and it came from reading a log table
rather than the code.**

Every space pass reads the same free-space figure from the same shared mount, then
planned its **full** deficit independently:

| Pass | Sees | Planned |
|---|---|---|
| Radarr `standard` | 922.8 GB free, band top 5500 | **396 GB** |
| Radarr `ultra` | 922.8 GB free, band top 5500 | **205 GB** |
| Sonarr TV | 922.8 GB free, band top 5500 | **476 GB** |

**1,077 GB of planned reclaim against one 922 GB pool**, and no pass knew the others
had already committed to part of it. Under live deletion each would shrink or evict
enough to close the WHOLE gap — roughly three times the quality given away for the
space actually needed.

✅ **FIXED** — `machine_learning/space/reclaim_ledger.py` (new). Each pass adds what
earlier passes planned to its effective free space. Verified live:

```
Sonarr TV        (nothing precedes it)                 plans 476
Radarr standard  476 GB already planned, need ~4105    plans 396
Radarr ultra     872 GB already planned, need ~3709
```

476 + 396 = 872, to the gigabyte.

**This makes the stated policy arithmetic rather than aspirational.** *"Deletion is
the true last resort"* and *"EVERYTHING shrinks before anything is deleted"* were
already true of the ORDER; now deletion is last in the chain and only ever sees the
deficit shrinking could not cover.

⚠️ **The reset placement is load-bearing.** It lives in `Main.run()`, not in a
service: **Sonarr runs BEFORE Radarr in Phase 2**, so a reset inside
`RadarrOrchestrationManager.run()` (where it started) would have wiped Sonarr's entry
mid-run and handed Radarr a deficit Sonarr had already claimed. And *not* resetting
fails in the dangerous direction — last run's plan credited to this one, every pass
concluding the gap is already closed, nothing reclaimed, silently, looking exactly
like a healthy disk.

### 0.0.1 🔴 The 4K instance was being shrunk by the pass that should not touch it

`RadarrSpacePressureManager` contained **zero references** to the 4K instance — no
`categorized_instance`, no `_UHD_LABELS`, no 2160 check. It treated every instance
alike. So in the SAME run:

* `uhd_reconcile` planned to **move 2160p copies ONTO** `ultra`
* `run_space_pressure` planned to **shrink 2160p copies ON** `ultra` toward 720p

`uhd_reconcile._source_instances` already excludes the 4K instance — *"the guard that
stops the sweep from dragging a real 4K library into the move"* — and this manager
had no equivalent.

✅ **FIXED**: the 4K instance's step-down floor is **2160p**, so the downgrade pass
finds nothing there (`candidates_found: 6 → 0`). Its space is reclaimed by
`_demote_overqualified_4k`, which deletes the 4K FILE only once a ≤1080p baseline is
confirmed surviving, ledgers the shell, and re-acquires if the score recovers —
make-before-break. Shrinking a 2160p to 720p in place gives up the tier with nothing
held anywhere.

`_is_uhd_instance` reads `radarr_instances_categorized` with the shared
`UHD_INSTANCE_LABELS` and **fails closed to False**, so an unreadable map means the
old 720p-floor behaviour rather than silently disabling downgrades everywhere.

### 0.0.2 🔴 The space-pressure table was mislabelling its own numbers

`run_downgrades` built **10 rows and 8 descriptions**. Two were missing at
`on cooldown`/`grab failed`, so every later row paired with the wrong text and the
last two ran off the end into blanks. The table read, among others:

> `freed now GB | 171.2 | candidates over the per-run re-grab cap - files KEPT`

— a byte total labelled as a count of titles kept, on the report about the
delete-adjacent pass. **The Sonarr TV twin printed directly above it has always had
both descriptions** (P-J, and it is what made the misalignment visible at all).

✅ **FIXED**, plus a `len(_descs) != len(_rows)` guard in **both** managers that warns
and pads rather than raising — a reporting bug must never take down the pass it
reports on. Third instance of two parallel lists with no length invariant, after
`GLD-MVF-01`'s `cast_*` triple and `_NUMERIC_COLUMNS` having no string twin.

### 0.1.0 ✅ FIRST GENUINELY CLEAN RUN — 2026-08-06 00:15

**Non-banner ERROR count: 9 → 0.** Two long-standing faults, both present in
`default-1.log` and `default-3.log` (i.e. predating every change in this sweep),
both now fixed at their root rather than their symptom.

| | Was | Root cause | Fix |
|---|---|---|---|
| **`GLD-EPF-01`** ×3/run | `Invalid value '2026-08-06T…' for dtype 'float64'` in `run_pilot_search`, `run_episode_file_enrichment`, `run_full_series_enrichment` — identical timestamp in all three, so ONE failure surfacing thrice | Parquet round-trips an **all-null column as float64**; assigning an ISO string into it **raises**. The file already had `_NUMERIC_COLUMNS` cast on load — **no string twin existed**, so each writer had to remember the incantation. Four did; three did not | Added `_STRING_COLUMNS`, cast to `object` on load beside the numeric cast. A writer can no longer forget |
| **`GLD-TRT-01`** ×6/run | `'NoneType' object has no attribute 'get'` in `auto_rate_watched_movies` → `run_movie_ratings` | `score_movie` declares `credits: dict` **positionally, no default**, docstring says *"REQUIRED (pass `{}` if unavailable)"*, then does `credits.get("cast")` unguarded. `radarr/repair/anomaly` honours the contract; **`trakt/ratings` passed `None`** — so the pass died on the first movie with no cached credits, **every run, on every instance, for months** | `credits: dict = {}` and `get_people(…) or {}` |

🔍 **`GLD-TRT-01` was diagnosed by SCAFFOLD, not by reading.** Two rounds of
inference picked the wrong `.get` — one shipped as a plausible-looking fix that
changed nothing. A temporary per-stage `try/except` naming its own location
settled it in one run: *"FAILED in score_movie for tmdb 497698 ('Black Widow'):
credits=None"*. Scaffold since removed; the defensive guards it revealed were
kept. See the **Instrument errors** note above.

✅ **The feature had never worked.** With the fix, `auto_rate_watched_movies`
completes for the first time: *"3 to rate, 44 already rated, 20 skipped"* over 67
watched movies. **44 already-rated proves the diagnosis** — the pass reached the
ratings fetch and died in the scoring loop, exactly where `credits=None` went in.
Its shows twin logged normally in the same pass, all along.

### 0.1.1 ✅ VERIFIED — dry_run pass, 2026-08-05 18:16

A full `dry_run` session was run against the live library. Everything observable
held.

| Check | Result |
|---|---|
| **The detector fires** | `[AxisDrift] radarr.persisted_watchability: baseline CAPTURED (n=2096)` and `sonarr.… (n=12618)` — bootstrap on **both** services, captured-not-judged, with the "not the same as a reviewed one" caveat printed as designed. **Next run produces a real comparison** |
| `_v2_cutoffs`' string match | ✅ Resolved AXIS V2 specs for **both** services — `GLD-DRIFT-02`'s fragility did not bite in practice |
| Errors | **Zero** `ERROR`/`CRITICAL`, zero `WARNING` in the end-of-run section. No `AttributeError`, no `ValueError` — so `TraktHistoryManager`'s four-level raise did **not** fire and nothing lost its `dry_run` attribute |
| `writeback` (line removed) | `[Writeback] disarmed (dry-run/disabled - no Plex writes): 0 create / 24 update / 0 delete` ✅ |
| `routing` | `[Routing] standard: 23 movie(s) misplaced (log only)` ✅ |
| `CrossInstanceMove` | `[would-retune]`, `[would-relocate]`, `[would-freeze]`, `[Dedup] would reclaim 18…` ✅ |
| `radarr/repair/anomaly` (line removed) | ✅ **CONFIRMED** — `[Anomaly] [dry_run] Stale-owned prune - 'standard' …` on all three instances. **All nine edits verified** |

⚠️ **A methodology note.** My first grep for these tables returned empty and I
nearly concluded the delete path was disabled. It was wrong: log table titles pad
with **non-breaking spaces (U+00A0)**, so a pattern containing a normal space
cannot match. **Absence of a log line is not absence of the event** — the same
class of error as `GLD-CAL-A1`, one layer further out.

### 0.1.2 🔴 What the run actually revealed — the system is PRIMED, not idle

```
[Anomaly] [dry_run] Stale-owned prune - 'standard'
    (floor<20, unmonitor@30d, delete@7d [expedited from 90d, 968GB free] [disabled])
```

Every clause matters, and together they say something the register did not know:

| Clause | Meaning |
|---|---|
| `968GB free` vs `free_space_limit: 5000` | **Free space is at 19% of the configured floor.** `T = 5000`, free = 968, so `free < T` — maximum pressure |
| `expedited from 90d` → `delete@7d` | The delete dwell is **clamped to `owned_delete_min_dwell_days`, its floor**. Not partially expedited — maximally |
| No `[space OK … clocks only, no prune]` | **`pressure_active` is TRUE.** Stage 1 (unmonitor) is live and acting, not merely clocking |
| `[disabled]` | `owned_delete_enabled: true`, and `delete_enabled = owned_delete_enabled AND deletions_enabled(config)` — therefore **`deletions_enabled(config)` is False** |
| `'standard'`, `'ultra'`, **`'test'`** | **Three** Radarr instances. The register assumed two |

**Stage 2 is held back by exactly one gate**, and it is not any of the per-feature
flags an operator would think to check. Everything else — pressure, dwell,
thresholds, enablement — is already in the acting state.

### 0.1.5 ✅ SECOND dry-run, 2026-08-05 14:36 — the detector COMPARED

```
[AxisDrift] OK radarr.persisted_watchability: median 6.0 -> 6.0 (anchor n=2096, now n=2096)
[AxisDrift] OK sonarr.persisted_watchability: median 8.0 -> 8.0 (anchor n=12618, now n=12618)
```

Full proof of the mechanism: the anchor **persisted across runs** (it did not
re-bootstrap), it **compared** rather than captured, and both services read `OK`.
Identical medians and counts twenty minutes apart is exactly right.

**Zero errors, zero warnings.** No `HistoryFetchError`, no `Prune floors CROSSED`,
no stale-serve. ⚠️ Those three session-84 fixes are therefore verified as **not
breaking the happy path** — they are failure-path changes and nothing failed. Their
failure behaviour remains unexercised.

### 0.1.6 🔴 The detector's first real output already corrected the register

**Measured: radarr median 6.0, sonarr median 8.0.**

`thresholds/registry.py`'s AXIS V2 block records the owned-movie median as **8**,
and `GLD-RQ-03` was filed on exactly that figure — I argued that
`WATCHABILITY_PROTECT_THRESHOLD = 6` "sits **below** the median (8), so the
non-exhaustive path shields more than half the owned library."

If the live movie median is **6.0**, that reading is wrong in an interesting
direction: the protect floor sits **AT** the median, which makes it look
**deliberate** — protect the top half — rather than accidental.

Two explanations, and they need different responses:

| If… | Then |
|---|---|
| The **populations differ** (registry measured file-owning rows; `plan_summary` aggregates every scored row) | `GLD-RQ-03` stands, and the drift anchor is measuring a **wider** population than the thresholds were calibrated on — which would make future comparisons subtly wrong. Fix by narrowing `_scores_by_service` to the same population |
| The **axis has genuinely moved 8 → 6** since that measurement | The anchor was captured against an **already-drifted** distribution — precisely what the bootstrap message warns about: *"records the CURRENT distribution, which is not the same as a reviewed one."* The baseline needs re-taking after a deliberate re-anchor |

**Either way the caveat earned its place on first use.** `GLD-DRIFT-04`.

---

### 0.1.3 ✅ The compound risk this exposed — FIXED session 84

The day `deletions_enabled` returns True, deletion begins **at a 7-day dwell,
across three instances, under maximum pressure**. Two open items would have become
live at that moment. **Both are now closed:**

| Item | Was | Now |
|---|---|---|
| `GLD-TWH-04` + `GLD-THY-01` | Both populate `watched_tmdb_ids` — the **hard guard** against deleting a watched movie — and both returned empty on failure, one caching it for an hour | Tautulli **raises and serves stale**; Trakt returns **`None`** and its two direct callers warn instead of silently reporting zero plays |
| `GLD-RAN-06` | `movie_demote` / `movie_restore` resolved in different methods, separate `get_threshold` calls, nothing comparing them | `_resolve_prune_floors()` resolves both together and **clamps the delete floor down** if they ever cross, warning with the affected score range |

The gate itself is unchanged — `deletions_enabled(config)` still holds stage 2 —
but flipping it no longer walks into a silent watched-set or an open hysteresis
band.


### 0.1.4 🎯 The UHD reconcile PLANS every run

```
[UHD] shared-storage standard->ultra: YES -> relocate
      (shared mount confirmed: common root '/data/media/movies/4k', equal capacity ~29796 GB)
[UHD] standard: 22 title(s) reconciled to dual-version on ultra.
[Dedup] would reclaim 18 redundant cross-instance copy(ies); 0 same-path duplicate(s) flagged.
```

The per-title **shared-storage probe fired and confirmed** — real-world validation
of the design in [`routing/DESIGN.md`](./managers/services/routing/DESIGN.md)
§1.5. Four hardlink relocates (F9, Coco, Hobbit, Avatar Aang), 18 retunes, 18
dedup reclaims. **Only the acting is gated; the planning half is live and
reviewable.** `GLD-RAD-01` is further along than §0.1 recorded.


## 0.2 Coverage — what now has a DESIGN.md

`machine_learning/` (broadly) · `services/` roots for **plex, acquisition,
coordinator, writeback, backup, mdblist, calendar, mal** · **sonarr/** root, sync,
quality, orchestration, series, series/retrieval, cache/episode_files, episodes,
storage, repair · **radarr/** quality, storage, repair · **routing/** (was wholly
undocumented) · **tautulli/** watch_history, metadata, users · **plex/playlists/**.

## 0.3 ❌ UNSWEPT — the remaining surface

| Area | State |
|---|---|
| **`services/trakt/`** | **15 subpackages, essentially untouched** — only `history/` head and the `movies/scorer.py` shim read. **Biggest single gap** |
| **`services/plex/`** | 13 subpackages beyond `playlists/` — discovery, users, metadata, watchlist, collections, episodes, libraries, movies, on_deck, ratings, instances |
| **`services/radarr/`** | cache (only `movie_files.py` head), movies, monitoring, sync, validator, instance, orchestration, api |
| **`services/sonarr/`** | api, instance, monitoring (8 modules), validator (4), series/sync, episodes/retrieval (6), repair's 15 modules |
| **`services/tautulli/`** | devices, episodes, instances, series, transcode — *none of them fetch, per `GLD-TUS-03`* |
| **`support/`** | daemons, notifications, tools, utilities — barely touched |
| **Read at the head only** | `radarr/quality/space_pressure.py` (151 KB) · `plex/playlists/builder.py` (120 KB) · `radarr/quality/universe.py` (60 KB) · `routing/uhd_reconcile.py` (57 KB) · `radarr/cache/movie_files.py` |

## 0.4 Open threads with a specific next action

| Thread | Next action |
|---|---|
| `GLD-TUS-02` | Read `TautulliManager.__init__` past line 80 — **is the user roster cached?** If yes it joins `GLD-TWH-04` in severity |
| `GLD-SRF-01` | Does anything call `get_all_series_chunked`? If not, delete it — **it may not terminate** |
| `GLD-EPI-06` | Audit `parent_name` across all `split_components` callers — one pass resolves `GLD-SPLIT-02` |
| `GLD-ORCH-S01` | What calls the **9 unrun** orchestration sub-orchestrators? |
| `GLD-BKP-07` | Enumerate the remaining destructive primitives that read the backup gate |
| `GLD-MVF-02` | Can `build_franchise_file_ids` failing open empty the franchise set? The docstring says franchise entries are **NEVER** deleted |
| **Greps** | ✅ `key=CacheKeyPaths` · ✅ `if not resp` · ⚠️ **`parent_name\s*=` output was captured but never analysed** · ❌ shim imports across **both** trees (`support/utilities/` **and** `services/trakt/movies/scorer`) |

## 0.5 What changed about how to read this register

Several of the most consequential findings arrived **late and unranked**
(`GLD-THY-02`, `GLD-RAN-06`), while several early 🔴s **dissolved on inspection**
(`GLD-RAD-01`, `GLD-PLY-01`/`02`). And three separately-filed items were **one bug
in three places**: `GLD-TWH-04`, `GLD-THY-01`, `GLD-SRF-01`.

**Do not work this register top-down by severity** — see the Calibration note at
the end of §8, and use §0.6 instead.

---

# §0.6 — BATCHED WORKLIST

## The batching rule

The scarce resource is **not developer time — it is dry-run verifications**, since
each one costs a full session against the live library. So batches are formed by
*verification cost*, not by severity or by folder.

A batch is safe when **every fix in it either cannot affect a run, or produces a
distinguishable signal in one.** A batch is unsafe when two fixes could mask each
other's failure — which is why the shared-component changes sit alone at the end.

**Batches 1 and 2 need ZERO dry-runs.** That is where the volume is.

---

## Batch 1 — Inert. No run required. Do all at once.

Nothing here can change what a run does: comments, docstrings, markdown, and
deletions of files nothing imports.

| Item | Fix |
|---|---|
| `GLD-RAN-01` 🔴 | Rewrite `radarr/repair/anomaly.py`'s docstring — it claims 2 of ~15 responsibilities and reads as though the file were read-only. **The worst P-G instance in the sweep**, on the file that owns four delete thresholds |
| `GLD-PLY-09` | Fix both mojibake instances (`playlists/__init__`, `writeback.py`) |
| `GLD-STO-03` / `GLD-RS-02` | Delete `sonarr/storage/deletion.md` and `sonarr/episodes/deletion.md` — both document a module **only Radarr has** |
| `GLD-TRK-01` | Delete `trakt/analytics.py`, `lookup.py`, `universe.py` — unreachable self-importing shims; even a path-import lands on the right class |
| `GLD-STO-09` | Document the `CacheKeyPaths` class-vs-path namespace inversion |
| `GLD-TWH-02` | Document the history TTL's **lower** bound (a request-coalescer, not a staleness bound) |
| `GLD-SP-04` | Record the phantom-reclaim bug as the origin of the coordinator's project/realize split |
| Reference citations | `GLD-TWH-01` · `GLD-TMD-02` · `GLD-TMD-04` · `GLD-TSC-02` · `GLD-RAN-04` · `GLD-RS-07` · `GLD-PLY-08` · `GLD-SERQ-03` · `GLD-THY-03` · `GLD-THY-04` — all "write this pattern into §8 so it is inherited, not rediscovered" |

**~18 items, zero verification cost.** ⚠️ `GLD-TRK-01` and the two `.md` deletions
need **you** to run `Remove-Item` — the Filesystem tools here are read/write/edit
only.

---

## Batch 2 — Dormant code. Cannot affect a run. Do all at once.

Every item is in a path P-I established never executes, so a mistake here cannot
break the live run. **They are also unverifiable until wired** — which is the
point: fix them now so wiring later is not simultaneously a debugging exercise.

| Item | Fix |
|---|---|
| `GLD-SQ-01` 🔴 | `SonarrQualityManager`'s construction loop subscripts a **class** — `TypeError` ×4, swallowed. The manager is filtered out of the loadable set, so this is inert today |
| `GLD-REP-11` 🔴 | `run_anomaly_repairs` calls `detect_unexpected_entries()`, which does not exist — untrapped at step 11 of 14 |
| `GLD-REP-08` 🔴 | `repair/anomaly.py` reads `sonarr::{inst}::series` — a double-colon format nothing produces |
| `GLD-REP-01` 🔴 | `critical_keys` says `"cache"`; the component is `"repair_cache"`. **One string** |
| `GLD-SRF-01` | Establish whether anything calls `get_all_series_chunked`; if not, delete it (it may not terminate) |

**5 items, zero verification cost.** `GLD-REP-01` is the only one visible in a run
at all — as a corrected repair component summary.

---

## Batch 3 — Live, independently observable. ONE dry-run verifies all five.

Each produces a **distinct** log signal, so a single run localises any failure.

| Item | Fix | Signal |
|---|---|---|
| `GLD-RQ-10` 🔴 | `selector.py` reads `radarr.quality.{inst}` expecting `{name: score}`; orchestration writes a **list of profiles**. The `isinstance` guard silently scores everything 0, so *best-by-custom-format* is really **first-valid-in-API-order** | Profile selection changes / new warning |
| `GLD-RT-01` 🟡 | `allowed_roots` fetch failure **disables the guard rather than the pass** | `[Routing]` line |
| `GLD-SRF-03` 🟡 | `validate_series_tags` uses the raw instance **and** a `.json` suffix — a 5th key format | Tag validation stops reporting everything invalid |
| `GLD-MVF-01` 🟡 | Three parallel pipe-strings with no length invariant; `people_matrix`'s billing decay reads `cast_order` | Parquet write OK, affinity unchanged |
| `GLD-STO-05` / `GLD-RS-04` 🟡 | `inf → 0.0` clamp conflates *unreadable* with *full* — live on multi-instance Radarr | Free-space lines |

**5 items, 1 dry-run.**

---

## Batch 4 — One concept, many files. ONE dry-run each, and alone.

High value — each collapses several register entries into one change — and for
exactly that reason a failure is hard to localise. **Do not combine these with
each other.**

| # | Item | Why alone |
|---|---|---|
| 4a | `GLD-COORD-01` + `GLD-RQ-01` — unify the pressure constant (**three sites at 25.0, one at 1000.0, one under a different name**) | Changes the space band on **every** service at once |
| 4b | `GLD-RAN-02` + `GLD-RQ-02` — import the copied vocabularies (`_UHD_LABELS`, `_KID_AGE_TIERS`) instead of duplicating | Creates the first *deliberate* `services → services` imports; settle `GLD-SP-03` first |
| 4c | `GLD-REP-02` + `GLD-SPLIT-01`/`02` + `GLD-STO-04` — the `parent_name` machinery: **3 sources, 4 semantics, 5 of 8 callers routing around it** | Touches component loading for every manager. **Needs the `parent_name` grep whose output was captured and never analysed** |
| 4d | `GLD-CACHE-S01` + `GLD-RT-07` + `GLD-TSC-01` — the shim tally (**12+ callers, 5 shims, 2 trees**) | MIGRATION Step 10. Do `GLD-TSC-01` first — replacing `import *` with the explicit list makes the surface finite and greppable |

---

## Never batch

| Item | Why |
|---|---|
| `GLD-DRIFT-01`/`02`/`03` | Changes to the drift detector must be verified against a **real comparison**, not a bootstrap. Wait until an anchor exists and one comparison has printed |
| Anything touching `BaseManager` | ~40 managers inherit it; the last such change took a full run to verify on its own |
| `deletions_enabled` | The one gate holding stage 2 back, at 968 GB against a 5000 GB floor with a 7-day dwell. **Not a fix — an operational decision** |

---

## Suggested order

**Batches 1 + 2 in one session** — ~23 items, no run needed. Then **Batch 3**, one
run. Then **Batch 4** one letter at a time, starting with **4d**, since
`GLD-TSC-01` alone is a one-line change that makes the largest remaining item
tractable.

That is **~28 fixes across 2 sessions and 1 dry-run**, before any of the
hard-to-localise work begins.

---

---

---

## 1. How this register works

Enhancements are identified **in place** — each `DESIGN.md` carries a §9
*Planned additions* table for its own folder, because that is where the context
lives. This file is the **rollup**, so the whole backlog is visible without
opening seventeen files.

| Field | Meaning |
|---|---|
| **ID** | Globally unique. `GLD-<AREA>-<nn>`. Stable — never renumber. |
| **Effort** | `S` ≈ hours · `M` ≈ a day · `L` ≈ multi-day |
| **Status** | `🔵 Open` · `🟢 Doing` · `✅ Done` · `⏸ Tabled` · `❌ Dropped` |
| **Source** | The `DESIGN.md` §9 row this came from |

### Area prefixes

| Prefix | Area | Prefix | Area |
|---|---|---|---|
| `CORE` | [`scripts/`](./DESIGN.md) | `DMN` | [`factories/daemons/`](./managers/factories/daemons/DESIGN.md) |
| `HOOK` | [`hooks/`](./hooks/DESIGN.md) | `ONB` | [`factories/onboarding/`](./managers/factories/onboarding/DESIGN.md) |
| `MGR` | [`managers/`](./managers/DESIGN.md) | `STEP` | [`onboarding/steps/`](./managers/factories/onboarding/steps/DESIGN.md) |
| `FAC` | [`factories/`](./managers/factories/DESIGN.md) | `ORCH` | [`orchestration/`](./managers/orchestration/DESIGN.md) |
| `WEB` | [`factories/web/`](./managers/factories/web/DESIGN.md) | `SVC` | [`services/`](./managers/services/DESIGN.md) |
| `REG` | [`factories/registry/`](./managers/factories/registry/DESIGN.md) | `TAUT` | [`services/tautulli/`](./managers/services/tautulli/DESIGN.md) |
| `CFG` | [`factories/config/`](./managers/factories/config/DESIGN.md) | `TRKT` | [`services/trakt/`](./managers/services/trakt/DESIGN.md) |
| `CACHE` | [`factories/cache/`](./managers/factories/cache/DESIGN.md) | `ML` | [`machine_learning/`](./managers/machine_learning/DESIGN.md) |
| `MIX` | [`factories/mixins/`](./managers/factories/mixins/DESIGN.md) | `RAD` | [`services/radarr/`](./managers/services/radarr/DESIGN.md) |
| `SON` | [`services/sonarr/`](./managers/services/sonarr/DESIGN.md) | `SUP` | [`support/`](./support/DESIGN.md) |
| `CON` | [`machine_learning/contracts/`](./managers/machine_learning/contracts/DESIGN.md) | `SCO` | [`machine_learning/scoring/`](./managers/machine_learning/scoring/DESIGN.md) |
| `SPA` | [`machine_learning/space/`](./managers/machine_learning/space/DESIGN.md) | `LIF` | [`machine_learning/lifecycle/`](./managers/machine_learning/lifecycle/DESIGN.md) |
| `LIK` | [`machine_learning/likelihood/`](./managers/machine_learning/likelihood/DESIGN.md) | `EVA` | [`machine_learning/eval/`](./managers/machine_learning/eval/DESIGN.md) |
| `LED` | [`machine_learning/ledger/`](./managers/machine_learning/ledger/DESIGN.md) | `THR` | [`machine_learning/thresholds/`](./managers/machine_learning/thresholds/DESIGN.md) |
| `LAB` | [`machine_learning/labels/`](./managers/machine_learning/labels/DESIGN.md) | `FND` | [`machine_learning/foundation/`](./managers/machine_learning/foundation/DESIGN.md) |
| `ROU` | [`machine_learning/routing/`](./managers/machine_learning/routing/DESIGN.md) | `FEA` | [`machine_learning/features/`](./managers/machine_learning/features/DESIGN.md) |
| `AFF` | [`machine_learning/affinity/`](./managers/machine_learning/affinity/DESIGN.md) | `SIZ` | [`machine_learning/sizing/`](./managers/machine_learning/sizing/DESIGN.md) |
| `QAN` | [`machine_learning/quality_analytics/`](./managers/machine_learning/quality_analytics/DESIGN.md) | `CLS` | [`machine_learning/classification/`](./managers/machine_learning/classification/DESIGN.md) |
| `DIS` | [`machine_learning/discovery/`](./managers/machine_learning/discovery/DESIGN.md) | `PLY` | [`machine_learning/playlists/`](./managers/machine_learning/playlists/DESIGN.md) |
| `ACQ` | [`machine_learning/acquisition/`](./managers/machine_learning/acquisition/DESIGN.md) | `NXW` | [`machine_learning/next_watch/`](./managers/machine_learning/next_watch/DESIGN.md) |
| `CHL` | [`machine_learning/challenger/`](./managers/machine_learning/challenger/DESIGN.md) | `PPL` | [`machine_learning/people_matrix/`](./managers/machine_learning/people_matrix/DESIGN.md) |
| `UPD` | [`machine_learning/updates/`](./managers/machine_learning/updates/DESIGN.md) | `PLX` | [`services/plex/`](./managers/services/plex/DESIGN.md) |
| `ACQS` | [`services/acquisition/`](./managers/services/acquisition/DESIGN.md) | `COORD` | [`services/coordinator/`](./managers/services/coordinator/DESIGN.md) |
| `WB` | [`services/writeback/`](./managers/services/writeback/DESIGN.md) | `BKP` | [`services/backup/`](./managers/services/backup/DESIGN.md) |
| `MDB` | [`services/mdblist/`](./managers/services/mdblist/DESIGN.md) | `CAL` | [`services/calendar/`](./managers/services/calendar/DESIGN.md) |
| `MAL` | [`services/mal/`](./managers/services/mal/DESIGN.md) | | |

**✅ `machine_learning/` sweep complete** — all 22 subpackages documented.

**Rule going forward:** a new §9 row must also get a row here, and both carry the
same global ID. See [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md) §10.

---

## 2. ⏸ Tabled — revisit after the documentation phase

| ID | Item | Why tabled |
|---|---|---|
| `GLD-ORCH-01` | **Central `dry_run` gate.** `dry_run` is currently a distributed invariant with no central enforcement — every manager individually remembers to check it. A manager that forgets applies for real during a rehearsal, and the only detection is noticing actual deletions. | Deliberately deferred until docs are complete. Needs a full audit of APPLY sites, which the remaining service docs will produce. |
| `GLD-ORCH-02` | `dry_run` audit hook — record every *suppressed* APPLY into the ledger, so a dry run reports what it would have done. | Depends on `GLD-ORCH-01`. |
| `GLD-ORCH-03` | AST guard: every APPLY method must check `dry_run`, enforced like brain purity. | Depends on `GLD-ORCH-01`. |

**Open decision blocking these:** does the gate live in `orchestration/` or
`factories/`? It is infrastructure with no media policy, which argues for
`factories/`. If it goes there, `orchestration/` has no remaining purpose and
should be deleted (`GLD-ORCH-06`).

---

## 3. Top 15 by value

If nothing else gets done, these are the ones that matter. Ordered by
value-per-effort, not by area.

| # | ID | Item | Effort | Why it's here |
|---|---|---|---|---|
| 1 | `GLD-REG-11` | ✅ **FIXED** — inverted registry anomaly heuristic | S | **Was a confirmed defect.** Flagged every row and stayed silent on a genuine stale-mirror import — the exact inverse of its purpose. See §4.7 |
| 2 | `GLD-MIX-01` | 🔴 Populate `load_summary` for pre-`prepare()` components | S | **Confirmed defect.** `instance_manager❌` / `radarr_cache❌` false negatives train you to ignore the one authoritative readiness line |
| 3 | `GLD-DMN-09` | Consume or drop the `translations` bucket | S | Free money — +1 Trakt call/movie across ~18k titles (~14% of enrichment cost) for a bucket nothing reads |
| 4 | `GLD-TAUT-01` | Consume or drop `tautulli/device_codec_matrix` | M | Keystone per-device signal, computed every run, zero readers |
| 5 | `GLD-CFG-03` | 🔴 Resolve `movieRootFolders` | S | Movie classification is computed then discarded; every movie lands in `/standard` |
| 6 | `GLD-ML-01` | Complete the brain migration — flat modules into subpackages | M | The only brain modules the purity guard cannot see; also resolves three duplicate module pairs |
| 7 | `GLD-ML-04` | Enrichment-coverage gate on scoring | M | Incomplete enrichment biases *uniformly toward deletion* and is indistinguishable from low affinity |
| 8 | `GLD-WEB-02` | Plan review UI | M | The dry-run plan is the primary output and is currently write-only Parquet |
| 9 | `GLD-DMN-01` | Daemon health heartbeat | S | A dead enrichment daemon is invisible until the next run; symptom is scores quietly not improving |
| 10 | `GLD-CACHE-01` | Thread-safe `MemoryManager` | S | Live race — the Radarr prefetch thread and main thread both touch it |
| 11 | `GLD-TRKT-02` | Audit every Trakt endpoint against the three-bug checklist | M | Three real bugs were found in one manager; nothing verifies the rest |
| 12 | `GLD-CFG-01` | Wire `validator.py` into the config load path | M | A typo'd key currently behaves exactly like an intentional default |
| 13 | `GLD-ML-05` | Determinism test — replay twice, assert identical | S | Verifies invariant I10, which nothing checks, and I10 underwrites the whole eval harness |
| 14 | `GLD-HOOK-02` | Run both guards in CI | S | Local hooks exit 0 when Python is missing — both invariants are advisory today |
| 15 | `GLD-SVC-01` | Trakt watchlist auto-pruning | M | The loop is open: watched items stay on the watchlist |

---

## 4. Full register

### 4.1 Confirmed defects (🔴)

These are not improvements — they are things that are wrong now.

| ID | Item | Effort | Status | Source |
|---|---|---|---|---|
| `GLD-REG-11` | Registry anomaly heuristic is inverted — flags the canonical checkout, silent on the stale one. `_is_expected_path()` is written but never called | S | ✅ **Fixed** — see §4.7. Riding along: `GLD-REG-13` (two entry shapes, three wrong readers) and `GLD-REG-14` (origin overwritten by the mixin) | [registry §6.1](./managers/factories/registry/DESIGN.md) |
| `GLD-MIX-01` | `load_summary` conflates *absent* with *failed* → false `❌` in prepare summaries | S | ✅ **Fixed in Sonarr, session 34** — `prepare()` pre-marks eagerly-built components: *"else they render ❌ despite being healthy."* A second fix is adjacent (*"previously such failures were silently swallowed"*). **Becomes: verify Radarr has the same pre-marking** — `GLD-SON-04` | [sonarr §12.5](./managers/services/sonarr/DESIGN.md) |
| `GLD-CFG-03` | `movieRootFolders` empty → `classify_movie` buckets discarded | S | 🔵 Open | [config §9](./managers/factories/config/DESIGN.md) |
| `GLD-ML-01` | Flat brain modules unguarded by purity; three duplicated module pairs | M | 🔵 Open | [ML §3.4](./managers/machine_learning/DESIGN.md) |
| `GLD-ML-15` | ⚠️ **Downgraded** — six of eight are documented shims awaiting Step 10, not drift | S | 🔵 Open | [ENH §8 P-E](#p-e--duplicate-implementations) |
| `GLD-SCO-01` | 🔴 "4K ≥ 70" is **stale** — live value is **75**; the 70 constant was dead and is deleted. Correct the six docs repeating it | S | 🔵 Open | [thresholds §3.4](./managers/machine_learning/thresholds/DESIGN.md) |
| `GLD-THR-01` | 🔴 Two watchability axes disagree on delete-eligibility for 16.5% of movies; specs carry no `axis` field | S | 🔵 Open | [thresholds §3.4](./managers/machine_learning/thresholds/DESIGN.md) |
| `GLD-REG-03` | `print_tree_view` called by `base_manager.py` but does not exist | S | ✅ **Fixed** — and the call site mattered more than the missing method: it sat inside the parent-linking `try`, so the debug flag could silently cost every manager its inherited `dry_run`. See §4.7 | [registry §9](./managers/factories/registry/DESIGN.md) |
| `GLD-SON-01` | 🔴 ✅ **CONFIRMED s34, DEEPENED s35** — `SonarrQualityManager` + `SonarrSyncManager` are in `full_components` but absent from `component_dependencies`, so `all_component_classes` drops them; `prepare()`/`run()` iterate `component_dependencies`, so **neither loads, prepares nor runs**. **And `SonarrSyncManager` has no `run()` and no `prepare()`** — re-wiring alone would construct it and invoke nothing. Its children are appliers taking an explicit config argument. **Reactivation needs a `run()` AND a source for the configuration to apply** | M | 🔵 Open | [sync §2](./managers/services/sonarr/sync/DESIGN.md) |
| `GLD-SYNC-02` | 🔴 **Sonarr config sync does not run** — five fully-implemented, individually-documented, `dry_run`-correct appliers (custom formats, naming, folders, media management, tags) with **no caller**. Radarr's equivalent runs. Decide: delete or reactivate | M | 🔵 Open | [sync §5](./managers/services/sonarr/sync/DESIGN.md) |
| `GLD-SON-13` | 🔴 **The prepare summary cannot reveal it** — the denominator is `component_dependencies`, so a healthy run reports **8/8** and is *structurally incapable* of naming `quality` or `sync`. This is why it has stayed invisible *(P-D)* | S | 🔵 Open | [sonarr §12.3](./managers/services/sonarr/DESIGN.md) |
| `GLD-SON-14` | **`run()` silently skips a component that fails to load** — `getattr(...) or _load_component(name)` returning `None` falls through the `hasattr` guard with **no entry in `results`**, so `all_ok = all(results.values())` stays `True` | S | 🔵 Open | [sonarr §12.4](./managers/services/sonarr/DESIGN.md) |
| `GLD-SYNC-01` | ✅ **ANSWERED session 37** — `orchestration/series_sync.py` is **series** sync, not **config** sync: `parent_name = "SonarrSeries"`, it reads `getattr(self.manager, "sync")` → `SonarrSeriesSyncManager` (a `series/` child), and calls `composite_sync_workflow`. Its error string confirms it. **Nothing constructs a config-sync child — `GLD-SYNC-02` stands** | S | ✅ Closed | [sync §4](./managers/services/sonarr/sync/DESIGN.md) |
| `GLD-SON-15` | 🟡 **Three different things are called "sync"** — `sonarr/sync/` (push config to Sonarr, **dead**), `sonarr/series/sync/` (series workflow, **live**), and `orchestration/series_sync.py` (orchestrates the second). The naming collision is what made `GLD-SYNC-01` necessary | S | 🔵 Open | [sync §4](./managers/services/sonarr/sync/DESIGN.md) |
| `GLD-SER-03` | 🟡 **And three are called "quality"** — `sonarr/quality/` (**dead** superseded copy), `orchestration/quality.py` (live orchestrator), `series/quality.py` (**40.8 KB, live and invoked**). Same shape as `GLD-SON-15`; the one that actually runs is the one a reader finds last. Both collisions have now cost a disambiguating read | S | 🔵 Open | [series §4](./managers/services/sonarr/series/DESIGN.md) |
| `GLD-SER-02` | 🟡 **Sonarr uses three component-loading mechanisms** — `SonarrManager` (`component_dependencies`+`topo_order`+`split_components`+`_load_component`), `sync`/`quality`/`orchestration.quality` (`split_components`+manual loop), `SonarrSeriesManager` (`load_components`). A loading-semantics fix must be applied three ways, and `GLD-ORCH-S02`'s protocol ported into each | S | 🔵 Open | [series §2](./managers/services/sonarr/series/DESIGN.md) |
| `GLD-SQ-01` | 🔴 **`SonarrQualityManager` is a superseded DEAD COPY — delete it.** `orchestration/quality.py` constructs the **same four components** with the same `critical_keys` and flag pattern, and **is** loaded. The only material difference: orchestration hoists `component_init_kwargs` and does `cls(**component_init_kwargs)` ✅; quality does `cls(**critical_components[name]["init_kwargs"])`, subscripting a class — `TypeError` for all four, swallowed by `try/except` ❌. **So the four quality subcomponents DO run, via orchestration** *(P-E)* | S | 🔵 Open | [quality §2.2](./managers/services/sonarr/quality/DESIGN.md) |
| `GLD-SPLIT-01` | 🟡 **`split_components` double-constructs noncritical components** — it instantiates each purely to read `parent_name`, then the caller instantiates it again. Constructor side effects (incl. `self.register()`) fire twice. Dormant in Sonarr (all components critical); live anywhere they are not | S | 🔵 Open | [quality §3](./managers/services/sonarr/quality/DESIGN.md) |
| `GLD-SPLIT-02` | 🔴 **`split_components` SILENTLY DROPS a component whose `parent_name` doesn't match** — absent from **both** returned dicts, no exception, no warning. ✅ **Session 47: the mechanism is worse than silence.** The convention is *conditionally applied* — managers declare `parent_name = "SonarrX"` as a class attribute, then **some** overwrite it with `self.__class__.__name__` in `__init__` and some don't. `split_components` constructs a temp instance and reads the **post-`__init__`** value, so **which components survive is accidental**. `sharding` is not an unlucky case, it is the one somebody noticed | S | 🔵 Open | [episodes §3.1](./managers/services/sonarr/episodes/DESIGN.md) |
| `GLD-EPI-01` | 🔴 **Shadowed modules — FOUR instances, confirmed by directory scan s60.** Python resolves a regular package **before** a same-named module, so a `.py` beside a same-named package is **unimportable**. • `sonarr/episodes/retrieval.py` (3.1 KB) — a **real stub class** also named `SonarrEpisodesRetrievalManager`, sole method logs *"🧪 Would pull episode data"*; a path-based import would silently substitute a do-nothing class. • `trakt/analytics.py` (179 B), `trakt/lookup.py` (167 B), `trakt/universe.py` (175 B) — **self-importing deprecation shims**: each does `from …trakt.X import XManager`, which resolves to **the package shadowing it**. Harmless but strictly dead *(P-E, silent-shadowing variant)* | S | 🔵 Open | [episodes §2.1](./managers/services/sonarr/episodes/DESIGN.md) |
| `GLD-TRK-01` | **Delete the three `trakt/` shadow shims** — `analytics.py`, `lookup.py`, `universe.py`. **Zero risk**: unreachable by import, and each re-exports from the package that shadows it, so even a path-import lands on the right class. Their *"Deprecated — use the X package instead"* comments describe a rule Python already enforces absolutely. ⚠️ `trakt.lookup.search_show_by_title_and_year` is cited in `mal/id_bridge`'s docstring — that reference resolves to the **package** and is unaffected | S | 🔵 Open | [episodes §2.1](./managers/services/sonarr/episodes/DESIGN.md) |
| `GLD-EPI-03` | 🟡 **The episodes summary cannot report `sharding`** — dropped from both dicts by `GLD-SPLIT-02`, then hand-loaded, so it is in **neither** collection `log_filtered_component_summary` reads. Third instance of a filtered denominator, at a third level of the tree | S | 🔵 Open | [episodes §4](./managers/services/sonarr/episodes/DESIGN.md) |
| `GLD-EPI-04` | 🟡 **`sonarr.episodes_manager_initialized` reflects 1 component of 5** — `critical_keys = {"retrieval"}`, so `all_components_loaded` evaluates `1 == 1` regardless of `file`, `history`, `monitoring`, `sharding` | S | 🔵 Open | [episodes §5](./managers/services/sonarr/episodes/DESIGN.md) |
| `GLD-STO-01` | ✅ **FIXED session 59 + 61 — TWO files, same bug.** `warm_cache` passed the **raw** `SPACE_ESTIMATES` template while the reader **formats** it, so the warm pass wrote a literal `sonarr/<instance>/…` key nothing read. Fixed in **`sonarr/storage/__init__.py`** (s59) and **`sonarr/storage/space.py`** (s61) — the second found only by the grep. Both now mirror Radarr's `.replace("<instance>", instance or "default")` | S | ✅ **Fixed ×2** | [radarr/storage §2](./managers/services/radarr/storage/DESIGN.md) |
| `GLD-STO-08` | ✅ **GREP DONE session 61.** Live unformatted `key=CacheKeyPaths` uses: **(1)** `sonarr/storage/__init__.py` ✅ fixed s59 · **(2)** `sonarr/storage/space.py` ✅ fixed s61 · **(3)** `support/utilities/cache/cache_warmup.py:121` ⚠️ **not touched** — it also passes `category="sonarr", instance=instance`, so `get_or_generate_cache` may format internally; **check the signature before editing** · **(4)** `sonarr/quality/custom_formats.py:43` — dormant (`quality` never loads, `GLD-SQ-01`). Radarr's two sites both correct | S | 🟡 3 of 4 resolved | [storage §2.1](./managers/services/sonarr/storage/DESIGN.md) |
| `GLD-RQ-10` | 🔴 **REVISED session 61 — it is a TYPE mismatch, not a missing key.** `radarr.quality.{inst}` **is** written — by `radarr/orchestration/__init__.py:176`, storing **`profiles`** (a list of API dicts). Three consumers read it as a **list** (`movie_files.py:1247`, `space_pressure.py:1883`, `repair/anomaly.py:1343`). But `selector.py:152` reads it as **`{profile_name: score}`** and guards with `if isinstance(cf_scores, dict) else 0` — so **every profile scores 0**, the strict `>` keeps the first, and *best-by-custom-format* silently becomes **first-valid-in-API-order**. The guard is what hides it | S | 🔵 Open | [radarr/quality §6.5.2](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-CW-01` | ❓ **`CacheWarmupManager.warm_cache` constructs `SonarrQualityManager` directly** — the dead manager whose construction loop raises `TypeError` for all four subcomponents (`GLD-SQ-01`), then calls `manager.get_episode_profiles(instance)` inside a lambda. Bypasses the component system entirely. Also: is `CacheWarmupManager.warm_cache` itself ever called? `run_all` iterates **module-level** `warm_cache` functions, and this is a **classmethod on the manager** | S | 🔵 Open | — |
| `GLD-RS-02` | ✅ **The two orphan `deletion.md` files, explained** — **Radarr has `deletion.py`** (11.2 KB, `critical`); Sonarr never did. `sonarr/storage/deletion.md` and `sonarr/episodes/deletion.md` are **scaffolding copied from Radarr's folder shape**; Sonarr deletion lives in `cache/episode_files.py` instead. Resolves `GLD-STO-03` from *"find the deleted modules"* to *"delete two docs, or write the module"* | S | 🔵 Open | [radarr/storage §3](./managers/services/radarr/storage/DESIGN.md) |
| `GLD-RS-03` | 🟡 **Re-scope `GLD-RAD-01` before working it** — `radarr/storage/` holds `cross_instance_move.py` (**18.4 KB, 15.8 KB tests**), `cross_instance_dedup_apply.py` (8.8 KB + 6.8 KB tests) and `shared_storage.py`. A cross-instance **move** implementation with a near-equal test suite is what the *"migration remains"* half describes | S | ✅ **Closed s57** — confirmed built; `GLD-RAD-01` re-scoped | [radarr/storage §4](./managers/services/radarr/storage/DESIGN.md) |
| `GLD-STO-08` | 🎯 **Grep every unformatted `CacheKeyPaths` use** — **all** keys carry `<instance>`/`<user>` placeholders, so any `key=CacheKeyPaths.X` without substitution is the same bug as `GLD-STO-01`. Note `<instance>` is not `{instance}`, so `str.format()` would not error either. ⚠️ **Cannot be done with the tools available** — `Filesystem:search_files` matches **filenames**, not contents. Needs one `grep`; see the standardisation note below §4.α | S | 🔵 Open — **needs a grep** | [storage §2.1](./managers/services/sonarr/storage/DESIGN.md) |
| `GLD-STO-04` | 🎯 **Does the non-critical / `parent_name` machinery earn its keep?** ⚠️ **Session 52 — full Sonarr count.** **Five** callers declare *every* component critical and route around the filter (`sync`, `quality`, `orchestration.quality`, `storage`, `monitoring`); **three** exercise it — `repair` (6 of 15, and `repair_cache` is mis-keyed into it), `episodes` (1 of 5, drops `sharding`), `validator` (4 of 5, **unverified**). **Avoided by the majority, malfunctioning in the minority that use it.** Deleting it would close `GLD-SPLIT-01` and `GLD-SPLIT-02` together | S | 🔵 Open | [sonarr §12.8](./managers/services/sonarr/DESIGN.md) |
| `GLD-SON-16` | 🎯 **`component_dependencies` is a CONSTRUCTION graph, not an EXECUTION graph** — **six of eight** entries define no `run()`. `SonarrManager.run()` writes a `results` entry only inside `if hasattr(component, "run")`, so the **run summary's denominator is ~2 of 8**. Prepare over-reports (8/8, filtered set); run under-reports (~2/8, capability-filtered). **Mostly deliberate**: `validator` exposes `audit_bootstrap_instances` and names its caller — these are service-layer facades, not `run()` participants. **Re-test `GLD-SERQ-01` and `GLD-REP-07` against "does the facade have a caller?"** rather than "does it have `run()`?" | M | 🔵 Open | [sonarr §12.7](./managers/services/sonarr/DESIGN.md) |
| `GLD-RQ-01` | 🔴 **Radarr names the pressure constant `PRESSURE_THRESHOLD_GB`** while three other sites use `PRESSURE_FALLBACK_GB` for the same concept, comment and value — so a grep for the common name **misses this site entirely**. See `GLD-COORD-01` | S | 🔵 Open | [radarr/quality §3](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-RQ-02` | 🟡 **`_KID_AGE_TIERS` is a copied Plex vocabulary** asserting the scorer and playlist age-gate *"cannot disagree about who is a child"* — with **no mechanism enforcing it**. Unlike `next_watch`'s copy (forced by the brain/service import rule), a `services → services` import **is** available here | S | 🔵 Open | [radarr/quality §4](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-RQ-03` | 🟡 **`WATCHABILITY_PROTECT_THRESHOLD = 6`** sits **below the AXIS V2 median (8)**, so the non-exhaustive path shields most of the owned library from downgrade. Appears absent from `THRESHOLD_SPECS`, and was not obviously re-anchored when Group D v2 translated the axis — while the docstring promises the legacy path is restorable *"byte-for-byte"* | S | 🔵 Open | [radarr/quality §5](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-RQ-10` | ✅ **FIXED session 87 — "best by custom-format score" was really "first-valid-in-API-order".** `selector.py` scored profiles from `global_cache["radarr.quality.{inst}"]` expecting `{name: score}`; `radarr/orchestration:176` writes the raw **LIST** of profile dicts there. The guard `score = cf_scores.get(name, 0) if isinstance(cf_scores, dict) else 0` returned **0 for every profile**, and strict `>` kept the first — **the isinstance guard is exactly what hid it**. Now scored from the profile itself (`_profile_cf_score`: `cutoffFormatScore` → `minFormatScore` → sum of POSITIVE `formatItems`, since negative scores are exclusions not preferences), so there is no second source to drift from and no key to be wrong about. Logs at debug when no candidate carries a CF score, so "fell back to order" is stated rather than assumed | S | ✅ **Fixed** | [radarr/quality §6.5.2](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-RT-01` | ✅ **FIXED session 87 — the cross-root guard now fails toward INACTION.** A failed `rootfolder` fetch left `allowed_roots` empty, which *"disables the guard rather than the pass"* — so a transient API error let same-instance moves proceed **unguarded**, in the one place that moves files. Now: when actuating and the root list is unreadable, the pass **plans only** and warns. The plan is still written to `routing.log` and the next run with a readable list applies it, so the cost is one run of re-organisation vs. a kids film landed in a root its audience cannot reach | S | ✅ **Fixed** | [routing §4](./managers/services/routing/DESIGN.md) |
| `GLD-SRF-03` | ✅ **FIXED session 87 — and the key was the smaller half.** `validate_series_tags` built `f"sonarr/{instance}/tags.json"` — raw instance (the next line resolves it) plus a `.json` suffix `CacheKeyPaths.sonarr.TAGS` does not carry. But the key only mattered because a miss gave `known_tags = set()`, so **every tag on every series read as an invalid reference** and the validator reported the whole library broken. Now uses the registry key with the resolved instance, and an unreadable tag list is a **SKIP with a reason**, not a finding — absent vs empty, in the one place a false positive is indistinguishable from a real detection | S | ✅ **Fixed** | [retrieval §3](./managers/services/sonarr/series/retrieval/DESIGN.md) |
| `GLD-STO-05` | ✅ **FIXED session 87 — selection and aggregate separated.** The `inf → 0.0` clamp is **right for selection** (route around a misconfigured instance) and **wrong for `get_minimum_free_space`**, where one unreadable instance reported the entire household at 0 GB — against a **5000 GB floor** that reads as maximum pressure, which is the delete path's input. Unreadable instances are now tracked in `_unreadable_instances`: still 0.0 for selection (behaviour unchanged), excluded from the minimum, and warned about by name. When **every** instance is unreadable the old value is preserved rather than invented, because that number may gate deletion and changing it silently in either direction is worse than saying it cannot be trusted | S | ✅ **Fixed** | [radarr/storage §5](./managers/services/radarr/storage/DESIGN.md) |
| `GLD-STO-10` | ✅ **A FIFTH `dry_run` clobber site — `radarr/storage/space.py`.** Same `kwargs.get("dry_run", getattr(parent, ...))` after `super().__init__()`. Removed; `BaseManager` resolves it. `GLD-ORCH-02` found three, `GLD-RAN-07` a fourth | S | ✅ **Fixed** | — |
| `GLD-MVF-01` | ✅ **FIXED session 92 — a LIVE defect, not the latent one I filed.** `refresh_enrichment` overwrote `cast_names` from **Trakt** while leaving `cast_characters` and `cast_order` at their **Radarr** values — different source, different ordering, different length, **misaligned by construction on every enrichment pass**. Not the hypothetical `\|`-in-a-name shift I had recorded. The consequence reaches the scorer: `people_matrix`'s billing decay `1/(1+0.25*rank)` was weighting **Trakt's actors by Radarr's billing positions**, so Group-C4 person affinity has been computed from mismatched pairs. Fixed by adding both parallels to `ENRICH_COLS` — correct either way, since a missing key nulls the stale array rather than leaving it describing a cast list that no longer exists. ⚠️ **I deferred this twice believing the write site was unfindable; the constructor (`_extract_people`) was always correct and the bug was 400 lines away in a different method** — see §8's *"check every WRITER, not the constructor"* | S | ✅ **Fixed** | `movie_files.py` |
| `GLD-RQ-11` | ✅ **CLOSED session 78** — the weak two-level `dry_run` in `radarr/quality/selector.py` is **removed**; `BaseManager` now resolves it. See `GLD-ORCH-01`/`02` | S | ✅ Closed | [radarr/quality §6.5.3](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-RAN-02` | 🟡 **`_UHD_LABELS` copied between two SERVICES with "MUST match" and no enforcement** — identical tuple in `radarr/repair/anomaly.py` and `services/routing/uhd_reconcile.py`, with the coupling spelled out in capitals. **Third copied-vocabulary instance**, and unlike `next_watch`'s (forced by the brain/service rule) **both files are services**, so an import is available — `uhd_reconcile` already imports from `radarr/storage`. Drift means the monitored-missing triage and the dual-version reconcile **disagree about which Radarr instance is the 4K one** | S | 🔵 Open | [radarr/repair §2](./managers/services/radarr/repair/DESIGN.md) |
| `GLD-RAN-01` | ✅ **FIXED session 85.** `radarr/repair/anomaly.py`'s docstring claimed two diagnostic scans; the module **demotes, unmonitors, DELETES, restores and 4K-routes** owned movies and owns four delete-family thresholds. Rewritten to cover: the four public passes and what each decides · the five thresholds it owns · **why AXIS LEGACY is 20 and not 17** (with the two-axis measurement) · the hysteresis constraint and `_resolve_prune_floors` · the two cache keys it keeps, including that the dwell clock advances under `dry_run` so elapsed time stays real. Opens *"DESPITE THE NAME, THIS IS NOT A REPORTER"* | S | ✅ **Fixed** | [radarr/repair §1](./managers/services/radarr/repair/DESIGN.md) |
| `GLD-RT-08` | ✅ **FIXED session 85 — a LIVE bug found by reading `routing.log`, not code.** `RoutingManager._reorg` gated folder moves on `mode == "same_instance"` by **equality**, but `reorg_mode` is set to `cross_instance` — which per the operator means *"right folder **AND** right instance"* (standard 720/1080 → ultra 2160), i.e. a **superset**. Opting into the MORE capable mode therefore silently switched the folder half **OFF**: 25 movies + 5 shows planned to `routing.log` every run and never applied, visible only as a *"log only"* count that looked deliberate. Now `mode in _ACTUATING_MODES` (frozenset), so adding a mode later cannot reintroduce it. ⚠️ `relocation_enabled` is **absent from config** — the second gate — so this alone does not arm the moves | S | ✅ **Fixed** | [routing §3](./managers/services/routing/DESIGN.md) |
| `GLD-CLS-05` | ✅ **DOCUMENTED session 85.** The anime bucket is **deliberately all Asian-origin animation**, not Japanese-only — Korean (Pinkfong) and Chinese (The Legend of Silk Boy, The Royal Cat) share the shelf with One Piece by design. Recorded in `library_classifier.py` with the reason it must not be "fixed": narrowing the language test would scatter the shelf across Kids and Standard, and splitting the bucket means **renaming a root folder**, which triggers a full relocation of everything inside it | S | ✅ **Documented** | — |
| `GLD-AUD-01` | ✅ **The `dry_run` gate is PROVEN, negatively.** `support/logs/audit.log` last modified **Aug 4 16:36** — none of the three Aug 5 dry runs wrote a Plex playlist despite building them each time; its 14 entries are all from the prior live run. **An absence observed across three runs is the strongest evidence in this exercise.** Note the two writebacks are separate gates: `[Writeback] disarmed` is `services/writeback` (Trakt/MAL); the Plex playlist writeback has its own arm check. Both held | S | ✅ Verified | — |
| `GLD-UHD-01` | ✅ **BUILT session 86 — per-category 4K roots + a content-library gate.** `classify_movie` puts CONTENT above RESOLUTION (anime → kids → 4k → standard), so a 2160p Pixar film is categorised **kids** — but `uhd_reconcile` relocated every 4K companion into ONE flat root, taking kids/anime titles off the Plex shelf their audience browses. For KIDS that is not untidiness: a restricted profile reaches only the Kids library, so the film silently disappears. Added `routing.movies.uhd_root_folders` (per-category, empty default = byte-identical), `_dest_root_for` on all three actuator call sites, and `category_uhd_allowed` — which caps at 1080p unless the 4K root is positively confirmed reachable | M | ✅ Done | `routing_targets.py`, `uhd_reconcile.py` |
| `GLD-UHD-02` | 🎯 **The category's Plex library is identified by its FOLDERS, never its name.** First attempt matched section titles against `kid`/`child`/`family` tokens — a guess that fails on "Little Ones", "Family Movies (4K)" or any non-English install, **and fails silently by capping content it should allow**. Now: whichever Plex MOVIE section already holds `movieRootFolders[category]` **is** that category's library, whatever it is called. A single-shelf household needs no configuration at all — one `Movies` library on `/data/media/movies` contains both the kids root and `/4k/kids`, so containment succeeds by itself | S | ✅ Done | `routing_targets.plex_sections_covering` |
| `GLD-UHD-03` | ✅ **Four distinct cap reasons, each naming its own fix** — `unconfigured` (silent, ordinary install) · `category-not-in-plex` (**no Plex library holds the category's standard root at all** — a bigger problem than 4K) · `not-in-library` (library exists, 4K root not attached to it — names the library: *"add '/4k/kids' as a second folder on 'Movies'"*) · `unverifiable` (`plex/sections` cold — *"a stale-cache problem, not a Plex mis-configuration"*). Warns **once per category+reason**, not per title | S | ✅ Done | `uhd_reconcile.py` |
| `GLD-UHD-04` | ✅ **`_validate_uhd_roots` — the Radarr-side twin of the Plex check.** Warns when a configured 4K root is not a **registered root folder** on the 4K instance: the directory existing on disk is not enough, and an add pointed at an unregistered root can fail or land in an unmanaged path. Two checks, same class of mistake, different application, different remedy — kept separate so the message says *which half* is outstanding | S | ✅ Done | `uhd_reconcile.py` |
| `GLD-CG-01` | ✅ **FIXED session 86 — `cert_gate.tier_level` conflated "no tier" with "adult".** Plex frequently omits `restrictionProfile`, so a managed CHILD profile and an unrestricted ADULT both arrived as `None`/`""` and both resolved to **ADULT** — absent conflated with a known value, **in the one gate that decides what a child sees**. Same P-C shape as `GLD-TWH-04`, `GLD-REP-08` and the cache-miss bug. Added `is_managed` + `unknown_managed_tier` (both default to today's answer, so byte-identical) and `is_ungated_managed()`, which makes *"we could not tell"* a distinct outcome | S | ✅ Done | `cert_gate.py` |
| `GLD-CG-02` | 🎯 **The two gates fail in OPPOSITE directions, and that is now justified rather than accidental.** The 4K gate fails CLOSED (cap at 1080p); the playlist age-gate fails OPEN (to adult). The asymmetry is real: **`cert_allowed` fails closed on an unknown cert for any restricted profile, and ~41% of the library carries no certification** — so gating an ungated profile at even TEEN silently removes every uncertified title CSM does not rescue. Capping 4K costs a resolution tier; gating a possibly-adult guest costs 41% of their library. **Recorded in `tier_level`'s docstring so the next reader does not "fix" one to match the other** | S | 📝 Recorded | `cert_gate.py` |
| `GLD-CG-03` | 🟡 **`is_ungated_managed` is BUILT but UNWIRED** *(P-I, labelled honestly)*. The right consumer is `plex/playlists/builder.py`: **skip** an ungated managed profile rather than guessing — the house pattern for unusable input, already used for PIN-less users (*"SKIPPED and COUNTED"*) in the same subsystem. `_warn_ungated_managed_users` already names the affected profiles by de-identified label, so the operator gets a list and a one-line fix. Needs a deliberate read of the 120 KB builder | M | 🔵 Open | `cert_gate.py` |
| `GLD-CG-04` | 🟡 **Onboarding should surface which Plex library holds each category root** — at the moment the routing step captures a root folder, show the library that already contains it (`category_library_titles`, on data already cached). Turns a silent misconfiguration into something visible while the operator is looking at it. ⚠️ Needs a proper read of the step's prompt machinery rather than pattern-matching from its header | S | 🔵 Open | `steps/routing.py` |
| `GLD-PLX-10` | ❌ **`plex/user_sections` cannot carry an audience map** — investigated and rejected as the basis for the 4K gate. Three reasons: **(1)** `_resolve_section_grants` is gated on `plex.playlists.this_week_in_history.enabled`, so the map exists only as a side-effect of an unrelated shelf feature; **(2)** grants come from plex.tv `shared_servers`, which describes **shared** users, not **Home managed** profiles — so it fails closed to empty for exactly the users the gate concerns (hence the `trust_home_managed` escape hatch); **(3)** `restrictionProfile` is frequently absent anyway. Folder attachment is the robust mechanism | S | 📝 Recorded | `plex/users/__init__.py` |
| `GLD-PLX-11` | ✅ **Every playlist builder labelled its output `[dry-run]` on runs that wrote to Plex** — the 2026-08-20 live run logged `[dry-run] 'Mom' Up Next …` for all three builders and every line of `playlists.log`, while `[Writeback] ARMED` updated **28 playlists** (28 branded, 2 retitled, 2 sort keys repaired after server drift). The builders never write to Plex — that part is true — but "dry-run" reads as *nothing happened*, and `CollectionPosters` genuinely WAS disarmed in the same run, so the two adjacent states made the mislabel easy to trust. Same class as `TOTAL +242.7`: a log that reads as a no-op over real work. Now `[plan]`, with each summary pointing at the `[Writeback]` banner for the actual outcome | S | ✅ **Fixed** — §0.1 #69 | `builder.py`, `movie_builder.py`, `combined_builder.py` |
| `GLD-RAN-04` | 🎯 **Propagate the independent-anchor idea** — `anomaly.py`'s repair grids show a **critic rating beside the watchability score**, *"so a critic Rating gives an outside anchor to read the watchability Score against"*, with two scales rendered correctly (IMDb/TMDb/Trakt as `x.x`, RT/Metacritic as `n%`). An operator reading *"delete: score 6"* can see whether the model is the outlier. **Almost nothing else in the repo shows a decision next to a signal the model did not produce** | S | 🔵 Open | [radarr/repair §4](./managers/services/radarr/repair/DESIGN.md) |
| `GLD-RAN-06` | ✅ **FIXED session 84.** `movie_demote` and `movie_restore` were resolved in **different methods**, each through its own `get_threshold` call, with **nothing comparing them** — so one independent re-anchor by the calibrator would open a band in which every movie in it is deleted one run and re-acquired the next, forever (`owned_restore_min_age_days` defaults to **0**, so nothing damps it). Both now resolve through **`_resolve_prune_floors()`**, which enforces `restore >= demote` and, on a crossing, **clamps the DELETE floor DOWN** — per the fail-direction rule, since clamping restore *up* would keep deleting at the same rate while stranding every title in the band as deleted-and-never-restored. Warns loudly with both values and the affected score range. ⚠️ **Verified as primed, not latent**: `owned_demote_enabled`/`owned_delete_enabled` are **true**, free space is **968 GB against a 5000 GB floor**, and the delete dwell is **expedited to its 7-day minimum** — only `deletions_enabled(config)` was holding stage 2 back | S | ✅ **Fixed** | [radarr/repair §5](./managers/services/radarr/repair/DESIGN.md) |
| `GLD-RAN-07` | ✅ **FIXED session 80 — a FOURTH `dry_run` clobber site, in the file that deletes movie files.** `RadarrRepairAnomalyManager.__init__` ran `self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)` **after** `super().__init__()`, overwriting a parent-inherited `True` with `False` — in the manager whose `demote_stale_monitored` stage 2 issues `DELETE moviefile/{id}`. `GLD-ORCH-02` had only found three | S | ✅ **Fixed** | [radarr/repair §6](./managers/services/radarr/repair/DESIGN.md) |
| `GLD-SON-10` | ⚠️ **RETRACTED s36, EXPLAINED s54** — I claimed Sonarr's `cache/` **and** `quality/` each carried `test_size_anomaly_*.py`. Sonarr's `quality/` has **no tests at all**; the pair is real but **Radarr's** (`radarr/quality/`). Original observation mis-attributed to the wrong service. Closes as a mis-scoped note, not a defect | S | ✅ Closed | [radarr/quality §1](./managers/services/radarr/quality/DESIGN.md) |
| `GLD-CAL-A1` | ⚠️ **Calibration, 8th and 9th downgrades (session 75).** `GLD-SRF-01` was attributed to the wrong method — the truncating loop is in `get_all_series_chunked`, which the live path never calls. `GLD-SRF-02` was rated 🔴 on "the guard passes the truncation as success" when the guard **fires correctly** on the realistic failure (total fetch failure) and is tautological only for a short-but-successful response. **Both errors share one cause: I traced a call site by name-similarity instead of reading the caller.** `refresh_all_series` and `get_all_series_chunked` both fetch series; only one is wired. Same shape as the `GLD-SIZ-04` triple-revision — **reachability requires reading the caller, not matching the callee's name** | — | 📝 Recorded | [retrieval §1](./managers/services/sonarr/series/retrieval/DESIGN.md) |
| `GLD-REP-01` | 🔴 **`repair/critical_keys` declares `"cache"`; the component is registered as `"repair_cache"`.** The key matches nothing, and `SonarrRepairCacheManager` silently falls to the **non-critical** path — so its failure leaves `sonarr.repair_manager_initialized` `True`, **and** it becomes a `GLD-SPLIT-02` drop candidate. **Second instance of `GLD-SON-11`'s shape**; `GLD-SON-12`'s assertion now has two live cases. ✅ **Session 53: `repair_cache` is confirmed the correct attribute** — `orchestration/repair.py` calls `self.repair.repair_cache`. So this is a **stale key**, and the fix is one string | S | 🔵 Open | [repair §2](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-REP-02` | 🎯 **ROOT CAUSE of `GLD-SPLIT-02`: `parent_name` has three sources and four semantics.** Sources: class attribute, `BaseManager` **path inference** (yields `"SonarrRepair"`), `BaseManager` overwrite from `init_args`, explicit assignment in `__init__`. Semantics across four managers: own-name-minus-Manager · own-full-name · parent-name-minus-Manager · **parent-full-name**. `repair/__init__.py` hand-forces **both sides** of the comparison, with both workarounds documented. **Define one semantic, or stop matching on it** | S | 🔵 Open | [repair §3](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-REP-03` | **`split_components` introspection dictates every caller's `init_kwargs`** — *"Pass through the API + instance refs so sub-managers can resolve their dependencies **without raising during split_components introspection**."* So `init_kwargs` is sized by what a **throwaway** construction needs, not what the components need to run. Strengthens `GLD-STO-04` | S | 🔵 Open | [repair §4](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-REP-07` | 🔴 **`SonarrRepairManager` has NO `run()` and NO `prepare()`** — yet `repair` is in `component_dependencies` **and** `critical_keys`. ✅ **Session 53 (Q9 applied) — the caller EXISTS**: `orchestration/repair.py` has **15** `run_*_repairs` methods and a `run_all_repairs` sequencing 14. But `orchestration.run()` never invokes `repair`, so this is **two-level dormancy** — a fully-built subsystem whose chain is never pulled. Not *"nobody wrote a caller"* | S | 🔵 Open | [repair §6](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-REP-11` | 🔴 **`run_anomaly_repairs` calls `self.repair.anomaly.detect_unexpected_entries()` — a method that does not exist.** `SonarrRepairAnomalyManager` defines only `scan_for_metadata_anomalies`, `identify_orphaned_episodes`, `generate_anomaly_report`. `run_all_repairs` invokes it **untrapped at step 11 of 14**, so `run_monitoring_repairs`, `run_history_repairs`, `run_episodes_repairs` and `run_metadata_repairs` never run and the completion log never prints. **Second instance of "never-executed code accumulates errors execution would catch"** after `GLD-SQ-01` | S | 🔵 Open | [repair §6](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-REP-08` | 🔴 **`repair/anomaly.py` reads `sonarr::{inst}::series`** — **double-colon**, a format `CacheKeyPaths` never produces (it is slash-separated throughout). So `cached_series` is always `[]` and `missing_in_cache` is the **entire library**, logged as one warning line. Dormant only because `GLD-REP-07` means nothing calls it — **live the moment repair is wired up**. Third distinct cache-key defect, and the only one that invents a *format* | S | 🔵 Open | [repair §5.5.3](./managers/services/sonarr/repair/DESIGN.md) |
| `GLD-ORCH-S01` | 🟡 **`SonarrOrchestrationManager.run()` invokes 2 of 11 sub-orchestrators** — only `series` and `episodes`. The other nine (incl. **`quality`**) are constructed and left on `self`, reachable only by attribute. `quality.py`'s four `get_*_manager()` accessors imply external callers were intended. **Until answered, "constructed" keeps being mistaken for "runs"** | M | 🔵 Open | [orchestration §2](./managers/services/sonarr/orchestration/DESIGN.md) |
| `GLD-ORCH-S02` | 🎯 **Propagate the `active` / `_inactive_reason` protocol** — a **three-state** load summary: ✅ Loaded / ⏭️ **Inactive with a stated reason** (*"soft-disabled itself … not an error"*) / ⚠️ Skipped (exception). **The best `load_summary` in the repo**, and it solves `GLD-MIX-01` more thoroughly than the parent-level fix | S | 🔵 Open | [orchestration §3](./managers/services/sonarr/orchestration/DESIGN.md) |
| `GLD-ORCH-S03` | 🎯 **Cite orchestration's denominator as the fix for `GLD-SON-13`** — it counts against the **full** map (`len(orchestrator_map)` = 11) while the parent counts against the **filtered** set (8). That single difference is why the parent can never surface `quality`/`sync` | S | 🔵 Open | [orchestration §4](./managers/services/sonarr/orchestration/DESIGN.md) |
| `GLD-ORCH-S08` | 🟡 **`orchestration/series.py` raises where its own parent offers soft-disable** — both `raise ValueError`s are missing-dependency cases, exactly what `active`/`_inactive_reason` exists for. And `series` is **one of only two** sub-orchestrators `run()` invokes: if skipped, `run_full_enrichment` hits `if self.series:` → `None` and **silently enriches nothing**, reporting success. ✅ **Session 53: this is an INCONSISTENCY, not an absence** — `orchestration/repair.py` uses the protocol correctly (`self.active = False` + `_inactive_reason`) for the identical case. It is the in-repo example to point `series.py` at | S | 🔵 Open | [orchestration §6.5.1](./managers/services/sonarr/orchestration/DESIGN.md) |
| `GLD-SER-01` | 🟡 **`SonarrSeriesManager` has NO component summary** — `prepare()`/`run()` log per-component only; `if comp and hasattr(...)` **silently skips** a component that failed to load. This manager owns the **TV delete ceiling** and the **monitor threshold**, so a silently-absent `space_pressure` means TV space-pressure does not run that pass *(P-D)* | S | 🔵 Open | [series §3](./managers/services/sonarr/series/DESIGN.md) |
| `GLD-SER-05` | ✅ **COMPLETE session 42** — **all three** Sonarr `THRESHOLD_SPECS` consumers route through `get_threshold`: `series_monitor` (35) and `series_demote` (17) in `quality.py`, `tv_delete_ceiling` (17) in `space_pressure.py`. **The calibration machinery covers TV completely.** The 20→17 re-anchoring rationale is stated **three** times independently | L | ✅ Closed | [verification](./managers/services/sonarr/series/VERIFICATION_space_pressure.md) |
| `GLD-SP-01` | 🎯 **The canonical `dry_run` resolution exists** — `sonarr/series/space_pressure.py` walks **kwargs → parent → SonarrManager → Main** and **RAISES** if unresolvable: *"Refusing to initialize without an explicit value to prevent accidental live profile changes."* Names the problem as *"the dry_run-propagation footgun"*. **Stronger than `coordinator/`'s three-level form — re-scopes `GLD-WB-03` and `GLD-CAL-01` to copy THIS** | S | 🔵 Open | [verification §2](./managers/services/sonarr/series/VERIFICATION_space_pressure.md) |
| `GLD-SP-03` | 🟡 **`services → services` is an undeclared pattern** — ⚠️ **session 62: a THIRD instance.** `sonarr/series/space_pressure.py` imports a **private** method from `RadarrSpacePressureManager`; `sonarr/cache/episode_files.py` imports `tv_group_maps_from_series` from **Plex**; `services/routing/__init__.py` imports `age_cache` from **mdblist**. `ARCHITECTURE.md` rule 3 is **silent** on `services → services`; `brain_purity` guards the brain, not the services. **Three pairs — a settled pattern, not an exception. Decide whether it is permitted, and state it** | S | 🔵 Open | [routing §6](./managers/services/routing/DESIGN.md) |
| `GLD-SPLIT-01` | ✅ **CLOSED session 89 — the `parent_name` filter is DELETED.** `split_components` decided noncritical membership by CONSTRUCTING a throwaway instance of every candidate and comparing its `parent_name` for **equality**. Measured on a live diagnostic run it dropped **four components that should have loaded and zero that should not**: `[SonarrEpisodesManager]` expected `'SonarrEpisodesManager'` but `file`, `monitoring` and `sharding` all reported `'SonarrEpisodes'`; `[SonarrValidatorManager]` expected `'SonarrServices'` while `api_factory` reported `'SonarrValidatorManager'` — **exactly inverted**. Closes `GLD-SPLIT-02`, `GLD-STO-04`, `GLD-REP-02` and `GLD-REP-03` with it | S | ✅ **Closed** | `component_splitter.py` |
| `GLD-SPLIT-03` | 🎯 **WHY it was unfixable by convention: five writers, one of them the FILESYSTEM.** `parent_name` is set by ① a class attribute (four different conventions across managers) ② `kwargs["parent_name"]`, which `BaseManager` overwrites from the caller's `init_args` ③ **PATH INFERENCE** — `ManagerAttributionMixin` derives it from the module's position on disk (`services/sonarr/episodes/` → `"Sonarr"` + `"Episodes"`), **so moving a file changes it** ④ explicit assignment in `__init__` ⑤ `register()`, which prefers `self.manager.__class__.__name__` over `self.parent_name`, so the REGISTRY's value can differ from the object's. Caught in the act: `monitoring.py` literally declares `parent_name = "SonarrEpisodesMonitoringManager"` and its instance reported `"SonarrEpisodes"` — source ③ beating source ① | S | 📝 Recorded | `mixins/mixins.py` |
| `GLD-SPLIT-04` | 🔴 **The drop was SILENT — that was the actual defect.** A `parent_name` mismatch produced no log line at all: the component simply was not in the returned dict, the manager never attached it, and nothing said why. That silence is why `episodes/__init__.py` grew a hand-written load for `sharding` — and why **`file` and `monitoring` were never noticed missing at all**. The workaround is now removed (it was already inert, guarded by `if not getattr(self, "sharding", None)`) | S | ✅ Fixed | `episodes/__init__.py` |
| `GLD-SPLIT-05` | ⚠️ **Enabling four never-constructed components exposed an UNGUARDED WRITE.** `episodes/monitoring.py`'s `batch_unmonitor_downloaded_if_cutoff_met` calls `bulk_update_episodes` — a real write — **with no `dry_run` check at all**. Unnoticed because the component had never loaded. The guard went in BEFORE the filter was deleted. Textbook P-I: never-executed code accumulates defects, exactly as `GLD-SQ-01` and `GLD-REP-11` did | S | ✅ Fixed | `episodes/monitoring.py` |
| `GLD-ORCH-03` | ✅ **Three MORE `dry_run` clobber sites** — `episodes/file.py`, `episodes/monitoring.py` and `episodes/__init__.py` (the last being the **weakest form found**: bare `kwargs.get("dry_run", False)`, one level, no parent fallback — and it is passed down to every subcomponent through `init_args`, so it set the mode for the whole episodes subtree). Running total **8 sites** removed since `BaseManager` took ownership | S | ✅ Fixed | — |
| `GLD-CACHE-S01` | ✅ **INVENTORY COMPLETE session 89 — 26 callers, not 12.** A repo-wide grep replaced an incidental tally assembled while reading other things; it was **less than half** the real figure. Per shim: **`space_targets` 13** (`main.py`, `acquisition/__init__`, `coordinator/space_coordinator`, `radarr/cache/movie_files`, `radarr/quality/space_pressure`, `radarr/quality/universe`, `radarr/repair/anomaly`, `radarr/repair/storage`, `routing/uhd_reconcile`, `sonarr/cache/episode_files`, `sonarr/orchestration/series`, `sonarr/series/quality`, `sonarr/series/space_pressure`) · **`watch_likelihood` 6** · **`size_model` 5** · **`library_classifier` 2**. Plus `trakt/movies/scorer` in the services tree = **5 shims**. **Step 10 now has a definitive list instead of a floor** | S | 🔵 Open | — |
| `GLD-ML-16` | ✅ **FIXED session 91 — and the GUARD had two holes, not one.** `machine_learning/space/dual_version.py` imported `scripts.support.utilities.size_model`, the Step-5 re-export shim for `machine_learning.sizing.size_model` — the brain reaching **outward through a services-era shim to reach its own neighbour two directories away**, and a module that would have broken at MIGRATION Step 10 for no reason. Repointed at the real module. **`brain_purity` could not have caught it**: a shim is neither a service nor a brain package, so it fits *between* the two rules by construction. Now `_FORBIDDEN_SUPPORT_SHIMS` names all four | S | ✅ **Fixed** | `brain_purity.py` |
| `GLD-ML-17` | ✅ **FIXED session 91 — the guard's scope was a hand-maintained ALLOWLIST, so a brain package was unguarded until someone remembered it.** `_GUARDED_SUBPACKAGES` listed 17; the brain has **23**. Six were never checked — including **`thresholds/`, the entire calibration + axis-drift machinery**. Inverted to *guarded unless excluded* (only `__pycache__` and `tests`, each with a stated reason), so a new decision core is covered the moment it exists rather than the moment someone notices. ✅ **Verified: all 23 clean** — the six newly-covered packages were already pure, so the widening cost nothing and closed the hole permanently. Same *absent-from-a-list ≠ does-not-exist* shape as `GLD-TWH-04` / `GLD-REP-08` / `GLD-CG-01` — **found in a guard whose entire job is catching things** | S | ✅ **Fixed** | `brain_purity.py` |
| `GLD-TSC-01` | ✅ **FIXED session 89 — wildcard removed, surface now finite.** `trakt/movies/scorer.py` was `from …movie_scorer import *` plus a **partial** explicit list, so the shim re-exported 11 names it never mentioned (`INTENT_HALF_LIFE_DAYS`, `INTENT_STALE_FLOOR`, `codec_transcode_prior`, `device_resolution_ceiling`, `normalize_lang`, `person_affinity_score`, `related_graph_affinity`, `route_people`, `select_profile_id`, `user_rating_score`, `watchlist_intent_score`). A caller importing any of them worked, and would have **broken silently at Step 10 with nothing to grep for**. Expanded to all 19, wildcard dropped, `__all__` added. Behaviour-identical today; a missed name now fails at **import** time with the symbol in the traceback. ⚠️ **Not yet exercised** — the 00:15 log predates this change | S | ✅ **Fixed** | — |
| `GLD-ACQ-18` | 🔴 **PRIMARY — REMOVE THE WHOLE-HOUSEHOLD WATCH MANDATE SYSTEM-WIDE. `all_household_watched` asks the wrong question and is close to unsatisfiable.** It requires EVERY configured member to have watched an episode before it is deletable. With six members that is almost never true and never will be — Mom is not going to watch Blue Bloods, so its episodes are permanently frozen, and the same holds for every series only one person follows. The right question is not *"has everyone seen it?"* but *"is anyone still walking toward it?"*, and **`retention_hold` already answers exactly that**: `lifecycle.viewer_retention` marks any episode inside an account's `[position - backward_buffer, position + pace x horizon]` interval, PER VIEWER, with `retention_hold_by` naming who. A series two people are mid-way through stays protected; the five only one person watches become reclaimable. ✅ **DONE in the recycle path** (`_recycle_to_fund_acquisition`). ✅ **DONE at the three delete-guard sites, 2026-08-06 — operator decision: keep the var, narrow guard USAGE to "household-not-all-watched WHO ARE ACTIVELY WATCHING and will come upon the episode reasonably soon".** Implemented as raw mandate ∩ row `retention_hold` at `_apply_grace_period` (`household_blocked`), `_do_delete_marked_files` (HOUSEHOLD GUARD, warning reworded), and `classification/guards.build_protected_file_ids` — reusing retention's window as the ONE definition of "approaching" (no seventh watched-bar; dormant accounts have no forward reach, so they cannot freeze anything). `all_household_watched` remains computed every sync, meaning unchanged. Guard tests extended (held / released / row-level branch-order); the brain branch verified in isolation 5/5 (no-column, rh-False/NaN, intersection-hold, retention-alone, whole-file-via-sibling). ⚠️ **Awaiting run verification** — the next dry run doubles as `GLD-ACQ-22`'s attribution experiment: TBBT's `protected` drop collapsing to ~pilot+retention ⇒ household WAS the recycle blocker; staying ~37 ⇒ the watchlist shield is. ⚠️ `GLD-ACQ-20` (retention horizon vs binge pace) is now the guard's live safety margin — verify before the first real APPLY delete. ✅ **RUN-VERIFIED 2026-08-06 23:36 UTC** — mtime proves the run imported the edited guards (edited 19:32 EDT, accessed 19:37 EDT by the 19:36 EDT run start); run clean, Curious George still funds, no regressions. **BUT the protected set barely moved (4,812 → 4,800) and TBBT stayed 37/38** — household was NOT the live recycle blocker; its contribution was shadowed by another guard. The blocker hunt continues at `GLD-ACQ-22`/`GLD-ACQ-23` (universe credit, floor 1.0, is the prime suspect) | M | ✅ **Implemented + run-clean (shadowed)** | `episode_files.py`, `classification/guards.py` |
| `GLD-ACQ-19` | 🔴 **MY BUG — the leapfrog recycle silently never fired, because I keyed it on a column that is NEVER POPULATED.** First live run after wiring: *"Acquisition skipped … 9 episode(s) remain queued"* and **no `Self-funded acquisition` line at all** — no error, no warning, just nothing. The eligible pool came back empty for every series because `_recycle_to_fund_acquisition` required `household_last_watched_at`, and the parquet has it on **0 of 12,637 rows** (`all_household_watched` is fine at 571 true; `is_watched` 567). Fixed: anchor on `last_watched_at`, falling back from the household stamp when present. ⚠️ **THIRD INSTANCE THIS SESSION** of a guard keying on a never-written column — after `series_tvdb_id` (which silently zeroed the entire TV half of the smart shelves) and the smart-shelf `issubset` schema check. All three failed the same way: **an empty result that looks exactly like "nothing qualified"**. The lesson is not "check column names" — it is that a filter over a column should VERIFY THE COLUMN HAS DATA before treating its absence as a verdict | S | ✅ **Fixed** | `episode_files.py` |
| `GLD-ACQ-21` | 🔴→✅ **THE SCAFFOLD `GLD-ACQ-19` ADDED SWALLOWS ITS OWN OUTPUT ON A PARTIAL FUND.** `_recycle_to_fund_acquisition` builds `_why` — per-series refusal reasons carrying per-stage drop counts (`no_fid`/`protected`/`unwatched`/`no_date`/`no_size`) or the planner's verbatim reason — but emitted it only under `if not funded:`. **First live partial fund (2026-08-06 22:46) hit it immediately**: Curious George funded, so `funded` was non-empty and the refusal reasons for every other pending series — verified next run as See, Attack on Titan, The Big Bang Theory (6 pending next-ups after the deliberate S03E01–06 removal test) and The Abbott and Costello Show; NOT the wider change-plan acquire list, which the parquet pending set does not mirror — were computed and DISCARDED. The question a partial fund raises — *"why did X not fund while Y did?"* — is exactly the one the swallowed lines answered, and the operator had to read the source to learn the answer did not exist. **P-A, inside the very scaffold built after four wrong inferences about this method** — the scaffold was written for the all-refused case and trusted as though it covered refusal generally (a P-B shape on a P-A fix). Fixed: `_why` now emits unconditionally when non-empty; the *"no series reached the planner"* line stays gated to the fully-empty case. ✅ **VERIFIED 2026-08-06 23:13** — all 4 reason lines on the first partial fund, and they located the blocker immediately: every refused pool died at the `protected` drop (TBBT 37/38 owned, See 10/10, AoT 7/7, Abbott 2/2; `keep_tagged=False` everywhere) — attribution filed as `GLD-ACQ-22` | S | ✅ **Fixed + verified** | `episode_files.py` |
| `GLD-ACQ-22` | 🎯 **ATTRIBUTE THE `protected` DROP TO ITS GUARD — the new reason lines localize the recycle blocker but cannot yet name it.** First verified output (23:13): every refused series' pool died entirely at `protected` — TBBT 37 of 38 owned files, See 10/10, AoT 7/7, Abbott & Costello 2/2 — with `keep_policy on 0` and `keep_tagged=False` everywhere, and a global protected-set of **4,812 fids**. Two guards inside `_build_protected_file_ids` can EACH explain series-wide protection at that scale: **household-not-all-watched** — the exact site `GLD-ACQ-18` names as STILL TO DO, and consistent with only 571 of 12,637 rows carrying `all_household_watched=True` — and the **WATCHLIST shield**, which protects whole series for still-active members (TBBT/See/AoT are all actively watched, so plausibly all watchlisted). The `protected` counter lumps seven guards into one number, so the line proves the recycle is guard-blocked without saying which guard — the same one-step-short shape `GLD-ACQ-21` just closed, one level down. Fix: expose fid→guard from `build_protected_file_ids` (or per-guard counts) and print the breakdown in the reason line; one run then either confirms `GLD-ACQ-18`'s remaining work as the live blocker or rules it out. NOTE the intentional property being preserved either way: the recycle docstring reuses the delete pass's guard set precisely so *"a recycle can never remove something the delete path itself would refuse to touch"* — attribution must not weaken that, only name it. 🔴 **2026-08-06 23:36 — THE EXPERIMENT REFUTED MY SHORTLIST, BOTH HALVES.** The `GLD-ACQ-18` narrowing ran (mtime-proven) and changed nothing: TBBT still 37/38, set 4,812 → 4,800 — **household eliminated**. Watchlist is arithmetically sidelined: the sonarr shield holds only **225 episode rows across 91 series** (avg ~2.5/series — it cannot cover TBBT's 37 alone, let alone the set). The guard I never shortlisted fits everything: **hot-universe credit** — `UNIVERSE_PROTECT_MIN` is **1.0**, TBBT's saga credit is **+6.0** (Georgiemandysfirst, 53d avail), and `_apply_universe_credit` broadcasts it onto EVERY episode row, so the universe branch protects the whole series; with 128 engaged franchises feeding credit, it plausibly dominates the watched-file share of the set beside pilots. Recording the miss per register culture: two rounds of candidate inference, both wrong — the per-guard breakdown stops the guessing and is now required, as confirmation. 🟡 **IMPLEMENTED 2026-08-06 (§0.1 edit 12)** — and a THIRD hypothesis wobbled during the evidence pass before the line ever fired: the saga grid's **+6.0 I cited as universe credit is `saga_credit`**, which the guard deliberately does NOT read ("the delete guards read universe_credit alone"), and the lent-credit line shows **all 125 credited series arrived via saga caught-up/depth** — whether guard-read `universe_credit` is nonzero anywhere is unknown (stale-parquet values from a prior run remain possible at recycle time). Three refuted/uncertain rounds; the `protected-by:` line on the next run ends it. ✅ **ANSWERED 2026-08-07 00:51 — first attribution run. THE BLOCKER IS A MOSAIC, not one guard:** TBBT `{universe: 36, retention: 4, pilot: 1}` — the UNIVERSE credit guard confirmed as TBBT's holder (36/38); See `{retention: 9, watchlist: 9, household: 4, pilot: 1}` — watchlist + an active walker; AoT `{retention: 6, household: 5, pilot: 1}` — pure retention; Abbott `{pilot: 1, retention: 1}` — only 2 files exist, both legitimately held. Every single-guard hypothesis was partially right and wrong as a universal — the breakdown was the only path to this answer. Bonus proof in every line: household ⊆ retention (4≤9, 5≤6) — the `GLD-ACQ-18` intersection working as designed in the wild | S | ✅ **Answered** | `classification/guards.py`, `episode_files.py` |
| `GLD-ACQ-24` | 🔴 **UNWATCHED TV IS UNREACHABLE BY EVERY DELETE PATH — the coordinator's pool is thinner than it looks.** Found chasing the operator's DBZ question ("100 unwatched episodes, slow walker — can we clear them?"): `build_delete_candidates` feeds the coordinator ONLY `marked_for_deletion` rows; marking is watched+grace-driven (grace anchors on last-watch, so a never-watched episode never marks); the stale-owned prune is **movies-only** ("stale low-watchability owned movies", both instances); TV downgrades shrink but never remove. Net: a cold pile of unwatched episodes can never be reclaimed by any pass — the exact bulk an operator would expect space pressure to take first. Fix direction: extend the stale-owned two-stage prune (unmonitor→delete) to TV, or a low-watchability unwatched-episode marker feeding the SAME marked→coordinator flow — the coordinator stays the sole delete decider (no second delete path; the `GLD-ACQ-23` cross-series discussion already rejected that shape) | M | 🟡 **Implemented (§0.1 edit 13), default OFF, awaiting first enabled run** | `episode_files.py` |
| `GLD-ACQ-23` | 🎯 **DECISION NEEDED — the recycle inherits ENGAGEMENT guards that structurally exclude exactly the series leapfrogging exists for.** `_recycle_to_fund_acquisition` deliberately reuses the delete pass's whole-file guard set (recycle ⊆ deletable — a real invariant worth keeping by default). But two guards in that set fire BECAUSE the household is engaged: **hot-universe credit** (floor 1.0 — nearly any engaged franchise qualifies) and the **watchlist shield**. Engagement is precisely when next-ups need funding (`recency_gate` walks hottest first), so as long as these sit in the recycle's set, the leapfrog can only ever fund series the household is NOT engaged with — inverting `GLD-ACQ-13`'s purpose. Curious George funds because it carries neither signal. The principled question per guard: does recycling an ALREADY-WATCHED episode of a hot/watchlisted series constitute the loss the guard exists to prevent? Arguably no — the title is not lost, the series stays present and monitored, the watcher consumed the episode, and retention still holds every approaching window (`GLD-ACQ-18`'s semantics). If so, the recycle's set becomes delete's set MINUS {universe, watchlist} — a deliberate, documented break of recycle ⊆ deletable, gated on `GLD-ACQ-20`'s horizon check. If not, accept that watchlist/universe series never self-fund and the leapfrog serves only the cold tail. Operator call either way — do not implement without it. 🎯 **2026-08-07: the attribution run made the consequences SURGICAL.** Subtracting {universe, watchlist} for watched rows unblocks EXACTLY TBBT (~32 watched S1/S2 fids become eligible; retention keeps its 4 + pilot); See stays mostly held by retention 9/10 (correct — an active walker), AoT stays fully held by retention (correct), Abbott has nothing to give. The carve-out is a scalpel, not a floodgate — retention alone keeps every legitimately-active series protected | M | 🎯 Decision | `episode_files.py`, `classification/guards.py` |
| `GLD-ACQ-20` | 🎯 **The replacement guard must cover "about to be watched", not just "currently mid-series".** With the household mandate gone, the risk `GLD-ACQ-18` must not reintroduce is recycling an episode ANOTHER viewer is about to reach — including within the same run window. `retention_hold`'s forward term (`position + pace x horizon`) is exactly this and is already computed, so the guard exists; what needs verifying is that its HORIZON is long enough for a household that binges (a viewer four episodes back with a 14-episode horizon is covered; one who watches a season in a weekend may outrun it). Check `episode_retention.horizon` against real per-viewer pace before `GLD-ACQ-18` lands on the delete path | S | 🔵 Open | `lifecycle/viewer_retention.py` |
| `GLD-ACQ-13` | ✅ **BUILT AND WIRED (session 95) — `machine_learning/acquisition/recycle_planner.py` (19 tests) + `episode_files._recycle_to_fund_acquisition`.** Live evidence for the problem: *"Acquisition skipped … **9 episode(s) remain queued**"* — and `recency_gate` *"walks the most-recently-watched series first"*, so those 9 ARE the next episodes of what the household is watching. The consequence is in `playlists.log`: **every Up Next entry is a PILOT** while Fallout S1E7, Blue Bloods S3E3 and Big Bang Theory S2E24 are absent, never acquired. **TWO PASSES, and the order is the design:** pass 1 prices the advance against the WHOLE eligible pool (a bigger pool buys a better tier — 8 recycled episodes reach 2160p where 4 reach only 1080p); pass 2 deletes ONLY what that advance costs, leaving the surplus on disk. Tier estimates come from the **library-calibrated size model** (~6,900 measured files) keyed on the series' own RUNTIME, modal CODEC (`Bluray-1080p@h265` — the anime-vs-live-action gap) and SOURCE TYPE, not a per-series median. ⚠️ **Not yet observed working** — see `GLD-ACQ-19` | L | 🟡 **Wired, unverified** | `acquisition/recycle_planner.py` |
| `GLD-ACQ-16` | 🎯 **The 40% tolerance and resolution TIERING are one mechanism — neither is safe alone.** `size_tolerance=0.4` lets an acquisition cost up to 1.4x what it recycles, because strict parity stalls a rotation over ordinary episode-to-episode variance, which is exactly what leaves a library stuck on pilots. But a tolerance WITHOUT tiering would let the rotation silently UPGRADE quality while every plan reported itself net-neutral, growing the disk one episode at a time. So the tier choice is part of the funding decision: the loop offers the best tier the recycled space affords and steps DOWN when it does not fit — recycling a 1.5 GB 720p episode funds a 720p replacement, never a 9 GB 2160p one. ⚠️ **The chosen tier is returned per acquisition and the caller MUST request at it**, or the size arithmetic is void; that contract is the one thing a future integrator could quietly break, so it is asserted in a test | S | ✅ Built | `acquisition/recycle_planner.py` |
| `GLD-ACQ-17` | 📝 **A test in this file was passing for the wrong reason — same shape as the other instrument errors.** `test_net_space_is_never_positive` asserted `freed >= cost`, which the 40% band **explicitly permits violating**; it passed only by coincidence of the original single-pass loop's arithmetic. Under the two-pass rewrite it failed immediately and correctly. Replaced with the real invariant (`cost <= freed x (1+tol)`). Worth recording because it is the recurring failure of this sweep in miniature: **a check that appeared to enforce something it did not, passing for reasons unrelated to its name** — cf. the `issubset` schema guard, the `parent_name` filter, and `brain_purity`'s allowlist | S | 📝 Recorded | — |
| `GLD-RAD-30` | 🔴→✅ **WRONG-MOVIE GRABS — the step-down picker never asked WHICH movie a release names.** First real apply (2026-08-07): 21 realized universe downgrades included 'A.Business.Proposal.2025' grabbed for Demon Slayer: Infinity Castle, 'Snapdragon.1993' for DBZ: Broly, 'Roujin.Z.1991' for Cooler's Revenge, 'the.maestro…' for Bio-Broly, 'Spiderman.2002' (film 1) for Spider-Man 2, 'Scorpion.King.4' for The Scorpion King, 'Superman.2025' for Superman, plus FOUR French-audio grabs (Clerks II, Hulk TRUEFRENCH, Man of Steel, DBZ Film 09) — `_pick_stepdown_release` filtered on resolution/size/seeders/sample-rejections only and never validated release identity or audio language. Fixed (§0.1 edit 16): filename-parse identity gate + audio-language gate + fatal mapping-rejection scan, conservative by design (no title evidence ⇒ file KEPT — a kept file beats a wrong grab on the destructive path). Operator's directive verbatim: "We'll need to do a parse of the filenames, ensuring we grab the correct movie". ⚠️ Follow-ups: Sonarr's episode step-down picker (separate implementation) still lacks the identity gate; the incident's 21 grabs need manual cleanup (blocklist + re-search) after deletes work again | M | ✅ **Fixed radarr-side; sonarr follow-up open** | `space_pressure.py`, `universe.py` |
| `GLD-ORCH-03` | 🔴→✅ **DELETE success was unverifiable BY CONTRACT — `_make_request` returned `None` for both a successful DELETE and a swallowed failure (default fallback `None`).** Every destructive path inherited the blindness: the 2026-08-07 apply logged 76 delete 500s while the coordinator counted 30 of them as `deleted` + `bytes_freed` (marks cleared, `deleted_tmdbs` fed downstream anomaly accounting), the recycle funded an acquisition on 6/6 failed Curious George deletes, and both step-down passes grabbed replacements for files still on disk (Scorpion King's 500 sat BETWEEN two 'Realized downgrade … file deleted' lines). Root cause of the 500s themselves is *arr-side ('Unable to delete movie file', `MediaFileDeletionService` — recycle-bin path/permissions territory, operator fixing in both apps) — but the code's inability to NOTICE was ours, and every try/except around a delete was dead protection. Fixed (§0.1 edit 15): DELETE success → `True` in the base; four sites check and fail loud + non-destructive. No existing caller could break — under the old contract no result check could ever have functioned. Repo audit note: any caller passing a custom DELETE `fallback=` must treat truthy as success (grep `method="DELETE"` for `fallback=`). ⚠️ **Fifth site found post-fix:** the 2026-08-07 log's failure spans include `UhdReconcileManager` (lines 680–715, relocate-4K/dual-version deletes) — not yet result-checked; harden next session | M | ✅ **Fixed (5th site pending)** | `base_instance_manager.py` + 4 sites |
| `GLD-RAD-31` | 🔴 **`movie_file_id` WAS EMPTY ON 19 OF 21 UNIVERSE REALIZE ROWS — the delete block was silently SKIPPED and 'file deleted' printed with no delete even attempted.** Found reconciling the 2026-08-07 failure map: every delete actually attempted failed (sonarr recycle 6/6, universe standard+ultra 2/2 attempts, exhaustive step-down span, coordinator 30/30, UHD reconcile span) — the universe pass's 19 'clean' realizes were rows where `_fid_row` was None/NaN, so the `if pd.notna(_fid_row)` gate bypassed the delete entirely and the grab was left to die on Radarr's cutoff-met rejection against the still-present file (a P-C shape: absent conflated with handled). Hardened same session (§0.1 edit 17): fid missing ⇒ grab SKIPPED + loud warning + backoff, both step-downs. 🎯 OPEN: WHY is the column empty on most realize rows — stale frame at realize time, or a sync path that never populates it? Diagnose against the cache builder before trusting realize again | M | 🔴 **Hardened; root cause open** | `universe.py`, `space_pressure.py`, radarr cache sync |
| `GLD-RAD-32` | 🔴 **ULTRA'S 2160-ONLY RULE IS NOT ENFORCED BY THE DOWNGRADE MACHINERY — step-downs treat ultra like any instance with the global 720 floor.** Operator's instance rules (2026-08-07): standard ≤ 1080 (no 4K — EXCEPTION RESOLVED: "unless a singular instance" is a DEPLOYMENT-SHAPE exception — with only one Radarr, that instance is standard-by-default and hosts all tiers incl. 4K; the ceiling applies only when a distinct 4K instance is categorized. Already embodied: UhdReconcile bails on "no distinct 4K instance", and both step-down reroutes key on `radarr_instances_categorized['4K']` existing and differing); ultra 2160-only. Standard's side IS enforced (categorized routing 4K→ultra; acquisition clamps to the ≤1080 durable floor; UhdReconcile relocates 4K found on standard — 38 titles last run). Ultra's side is violated live: the ultra universe pass realized Batman Begins (Ultra-HD → HD Bluray 1080p DoVi) and The Dark Knight (1080p AV1) ON ultra, and a Bluray-720p Encanto was already resident on ultra (the ENOSPC delete). No per-instance resolution floor exists in `downgrade_planner`/`_pick_stepdown_release`/either caller. Proposed direction (operator to confirm): a sub-2160 need on ultra is a DEMOTE/REHOME event, not a grab — route through the existing `demote_4k_on_watchability`/`evict_uhd_first`/`triage_rehome_to_standard` machinery (standard copy at `rehome_floor_profile` HD-720p+), delete the ultra copy; belt: per-instance floor map in the picker (ultra=2160, standard=720) + standard ceiling 1080. TV note: glidearr manages ONE sonarr (8990); TV-1080/2160 unmanaged; `jit_per_episode_tiers` climbs episodes to 1080/2160 on the managed instance — whether that violates "TV-720 = 720-only" or the rule describes app profiles awaits operator ruling (empty `sonarr_instances_categorized` may be the seam for a future cross-instance tier router). ✅ **MOVIES SIDE IMPLEMENTED 2026-08-07 (§0.1 edit 18) on the operator's ruling ("we rehome non-ultra downgrades to standard from the ultra instance")**: ① universe realize defers sub-2160 downgrade actions on the 4K instance BEFORE the profile PUT (`uhd_deferred` stat + grid row; even flipping ultra's profile to ≤1080 is the violation); ② the space step-down defers ENTIRELY on the 4K instance (its whole purpose is shrink-below-current = sub-2160 there); ③ `_demote_overqualified_4k` gained a SUB-2160 RESIDENT branch — a ≤1080 file on ultra rehomes regardless of score via the same survivor/make-before-break/wait machinery (keep-pins + same-physical-file guard honoured; evicted records become ledgered shells so RECOVER re-acquires a proper 2160 if the score later crosses the threshold) — this cleans the Encanto resident and any imported Batman/Dark Knight junk automatically; ④ the existing demote delete is now result-checked (re-monitor + retry on failure — partial ORCH-03 fifth-site closure; the move/relocate legs remain). Sim-verified on 6 resident shapes: survivor-evict, only-here baseline grab, good-2160 untouched, classic demote intact, pin respected, failed-delete re-monitor, same-path guard. Sim also caught an UnboundLocalError (`grabbed` bound later than the new branch's reference) — fixed with an early binding. TV ruling still open | L | 🟡 **Movies implemented; TV ruling open** | `uhd_reconcile.py`, `universe.py`, `space_pressure.py` |
| `GLD-ACQ-14` | 🎯 **`GLD-ACQ-13` NEEDS ITS OWN CONSENT FLAG — do not gate it on `deletions_consent`.** They are different acts. General deletion is *"the disk is full, give something up"*; a leapfrog recycle is *"I finished this episode, spend it on the next one"* — no title is lost, nothing the household still wants disappears, and the library does not shrink. An operator could reasonably want rolling-window recycling while keeping general deletion firmly off, and conflating them makes that impossible. Precedent exists in this codebase: `relocation_consent` is separate from `deletions_consent` for exactly this reason. Proposed `acquisition.next_episode.recycle_watched.{enabled, consent}` | S | 🔵 Open | — |
| `GLD-ACQ-15` | 🎯 **The five guards `GLD-ACQ-13` must carry, each for a specific failure.** ① **SAME SERIES ONLY** — funding Fallout by deleting Blue Bloods is a reallocation decision, which is `space_pressure`'s job, not a rotation. ② **WATCHED ONLY**, via the existing watched-set (never delete something unwatched to fund something unwatched). ③ **SIZE-MATCHED** — `estimate_gb(freed) >= estimate_gb(acquire)` or the net is positive and the whole premise fails; `size_model.estimate_gb` already does this. ④ **DELETE FIRST, THEN ACQUIRE** — the inverse of the 4K path's make-before-break, and correct here for the opposite reason: the episode is already CONSUMED, so losing it costs nothing, whereas acquiring first would breach the floor the gate exists to protect. ⑤ **HONOUR `keep` TAGS** — a series the operator pinned must never be recycled, even watched. ⚠️ Also decide how a leapfrog deletion interacts with the cross-pass reclaim ledger: the space it frees is IMMEDIATELY SPENT, so it must not be published as reclaim available to a later pass or the chain will double-count it | M | 🔵 Open | `machine_learning/space/reclaim_ledger.py` |
| `GLD-PLX-20` | 🔴 **SEPARATE BUG — the per-user playlist POOL is identical for every adult; only the RANKING is personalised.** `playlists.log` shows **five of six users with byte-identical counts**: Trizzd / Aiden / Mom / Raina / mirandan75 each get *"100 episode(s), **107 unmatched**"* and *"73 movie(s), 4 unmatched"*. Only Wyatt differs (`8 unmatched`, `2` fresh movies) — and he is the only `little_kid`, so the CERT gate is the one filter that reaches the pool. Identical counts across five people with completely different histories cannot happen if per-user watch history shaped candidacy. Per-user scores DO differ (0.54 for Trizzd vs 0.16 for Aiden on the same title), so the ranking layer works — the candidate POOL does not. ⚠️ **`107 of 207 candidates unmatched (52%)`** is a second signal in the same line and may share a root cause. NOT fixed by `GLD-ACQ-13`: acquiring the right episodes would improve what the pool CONTAINS, but every profile would still see the same pool | M | 🔴 **Open** | `plex/playlists/builder.py` |
| `GLD-ACQ-10` | 🔴 **PRIMARY — YOUR OWN MONITORED-BUT-UNOWNED *arr RECORDS ARE NOT AN ACQUISITION SOURCE.** The grab PATH exists and is well-guarded: `acquisition/__init__.py:343` `_find_movie_record` scans EVERY instance for a tmdbId, `_grab_existing` handles the *record present, `hasFile` false, monitored* case, and `test_universe_grab.py` covers the branching (dedup / hasFile / exact-tmdb guard / dry-run). But that is a **LOOKUP**, not an enumeration — it answers *"is this tmdb already in a library?"* for a candidate that arrived from somewhere else. `acquisition.sources` is `trakt_recommendations` · `trakt_watchlist` · `mal` · `people_cooccurrence`, plus the universe backfill; **nothing walks the *arr libraries looking for monitored records that never found a release**. So a film added to Radarr eight months ago and never grabbed is invisible to BOTH systems: too unowned for `watchability_score` (the parquets skip `hasFile=false` rows by design) and not a candidate for acquisition. ⚠️ These are the STRONGEST candidates in the system — titles the operator already decided they wanted — and the cheapest to gather: no external API, the data is in the library payload every run already fetches. Add `arr_monitored_unowned` as a first-class source | M | 🔴 **Open** | `acquisition/candidates.py` |
| `GLD-ACQ-11` | 🎯 **Do NOT solve `GLD-ACQ-10` by extending `watchability_score` to unowned items — it would poison the delete path.** The obvious fix ("score unowned titles too, then acquire the high ones") breaks a deliberate separation: `movie_files`/`episode_files` skip `hasFile=false` rows because **every** watchability consumer is a RETENTION consumer — `space_pressure`, `tv_delete_ceiling` / `movie_delete_ceiling`, `_demote_overqualified_4k`, the grace window. Unowned rows would land in delete pools, downgrade plans and space projections **for files that do not exist**, and `uhd_reconcile` would try to route them. That is the `score=None` treated as 4K-eligible bug at library scale. Two scorers exist on purpose: watchability answers *"how much do we want to KEEP this"*, `AcquisitionScorer` answers *"how much do we want to GET this"* — and only the latter should ever see an unowned title | S | 📝 Recorded | — |
| `GLD-ACQ-12` | 🎯 **The anniversary shelf work makes `GLD-ACQ-10` nearly free.** `smart_shelves._episode_members` already reads `sonarr/{inst}/episodes/by_series/*.json`, and every row carries `hasFile` AND `monitored` AND `airDateUtc` together. The membership pass therefore already touches exactly the data an `arr_monitored_unowned` source needs — it currently discards the unowned rows (`if not r.get("hasFile"): continue`). Partitioning instead of discarding gives *owned → label the shelf* and *unowned + monitored → acquisition candidate* from ONE walk. **"Aired this week in history, monitored, never grabbed"** is a genuinely strong acquisition signal and it costs one changed branch | S | 🔵 Open | `plex/playlists/smart_shelves.py` |
| `GLD-TRK-10` | 🔴 **Decide whether Trakt is CRITICAL.** `TraktManager.__init__` does `raise RuntimeError` when `register_and_validate()` fails, so a bad token **aborts manager construction** — while Plex *"self-disables when unconfigured/unreachable/scope-fails"* and MAL *"self-disables if not authorized"*. `main.py` builds Trakt **before** both *arr and `_validate_managers` lists `trakt_initialized` as critical, so it may be deliberate — but an expired token then stops movie and TV work that needs nothing from Trakt it cannot degrade without (`run_relational_pull` explicitly falls back to *"studios-only … when no Trakt enrichment is available"*). Either justify the asymmetry or make it self-disable | S | 🔵 Open | [trakt §2](./managers/services/trakt/DESIGN.md) |
| `GLD-TRK-11` | ✅ **FIXED session 93 — `run()` paginated ~1,900 rows and threw them away, every run.** `get_full_watch_history()` writes NOTHING to cache and `run()` discarded its return, so a full rate-limited sweep produced nothing — then each of its three consumers (`get_history_grouped_by_series`, `get_series_watch_counts`, `history_dataframe("episodes")`) re-paginated the same history independently. **Four sweeps for one dataset.** The tell was the codebase's own docstring: *"the movie side is served by the 24h `get_full_movie_history_cached`, the episode side re-paginates live on every call. **Pass `rows` if you are calling this in a loop.**"* — a workaround instruction for a missing cache, with the working twin ten lines away. Added `get_full_watch_history_cached()` (key `trakt/history/episodes`, 24 h, `regenerate_on_expiry`), routed all four callers through it. **10th P-J instance**, and the second whose fix was simply *make the twin symmetric* | S | ✅ **Fixed** | [trakt §3](./managers/services/trakt/DESIGN.md) |
| `GLD-TRK-12` | ✅ **ANSWERED session 94 — `trakt/sync` was USELESS, and is now unconstructed.** Every method was a LESS COMPLETE duplicate of something that already works: `get_collection()` issues the same `sync/collection/shows` request `writeback/trakt_collection` makes itself (and that one also does `/movies`, which `trakt/sync` never had); `get_watched()` overlaps `trakt/history`; `get_watched_episodes()` **is** `trakt/progress`. "sync" was Trakt's `/sync/` API NAMESPACE, not write-synchronisation — the module is entirely GETs. ⚠️ **My prediction was wrong**: I opened it first of the five expecting writes, purely from the name — the same reasoning error as `GLD-CAL-A1`. Removed from `sub_classes`; the file is dead and can be deleted. Four unreached subsystems remain (`lookup`, `analytics`, `universe`, `lists`) plus `shows/`, which is not instantiated anywhere | S | ✅ **Closed** | [trakt §1.1](./managers/services/trakt/DESIGN.md) |
| `GLD-TRK-16` | 🟡 **P-I held even so — a FAIL-OPEN guard in the dead module.** `sync.last_watched_within_threshold` returns `False` on every failure path (no history, unreadable `watched_at`, unparseable timestamp, exception). For a retention guard `False` = *not watched recently* = **not protected**, so a transient Trakt failure would make a recently-watched series look abandoned. Never fired because nothing calls it. **Documented rather than changed** — with no consumer, a contract change would be speculative; the note tells whoever wires it to decide the failure contract (return `None` for unknown, or raise) rather than leave a three-way answer collapsed into a bool that defaults to the destructive side | S | 📝 Recorded | — |
| `GLD-TRK-17` | ✅ **Write-back is BUILT and now reachable from onboarding.** `services/writeback` already did all of it — *"push local state outward (Trakt collection/history, MAL list)… runs in main.py's final phase"* — config-gated and `dry_run`-honouring, but with no prompt anywhere, so it sat at `enabled: false` invisibly. Added the Trakt step's write-back questions, which are the only place the distinction is explained: **COLLECTION = "I own this"**, **HISTORY = "I watched this"**, two independent Trakt lists. Plus `collection_watched_only` (new) so an operator can mark only WATCHED titles as owned — implemented in `TraktCollectionSync` against the SAME cached Trakt history that feeds the delete guard's `watched_tmdb_ids`, so "watched" means one thing system-wide. When that history is unreadable the scoped push **skips rather than widening to the whole library** — a bulk collection add has no undo | M | ✅ **Done** | `steps/trakt.py`, `writeback/trakt_collection.py` |
| `GLD-ONB-01` | 🔴 **FIXED session 94 — an onboarding step gated a QUESTION on another service's config, and it never fired.** The MAL write-back prompt was first written into `TraktStep`, gated on `cfg["mal"]["client_id"]`. `AccountsStep.members` is `[TraktStep, MalStep, …]` — **Trakt runs first**, so on a FIRST-TIME onboarding MAL has no client_id yet and the question silently never appeared; `--service mal` could not reach it either. **Invisible on every re-run** (the key is populated by then) and broken only on the run that matters most. Moved to `steps/mal.py`, where the credentials were just collected. Same *absent-because-not-yet-written read as absent-because-not-wanted* shape as `GLD-TWH-04` / `GLD-REP-08` / `GLD-CG-01` | S | ✅ **Fixed** | `steps/mal.py` |
| `GLD-ONB-02` | ✅ **Onboarding cross-step audit — one real defect, one disclosed overlap, two correct.** Grepped every step for reads of another step's `cfg`. **Correct:** `ctx['root_folders']` (arr→library/routing) and Trakt→daemon are documented and honoured; `routing.py`'s read of `radarr_instances_categorized` is undocumented but sound (phase 1 writes, phase 2 reads). **Disclosed:** `free_space_limit` is written by BOTH `library.py` (phase 2) and `deletions.py` (phase 6) — the operator is asked twice and Deletions wins. Right precedence, but silent; the Deletions prompt now says *"You already set a free-space floor of N GB earlier — a different value here REPLACES it"*. **Defect:** `GLD-ONB-01`. Nothing else gates a question's existence on another service's state — that was the dangerous class | S | ✅ **Done** | `steps/deletions.py` |
| `GLD-TRK-18` | ✅ **DELETED session 94 — `trakt/universe` held FABRICATED DATA in a domain that gates deletion.** `get_universe_mapping` returned hardcoded tvdb ids in which **`marvel-cinematic-universe` and `arrowverse` are the SAME THREE IDS** ([295759, 295760, 326490]) — two franchises cannot share a show — and the block runs in perfect sequential PAIRS across unrelated franchises (295759/60 MCU, 295761/62 X-Men, 295763/64 DCEU, 295765/66 Walking Dead…), which real tvdb data does not do. Only the last few entries look plausible. **Universe membership decides RETENTION** — `keep-universe` is never deleted, bare `universe` is deletable as a last resort — so a fabricated map would have mis-protected titles that should age out and mis-exposed titles that should be pinned, silently, because nothing validates a membership list against reality. `add_custom_universe` was a no-op that logged and discarded its argument. Superseded by `plex/playlists/universe_order.py` (curated + live mdblist + Kometa learning + the chronolists bake) and `radarr/quality/universe_membership.py` | S | ✅ **Deleted** | — |
| `GLD-TRK-19` | ✅ **DELETED session 94 — `trakt/analytics.analyze_actors` CANNOT EVER RETURN ANYTHING.** It requests `search/tvdb/{id}/people`, which is not a Trakt route: `search/tvdb/{id}` returns a SEARCH RESULT LIST and cannot take a `/people` suffix (the real endpoint is `shows/{id}/people`). Every call 404s → `_make_request` returns `fallback` (None) → swallowed by `if not people: continue` → returns `{}` for any history whatsoever. ⚠️ Both methods are also **O(N) API calls per run** — one request per watched series, a few hundred sequential, against a 1000-per-5-minute limit — to build a histogram that `tautulli/users.compute_genre_affinity` and `machine_learning/people_matrix` already produce **from data in hand with no extra calls** | S | ✅ **Deleted** | — |
| `GLD-TRK-20` | 🟡 **`trakt/lists` — unconstructed, but the FILE IS KEPT: it holds unique capability.** `_generate_unified_summary` declares five fields and writes three — `title` (`""`) and `in_library` (`False`) are set in the defaultdict factory and **never assigned by anything**, so both are constants rather than data. It also returns `lists` as a `set()` (not JSON-serialisable), and being a defaultdict it silently includes **every watched series** rather than only list members. ✅ **But** `get_user_lists` / `get_list_items` read the operator's OWN Trakt lists and **nothing else in the repo does** — the enrich daemon's `lists` scope is per-TITLE, acquisition reads watchlist + recommendations only, playlist universes come from mdblist. *"Acquire from my Trakt list X"* is a real gap this could fill | S | 🔵 Open | — |
| `GLD-TRK-21` | 🟡 **`trakt/lookup` — unreached but CORRECT, so kept constructed.** 25 honest thin GET wrappers over real Trakt endpoints: no fabricated data, no fail-open guards, no dead routes. **Deleting wrong code and deleting merely unused code are different decisions** — the other four were deleted for being wrong, not for being unused. ⚠️ One hazard: **`TraktLookupManager.get_user_ratings(username)` and `TraktRatingsManager.get_user_ratings()` share a name** with different signatures on different managers — exactly the checklist-Q10 trap. The LIVE one is on `ratings`; consider renaming the dead one | S | 🔵 Open | — |
| `GLD-TRK-22` | 🔴 **I WAS WRONG ABOUT `trakt/shows` — it is LOAD-BEARING, and the error is instructive.** I recorded it as *"not instantiated anywhere"* after grepping `Trakt(Analytics\|Lists\|Lookup\|Shows\|Sync\|Universe)Manager`. `TraktShowsManager` appears **only as a `parent_name` STRING** in `shows/cache.py`, never as a class — the real exports are `TraktShowCacheManager` and a `score_show` FUNCTION, so the pattern could not match either. In fact `sonarr/cache/episode_files.py` imports both: **`score_show()` drives the entire Sonarr watchability axis** and `TraktShowCacheManager` reads the enrich daemon's per-tvdbId show buckets. `shows/__init__.py` states its contents in four lines. **Checklist Q10 applied to a PACKAGE**: I searched for a plausible class name instead of reading what the package exports | S | ✅ Corrected | [trakt §1.1](./managers/services/trakt/DESIGN.md) |
| `GLD-ORCH-04` | ✅ **`dry_run` clobbers 11–15 — the whole Trakt subtree.** `trakt/sync`, `trakt/universe`, `trakt/analytics`, `trakt/lists`, `trakt/lookup` each carried the same post-`super()` local resolution defaulting to False. Inert individually (all read-only), but with `GLD-TRK-14` (TraktManager) and the `TraktAPIManager` instance that is **every Trakt manager in the package**. Running total **15 sites** removed since `BaseManager` took ownership | S | ✅ Fixed | — |
| `GLD-TRK-13` | 🎯 **Cite the progress-threading as the reference for result-forwarding** — `run()` fetches `get_combined_progress_watched()` once and passes it in: `auto_rate_watched_shows(progress_map=progress)`, *"avoids a second round of ~150 per-show API calls"*. Rare explicit forwarding; most passes re-fetch. It is also why `run_movie_ratings` builds its own completion map rather than asking the ratings manager to | S | 🔵 Open | [trakt §1.2](./managers/services/trakt/DESIGN.md) |
| `GLD-TRK-14` | ✅ **A NINTH `dry_run` clobber — at the Trakt SERVICE ROOT.** `TraktManager.__init__` ran `kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)` after `super().__init__()`, and `self.dry_run` feeds `base_kwargs` — so it set the mode for **every Trakt sub-manager**, including `ratings`, which POSTs to a third-party account with no undo. Highest-leverage of the nine | S | ✅ **Fixed** | — |
| `GLD-TRK-15` | ✅ **A tenth shipped bug recorded at its fix site, and a textbook P-I** — *"The MOVIE watchlist was never fetched here even though the manager has always exposed it, so `trakt/{user}/watchlist/movies` had never been written — the acquisition path calls it lazily, and acquisition is off by default. Group-A5 reads BOTH halves (Trakt is the only feed carrying a real `listed_at`…), and a TV-only watchlist would have meant movie intent silently had no dated source at all."* Built, exposed, never called. The note explains **what broke downstream**, not just what was missing — which is what makes it worth citing | S | 🔵 Open | [trakt §4](./managers/services/trakt/DESIGN.md) |
| `GLD-TSC-02` | ✅ **This shim names its callers — cite as the reference.** *"…so every existing `from …trakt.movies.scorer import score_movie` (**radarr space_pressure / repair, trakt ratings**) keeps working unchanged. **There is exactly one implementation now.** Deleted at MIGRATION.md Step 10."* Scope, callers, the invariant it preserves, and its own expiry — the `support/utilities/` shims state none of this, which is why all 12 callers had to be found by accident | S | 🔵 Open | — |
| `GLD-SP-04` | **Record the phantom-reclaim bug** — *"\*arr never downgrades an existing file … the old live path reclaimed **NOTHING (TV step-down space was phantom)**."* This is the **origin** of `coordinator/`'s project-vs-realize split, and neither doc says so | S | 🔵 Open | [verification §4](./managers/services/sonarr/series/VERIFICATION_space_pressure.md) |
| `GLD-SERQ-01` | 🟡 **`SonarrSeriesQualityManager.run()` and `prepare()` are no-op log lines** — the real entrypoints (`run_active_watcher_upgrades`, `run_monitor_by_watchability`) are methods someone else must call. So the run tree's `✅ Ran: quality` records **nothing having happened** | S | 🔵 Open | [verification §3](./managers/services/sonarr/series/VERIFICATION_quality.md) |
| `GLD-SERQ-03` | 🎯 **Cite the keep-tag guard as the reference fail-safe** — `fallback=None` separates a **failed fetch** from an **empty catalogue**; on failure it suppresses only the **destructive** leg (unmonitor) while re-monitoring climbers continues; and the deferral is **counted** as `guard_deferred` with a description. Fifth confirmed-absent-vs-transient instance, and the first applied to a *guard* | S | 🔵 Open | [verification §2](./managers/services/sonarr/series/VERIFICATION_quality.md) |
| `GLD-SON-11` | 🔴 ✅ **CONFIRMED session 34** — `critical_keys` holds **all 8 loadable components plus `"quality"`**, which is filtered out and can never load. The entry matches nothing; `noncritical_components` is **empty** (confirmed by `run()`'s hardcoded `noncritical_components=[]`). ⚠️ **Session 50: a second instance found** — `repair/critical_keys` declares `"cache"` for a component named `"repair_cache"` (`GLD-REP-01`). Not a one-off | S | 🔵 Open | [sonarr §12.2](./managers/services/sonarr/DESIGN.md) |
| `GLD-RAD-01` | ✅ **FULLY RESOLVED session 63 — built, wired, gated off, and self-declared EXPERIMENTAL.** `uhd_reconcile.py`'s header: *"**EXPERIMENTAL** — the move uses Radarr's async import/rescan commands; the exact timing/convergence **wants validation against a live shared-storage pair before the consent gate is flipped on**."* Behind a **seven-condition gate** (`routing.configured` · `4k_policy=='both'` · distinct 4K instance · `reorg_mode` — **default `log_only`** · `cross_instance_move_consent` · `cross_instance_dedup_consent` · shared-storage pre-flight) plus `effective_dry_run` on every destructive step. **Not an incomplete feature — a complete one held pending live validation.** The register carried it as "incomplete" for the entire sweep | — | ✅ **Resolved** | [routing §1.5](./managers/services/routing/DESIGN.md) |
| `GLD-RT-07` | 🔴 **`uhd_reconcile.py` imports THREE shims in one block** — `size_model`, `space_targets`, `watch_likelihood` — alongside **five direct** brain imports. Note `routing_targets` is imported *direct* while `space_targets`, its neighbour in `machine_learning/space/`, comes via the shim. **Worst `GLD-CACHE-S01` instance. Tally: 10 callers across 4 shims** (`space_targets` ×5, `watch_likelihood` ×3, `library_classifier` ×1, `size_model` ×1) | S | 🔵 Open | [routing §1.5.5](./managers/services/routing/DESIGN.md) |
| `GLD-RS-07` | 🎯 **`CrossInstanceMove.__init__` defaults `dry_run: bool = True`** — the **only safe-by-default constructor found in the sweep**. Every other manager defaults `False` (live) or refuses to construct. **Answers `writeback` Q4 with in-repo precedent**, from the module that moves 60 GB files between instances | S | 🔵 Open | [radarr/storage §4.5](./managers/services/radarr/storage/DESIGN.md) |
| `GLD-RT-01` | 🟡 **`services/routing`'s `allowed_roots` guard fails toward ACTION** — a `rootfolder` fetch error leaves the set empty, which *"disables the guard rather than the pass"*, so same-instance moves proceed **without** the cross-library check (the guard that stops a 4K instance moving a kids film into the 1080p kids root). **Second documented fail-toward-action** after `build_franchise_file_ids`, which caused an outage | S | 🔵 Open | [routing §4](./managers/services/routing/DESIGN.md) |
| `GLD-TWH-04` | ✅ **FIXED session 84.** `get_all_history` returned `[]` on a dead API and `break`-ed out of the page loop on a failed page, and the result was **cached for an hour** under the key ~6 consumers read. Three changes: **(1)** a failed page now raises `HistoryFetchError` carrying the offset and partial count, so a truncated history can never be cached; **(2)** a **short-page terminator** replaces the `recordsFiltered`-defaults-to-0 trap that truncated the whole history to ONE page when the key was absent — the grand-total check is now only a fast-path; **(3)** `get_all_history_cached` **prefers STALE over EMPTY**, falling back to the previous cached list and warning that *"an empty watched-set un-guards every watched title"*, with a belt-and-braces check in case the cache layer swallows the exception. Also fixes `GLD-TWH-06` (docstring said 24 h against a 3600 s TTL) | S | ✅ **Fixed** | [watch_history §5.5](./managers/services/tautulli/watch_history/DESIGN.md) |
| `GLD-THY-01` | ✅ **FIXED session 84.** `get_full_watch_history` was **the only one of three fetchers in its own file** that conflated failure with end-of-data — `_fetch_full_movie_history` and `fetch_all_history_threaded` both already returned `None` on a failed page and documented the contract. It now matches them: `items is None` → warn with page + partial count → return `None`; empty page and short page remain the genuine terminators. ⚠️ **Two callers had to be fixed with it** — `get_history_grouped_by_series` and `get_series_watch_counts` iterate the result directly and would have **crashed on `None`**. Both now handle it and warn that an empty map *"reads as zero plays for every series"*; their `{}` contract is unchanged because their own callers have not been audited | S | ✅ **Fixed** | — |
| `GLD-THY-02` | ✅ **RESOLVED session 84 — both halves fixed.** The compound risk was that **both** watched-set sources shared one bug shape: `anomaly.py` folds Tautulli completions into `watched_tmdb_ids` as a documented backstop *"even when the live Trakt history fetch is **rate-limited and served stale**"* — i.e. the backstop existed **because of `GLD-THY-01`** — but it reads a key derived from `watch_history.get_all_history`, which was **`GLD-TWH-04`**. A Trakt truncation plus a Tautulli blip left `watched_tmdb_ids` **empty** and the never-delete-a-watched-movie guard silently off. Both producers now fail **visibly** rather than as "nothing was watched": Trakt returns `None`, Tautulli raises and serves stale | S | ✅ **Resolved** | — |
| `GLD-THY-03` | ✅ **A seventh shipped bug recorded at its fix site — and a subtle one.** `get_history`: *"Trakt takes the media type as a PATH segment with a PLURAL name (`sync/history/episodes`). `type=episode` as a QUERY param is not a recognised parameter — it was **silently dropped**, so this returned the UNFILTERED history and **the movie fetch below got the identical payload**."* An API answering 200 with the wrong data because it ignored an unknown parameter — the episode and movie histories were the same list. Worth citing beside the `importMode=Move` and frozen-history notes | S | 🔵 Open | — |
| `GLD-THY-04` | 🎯 **The canonical `dry_run` form is a CONVENTION, not a one-off — and it sits in the wrong places.** `trakt/history` carries the **identical** four-level raise-if-unresolvable resolution as `sonarr/series/space_pressure` (`GLD-SP-01`), comment and all. So the strongest form appears in **at least two** managers — and `TraktHistoryManager` is **read-only**, while the weakest form was in `writeback` (pushes to third-party accounts), `calendar` (PUTs to both *arrs) and `radarr/repair/anomaly` (**deletes movie files**). **Rigour is inversely correlated with blast radius**, which sharpens the session-32 observation from "systemic" to "inverted" | S | 🔵 Open | — |
| `GLD-TWH-01` | 🎯 **Cite the Tautulli history projection as the PII-minimisation reference** — per-field justification for **every** admission and **every** drop (`friendly_name` = real names, `ip_address` = location-linkable, `machine_id` = *"can re-identify a viewer"*), borderline retentions **argued** (`location` as a lan/wan bit, *"NOT an IP and NOT geolocation"*), a **stated verification method** (grepping named consumer packages), and the cost of exclusion recorded (per-platform, not per-box). **The best privacy documentation in the repo** | S | 🔵 Open | [watch_history §2](./managers/services/tautulli/watch_history/DESIGN.md) |
| `GLD-TWH-02` | **Document the history TTL's lower bound** — `_HISTORY_TTL = 3600` is *"kept positive (not per-run) because ~6 consumers call `get_all_history_cached` per run; the TTL must exceed a run's duration so they share one fetch."* It is a **request-coalescing** mechanism, not just a staleness bound | S | 🔵 Open | [watch_history §5](./managers/services/tautulli/watch_history/DESIGN.md) |
| `GLD-TWH-06` | **Fix the stale docstring** — `get_all_history_cached` says *"cached for 24 hours"*; `_HISTORY_TTL` is **3600** | S | 🔵 Open | — |
| `GLD-MVF-01` | 🟡 **Three PARALLEL pipe-separated arrays with no length invariant** — `radarr/cache/movie_files.py`'s `SCHEMA_COLUMNS` stores `cast_names`, `cast_characters` and `cast_order` as three separate pipe-joined strings that must stay index-aligned. Nothing enforces it: an actor with a `\|` in their name, a missing character, or a null billing order shifts one column relative to the others and every downstream `zip` silently mis-pairs actor→character→billing. **`people_matrix`'s billing decay `1/(1+0.25*rank)` reads `cast_order`** — a shift there mis-weights the affinity signal without any error. Store one JSON array of objects, or assert equal lengths on write | S | 🔵 Open | — |
| `GLD-MVF-02` | 🔴 **`build_franchise_file_ids` — the fail-toward-action function that caused an outage — is used HERE**, in the manager whose docstring asserts *"Franchise entries are **NEVER** deleted — they provide the quality/codec fingerprint for the collection and represent the user's entry point."* An absolute invariant resting on a helper documented as failing open. **Confirm the failure path cannot empty the franchise set** | S | 🔵 Open | `GLD-CLS-02`, `GLD-RT-01` |
| `GLD-TMD-01` | **Sibling audit, 2 of 7 done.** `metadata.build_metadata_index` handles the same failure class **correctly** — `continue` (skip one key, **warn per key**) where `watch_history` does `break` (abandon the rest, silently). It iterates *items*, not *pages*, so a mid-loop failure costs one entry. But it still returns a **partial index silently**, and those holes are the documented reason `affinity.build_library_index` exists. **Return the skip count** so that backstop's necessity is measurable | S | 🔵 Open | [metadata §2](./managers/services/tautulli/metadata/DESIGN.md) |
| `GLD-TMD-02` | 🎯 **Record the schema-migration rule** — `watch_history` **falls back** (absent `watched_status` → `percent_complete`), `metadata` **invalidates and rebuilds** (probes for `tmdb_id`, deletes a pre-schema cache). **Both are right; the deciding factor is whether a fallback EXISTS, not cost.** `percent_complete` substitutes for `watched_status`; nothing substitutes for a tmdb id — which is why the *expensive* one (an API call per key, 7-day TTL) is the one that rebuilds | S | 🔵 Open | [metadata §4.1](./managers/services/tautulli/metadata/DESIGN.md) |
| `GLD-TMD-04` | ✅ **A three-way absent/empty split — cite as reference.** `metadata` separates **fetch-failed** (warn, skip) from **`result==success` with `data=={}`** (*"item no longer exists in Plex"* → `not_in_metadata` bucket) from **present-but-no-tmdb** (→ `no_tmdb_guid` bucket). The middle case is the one most code misses: the call *worked*, so the naive read files it under the wrong diagnostic. **Eleventh** absent-vs-empty instance and the first three-way | S | 🔵 Open | [metadata §3](./managers/services/tautulli/metadata/DESIGN.md) |
| `GLD-TUS-02` | ❓ **Sibling audit, 3 of 7 — three shapes, three verdicts.** `watch_history` = **page** loop, `break` on failure, **cached 1 h** → 🔴. `metadata` = **item** loop, `continue`, warns per key → ✅. `users` = **single call**, `[]` on failure → 🟡. **Consequence scales with shape.** ⚠️ **Session 76: still open** — `TautulliManager.__init__` (first 80 lines) shows the component wiring but not the affinity cache-write, so whether `get_all_users`' `[]` is persisted is **unread**. If it is, an empty roster yields an **empty per-user affinity matrix** for every consumer until the TTL expires | S | 🔵 Open | [users §3.1](./managers/services/tautulli/users/DESIGN.md) |
| `GLD-ORCH-01` | ✅ **FIXED IN `BaseManager` session 77 — and it was a one-line omission.** Root cause: `BaseManager.__init__`'s parent-link block already inherited `logger`, `config`, `global_cache` and `validator` from the registry parent — **`dry_run` was simply missing from that list**, so every manager re-implemented the capture and any that forgot defaulted to `False` = **LIVE**. Fixed with a 4-step precedence: **explicit kwarg** (must win outright — `__new__` is a singleton registry, so `__init__` can re-run and a later explicit `False` has to override an earlier `True`) → **pre-`super()` value** (never clobber a subclass that walked its own chain, e.g. `radarr/repair/anomaly.py`) → **registry parent** (the actual fix) → **`False`** (today's behaviour, so it is byte-identical wherever the kwarg was already passed). **Deliberately does NOT raise** — that is right for a manager that moves files, not for a base class shared with read-only ones | M | ✅ **Fixed** | `base_manager.py` |
| `GLD-ORCH-02` | ✅ **DONE session 78 — all three redundant captures removed.** `services/writeback`, `services/calendar` and `radarr/quality/selector` each ran `self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)` **after** `super().__init__()`, overwriting a parent-inherited `True` with `False`. ⚠️ **Deleting them alone would have regressed** — they resolved from `kwargs["manager"]` (the **constructing** parent) while `BaseManager` used the **registry** parent, and `parent_name` lookups can miss (`GLD-REP-02`). So `BaseManager` gained a `kwargs["manager"]` rung **first**, making the three lines exactly redundant, and each deletion left an inline note recording what the line did and why it was harmful. **Closes `GLD-WB-03`, `GLD-CAL-01`, `GLD-RQ-11`** | S | ✅ **Done** | `base_manager.py` |
| `GLD-TUS-03` | ✅ **CLOSED session 73 — Tautulli has exactly THREE `if not resp*` sites, and only one is a page loop.** `__init__.py:183` (server unreachable → warn + *"skipping data collection"* — the whole pass aborts rather than proceeding on empty data ✅) · `metadata:19` (`continue` ✅) · `watch_history:128` (`break` 🔴 = `GLD-TWH-04`). **`devices`, `transcode`, `episodes`, `series` do not fetch at all** — they compute from already-fetched history, like `users` does for affinity. So `watch_history` is the **only** paginated fetcher in Tautulli, and it is the broken one | S | ✅ Closed | [users §3.1](./managers/services/tautulli/users/DESIGN.md) |
| `GLD-SRF-01` | ⚠️ **SUBSTANTIALLY CORRECTED session 75 — I attributed the bug to the wrong method.** The `break`-on-falsy loop is in **`get_all_series_chunked`**, which **`refresh_all_series` never calls**. `refresh_all_series` does a **single** `_make_request(resolved, "series", fallback=[])` — Sonarr v3's `/series` returns the whole library, so there is **no pagination and no truncation risk** on the live path. 🔴 **But `get_all_series_chunked` has a worse latent bug**: it pages with `?page=N&pageSize=200`, and `refresh_all_series`'s own docstring says v3 `/series` *"always returns the full library"* — so if the paging params are ignored, every iteration returns all ~8k series, `len(response) < chunk_size` is never true, and the loop **never terminates**, appending the full library each pass. **Establish whether anything calls it; if not, delete it** *(P-I: dormant code accumulating defects)* | S | 🔵 Open | [retrieval §1](./managers/services/sonarr/series/retrieval/DESIGN.md) |
| `GLD-SRF-02` | ✅ **FIXED session 75 — and downgraded 🔴→🟡 first.** My claim that the guard *"reports the truncation as success"* was too strong: `refresh_all_series` returns `[]` on a failed fetch **and skips the cache update**, so `validate_series_count` sees `live=0 vs cache=8000`, `diff_pct` ≫ 10%, and **warns correctly**. The tautology is real but **narrow** — it hides only a *short-but-successful* response (Sonarr answering 200 with a partial list), because the fetch is its own reference. **Fix**: a short-list guard in `refresh_all_series`, at the one point where the **pre-sync** cache count is still independent of this run's fetch. Warns (never blocks) when >20% of a ≥50-series library vanishes in one run, and states that no downstream check will flag it because every downstream count derives from the same response | S | ✅ **Fixed** | [retrieval §2](./managers/services/sonarr/series/retrieval/DESIGN.md) |
| `GLD-SRF-03` | 🟡 **`validate_series_tags` builds its key two ways in three lines** — `f"sonarr/{instance}/tags.json"` uses the **raw** instance (the next line computes `resolved_instance`, which every other line uses) **and a `.json` suffix** that `CacheKeyPaths.sonarr.TAGS` does not have. A **fifth** cache-key format. On a miss `known_tags` is empty and **every tag reference on every series reads as invalid** — that this is not observed suggests either a different `sonarr_cache` convention or that **nothing calls the method** | S | 🔵 Open | [retrieval §3](./managers/services/sonarr/series/retrieval/DESIGN.md) |
| `GLD-PMD-01` | 🎯 **`plex/metadata/__init__.py:205` is the REFERENCE implementation** — `if not resp: return None  # transient — allow retry on a later run`. Returns **`None`, not `{}`**, and states **why** in four words. Exactly the discipline `watch_history` lacks, in the same repo, on the same class of call. **Cite it in the `GLD-TWH-04` fix** | S | 🔵 Open | `GLD-TWH-04` |
| `GLD-LIK-01` | ✅ **BUILT AND WIRED session 72.** `machine_learning/thresholds/drift.py` (+ `test_drift.py`, 13 cases), called from `ledger/plan_summary.log_axis_drift()` immediately after `log_thresholds()`. Pure, stdlib-only, **scale-agnostic** so it covers the watchability axis **and** `untouched_base` on the likelihood scale — the one a `THRESHOLD_SPECS`-only pass misses. Tracks **selectivity, not the median**: the quantity the real re-anchor preserved and the one a cutoff controls. Tolerances anchored on two measured points — benign churn moved translation-immune percentile mode **0.0/+4.9 pp**, the incident ran at **45 pp** → warn **10**, alarm **20**. `unknown ≠ ok` throughout. **The anchor bootstraps once and is never auto-refreshed** (a baseline that regenerates drifts with what it watches); the bootstrap run says explicitly that a captured baseline is **not** a clean bill of health. On drift it names `untouched_base` as the thing a specs-only re-anchor will miss | M | ✅ **Done** | [drift.py](./managers/machine_learning/thresholds/drift.py) |
| `GLD-DRIFT-01` | 🟡 **The anchor is a `global_cache` key (`ml/thresholds/axis_anchor`), not a reviewed artifact.** It bootstraps from whatever the distribution happens to be on first run — which may already be drifted — and clearing it is a manual cache delete. Consider a committed JSON beside `thresholds_*.json`, so the baseline is diffable and blameable the way derived cutoffs already are | S | 🔵 Open | — |
| `GLD-DRIFT-02` | 🟡 **`_v2_cutoffs` detects the axis by string-matching `spec.note.startswith("AXIS V2")`.** The axis is recorded in prose and nowhere structured, so a reworded note silently drops a threshold from drift coverage — or, worse, admits a LEGACY one and compares it against a distribution it never sat on. **Add `ThresholdSpec.axis`** | S | 🔵 Open | — |
| `GLD-DRIFT-03` | **Extend coverage to the likelihood scale** — `drift.py` is scale-agnostic and tested on it, but `plan_summary` only feeds it the persisted `watchability_score` column. `untouched_base` (12→25, the constant behind the **456→8** collapse) is still unwatched at runtime | M | 🔵 Open | `GLD-LIK-01` |
| `GLD-TUS-01` | ✅ **FIXED — all 3 sites, session 71.** D32's config key is now linked to the thresholds it invalidates, in both directions: **(1)** `tautulli/users._affinity_half_life` — *"SETTING THIS IS AN AXIS TRANSLATION — IT IS NOT A LOCAL CHANGE"*, the mechanism (gain 1.0), the Group D v2 precedent (delete family 20→17, `untouched_base` 12→25 after a **456→8, –98.2%** collapse, monitor left at 35), no detector, a 3-step pre-flight, thin evidence (n~931). **(2)** `affinity/genre_affinity.py` module docstring — the same warning at the brain-side knob, pointing back to both the config surface and the registry. **(3)** `thresholds/registry.py` delete block — the sentence *"re-derive it whenever the score axis is translated again"* now **names the three known triggers** (another scorer revision · `affinity_half_life_days` · a newly-enriched group) and warns that **walking `THRESHOLD_SPECS` alone is insufficient**, because `likelihood.untouched_base` is on a different scale and has no spec there | S | ✅ **Fixed** | [users §2](./managers/services/tautulli/users/DESIGN.md) |
| `GLD-RT-04` | 🔴 **`services/routing/` has ZERO markdown** — 130 KB of source, **62 KB of tests** (ratio > 1.0), and no README, no DESIGN, no per-module `.md`. The only service package in the repo with none — and it holds `GLD-RAD-01`'s caller plus a 57 KB `uhd_reconcile.py` | M | 🔵 Open | [routing §1](./managers/services/routing/DESIGN.md) |
| `GLD-RT-06` | **Reconcile `machine_learning/routing/` with `services/routing/`** — the brain package is an empty stub declaring `select_instance` whose stated source file does not exist (`GLD-ROU-01`/`02`), while the service package is 130 KB of live routing. Either the stub is `uhd_reconcile`'s intended decision half, or it should go | S | 🔵 Open | [routing §10](./managers/services/routing/DESIGN.md) |

### 4.2 Core — `scripts/`

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CORE-01` | Web interface (umbrella — see §4.6) | L | 🔵 Open |
| `GLD-CORE-02` | Trakt watchlist auto-pruning from Tautulli history | M | 🔵 Open |
| `GLD-CORE-03` | Resolve `movieRootFolders` *(dup of `GLD-CFG-03`)* | S | 🔵 Open |
| `GLD-CORE-04` | Docs-lint in CI — assert doc/code parity | S | 🔵 Open |
| `GLD-CORE-05` | Logging cleanup — `TraktRegistrar` at INFO, verbose `__init__`, sub-manager `load_components` during parent `prepare()` | S | 🔵 Open |
| `GLD-CORE-06` | `load_summary` for pre-`prepare()` components *(dup of `GLD-MIX-01`)* | S | 🔵 Open |
| `GLD-CORE-07` | Run history + trend view | M | 🔵 Open |
| `GLD-CORE-08` | Structured event bus replacing ad-hoc `run_stats` | M | 🔵 Open |
| `GLD-CORE-09` | Per-user playlist personalisation surface | M | 🔵 Open |
| `GLD-CORE-10` | Challenger-model promotion pipeline | L | 🔵 Open |

### 4.3 Hooks

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-HOOK-01` | Migrate legacy flat brain modules into guarded subpackages | S | 🔵 Open |
| `GLD-HOOK-02` | Run both guards in CI, not only locally | S | 🔵 Open |
| `GLD-HOOK-03` | Docs-parity guard | S | 🔵 Open |
| `GLD-HOOK-04` | Purity rule: no filesystem writes in the brain | M | 🔵 Open |
| `GLD-HOOK-05` | No-`global_cache` guard for brain modules | M | 🔵 Open |
| `GLD-HOOK-06` | Review the blanket `test_*.py` purity exemption | S | 🔵 Open |
| `GLD-HOOK-07` | Unit tests for the secret scan (currently untested) | S | 🔵 Open |
| `GLD-HOOK-08` | Commit-msg hook for conventional commits | S | 🔵 Open |
| `GLD-HOOK-09` | Machine-readable `--json` output for CI annotation | S | 🔵 Open |

### 4.4 Managers (layer-wide)

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-MGR-01` | Service-purity hook — AST-detect scoring inside `services/` | M | 🔵 Open |
| `GLD-MGR-02` | Populate `load_summary` pre-`prepare()` *(dup of `GLD-MIX-01`)* | S | 🔵 Open |
| `GLD-MGR-03` | Warn on unresolved parent link after deferred retry | S | 🔵 Open |
| `GLD-MGR-04` | Validate `_infer_parent_from_path` against the registry at startup | S | 🔵 Open |
| `GLD-MGR-05` | Manager-tree dump command | S | 🔵 Open |
| `GLD-MGR-06` | Typed contracts at the service↔brain seam, checked in CI | M | 🔵 Open |
| `GLD-MGR-07` | Lifecycle hooks (`on_prepare_complete`, `on_run_error`) | M | 🔵 Open |
| `GLD-MGR-08` | Structured event bus *(dup of `GLD-CORE-08`)* | M | 🔵 Open |
| `GLD-MGR-09` | Explicit `parent_name` everywhere, deprecating path inference | M | 🔵 Open |
| `GLD-MGR-10` | `Protocol`-based manager interface | M | 🔵 Open |
| `GLD-MGR-11` | 🔴→✅ **`cache_keys` has reported an EMPTY CACHE on every manager since the field was written, and could not have reported anything else.** `BaseManager` read `getattr(self.global_cache, 'memory_cache', {})` — `GlobalCacheManager` exposes the `MemoryManager` as **`.memory`** and has never had a `memory_cache` attribute, so the `{}` default converted the miss into an empty result and `dep_versions["cache_keys"]` plus every init summary were permanently `[]`. Absent conflated with empty (P-C) in the one field whose job is to say what the cache holds. **Two copies** — an inline block in `__init__` and `_preview_cache_keys()`, byte-identical (P-E) — so the same wrong name had to be fixed twice; `__init__` now delegates and there is one implementation. A missing `.memory` WARNS once per process instead of returning a silent `[]`, because that would be a contract change, which is precisely the condition that went unnoticed here. Sim: 12 assertions incl. the old read replayed on a 6-key cache (`[]`), the new read, the 5-cap, both guards, in-place regeneration through a held handle, once-only warning across 40 inits, and `keys()` raising | S | ✅ **Fixed** |
| `GLD-MGR-12` | ✅ **The `GLD-MGR-11` guard warned CONTRACT CHANGE at a cache that is not a `GlobalCacheManager`** — the fix gave `_preview_cache_keys` a voice (it had read `memory_cache`, an attribute nothing has ever had, so it returned a silent `[]` and could never warn). But it warned on ANY missing `.memory`, and a non-`GlobalCacheManager` cache is a supported arrangement: `GLD-CACHE-13` forbids a second `GlobalCacheManager` per process, so alternative caches exist by design (`pilot_search_daemon.LedgerCache` is one). **Two absences, now distinguished**: a real `GlobalCacheManager` without `.memory` still WARNS once (the condition the guard was written for); anything else gets a once-per-process DEBUG line naming its type. Duck-typed rather than imported — importing `GlobalCacheManager` into `BaseManager` is a factories→factories cycle at class-definition time. Separate class flags so one can never consume the other's once-per-process budget. 🔴 **THE CAUSE ORIGINALLY FILED HERE WAS WRONG AND IS RETRACTED.** This row asserted the warning came from the DAEMON passing `LedgerCache`. It did not: `pilot_search_daemon` never constructs `SonarrCacheOwnedEpisodesManager`, and the log shows **no second process**. The `ConfigLoader`/`SecretBootstrap` pair I read as a process boundary is a SECOND MANAGER STACK built inside the same run, at the end-of-run threshold-derivation/axis-drift shadow pass (`PlanSummary.log_thresholds` / `log_axis_drift` follow it immediately). **The tell I had and did not use**: `pilot_search_daemon.py` contains no reference to that manager — one grep, available before the row was written. ⚠️ **The missing `.memory` is a SYMPTOM, not the defect.** The same stack logged `2 owned episode(s) across 2 series -> owned_episodes.parquet (full rebuild: 2 series fetched)` against a library of 12,133 series, and `df.to_parquet(path)` is UNCONDITIONAL — yet the real parquet's mtime is the FIRST build's, so that write landed elsewhere. Open as `GLD-MGR-13`: the shadow stack appears to resolve a different cache root, which would explain all four symptoms at once (no `.memory`, a near-empty series cache, a config/secret re-bootstrap, and a write that vanished) — and would mean threshold derivation runs against a nearly empty library | S | ✅ **Fixed** (the guard) · 🔴 **cause retracted** — §0.1 #78, see `GLD-MGR-13` |
| `GLD-MGR-13` | 🔴 **The end-of-run threshold shadow pass builds a SECOND manager stack, and it sees almost nothing** — after the final tables (`Change plan`, `Dry-run plan ledger`, `Next watch`), the 2026-08-21 02:38 run reloads config, re-bootstraps secrets, rebuilds the registry, and constructs managers again; `PlanSummary.log_thresholds` / `log_axis_drift` immediately follow. That stack: (a) gets a cache with no `.memory` (surfaced as `GLD-MGR-12`), (b) does a **full** owned-episodes rebuild finding **2 series** where the run's own build found **12,133**, and (c) writes a parquet that never appears — the live file's mtime is 06:32:45 (the good build), while the shadow build ran at 06:38:16, and a repo-wide search finds exactly one `owned_episodes.parquet`. Since `to_parquet` is unguarded, the write happened somewhere else. Working hypothesis, ONE cause for all four: the shadow stack resolves a different `CacheKeyBuilder.base_dir`, so it reads an empty parallel cache and writes into it. **Consequence if true**: the derived thresholds and the axis-drift comparison — which exist to tell the operator whether hand-set cutoffs are drifting — are computed against a nearly empty library, i.e. the shadow numbers are meaningless rather than merely stale. ⚠️ NOT destructive: the real parquet was never touched, verified by mtime after an initial (wrong) claim that it had been overwritten. Next step: confirm the base_dir divergence and make the shadow pass reuse the run's existing stack rather than constructing its own | M | 🔴 **Open** |

### 4.5 Factories (layer-wide)

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-FAC-01` | Web interface *(umbrella — §4.6)* | L | 🔵 Open |
| `GLD-FAC-02` | Warn on unresolved parent link *(dup of `GLD-MGR-03`)* | S | 🔵 Open |
| `GLD-FAC-03` | Populate `load_summary` *(dup of `GLD-MIX-01`)* | S | 🔵 Open |
| `GLD-FAC-04` | Remove or implement `print_tree_view` *(dup of `GLD-REG-03`)* | S | ✅ **Fixed with `GLD-REG-03`** |
| `GLD-FAC-05` | Thread-safe `MemoryManager` *(dup of `GLD-CACHE-01`)* | S | 🔵 Open |
| `GLD-FAC-06` | Config schema validation on load *(dup of `GLD-CFG-01`)* | M | 🔵 Open |
| `GLD-FAC-07` | Cache size accounting + LRU eviction option | M | 🔵 Open |
| `GLD-FAC-08` | Config hot-reload via `RegistryConfigSync` | M | 🔵 Open |
| `GLD-FAC-09` | Structured event bus *(dup of `GLD-CORE-08`)* | M | 🔵 Open |
| `GLD-FAC-10` | Secret rotation helper | S | 🔵 Open |
| `GLD-FAC-11` | Cache versioning / schema stamps | M | 🔵 Open |
| `GLD-FAC-12` | Registry snapshot dump at run end | S | 🔵 Open |
| `GLD-FAC-13` | `Protocol` for the manager contract *(dup of `GLD-MGR-10`)* | M | 🔵 Open |

### 4.6 Web interface — phased

**Phase 1 — read-only**

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-WEB-01` | Dashboard — last run, health, space, pending plan | M | 🔵 Open |
| `GLD-WEB-02` | **Plan review** — dry-run plan, sortable, per-item rationale | M | 🔵 Open |
| `GLD-WEB-03` | Ledger browser — filter by action/service/title/date + signal breakdown | M | 🔵 Open |
| `GLD-WEB-04` | Library explorer — score, tags, quality, size, watch data | M | 🔵 Open |
| `GLD-WEB-05` | Health view — reachability, daemons, cache stats | S | 🔵 Open |
| `GLD-WEB-06` | Config viewer (read-only, secrets as status) | S | 🔵 Open |

**Phase 2 — write**

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-WEB-07` | Config editor — schema-generated forms, validated, atomic save | L | 🔵 Open |
| `GLD-WEB-08` | Run trigger with dry-run toggle + live SSE log | M | 🔵 Open |
| `GLD-WEB-09` | Daemon control — start/stop/restart/status | S | 🔵 Open |
| `GLD-WEB-10` | Per-item overrides — pin, protect, force-upgrade, exclude | M | 🔵 Open |

**Phase 3 — analysis**

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-WEB-11` | Threshold sandbox — replay ledger under altered weights, diff | L | 🔵 Open |
| `GLD-WEB-12` | Run history + trends | M | 🔵 Open |
| `GLD-WEB-13` | Score explainer — per-title signal waterfall | M | 🔵 Open |
| `GLD-WEB-14` | Watchlist manager — review, prune, see why items persist | M | 🔵 Open |
| `GLD-WEB-15` | Playlist preview before writeback | M | 🔵 Open |
| `GLD-WEB-16` | Space simulator — "if I add 2TB, what changes?" | M | 🔵 Open |

**Phase 4 — platform**

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-WEB-17` | Auth beyond localhost | M | 🔵 Open |
| `GLD-WEB-18` | Mobile-responsive layout | S | 🔵 Open |
| `GLD-WEB-19` | Webhook receiver — Sonarr/Radarr/Plex events | L | 🔵 Open |
| `GLD-WEB-20` | Notification centre | S | 🔵 Open |
| `GLD-WEB-21` | Export ledger/plan to CSV/JSON | S | 🔵 Open |
| `GLD-WEB-22` | Dark mode | S | 🔵 Open |

### 4.7 Registry

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-REG-01` | Warn on name collision in `register()` | S | 🔵 Open |
| `GLD-REG-02` | Cycle detection in `inject_dependencies_for_subtree` | S | 🔵 Open |
| `GLD-REG-03` | 🔴→✅ **`print_tree_view` implemented, and the call site was the worse half.** `base_manager` called it behind `print_registry_tree` **inside the same `try` as registration AND parent linking** — so switching the debug flag on raised `AttributeError` before the link ran and cost every manager its inherited `logger`, `config`, `global_cache`, `validator` **and `dry_run`**, surfacing only as *"failed to register"*. A debug print that can disarm `dry_run`. Call moved outside the block with its own guard; method written (parent→child walk, self-parent-as-root per `GLD-SPLIT-02`, cycle guard, absent-parent-becomes-root). Sim: 6-node tree, self-parent, mutual cycle, ghost parent, empty | S | ✅ **Fixed** |
| `GLD-REG-04` | Snapshot registry to disk at run end | S | 🔵 Open |
| `GLD-REG-05` | Registry view in the web UI | M | 🔵 Open |
| `GLD-REG-06` | Typed lookup — `get(cat, name, expect=Cls)` | S | 🔵 Open |
| `GLD-REG-07` | Read lock or immutable snapshot reads | M | 🔵 Open |
| `GLD-REG-08` | Registration timestamps — build-order forensics | S | 🔵 Open |
| `GLD-REG-09` | Flag namespace validation | S | 🔵 Open |
| `GLD-REG-10` | Deregistration on manager teardown | M | 🔵 Open |
| `GLD-REG-11` | 🔴→✅ **Anomaly heuristic was inverted — fixed.** It flagged any source containing `pycharmprojects`; the canonical checkout **is** `C:\Users\rober\PycharmProjects\glidearr`, so every healthy row got ❌ and a stale-mirror import read clean. Replaced with a real test: reference root derived from `cli.py`'s own `__file__` (it cannot come from the config a suspect import would have read), ❌ for a source outside it. `_is_expected_path` wired as a secondary ⚠️ — but only after splitting `_expected_subpaths()` out, because it has rules for **four service prefixes only** and `False`-for-no-rule would have flagged every brain and factory manager (P-C). Also separator-tolerant: `SonarrSeriesSpacePressureManager` lives in `space_pressure.py`, not `space/pressure.py`. Note the helper took a bare PATH while the only datum available is `"<file>:<line> in <func>()"` — part of why it sat uncalled | S | ✅ **Fixed** |
| `GLD-REG-12` | 🔴 **`auto_hot_swap_from_config` is a silent no-op on EVERY manager init.** `BaseManager` passes `self.config.raw_data` — a plain dict (confirmed at the property) — to a method that wants an object with `_registry_category`/`name`, so control always reached the else branch. That branch guarded on `self.registry.logger`, an attribute `RegistryManager` never defines, **so the warning never emitted either**: a no-op whose own failure detector was itself unreachable (P-A + P-D in one method). **Deliberately not repaired silently** — either the call site is wrong (config data was never this argument) or the feature was never written, and the two need different fixes. Now warns once per process; next run decides it on evidence. **Operator decision** | S | 🔵 **Open — decision** |
| `GLD-REG-13` | 🔴→✅ **Two entry shapes, three readers each assuming the wrong one.** `register()` stores a wrapper dict `{instance, origin, parent_name}`; `set()` stores the bare object. Consequences, all silent: `find_by_attr` getattr'd the **wrapper**, so it has **always returned `[]`**; `load_config_and_propagate` `hasattr`'d the wrapper, so it propagated to **nothing**; `get_all` would raise on a `set()` row; and the CLI dump's dict branch read `"class"`/`"source"`, **keys the registry has never written**. One `_unwrap()` on `RegistryCore`, used by all four, so a shape change cannot desynchronise them again (P-E + P-C). `flags` skipped — its values are bools, not managers | S | ✅ **Fixed** |
| `GLD-REG-14` | 🔴→✅ **The Source column named the same wrapper file for dozens of rows.** `ComponentManagerMixin.register()` calls `RegistryCore.register`, and `factories/mixins/` was **not** in `utility_keywords` — so the origin walk stopped on the MIXIN and recorded `component_manager.py`. Worse, the mixin registers **after** `BaseManager` already did, so the correct origin was written first and then **overwritten by the wrapper's**. Found only because it would have made `GLD-REG-11`'s new ⚠️ fire on every service component — i.e. the fix's own false-positive check found the defect. `factories/mixins/` + `factories/base_instance_manager` added to the skip list | S | ✅ **Fixed** |

### 4.8 Config

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CFG-01` | Wire `validator.py` into the load path with a declared schema | M | 🔵 Open |
| `GLD-CFG-02` | Warn or auto-save on `set_bulk()` without `save()` | S | 🔵 Open |
| `GLD-CFG-03` | 🔴 Resolve `movieRootFolders` | S | 🔵 Open |
| `GLD-CFG-04` | Dotted-path `get()` helper | S | 🔵 Open |
| `GLD-CFG-05` | Config hot-reload | M | 🔵 Open |
| `GLD-CFG-06` | Secret rotation helper | S | 🔵 Open |
| `GLD-CFG-07` | Config diff/history snapshot on save | S | 🔵 Open |
| `GLD-CFG-08` | Typed accessors (`get_int`, `get_bool`, `get_path`) | S | 🔵 Open |
| `GLD-CFG-09` | Schema export for web form generation | M | 🔵 Open |
| `GLD-CFG-10` | Unused-key report at startup | S | 🔵 Open |
| `GLD-CFG-11` | Per-instance secret namespacing | S | 🔵 Open |

### 4.9 Cache

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CACHE-01` | Thread-safe `MemoryManager` | S | 🔵 Open |
| `GLD-CACHE-02` | Cache schema versioning | M | 🔵 Open |
| `GLD-CACHE-03` | Size accounting + optional LRU eviction | M | 🔵 Open |
| `GLD-CACHE-04` | Stale-key report — keys served past TTL and by how far | S | 🔵 Open |
| `GLD-CACHE-05` | Per-key metrics (hit/miss/stale/regen) | S | 🔵 Open |
| `GLD-CACHE-06` | Atomic JSON writes (`mkstemp` + `os.replace`) | S | 🔵 Open |
| `GLD-CACHE-07` | Cache warm command | S | 🔵 Open |
| `GLD-CACHE-08` | Compression by default for large snapshots | S | 🔵 Open |
| `GLD-CACHE-09` | Cache browser in the web UI | M | 🔵 Open |
| `GLD-CACHE-10` | Typed cache accessors | M | 🔵 Open |
| `GLD-CACHE-11` | Negative-result caching with short TTL | S | 🔵 Open |
| `GLD-CACHE-12` | **Cache consumer registry** — declare who reads each key; report zero-reader keys at run end *(closes P-A mechanically)* | S | 🔵 Open |
| `GLD-CACHE-13` | **Idempotent singleton `__init__`** — a second in-process construction warns + no-ops instead of re-running `__init__` on the singleton (`memory`/`run_summary` preserved); `_reset_singleton()` test hook. Closed a latent P-D behind invariant I4 | S | ✅ Done |

### 4.10 Mixins

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-MIX-01` | 🔴 Populate `load_summary` for pre-`prepare()` components | S | 🔵 Open |
| `GLD-MIX-02` | Warn on silently-dropped caller kwargs | S | 🔵 Open |
| `GLD-MIX-03` | Distinguish signature mismatch from runtime failure | S | 🔵 Open |
| `GLD-MIX-04` | Declare required vs optional dependencies per component | M | 🔵 Open |
| `GLD-MIX-05` | Component load timing in the summary | S | 🔵 Open |
| `GLD-MIX-06` | Dependency-ordered loading via declared `depends_on` | M | 🔵 Open |
| `GLD-MIX-07` | Lazy component construction | M | 🔵 Open |
| `GLD-MIX-08` | Component health re-check after load | M | 🔵 Open |
| `GLD-MIX-09` | Typed `component_map` with `Protocol` conformance | M | 🔵 Open |
| `GLD-MIX-10` | Component tree in the web UI | M | 🔵 Open |

### 4.11 Daemons

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-DMN-01` | Daemon health heartbeat | S | 🔵 Open |
| `GLD-DMN-02` | Spawn lock (`O_EXCL` pid file) | S | 🔵 Open |
| `GLD-DMN-03` | Fail spawn when the pid file is unwritable | S | 🔵 Open |
| `GLD-DMN-04` | Daemon status in the web UI | M | 🔵 Open |
| `GLD-DMN-05` | Adaptive throughput from `X-RateLimit-*` headers | M | 🔵 Open |
| `GLD-DMN-06` | Auto-restart on daemon crash | M | 🔵 Open |
| `GLD-DMN-07` | Structured progress reporting (enriched/remaining/ETA) | S | 🔵 Open |
| `GLD-DMN-08` | Generalise the sentinel handshake for future daemons | S | 🔵 Open |
| `GLD-DMN-09` | Consume or drop the `translations` bucket | S | 🔵 Open |
| `GLD-DMN-10` | Bucket size accounting + prune | M | 🔵 Open |
| `GLD-DMN-11` | Graceful pilot drain on stop | S | 🔵 Open |
| `GLD-DMN-12` | systemd unit / Windows Service definitions | M | 🔵 Open |

### 4.12 Onboarding

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ONB-01` | Validate URL + API key as a pair in `_looks_fresh` | S | 🔵 Open |
| `GLD-ONB-02` | Re-run a single step (`--step trakt`) | S | 🔵 Open |
| `GLD-ONB-03` | Config doctor — validate an install, report what's broken | M | 🔵 Open |
| `GLD-ONB-04` | Secret rotation | S | 🔵 Open |
| `GLD-ONB-05` | Emit `.env` / compose template from an interactive run | S | 🔵 Open |
| `GLD-ONB-06` | Schema-driven step generation | M | 🔵 Open |
| `GLD-ONB-07` | Dry-run onboarding | S | 🔵 Open |
| `GLD-ONB-08` | Web onboarding | L | 🔵 Open |
| `GLD-ONB-09` | Re-validate on demand | S | 🔵 Open |
| `GLD-ONB-10` | Guided `movieRootFolders` setup | S | 🔵 Open |
| `GLD-ONB-11` | Import from an existing Recommendarr config | S | 🔵 Open |
| `GLD-ONB-12` | Token expiry warnings | M | 🔵 Open |

### 4.13 Onboarding steps

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-STEP-01` | Declared `depends_on` per step, asserted by `build_steps()` | S | 🔵 Open |
| `GLD-STEP-02` | Per-step re-run finer than `only_service` | S | 🔵 Open |
| `GLD-STEP-03` | Key-ownership assertion — overlapping writes fail loudly | M | 🔵 Open |
| `GLD-STEP-04` | Step-level dry run | S | 🔵 Open |
| `GLD-STEP-05` | `movieRootFolders` capture in `library.py` | S | 🔵 Open |
| `GLD-STEP-06` | Schema-derived steps | M | 🔵 Open |
| `GLD-STEP-07` | Web renderers per step | L | 🔵 Open |
| `GLD-STEP-08` | Idempotent re-run — detect existing valid values, offer "keep" | M | 🔵 Open |
| `GLD-STEP-09` | Progress indicator (`step 4/14`) | S | 🔵 Open |
| `GLD-STEP-10` | Per-step validation summary — what was probed, what returned | S | 🔵 Open |

### 4.14 Orchestration

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ORCH-01` | Central dry-run gate | M | ⏸ **Tabled** |
| `GLD-ORCH-02` | Dry-run audit hook — ledger the suppressed APPLYs | M | ⏸ **Tabled** |
| `GLD-ORCH-03` | AST guard: every APPLY checks `dry_run` | M | ⏸ **Tabled** |
| `GLD-ORCH-04` | Extract `Main.run()` phase sequencing into a declarative list | M | 🔵 Open |
| `GLD-ORCH-05` | Add `__init__.py` so the folder is importable | S | 🔵 Open |
| `GLD-ORCH-06` | Delete the folder if 01–04 land elsewhere | S | 🔵 Open |

### 4.15 Services (layer-wide)

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SVC-01` | Trakt watchlist auto-pruning | M | 🔵 Open |
| `GLD-SVC-02` | Resolve `movieRootFolders` *(dup of `GLD-CFG-03`)* | S | 🔵 Open |
| `GLD-SVC-03` | Service-purity hook *(dup of `GLD-MGR-01`)* | M | 🔵 Open |
| `GLD-SVC-04` | Transactional APPLY batching with rollback journal | L | 🔵 Open |
| `GLD-SVC-05` | Unified service health surface | M | 🔵 Open |
| `GLD-SVC-06` | Shared retry-with-backoff policy across API clients | M | 🔵 Open |
| `GLD-SVC-07` | Circuit breaker per service | M | 🔵 Open |
| `GLD-SVC-08` | Plex playlist writeback preview | M | 🔵 Open |
| `GLD-SVC-09` | Per-service rate budget accounting in run stats | S | 🔵 Open |
| `GLD-SVC-10` | Webhook ingestion | L | 🔵 Open |
| `GLD-SVC-11` | Document + test `renamer.py` | S | 🔵 Open |
| `GLD-SVC-12` | Instance-level failure isolation | M | 🔵 Open |

### 4.16 Tautulli

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-TAUT-01` | Consume or drop `tautulli/device_codec_matrix` | M | 🔵 Open |
| `GLD-TAUT-02` | Watched-definition contract for auto-prune | M | 🔵 Open |
| `GLD-TAUT-03` | Completeness marker on affinity | S | 🔵 Open |
| `GLD-TAUT-04` | Incremental history pull via persisted cursor | M | 🔵 Open |
| `GLD-TAUT-05` | Unresolved-mapping report in the run summary | S | 🔵 Open |
| `GLD-TAUT-06` | Per-user completion, not just group max | M | 🔵 Open |
| `GLD-TAUT-07` | Device capability inference | M | 🔵 Open |
| `GLD-TAUT-08` | Session-level dwell signals (pauses, rewinds, abandons) | L | 🔵 Open |
| `GLD-TAUT-09` | Multi-instance Tautulli | M | 🔵 Open |
| `GLD-TAUT-10` | Affinity decay | M | 🔵 Open |
| `GLD-TAUT-11` | Implement the `validator_manager` stub | S | 🔵 Open |
| `GLD-TAUT-12` | ✅ **Household affinity scoped to family** (`household_affinity.family_only`) — non-Home accounts (shared friends streaming remotely) no longer steer the household genre/actor/director maps that drive acquisition genre scoring, playlists and watch-likelihood; their watches still build their own per-user matrices, so an outsider keeps a full grading while losing the family vote. Family = Plex HOME membership resolved to Tautulli `user_id`s via the ALREADY-persisted `plex/identity_map` (zero Plex-side changes — `_persist` was written for exactly this after-the-consumers ordering). Run-order fact that shaped it: Tautulli aggregates at log-line ~29, PlexUsers enumerates at ~261, so the map read is the PREVIOUS run's; first run after enabling degrades OPEN with a loud `UNSCOPED` warning — an empty household aggregate would hurt the whole system far more than one run of outsider drift. Unknown-owner plays (no `user_id`) are KEPT and counted in the log note, a stated trade. Scope note appended to the affinity summary line every scoped run | M | ✅ **Fixed** — §0.1 #61 |
| `GLD-TAUT-13` | 🟡 **Watched-FLAG consumers remain household-agnostic** — `family_only` scopes the AFFINITY aggregate, but the parquet `is_watched` flags (fed by any account's plays) still drive engagement floors (`watch_likelihood`), episode-retention watched gates, and the people-matrix watched-set. A non-family stream can therefore still mark a title watched for retention/upgrade purposes. Scoping THOSE is per-consumer provenance surgery, not a filter — and it is the delete-guard half of the `GLD-ACQS-19` question (acquire for an outsider whose watches the retention pass then discounts → first-culled) | L | 🔵 Open |
| `GLD-TAUT-14` | ✅ **Per-account gradings table** — `family_only`'s second promise (outsiders keep their own grading) was unverifiable from a run: the log showed a household total and a COUNT of matrices, so a graded outsider and a dropped one looked the same. One boxed row per Tautulli account with Scope/Genres/Actors/Directors/Top-genres, joined on `user_id` against the extracted `_family_ids` — the same set that scoped the aggregate, so a row cannot contradict the household line printed above it. Unresolvable id set or `family_only` off renders Scope `-` with an INACTIVE caption rather than guessing. Renders `no history in window` / `none` / absent as three distinct facts, answering `GLD-TUS-05` | S | ✅ **Fixed** — §0.1 #62 |
| `GLD-TAUT-15` | ✅ **Account linking — two logins, one viewer** (`account_links`). The 2026-08-20 gradings table showed `Mom` and `mirandan75` graded separately (16 genres/820 actors vs 5/236); operator confirmed one person. Their plays now merge onto the group PRIMARY before the brain groups them, and the merged matrix is fanned back out to EVERY member, so both Plex profiles render the same recommendations instead of two thinner divergent ones. Members may be usernames (case-insensitive) or `user_id`s. Three invariants: entries shallow-copied (the household aggregate must keep seeing real account ids — in-place rewriting would re-route plays through the family filter); each member gets its own matrix COPY (no write-through aliasing between accounts); a link NEVER widens the family aggregate (family = Plex HOME, and a grading knob must not reopen the leak `family_only` closes). Malformed shapes warn and no-op, including the flat-string list an env overlay would produce — which would otherwise iterate CHARACTERS and alias single letters | M | ✅ **Fixed** — §0.1 #64 |
| `GLD-TAUT-16` | 🟡 **Linked accounts share a GRADING but not a watched-set** — "both playlists show the same thing" has a second input `GLD-TAUT-15` does not merge. The builder's watched-set is a UNION of (a) Tautulli history by `user_id`, which merges for free once linked history is canonicalised there too, and (b) Plex's own per-profile `viewCount`, read with that profile's TOKEN — inherently per-Plex-account and mergeable only by explicitly unioning across the group's tokens in `plex/playlists`. Until then two linked profiles rank identically but can still differ on what is filtered out as already-seen. ⚠️ **Hard invariant for that work**: age gates must NEVER merge — if linked accounts sit in different parental-control tiers their playlists cannot be identical without a link silently relaxing a restriction, so each profile keeps its own ceiling | M | 🟡 **Tautulli half FIXED** — §0.1 #70 (TV) + #71 (movies, delete shield, "watching now"); production-proven at 25-vs-25 with three of four shelves byte-identical. Only the Plex per-profile `viewCount` union remains |

### 4.17 Trakt

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-TRKT-01` | Watchlist auto-pruning *(dup of `GLD-SVC-01`)* | M | 🔵 Open |
| `GLD-TRKT-02` | Endpoint audit vs the three-bug checklist | M | 🔵 Open |
| `GLD-TRKT-03` | Daemon liveness in the run summary | S | 🔵 Open |
| `GLD-TRKT-04` | Adaptive rate budget from headers | M | 🔵 Open |
| `GLD-TRKT-05` | Watchlist review UI | M | 🔵 Open |
| `GLD-TRKT-06` | Trakt list import as acquisition sources | M | 🔵 Open |
| `GLD-TRKT-07` | Token refresh automation + expiry warning | S | 🔵 Open |
| `GLD-TRKT-08` | Reconcile Trakt vs Tautulli history, documented precedence | M | 🔵 Open |
| `GLD-TRKT-09` | Consume or drop `translations` *(dup of `GLD-DMN-09`)* | S | 🔵 Open |
| `GLD-TRKT-10` | Per-endpoint rate accounting | S | 🔵 Open |
| `GLD-TRKT-11` | Backfill detection — flag low enrichment coverage | S | 🔵 Open |

### 4.18 Machine learning

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ML-01` | 🔴 Complete the migration — flat modules into subpackages | M | 🔵 Open |
| `GLD-ML-02` | Extend `_GUARDED_SUBPACKAGES` to six unguarded packages | S | 🔵 Open |
| `GLD-ML-03` | Purity rule: no filesystem writes | M | 🔵 Open |
| `GLD-ML-04` | Enrichment-coverage gate on scoring | M | 🔵 Open |
| `GLD-ML-05` | Determinism test — replay twice, assert identical | S | 🔵 Open |
| `GLD-ML-06` | Ledger schema versioning | S | 🔵 Open |
| `GLD-ML-07` | Score explainer — per-title signal waterfall | M | 🔵 Open |
| `GLD-ML-08` | Challenger promotion pipeline | L | 🔵 Open |
| `GLD-ML-09` | Threshold sandbox | L | 🔵 Open |
| `GLD-ML-10` | Affinity decay | M | 🔵 Open |
| `GLD-ML-11` | Confidence intervals on scores | L | 🔵 Open |
| `GLD-ML-12` | Per-device profile selection consuming the codec matrix | M | 🔵 Open |
| `GLD-ML-13` | Cross-medium next-watch | M | 🔵 Open |
| `GLD-ML-14` | Golden corpus expansion with adversarial cases | S | 🔵 Open |
| `GLD-ML-15` | 🔴 **Resolve duplicate module pairs** — ⚠️ **downgraded, session 5.** Six of the eight are documented re-export shims awaiting `MIGRATION.md` Step 10, not drift. Remaining work: execute Step 10, updating `router_show.py` / `router_movie.py` first (they depend on the `library_classifier` shim's bare-`managers.` fallback) | S | 🔵 Open |
| `GLD-ML-17` | **Document the brain's two-tier structure** — impure orchestration wrappers at root, pure cores in guarded subpackages. Currently undeclared, which makes a deliberate exemption look like a coverage gap *(P-F)* | S | 🔵 Open |
| `GLD-ML-18` | **Verify `machine_learning/transcode_analyzer.py`** — root implementation vs `quality_analytics/transcode_analyzer.py`. Two concerns to confirm: it calls `get_or_generate_cache(..., generator=lambda: ...)` where the documented parameter is `generator_function`, and passes `expiration_time=24` where the convention elsewhere is seconds (24s vs an apparent intent of 24h) | S | 🔵 Open |
| `GLD-ML-16` | **Stamp enrichment coverage on every score** so a partial score is distinguishable from a genuinely low one. `GLD-ML-04` gates; this makes the gap *visible* on the score itself *(P-C)* | S | 🔵 Open |

### 4.19 Radarr

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-RAD-01` | 🔴 **Complete multi-instance tier routing** — add-if-absent/tag + safe make-before-break migration. Phases 1–2 built; a 4K-worthy upgrade currently lands on `standard` with no path to `ultra` | L | 🔵 Open |
| `GLD-RAD-02` | Wire `router_movie.py` to `categorized_instance` for new adds | M | 🔵 Open |
| `GLD-RAD-03` | Extend `RadarrQualitySelector` to pick the instance from target tier first | M | 🔵 Open |
| `GLD-RAD-04` | Signal on mis-tiered placement *(P-D)* | S | 🔵 Open |
| `GLD-RAD-05` | Signal when `movieRootFolders` is empty and buckets are discarded *(P-D)* | S | 🔵 Open |
| `GLD-RAD-06` | Prefetch outcome in the run summary — hit/miss/timeout/stale-reuse *(P-D)* | S | 🔵 Open |
| `GLD-RAD-07` | Profile-drift report instead of silent overwrite *(P-D)* | S | 🔵 Open |
| `GLD-RAD-08` | Incremental library fetch via cursor, removing the ~39s cold cost | M | 🔵 Open |
| `GLD-RAD-09` | Per-instance failure isolation | M | 🔵 Open |
| `GLD-RAD-10` | Document `radarr/api/` — only subfolder with no README | S | 🔵 Open |
| `GLD-RAD-33` | ✅ **Protocol-aware release picker** — the step-down picker was choosing releases on protocols the deployment cannot actually download, so a "successful" pick could never import. One `downloadclient` probe per pass, lazily memoised per instance, gives `_pick_stepdown_release` an `allowed_protocols` set to refuse against. Row filed **retroactively**: the ID was cited in `radarr/quality/space_pressure.py` directly above the probe and appeared in NO table — found 2026-08-20 while tracing the fetch-grade-push primitive. **Third instance of this pattern** after `GLD-SON-18`/`-19`; the tell is identical both times — an ID written into a code comment is a claim that a register row exists, and nothing verifies that claim. A grep of every `GLD-` cited in code against the register would have caught all three, and should be part of the §8 sweep | S | ✅ **Fixed** (retroactive row) |
| `GLD-RAD-34` | 🔵 **Wire the size ceiling into Radarr** — `quality_caps.plan_caps` (`GLD-SIZ-13`) is built and tested but nothing calls it. `radarr/cache/quality.py` already FETCHES `qualitydefinition` every run and caches it; the ONLY consumer is `log_quality_summary`, which prints `• Definitions: N` — **P-A**: the exact data a grab-time cap needs, pulled every run and used for a count. No PUT exists anywhere in the three Radarr quality managers (checked). Needs: measured stats from the live movie parquet, a proposals/skipped diff table, `effective_dry_run` + the backup gate (this writes *arr CONFIGURATION, a different class of change from a grab), and the PUT. Sonarr is a separate pass — same endpoint and model, but per-episode runtimes change the arithmetic and `GLD-SON-01` says its config push does not run at all | M | 🔵 Open |
| `GLD-RAD-35` | ✅ **Universe tag audit compared a BROAD set against ONE exact string** — `"Mismatch: 6 tagged in Radarr vs 0 in Parquet. Run movie_files.refresh() to sync"` fired every run and always would have. The Radarr side matches three tag shapes (`keep-universe`, `keep-universe-*`, `universe`) while the Parquet side filtered `keep_policy == "universe"` EXACTLY — and a named tag like `keep-universe-mcu` is stamped under its own label (`mcu`, `xmen`, …), so a library whose universes are all named could never match. Confirmed against the same run's playlists, which render `mcu universe` / `xmen universe` on real movies: the Parquet knew perfectly well. Now broad vs broad, and the warning names the actual policies found (`mcu=3, xmen=2, …`) so a real mismatch is diagnosable instead of just numeric. ⚠️ The remedy it advised was also wrong — a refresh could never change the number — which is the worse half: advice that cannot work trains an operator to ignore the diagnostic | S | ✅ **Fixed** — §0.1 #69 |
| `GLD-RAD-36` | ✅ **The exhaustive step-down's new pass-ledger read was the only unguarded `global_cache` access in `space_pressure.py`** — every other read in the file checks first (`if not self.global_cache` in `_affinity_inputs`, `if self.global_cache` in the device/transcode block). A manager constructed without a cache crashed `run_downgrades` with `'NoneType' object has no attribute 'get'` before any downgrade was planned, taking the whole pass down rather than the rate limit. Guarded on `hasattr(..., 'get')` rather than truthiness, because the stub that also hit this is truthy and simply has no `get`. With no cache there is no persisted stamp, which `pass_allowed` already treats as a first run, so the pass proceeds — but it now WARNS, since a cacheless run is exactly when an unthrottled exhaustive pass would go unnoticed | S | ✅ **Fixed** — §0.1 #73 |

### 4.20 Sonarr

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SON-01` | 🔴 **Resolve `SonarrQualityManager` + `SonarrSyncManager`** — both in `full_components`, both absent from `component_dependencies`, so neither loads/prepares/runs. `sync` pushes custom formats/naming/folders/tags into Sonarr and Radarr's equivalent *does* run *(P-A)* | M | 🔵 Open |
| `GLD-SON-02` | Signal on false `no_results` — indexer timeout vs genuine miss *(P-C)* | M | 🔵 Open |
| `GLD-SON-03` | `dry_run` propagation assertion at construction | S | 🔵 Open |
| `GLD-SON-04` | Adopt Sonarr's `prepare()` pre-marking across other managers | S | 🔵 Open |
| `GLD-SON-05` | Pilot outcome reporting — climbed vs deleted | S | 🔵 Open |
| `GLD-SON-06` | Per-series watched-definition for prune | M | 🔵 Open |
| `GLD-SON-07` | Partial-APPLY drift signal *(P-D)* | S | 🔵 Open |
| `GLD-SON-08` | Document `sonarr/api/` — only subfolder with no README | S | 🔵 Open |
| `GLD-SON-09` | Component load timing in the prepare summary | S | 🔵 Open |
| `GLD-SON-10` | Consolidate duplicate `size_anomaly` tests in `cache/` and `quality/` *(P-E)* | S | 🔵 Open |
| `GLD-SON-11` | 🔴 **`critical_keys` contradiction** — `"quality"` declared critical while filtered out of `all_component_classes`, so it can never load *(P-B)* | S | 🔵 Open |
| `GLD-SON-12` | Assert `critical_keys ⊆ all_component_classes` at construction, for every manager | S | 🔵 Open |
| `GLD-SON-13` | ✅ **Bloat re-grab had no detector and no bound** — the search is accepted by Sonarr, grabs nothing (a SMALLER file is never an *upgrade*), and re-fires forever. Measured across two consecutive runs: 496 anomalies, 232 oversized, ~433 GB "reclaimable", the same 38 Dragon Ball Z episodes searched both times, every figure identical *(P-D)* | S | ✅ **Fixed** — attempt ledger, §0.1 #57 |
| `GLD-SON-14` | ✅ **Broadcast captures were routed to re-grab instead of rescan** — `_JUNK_OR_SD_GRADES` stopped at `Bluray-576p`, so `HDTV-1080p` at 5x its expected bitrate went down the re-grab path. Broadcast bitrate is capped far below disc bitrate, so that file is a mis-GRADED disc source, and only a rescan can fix it. 53 of 54 live searches were HDTV/SD-graded 4:3 cartoons | S | ✅ **Fixed** — §0.1 #57 |
| `GLD-SON-15` | 🟡 **The 720p WEB tiers are probably the same mis-grade shape** — `WEBRip-720p` at x3.3-4.1 (Dracula 2020, Unbelievable) looks identical to the HDTV case, but the "broadcast bitrate is capped" argument does not transfer to a web source, so it was left out of `GLD-SON-14` rather than folded in on a weaker rationale. Decide with the post-rescan data | S | 🔵 Open |
| `GLD-SON-16` | 🟡 **Verify the rescan actually re-grades** — `GLD-SON-14` assumes `RefreshSeries` re-reads mediainfo and corrects the quality. If Sonarr keeps the original grade, those files leave the re-grab path and gain nothing, and the anomaly count stays at 496 with no action at all. First post-change run answers it | S | 🔵 Open |
| `GLD-SON-17` | **The size model is calibrated on live-action rates** — 20 of 25 top anomaly rows are one show (Dragon Ball Z), which is a content-class signal, not 20 independent defects. Operator has explicitly DECLINED excluding remux/anime for now; recorded so the concentration is not re-diagnosed as a new finding | M | ⏸ Declined |
| `GLD-SON-18` | ✅ **Bloat re-grab converted DELETE-then-search → SEARCH-ONLY** — the armed path was `DELETE episodefile/{fid}` then `EpisodeSearch`, which is what made the orphan guard necessary: Sonarr will not search for an unmonitored episode, so the command was accepted, did nothing, and left the file deleted with no replacement. Search-only removes the hazard instead of guarding it — the bloated file is KEPT until a replacement imports (Sonarr retires it into the recycle bin on import, the same path every upgrade takes and the one `bin_forecast` already accounts for), so there is never a window with no file. Unmonitored episodes became SEARCHABLE (the search is inert, not destructive) and are counted separately because inert is the *expected* outcome there — it tells the operator the file needs `monitored: true` before the bloat can correct | S | ✅ **Fixed** (row filed retroactively — the ID was cited in code and §0.1 #57 with no table row) |
| `GLD-SON-19` | ✅ **UNRESOLVED and UNMONITORED were one counter and one message** — 38 skips could be 38 policy states, 38 broken coord mappings, or any mix, and the log could not say which. *Unresolved* (the file backs a coord Sonarr cannot resolve — a search has nothing to look FOR) is now reported distinctly from *unmonitored* (exists, not tracked — searched inert under `GLD-SON-18`) | S | ✅ **Fixed** (row filed retroactively) |
| `GLD-SON-20` | 🔴→✅ **JIT restore pass: wrote during dry runs, counted rejected PUTs as successes, and destroyed the retry evidence** — found in the 2026-08-20 14:14 run, which reported `restored: 4, failed: 0` while Sonarr returned **400 Bad Request** to all four (`$.quality.quality.resolution: The JSON value could not be converted to System.Int32`). FOUR defects in one method: (1) **dry-run violation** — `run_jit_quality_restores` checked neither `dry_run` nor the backup gate and issued live PUTs on a `dry_run=True` run, the one promise the flag exists to make; (2) **P-D** — `_make_request` LOGS HTTP failures and returns the fallback rather than raising, so the `except Exception` guarding the call could never fire and the result was ignored outright: `restored` incremented unconditionally and a success line was logged for a write the server refused; (3) **the failure destroyed its own remedy** — the same unconditional path cleared `pre_upgrade_quality` and `upgraded_for_watching` and saved the parquet, so the cache asserted a rollback that never happened AND discarded the only snapshot a retry needs (unrecoverable from cache); (4) the pilot-floor SKIP branch also incremented `restored`, mislabelling a deliberate no-op as a rollback. Root cause of the 400 itself: the snapshot's `resolution` is JSON off the parquet and can arrive as `"720"`/`720.0`/numpy int — now coerced with `int(float(...))`, falling back to the CURRENT value rather than sending junk. Fixed: `effective_dry_run` gate (withheld rollbacks counted as `would-restore`, snapshots kept); falsy PUT result = FAILED with a warning and BOTH the snapshot and the JIT flag left intact for next run; `skipped-floor` split out; `checked` surfaced (it was tallied every pass and never displayed — a small P-A). Verified by exec-extracting the REAL fixed method and replaying run-3's exact shape: 4 rejected PUTs → `restored 0 / failed 4` with snapshots INTACT; dry run → **0 PUTs issued**, 4 would-restore, snapshots intact; happy path → 4 restored, snapshots cleared, quality rolled back; resolution coercion checked across `"720"`/`720.0`/`"720p"`/`None` | S | ✅ **Fixed** — §0.1 #63 |
| `GLD-SON-21` | 🔵 **Wire the size ceiling into Sonarr** — the Sonarr half of `GLD-SIZ-13`. `sonarr/cache/quality.py` has the SAME P-A as Radarr: `refresh_quality_definitions` fetches and caches `qualitydefinition` every run, and the only consumer is `log_quality_summary` printing `- Quality Definitions: N entries`. The arithmetic needs no change — MiB/min is a bitrate, so an episode and a feature are priced identically; only the source parquet (`episode_files` vs `movie_files`) and the diff table's runtime label (~42m vs ~162m) differ, and `push_caps` already takes both as arguments. Write primitive is the shared `BaseInstanceManager._make_request`, same as Radarr. ⚠️ Two Sonarr-specific findings while tracing it: `sonarr/api/client.py` is a **ZERO-BYTE file** (the Sonarr API surface is thinner than Radarr's, consistent with `GLD-SON-01`'s dead config push), and the two services' definition cache keys have already drifted in FORMAT — `radarr.{i}.quality.definitions` vs `sonarr/{i}/quality_definitions.json` — which is why the diff table and write path were deliberately built ONCE in the brain rather than per-service | M | 🔵 Open |
| `GLD-SON-22` | 🔵 **DECISION: does the Sonarr bloat re-grab delete first, or push the guid in place?** — `GLD-SON-18` chose *keep the file, search only*, which is safe (no window with nothing on disk) but is very likely INERT for bloat: a right-sized file is not an *upgrade* at the same quality tier, and `GLD-RAD-31`'s own comment records the empirical proof from 2026-08-07 — with the delete silently skipped, *"the grab left to die on cutoff-met"*. So the safe path may be structurally incapable of ever succeeding. Radarr's answer (`space_pressure`) is verify-a-smaller-release-exists → DELETE → grab by guid, with real hardening already built (no pick ⇒ no delete; no `movie_file_id` ⇒ no delete and no grab; per-title backoff because 31 titles have no smaller encode at any indexer) — but its comments also record a `POST /release` 404 that left a file deleted with nothing to replace it. Porting Radarr's shape to Sonarr means importing that no-file window deliberately. **Third option to test FIRST, one API call**: whether `POST /release` succeeds on Sonarr WITHOUT deleting — a pushed guid is the interactive override path, so it may bypass the cutoff-met blocker that kills a blind search. If it does, Sonarr gets the effective behaviour AND keeps `GLD-SON-18`'s no-window guarantee, and no port is needed. Blocked on an armed run (backup gate keeps disarming on Radarr startup connect failure) | M | 🔵 **Open — operator decision, test first** |
| `GLD-SON-23` | ✅ **The TV step-down counted a FAILED episode-file delete as realized** — `_realize_stepdown_files` wrapped its `DELETE episodefile/{fid}` in `try/except` and checked nothing else, but `_make_request` SWALLOWS a failed call: it logs a warning and returns the fallback rather than raising, so the `except` was **unreachable for every HTTP failure** — the identical P-A shape `universe.py` documents at its own delete ("CHECK THE RETURN VALUE, do not rely on an exception"). A 500 or a 404 therefore incremented `realized`, added the file's bytes to `realized_reclaim_gb` — which feeds the exhaustive loop's stop condition, so the pass believed it had freed space it had not — and fired a replacement grab at a file that still exists, which Sonarr rejects as cutoff-met: precisely the blocker the delete exists to remove. Only detectable at all because the base contract now returns True on a successful DELETE (§0.1 #74); under the old contract success and swallowed failure were both `None` and no check could have worked. Now checks the result, keeps the file and skips the grab on failure — same gate radarr's universe realize already had, so the two passes cannot disagree | S | ✅ **Fixed** — §0.1 #74 |
| `GLD-SON-24` | ✅ **`empty_search` counted, named per-title, and left OUT of the summary line** — an omitted bucket makes a summary read as a no-op, the same shape as `GLD-ACQS-20` ("0 refused" while 122 were capped). `legacy_regrab` deliberately refuses to record a zero-release indexer response as `no_release` — per `GLD-SON-02` that costs a 14-day cooldown, and 814 of 881 files were once benched that way on evidence never gathered — so the bucket is correct and only its reporting was missing. Now printed, with a WARNING when `empty_search == checked` naming indexer health as the likely cause. 🔴 **THE CAUSAL CLAIM ORIGINALLY FILED HERE WAS WRONG AND IS RETRACTED.** This row asserted that the 2026-08-20 run's `0 grabbed, 0 no modern release, 0 failed (of 69 checked)` meant all 69 landed in `empty_search` — that the indexer returned zero releases for every one. Measured against the live library: `empty_search == 0` and **zero searches were issued at all**. The real cause is `GLD-SON-25` (stale `episode_file_id` pointers, silent unresolved-return). **The tell was in the log I had already read and did not use**: `22:21:17 → 22:21:18`, one second for 69 files, when a single interactive search takes four — an arithmetic impossibility that rules out every explanation involving searches actually running. I also read `legacy_regrab._one` from its `empty_search` branch DOWNWARD and never traced the episode-resolution step above it, which is where the returns actually happened. Second wrong mechanism filed for one symptom (the first was "a fourth unchecked-`_make_request`"), and the row's own "registered against a WRONG prediction" caveat was itself describing the wrong wrong-prediction. The reporting fix stands on its own merits; the attribution did not | S | ✅ **Fixed** (reporting) · 🔴 **attribution retracted** — §0.1 #76, see `GLD-SON-25` |
| `GLD-SON-25` | ✅ **Legacy re-grab resolved rows by a POINTER, so 69 of 70 candidates silently evaporated** — `_episode()` looked an episode up by `episode_file_id` alone. That is a HANDLE, invalidated by every lane that replaces a file (legacy re-grab, JIT upgrade, size-anomaly remediation, space-pressure step-down), and the parquet rebuilds on a per-series freshness clock (`GLD-CACHE-14`) — so between a replacement and that series' next refresh the row carries a dead id and can NEVER resolve. `_one()` then returned bare, incrementing no counter, which is why the pass reported `0 grabbed, 0 no modern release, 0 failed (of 69 checked)`: `checked` counts loop iterations, not searches. **Measured on the live library: 1 of 70 eligible rows resolved, 69 were stale, and 67 of those still had a file — the id had simply changed** (Dragon Ball Kai S01E30: 44437 in parquet, 64041 in Sonarr), and the replacements are ALREADY x264/Bluray-720p, i.e. the work was done. Self-perpetuating no-op: the silent drop meant no ledger entry, so they stayed eligible forever. Now resolves by `(series, season, episode)` — the identity Sonarr answers authoritatively — and records `superseded` / `episode_no_file` / `unresolved`, of which only `superseded` persists (a dead pointer is retired; a transient fetch failure lands in `unresolved` and must NOT bench a file for 14 days, per `GLD-SON-02`). **The tell that named it without reading any code**: `22:21:17 → 22:21:18`, one second for 69 files, when a single interactive search takes four | M | ✅ **Fixed** — §0.1 #76 |
| `GLD-SON-26` | ✅ **A series that can never be satisfied consumed the JIT search budget, every run, forever** — measured over one 298-minute daemon window: 146 step-downs produced 14 grabs, at ~112s per profile-flip-and-search, and **116 of those 146 went to FIVE series that grabbed nothing** (Johnny Bravo 37 steps, Attack on Titan 22, Ted Lasso 21, Abbott & Costello 18, TMNT 18). Each ended `queued for retry next run`; `_reconcile_failed_jit` then reset their episode flags and DELETED its ledger key, so the next run began with no memory — same five series, same three hours, indefinitely. Two fixes. **(1) A step budget** in `jit_search`: stop after `MAX_CONSECUTIVE_MISSES` (4) consecutive misses within a ladder; the tail of a ladder is where releases are rarest, so the marginal profile is both the least likely to grab and exactly as expensive as the first. Any grab RESETS the counter, so a productive ladder is never truncated (verified: barren 37-profile ladder 37→4 searches; a grab at step 2 yields 7, not 4). **(2) A demotion queue** (`space/jit_backoff`): a series that exhausts its ladder sorts to the BACK next run, and any grab clears it. ⚠️ **ORDERING, NOT EXCLUSION, and the distinction is the whole design.** `legacy_regrab` benches for 14 days; `GLD-SON-02` records that going wrong at 814-of-881 scale because a disabled or rate-limited indexer is indistinguishable from "no release exists". Those five titles are old/obscure/anime — exactly what lives on the 13 torrent indexers currently DISABLED here — so a bench would survive re-enabling them, while a demotion self-heals on the first grab with no operator action. **Deliberately NOT configurable**: a knob here would be a knob to re-enable the defect, and it is safe to hard-code precisely because both limits degrade ORDER rather than eligibility. Ledger separation documented in both `_record_jit_backoff` and `_reconcile_failed_jit`: `jit/failed_upgrades` answers "is this EPISODE still owed a grab?" (yes → retry), `jit/backoff` answers "how often has this SERIES come up empty?" (→ sort it last); merging them would wipe the counters every run and restore the loop | M | ✅ **Fixed** — §0.1 #77 · 🔵 daemon side outstanding |

### 4.21 Support

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SUP-01` | Execute `MIGRATION.md` Step 10 — delete the four shims, updating the standalone routers first *(P-E)* | S | 🔵 Open |
| `GLD-SUP-02` | Consistent `dry_run` across `tools/` — destructive tools gate inconsistently | M | 🔵 Open |
| `GLD-SUP-03` | Declare each tool's execution context (repo-root vs standalone) | S | 🔵 Open |
| `GLD-SUP-04` | TRaSH data freshness marker + stale warning *(P-D)* | S | 🔵 Open |
| `GLD-SUP-05` | Assert `_pre_apply_snapshot` written before any profile sync *(P-D)* | S | 🔵 Open |
| `GLD-SUP-06` | Cache/log root size reporting in the run summary | S | 🔵 Open |
| `GLD-SUP-07` | Tool catalogue with purpose + safety class (read-only / mutating / destructive) | S | 🔵 Open |
| `GLD-SUP-08` | Retire or document `plex_stresstest.py` | S | 🔵 Open |
| `GLD-SUP-09` | Config backup rotation — `.bak-<timestamp>` files accumulate unbounded | S | 🔵 Open |
| `GLD-SUP-10` | Shim-removal CI guard forbidding old import paths | S | 🔵 Open |

### 4.22 ML contracts

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CON-01` | Implement the re-exports `contracts/__init__.py` promises, or correct the docstring | S | 🔵 Open |
| `GLD-CON-02` | Document the two missing-data strategies (renormalise-out vs contribute-zero) as an explicit contract note | S | 🔵 Open |
| `GLD-CON-03` | Coverage field on feature rows — record which signal groups were populated *(P-C)* | M | 🔵 Open |
| `GLD-CON-04` | Runtime shape assertions in tests — annotations are documentation only | S | 🔵 Open |
| `GLD-CON-05` | Document the aggregation statistic per `ShowFeatureRow` field | S | 🔵 Open |
| `GLD-CON-06` | `slots=True` on the dataclasses — ~45-field rows per title at library scale | S | 🔵 Open |
| `GLD-CON-07` | Contract versioning so a shape change is detectable by replay | M | 🔵 Open |
| `GLD-CON-08` | Better error on unknown kwargs at construction | S | 🔵 Open |

### 4.23 ML scoring

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SCO-01` | 🔴 **Resolve the "4K ≥ 70" statement** — matches neither the ladder's 38 nor the gates' 75; then correct every doc repeating it *(P-G)* | S | 🔵 Open |
| `GLD-SCO-02` | Inject `now` into F3/G4 instead of reading the clock — makes replay byte-exact | S | 🔵 Open |
| `GLD-SCO-03` | Stamp the ladder/axis version on every ledger row | S | 🔵 Open |
| `GLD-SCO-04` | Group-level renormalisation, extending `device_fit.py`'s pattern *(P-C)* | M | 🔵 Open |
| `GLD-SCO-05` | Reconcile the 4K rung with observed curation (34 admitted vs 67 kept) or surface the gap in the ladder table | S | 🔵 Open |
| `GLD-SCO-06` | Score explainer consuming the existing free breakdown dict | M | 🔵 Open |
| `GLD-SCO-07` | Resolve the `A1`/`C2` namespace collision between scoring groups and enhancement batches | S | 🔵 Open |
| `GLD-SCO-08` | Adversarial golden-corpus cases — all-penalty, all-missing, stub, single-signal | S | 🔵 Open |
| `GLD-SCO-09` | Publish score distribution + rung occupancy each run | S | 🔵 Open |
| `GLD-SCO-10` | Per-group contribution stats — reveal any effectively inert group | S | 🔵 Open |

### 4.24 ML space

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SPA-01` | Warn when the band collapses to `(T, T)` — `total_gb` unreadable silently removes the anti-oscillation guarantee *(P-D)* | S | 🔵 Open |
| `GLD-SPA-02` | Report regrab-cap saturation — how many items deferred | S | 🔵 Open |
| `GLD-SPA-03` | Warn on a non-truthy consent env var overriding a config `true` *(P-D)* | S | 🔵 Open |
| `GLD-SPA-04` | Space forecast — at current growth, when is the floor reached? | M | 🔵 Open |
| `GLD-SPA-05` | Downgrade-vs-delete accounting — GB reclaimed by each path | S | 🔵 Open |
| `GLD-SPA-06` | Per-mount bands rather than one global floor | M | 🔵 Open |
| `GLD-SPA-07` | Document `universe.DEFAULT_DOWNGRADE_GB` (10) as a named exception, not only inline | S | 🔵 Open |
| `GLD-SPA-08` | Dry-run space projection — netted, using the signed contract fields | S | 🔵 Open |
| `GLD-SPA-09` | Surface tightness `t` in run stats | S | 🔵 Open |
| `GLD-SPA-10` | Confirm regrab-cap deferrals converge rather than starving | M | 🔵 Open |

### 4.25 ML lifecycle

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-LIF-01` | **Series-level watched definition** — the per-play bar exists, the per-series rollup does not. The single remaining gap in the "watched" story; blocks auto-prune | M | 🔵 Open |
| `GLD-LIF-02` | Detect + report a changed Tautulli watched threshold — it silently re-verdicts the whole library *(P-D)* | S | 🔵 Open |
| `GLD-LIF-03` | Report `'clear'` reason counts per run — which guard protected how many rows | S | 🔵 Open |
| `GLD-LIF-04` | Report stale-mark clears — how many `marked_for_deletion` flags a definition flip cleared | S | 🔵 Open |
| `GLD-LIF-05` | Warn on unrecognised `watched_status` values instead of silently falling through | S | 🔵 Open |
| `GLD-LIF-06` | Enable the percentile grace ramp by default once measured | S | 🔵 Open |
| `GLD-LIF-07` | Reconcile Tautulli and Trakt history with documented precedence | M | 🔵 Open |
| `GLD-LIF-08` | Per-viewer retention visibility — which accounts hold which episodes | S | 🔵 Open |
| `GLD-LIF-09` | Re-measure the sample rate (was 9.8% episodes / 36.2% movies) | S | 🔵 Open |
| `GLD-LIF-10` | Auto-rater confidence — inferred vs declared ratings look alike downstream | M | 🔵 Open |

### 4.26 ML likelihood

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-LIK-01` | 🔴 **Axis-coupling detector** — assert the scoring-axis distribution against a baseline, fail loudly on translation. The last one cost **−98.2% of a quality tier** and was caught only by manual measurement | M | 🔵 Open |
| `GLD-LIK-02` | Assert `affinity_cap < uhd_cutoff` at config load — config can currently break the structural 4K guard | S | 🔵 Open |
| `GLD-LIK-03` | Default `untouched_mode` to `percentile` — structurally immune to axis translation (0.0%/+4.9% vs −98.2%) | S | 🔵 Open |
| `GLD-LIK-04` | Surface the likelihood distribution + branch membership per run | S | 🔵 Open |
| `GLD-LIK-05` | Report `explain_likelihood` attribution counts per branch | S | 🔵 Open |
| `GLD-LIK-06` | Remove or warn on `hd_cutoff` — crossing it has no behavioural consequence | S | 🔵 Open |
| `GLD-LIK-07` | Validate `radarr_quality_ladder` is ascending with existing profile ids | S | 🔵 Open |
| `GLD-LIK-08` | Document the three-boundary re-anchor procedure — currently tribal knowledge in a docstring | S | 🔵 Open |
| `GLD-LIK-09` | Record anchor provenance in the ledger — which `untouched_base` produced a decision | S | 🔵 Open |
| `GLD-LIK-10` | Assert the `affinity_cap` saturation invariant against live data each run | S | 🔵 Open |

### 4.27 ML eval

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-EVA-01` | 🔴 **Watched-threshold divergence** — `eval/replay.py` and `eval/forward.py` default `watched_threshold=0.9` and never import `lifecycle.watched_definition` (production bar 85, preferring Tautulli's `watched_status`). Confirm whether drivers override; if not, the leakage-free baseline reconstructs a household that never existed | S | 🔵 Open |
| `GLD-EVA-02` | Assert eval and production agree on "watched"; print the effective bar | S | 🔵 Open |
| `GLD-EVA-03` | Record snapshot coverage — which forward windows are measurable, which aged out *(P-D)* | S | 🔵 Open |
| `GLD-EVA-04` | Freeze the clock during replay | S | 🔵 Open |
| `GLD-EVA-05` | Stamp axis + anchor version on eval output | S | 🔵 Open |
| `GLD-EVA-06` | Extend forward validation beyond the watchlist — Trakt lists, discovery shelf, next-watch | M | 🔵 Open |
| `GLD-EVA-07` | Replay for **deletion** decisions, not only recommendations | L | 🔵 Open |
| `GLD-EVA-08` | Confidence bands on lift given small n | M | 🔵 Open |
| `GLD-EVA-09` | Scheduled forward-validation snapshots rather than ad-hoc | S | 🔵 Open |
| `GLD-EVA-10` | Publish eval results to the ledger | M | 🔵 Open |

### 4.28 ML ledger

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-LED-01` | 🔴 **Plan snapshot or decision record?** The ledger is three columns on the item's own row. Four registered items (`GLD-SCO-03`, `GLD-LIK-09`, `GLD-EVA-05`, `GLD-EVA-07`) assume an append-only decision record. Settle which, then extend or add a store | M | 🔵 Open |
| `GLD-LED-02` | 🔴 **Replace the bare `except: pass`** in `stamp_universe_plan` — a silently skipped stamp makes the parity oracle silently under-report | S | 🔵 Open |
| `GLD-LED-03` | Report roll-up coverage — instances read vs skipped *(P-D)* | S | 🔵 Open |
| `GLD-LED-04` | Add provenance columns — run id, timestamp, axis version, `untouched_base` | S | 🔵 Open |
| `GLD-LED-05` | Persist the scoring breakdown alongside the plan — it is already computed free | M | 🔵 Open |
| `GLD-LED-06` | Structure `plan_reason` — code + free text, so reasons are filterable | S | 🔵 Open |
| `GLD-LED-07` | Preserve a pre-deletion snapshot row — the only way `GLD-EVA-07` becomes possible | M | 🔵 Open |
| `GLD-LED-08` | Run-over-run history — the missing source for `GLD-WEB-12` and `GLD-CORE-07` | M | 🔵 Open |
| `GLD-LED-09` | Warn when `runtime_minutes` is missing during a universe stamp — silently overstates reclaim | S | 🔵 Open |
| `GLD-LED-10` | Record superseded stamps rather than overwriting | M | 🔵 Open |
| `GLD-LED-11` | Split `first_run_backfill` out of `PlanSummary`, or document the read-only exception | S | 🔵 Open |

### 4.29 ML thresholds

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-THR-01` | 🔴 **Axis-tagged specs + assertion** — `ThresholdSpec` has `service` but no `axis`. Two watchability axes already disagree on delete-eligibility for **354 of 2,151 movies (16.5%)** | S | 🔵 Open |
| `GLD-THR-02` | 🔴 **Axis-drift detector** — the calibrator already fits the live distribution every run; compare moments to the last report and warn. Closes `GLD-LIK-01` at near-zero marginal cost | M | 🔵 Open |
| `GLD-THR-03` | Unify the LEGACY axis onto the persisted `watchability_score` column | L | 🔵 Open |
| `GLD-THR-04` | Surface the shadow grid in the run summary, not only the audit JSON | S | 🔵 Open |
| `GLD-THR-05` | Route `movie_search` (60) once per-tier storage break-evens exist | M | 🔵 Open |
| `GLD-THR-06` | Ladder-shaped derivation for `quality_ladder` — P-bands per rung | L | 🔵 Open |
| `GLD-THR-07` | Likelihood-scale calibrator so `uhd_reconcile` can be derived | M | 🔵 Open |
| `GLD-THR-08` | Re-anchor procedure as a runnable tool, not a comment | S | 🔵 Open |
| `GLD-THR-09` | Assert restore ≥ delete for each hysteresis pair at config load | S | 🔵 Open |
| `GLD-THR-10` | Report cross-axis disagreement count each run | S | 🔵 Open |
| `GLD-THR-11` | Bound `n_pos` confidence in the grid | S | 🔵 Open |

### 4.30 ML labels

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-LAB-01` | 🔴 **Unify the watched bars** — **four** now exist: production 85/`watched_status`, eval 0.9, labels movie 90, labels episode 50. `lifecycle/watched_definition.py` is imported by the two producers and **nothing else in the brain** | M | 🔵 Open |
| `GLD-LAB-02` | 🔴 **Build a `rating_key → series id` map** — shows are joined by normalised title string only; `grandparent_rating_key` is in the history and unusable | M | 🔵 Open |
| `GLD-LAB-03` | Report join coverage — labeled vs dropped per media type *(P-D)* | S | 🔵 Open |
| `GLD-LAB-04` | Detect normalised-title collisions and warn | S | 🔵 Open |
| `GLD-LAB-05` | **Revisit `GLD-ML-02`** — `labels` cannot simply join `_GUARDED_SUBPACKAGES`; `first_run.py` is deliberately impure and its `importlib` load evades an AST guard anyway *(P-F)* | S | 🔵 Open |
| `GLD-LAB-06` | Warn when the backfill produces zero rows — currently looks like success | S | 🔵 Open |
| `GLD-LAB-07` | Report evidence growth per run — labels, matured, positives by source | S | 🔵 Open |
| `GLD-LAB-08` | Allow a second backfill window when the grid cap truncated real history | M | 🔵 Open |
| `GLD-LAB-09` | Use `user_id` for per-viewer labels where evidence supports it | M | 🔵 Open |
| `GLD-LAB-10` | Re-verify the documented cache shapes — field names drift silently | S | 🔵 Open |

### 4.31 ML foundation

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-FND-01` | **Add `foundation` to `_GUARDED_SUBPACKAGES`** — genuinely pure, unlike `labels`, so a safe addition where `GLD-ML-02` as a flat list is not | S | 🔵 Open |
| `GLD-FND-02` | **Adopt it somewhere real** — port one tool to import from `foundation`. An adoption point with zero adopters is an untested hypothesis | S | 🔵 Open |
| `GLD-FND-03` | **Delegate `downgrade_credit`** — the one function that mirrors rather than wraps; kept in step by a test, not by construction *(P-E)* | M | 🔵 Open |
| `GLD-FND-04` | **Assert the documented regime numbers** (~2% prevalence, n≈931, n_pos≪100) against live data and warn on movement — every estimator choice is justified by them | S | 🔵 Open |
| `GLD-FND-05` | Cross-link `MATH_FOUNDATION.md` section anchors from each docstring | S | 🔵 Open |
| `GLD-FND-06` | Guard against the scorers importing `linear_utility` — the docstring warns, nothing enforces | S | 🔵 Open |
| `GLD-FND-07` | Surface at-risk counts and per-bin ECE counts wherever these metrics are reported | S | 🔵 Open |
| `GLD-FND-08` | Document the Stage numbering (1, 1b, 2, 3, 4, 5a, 5b) in one place | S | 🔵 Open |

### 4.32 ML routing

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ROU-01` | 🔴 **Implement `select_instance`** and shim the service callers — the package is a declared stub, so instance-selection *decisions* live in `services/` and one operator tool, violating `managers/DESIGN.md` I3 | M | 🔵 Open |
| `GLD-ROU-02` | **Correct `ARCHITECTURE.md`'s `routing/` row** — the named source `machine_learning/instance_selector.py` does not exist *(P-G, in the repo's own docs)* | S | 🔵 Open |
| `GLD-ROU-03` | Move tier routing out of `router_movie.py` into the run path | M | 🔵 Open |
| `GLD-ROU-04` | Add the no-op single-instance test before implementing | S | 🔵 Open |
| `GLD-ROU-05` | Record guarded-but-empty packages in the purity guard's output — `_GUARDED_SUBPACKAGES` coverage is currently overstated | S | 🔵 Open |
| `GLD-ROU-06` | **Audit `ARCHITECTURE.md`'s full subpackage map against the tree** — two rows already known wrong | S | 🔵 Open |
| `GLD-ROU-07` | ✅ **`reorg_mode` forced a choice between two INDEPENDENT axes** — `same_instance` (content bucket within one instance: kids/anime/standard) and `cross_instance` (resolution tier across two: standard→ultra, actuated by `uhd_reconcile`) are peers, not alternatives. A title can be in the wrong folder AND on the wrong instance. The operator had **both consents armed** and was still running one axis log-only — 121 same-instance misplacements re-planned every run with no path to act. New `all` mode arms both; each axis still demands its OWN consent, so nothing is loosened (tested by disabling each consent under `all` — each stays blocked). Unknown modes still fall back to `log_only`. Also removed the redundant `mode == "same_instance"` in the routing manager's `apply`: `relocation_enabled()` is now the single source of truth, so two places can no longer disagree about which modes actuate | M | ✅ **Fixed** — §0.1 #69 |
| `GLD-ROU-08` | ✅ **The routing log's header read as intent-to-act** — `"==== routing relocation plan ===="` above 121 move-shaped lines, in a mode where this manager moves nothing. The per-instance `log only` marker went to the MAIN log while the plan went to `routing.log`, so the file with the move-shaped lines never said it was inert. Header now states the effective capability, names the two config keys needed to actuate, and cross-references `uhd_reconcile`/`relocation.log` as the other axis | S | ✅ **Fixed** — §0.1 #69 |
| `GLD-ROU-09` | ✅ **A stale test double reported the `allowed_roots` guard as a broken consent gate** — `test_routing_manager`'s fake instance-manager resolved GETs on the instance NAME alone, ignoring the endpoint. `_reorg` makes TWO per instance (`movie`/`series`, then `rootfolder`), so the guard added by `GLD-RT-01` was answered with the ITEM list; those dicts carry no `path`, `allowed_roots` came out EMPTY, and the guard did exactly what it should — downgraded a consented live run to planning-only. Surfaced as `assert 0 == 1` on the PUT count in the two 'should move' tests while every NEGATIVE test stayed green, a signature that reads as a loosened consent gate rather than a double that cannot answer a new question. Double is now endpoint-aware and serves each service its own roots | S | ✅ **Fixed** — §0.1 #72 |
| `GLD-ROU-10` | 🔴 **`reorg_mode: all` has been armed for hours and the router has never run — a DIFFERENT gate stops it on line one.** `RoutingManager.run()` opens with `if not self._routing.get("configured"): return`, before it ever reads `reorg_mode`. This install has `routing.configured: false`, so the operator flip from `cross_instance` to `all` — with `relocation_consent`, `cross_instance_move_consent` and `cross_instance_dedup_consent` all true and `dry_run: false` — changed nothing: two full runs since produced **zero** routing output, and `relocation.log` was last written 2026-08-19 (its entries are all `standard -> ultra` retunes/dedups, which `uhd_reconcile` actuates on a different path and which were therefore never gated by this). **The 121 same-instance misplacements have been dark the entire time, not merely unactuated.** ⚠️ The gate itself is correct — a never-onboarded install must do nothing — but it was SILENT, and that was the defect: an operator who sets `reorg_mode`, grants both consents and disables dry-run has stated an intention the code then declined without a word. Every other refusal on this path says so (`log_only` logs the plan; a consent miss logs the plan and names the missing consent). Fixed: when `configured` is false but `relocation_enabled()` is true, warn ONCE naming the key — reusing `relocation_enabled` rather than re-deriving the condition, so the warning can never drift from the gate it describes. A genuinely un-onboarded install with no consents set stays silent, exactly as before. **Operator subsequently set `configured: true` with `reorg_mode: log_only`, and the router produced its first plan: 123 misplacements (29 movies, 94 shows), all within-root** | S | ✅ **Fixed** — §0.1 #79 |
| `GLD-ROU-11` | ✅ **The kids age gate was a hard-coded 11 the router never passed** — `classify_movie`/`classify_show` both accept `kids_age_max` (the Common Sense Media ceiling: an age OVER it demotes a title OUT of the kids bucket, while a low or absent age never promotes one IN — *"never trust Common Sense alone"*, Star Trek: DS9 is CSM ~10 and is an adult drama). `RoutingManager._classifier` called neither with it, so the single most consequential number in kids routing sat at the classifier's default, invisible in config and unchangeable without editing `library_classifier.py`. Surfaced when a `log_only` plan demoted five titles out of `movies/kids` (Monster High, The Mighty Ducks, Archie, A Hollywood Hounds Christmas, +1) and the operator could not see, let alone tune, the number that did it. Now read from **`plex.playlists.kids_age_max`**, added to the onboarding schema beside `profile_ages` — the same household question, asked of the parent who is already setting up their children's profiles — with a top-level `kidsAgeMax` honoured as an alias for hand-edited configs. ⚠️ **Bad values are REFUSED, not clamped** (range 2-17, else default 11 + WARNING). Clamping is the conventional choice and is the wrong failure direction on a parental control: `25` typed for `12` would clamp to 17 and silently WIDEN the gate, putting age-inappropriate titles in the kids library. An unusable value must not be quietly made usable. ⚠️ Two self-caught defects while wiring it: the new reader was inserted ABOVE `_movie_ages`/`_show_ages`, orphaning both CSM caches behind a `return`; and the onboarding key was first written at `plex.playlists.kids_age_max` while the reader looked only at top-level `kidsAgeMax` — a value written and never consumed (P-A), which on a parental control fails silently and looks exactly like the setting having no effect. Verified: `py_compile`; the real reader exec-extracted and replayed across 11 config shapes; suite 3065 unchanged | S | ✅ **Fixed** — §0.1 #79 |
| `GLD-ROU-12` | ✅ **Two classification rules the operator's own routing plan exposed.** Reviewing the first-ever `log_only` plan (123 misplacements) turned up two wrong-looking clusters, both traceable to precedence rather than to the genre lists. **(1) NEWS beats reality.** Widening `realityGenres` back to its defaults correctly caught genuine talk/game shows (`Maury`, `Steve Wilkos`, `Supermarket Sweep`, `What's My Line?`, `House of Games`) — but TVDB tags news magazines and satire with the SAME `Talk Show` genre, so `20/20`, `Axios`, `You Can't Ask That` and 11 others were swept from `tv/documentaries` INTO `tv/reality`. Reality is checked before documentary, so anything tagged both landed in reality. New `newsGenres` set checked AHEAD of reality routes news to the DOCUMENTARY bucket — kept as its own set rather than folded into `documentaryGenres` because the two do different jobs: documentary genres COMPETE (after reality), news WINS (before it). Deliberately not its own library — operator: *"i dont think we have enough news genres to warrant a news library"*, and a four-item folder is worse than none. **(2) A kids NETWORK beats a format tag.** `nonKidsGenres` contains reality/game show/talk show, and that veto also blocked the kids-NETWORK route — so `Take Two with Phineas and Ferb` (Disney Channel, tagged Talk Show) and `Crashbox` (HBO Family, Game Show) routed to REALITY, away from the children they are made for. The veto exists to stop a GENERAL network's cooking/talk output being called kids; a children's channel is the one case where a format tag carries no such risk, because it does not broadcast adult content. Veto lifted for that route only — the guards that speak to AUDIENCE (adult certificate, CSM over the ceiling) still refuse, and the same tags on a general network still take the veto. ⚠️ **A deliberate POLICY REVERSAL, recorded as one**: `test_show_kids_network_is_gated` asserted `_show(["Reality"], network="Nickelodeon") == "reality"` with the comment *"lifestyle veto wins"*. Split into two tests so the audience-vs-format distinction cannot drift back, with the two real titles in the docstring. Six networks added (`hbo family`, `abc kids`, `cbc kids`, `noggin`, `tiny pop`, `starz kids`); ⚠️ **`pop` and `nick at nite` deliberately REFUSED** — matching is `tok in netw`, a SUBSTRING test, so a three-letter entry would match any network containing it, and Nick at Nite is Nickelodeon's ADULT rerun block. Verified: 13/13 news precedence + 11/11 network override against the real classifier, 32/32 in `test_library_classifier`, suite 3065 → 3066 | S | ✅ **Fixed** — §0.1 #80 |
| `GLD-ROU-13` | ✅ **The relocation axis executed for the first time — 235/235.** `reorg_mode: same_instance` with `configured: true` and both consents, after `GLD-ROU-10` revealed the router had never run at all. Radarr 31 (16→kids, 6→standard, 9→anime); Sonarr 204 (102→reality, 35→documentaries, 16→kids, 7→series, 6→anime, 38 seriesType-only). Every line `SUCCESS`, and the total reconciles exactly against the plan the operator had reviewed — which is the point of `log_only` existing as a separate mode. **This is also the production proof of `GLD-ROU-12`**: the 102 reality moves include the talk/game shows the widened `realityGenres` was meant to catch, and the 6 `reality→documentaries` are the news programmes the new `newsGenres` precedence rescued (`20/20`, `Axios` correctly stayed put this time). ⚠️ **I reported "nothing moved" before checking properly**: I grepped `routing.log` for `moved` / `applied` / `PUT`, found none, and said so. The log says **`relocated`**, and `routing.log` is the PLAN file written before actuation — the SUCCESS lines were in `default.log` all along. Eighth misread of the session, same shape as the truncating-grep cross-root claim: I searched for the word I expected instead of reading what the code emits | S | ✅ **Done** — §0.1 #82 |

### 4.33 ML features

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-FEA-01` | 🔴 **Implement `build_episode_feature_row`** — `EpisodeFeatureRow` is defined in `contracts/` and built by **nothing** in the brain; episode field reads stay inline in `sonarr/cache/episode_files`, including on the destructive `build_delete_candidates` path. ✅ **Sessions 44–45 — scope now precise.** Its **19 fields are a strict subset** of `SCHEMA_COLUMNS`, and it carries **none** of the Group D transcode inputs the movie/show rows do — by design (*"carries the broadcast series score"*; episodes are not scored, series are). So the adapter is **narrow**: identity + lifecycle + broadcast score. But **every one of the 19 feeds a delete/keep decision**, and the Parquet gives `NaN` where the contract gives typed defaults — so the gap lands exactly on `build_delete_candidates` | M | 🔵 Open |
| `GLD-FEA-02` | Implement `build_watched_set` — assembly still in `radarr/orchestration` (~L350, ~L485) | M | 🔵 Open |
| `GLD-FEA-03` | 🔴 **Route `completion_stats`' `>= 90` through `lifecycle.watched_definition`** — the **fifth** watched bar, and the second one counting *episodes* (labels uses 50) | S | 🔵 Open |
| `GLD-FEA-04` | ❓ Confirm whether `space_pressure._score_row` delegates here or computes independently — if both, that is a **third** watchability path | S | 🔵 Open |
| `GLD-FEA-05` | Warn on malformed `genres` instead of silently yielding `[]` *(P-C)* | S | 🔵 Open |
| `GLD-FEA-06` | Coverage flags on the built row — the natural implementation point for `GLD-CON-03` | M | 🔵 Open |
| `GLD-FEA-07` | Assert the Parquet schema the adapter expects; fail loudly on a rename | S | 🔵 Open |
| `GLD-FEA-08` | Property-test the coercers against real null shapes (`NaN`, `None`, `pd.NA`, `""`) | S | 🔵 Open |
| `GLD-FEA-09` | Document the show-side aggregation beside `contracts/DESIGN.md` §3.4's statistic table | S | 🔵 Open |

### 4.34 ML affinity

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-AFF-01` | 🟢 **Choose and set `half_life_days`** — temporal decay is **already built and tested**; only the value is missing. **Closes `GLD-TAUT-10` and `GLD-ML-10`, both logged as unbuilt** | S | 🔵 Open |
| `GLD-AFF-02` | ❓ Confirm per-user join parity between `per_user_affinity` and `per_user_platform_usage` — the latter claims to mirror the former "exactly"; the former's docstring describes a simpler join. If they differ, device weights attach to the wrong user | S | 🔵 Open |
| `GLD-AFF-03` | 🔴 **Reconcile `group_completion`'s 0.9 / 0.7 grace with `watched_definition`'s 85** — the **sixth** bar, and the most principled of the six. Consider adopting the grace model rather than collapsing onto the single bar | M | 🔵 Open |
| `GLD-AFF-04` | Distinguish "user omitted" from "user watched nothing" in per-user output *(P-C)* | S | 🔵 Open |
| `GLD-AFF-05` | Report affinity coverage — entries matched vs unmatched per source *(P-D)* | S | 🔵 Open |
| `GLD-AFF-06` | **Document the byte-identical opt-in pattern** in `DOCS_CONVENTIONS.md` — four instances now follow it; a genuine strength currently visible only per-module | S | 🔵 Open |
| `GLD-AFF-07` | Decay sensitivity report — how the top-20 ranking shifts across candidate half-lives, **before** committing | M | 🔵 Open |
| `GLD-AFF-08` | Check whether `format_metrics` has any reader *(possible P-A)* | S | 🔵 Open |
| `GLD-AFF-09` | Warn on unparseable `percent_complete` in `group_movie_completions` | S | 🔵 Open |

### 4.35 ML sizing

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-SIZ-01` | 🔴 **Per-tier anime rates** — `ANIMATED_MB_PER_MIN = 5.0` is flat across every profile, with an in-source NOTE flagging that it *"underestimates anime 4K/remux films."* Anime is first-class here, and under-estimation **under-reserves space and over-credits downgrade reclaim** | M | 🔵 Open |
| `GLD-SIZ-02` | 🟡 **Make `storage_estimator.py` match its own package's standard** — it logs and stores an unused `cache` param, while `size_model.py` beside it is *"intentionally dependency-free (no logger, cache, or registry)"* *(P-B + P-A)* | S | 🔵 Open |
| `GLD-SIZ-03` | Assert calibration-table monotonicity after every refresh — hand-maintained for cold start, unchecked for the live overlay | S | 🔵 Open |
| `GLD-SIZ-04` | Complete Step 1d — make Sonarr's `compare_file_sizes` delegate here *(P-E)* | S | ⚠️ **THIRD REVISION, session 38.** Chain fully traced: `SonarrQualityFileSizesManager` **is constructed** (by `orchestration/quality.py`, itself constructed by `orchestration/__init__.py`) — but **`orchestration.run()` invokes only `series` and `episodes`, not `quality`**. So it is *constructed*, **not demonstrably invoked**. The real question is whether anything calls `get_file_sizes_manager()` — `GLD-SQ-03` |
| `GLD-SIZ-05` | Report calibration coverage — tiers calibrated vs falling back, with `n` *(P-D)* | S | 🔵 Open |
| `GLD-SIZ-06` | Extend `brain_purity` to flag logger imports in guarded subpackages, or declare logging permitted | S | 🔵 Open |
| `GLD-SIZ-07` | Distinguish `actual == 0` from unknown size in `classify_file_size` | S | 🔵 Open |
| `GLD-SIZ-08` | Re-run `calibrate_sizes.py` and refresh the cold-start table — dated to one run, library has grown | S | 🔵 Open |
| `GLD-SIZ-09` | Warn when an estimate hits the clamp — it silently rescues an upstream units bug | S | 🔵 Open |
| `GLD-SIZ-10` | Document the size model in `MATH_FOUNDATION.md` and wrap it in `foundation/` — the one production estimator absent from the index | S | 🔵 Open |
| `GLD-SIZ-12` | ✅ **Calibration ratchet: a mislabelled file raised the threshold meant to catch the next one** — `[MIN_MB_PER_MIN, MAX_MB_PER_MIN]` = `[0.5, 900]` is a units guard against CORRUPT RUNTIMES (its own docstring), not an outlier guard, so a 321.7 MiB/min disc image graded `Bluray-720p` entered the tier's plain `.mean()`, raised `expected_size_gb`, and raised the `over_ratio x expected` anomaly threshold. Measured on an n=18 tier: **+20.8% mean, threshold 195 → 236 MiB/min from ONE file** — the detector and the calibrator disagreeing about the same file. Fixed with median-anchored rejection (`outlier_ratio`, `outlier_min_n=5`): median because the mean is what the outlier corrupts; `n >= 5` because an outlier cannot be identified in a sample of one — thin tiers stay protected by `min_samples` on the cap instead | S | ✅ **Fixed** — §0.1 #65 |
| `GLD-SIZ-13` | ✅ **Grab-time size ceiling planner** (`quality_caps.py`) — measured MiB/min → *arr `qualitydefinition` `maxSize` in MB/min at the anomaly detector's own `over_ratio`: *if we would flag it after the grab, refuse it before*. Tighten-only (a poisoned rate must never widen a ceiling — second guard on `GLD-SIZ-12`), thin tiers exempt (too LOW a cap starves a tier silently; that check precedes the tighten check), `MIB_TO_MB` applied (skipping it over-tightens every cap by 4.9%), unlimited (`None`) never read as 0. Now also carries the SHARED `render_rows` + `push_caps` orchestration — one diff table and one write path for BOTH services, with `put`/`logger` injected and `runtime_minutes` the only legitimate difference (162m feature vs 42m episode). MiB/min is a bitrate, so the arithmetic is identical for movies and episodes; only the source parquet and the runtime label change. Wiring is `GLD-RAD-34` (Radarr) and `GLD-SON-21` (Sonarr) | M | ✅ **Fixed** — §0.1 #65 |

### 4.36 ML quality_analytics

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-QAN-01` | ❓ **Confirm whether `choose_codec_profile` is invoked by any service** — a complete, pure, heavily-tested per-viewer codec selector with unknown wiring. Same shape as `GLD-SON-01` | S | 🔵 Open |
| `GLD-QAN-02` | ❓ **Determine which matrix is live** — the cached per-device `tautulli/device_codec_matrix`, or the per-user fingerprint matrix `profile_selector` actually consumes. **Reframes `GLD-TAUT-01`** from "build a consumer" to "which of two derivations is redundant" | S | 🔵 Open |
| `GLD-QAN-03` | 🟡 **Correct the package label** — `__init__.py` says *"partly stubs, lowest priority (Step 9)"* and `ARCHITECTURE.md` names only the one module that **is** a stub. It is the most-tested package in the brain *(P-G)* | S | 🔵 Open |
| `GLD-QAN-04` | Implement or drop `transcode_analyzer.transcode_penalty` — the one remaining stub | M | 🔵 Open |
| `GLD-QAN-05` | Report codec-selection outcomes — titles considered, variant chosen, transcode cost avoided | S | 🔵 Open |
| `GLD-QAN-06` | Surface the `reason` dict the selector already returns | S | 🔵 Open |
| `GLD-QAN-07` | Warn when codec is **inferred** from CF scores rather than read from the profile name | S | 🔵 Open |
| `GLD-QAN-08` | Validate `min_coverage` as an alternative to pure argmin — built, defaulted off, never exercised | M | 🔵 Open |
| `GLD-QAN-09` | Feed `codec_direct_play_rate` into a report so per-device capability is inspectable | S | 🔵 Open |
| `GLD-QAN-10` | Cross-check `_BAN_THRESHOLD` (−1000) against live profiles — a ban at −900 would not register | S | 🔵 Open |

### 4.37 ML classification

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CLS-01` | 🔴 **Warn when `target_folder` returns `""`** — the exact line where `movieRootFolders={}` discards classification. `mrf.get(cat) or mrf.get("standard") or ""` | S | 🔵 Open |
| `GLD-CLS-02` | 🔴 **Fail loudly on missing columns** in `build_franchise_file_ids` — it returns `frozenset()`, i.e. **nothing franchise-protected**. Absent ⇒ unprotected, the opposite of `watched_definition`'s fail-open rule | S | 🔵 Open |
| `GLD-CLS-03` | 🟡 `build_keep_policy_map` **redefines** `keep_forever_labels`/`keep_movie_labels` locally while the module constants are documented as *"Shared by"* both resolvers. Adding a label reaches only one | S | 🔵 Open |
| `GLD-CLS-04` | 🟡 Remove or alias `"star"` in `FRANCHISE_HINTS` — the comment deprecates it as ambiguous; the token is still live and yields compound buckets like `"star\|starwars"` | S | 🔵 Open |
| `GLD-CLS-05` | Consolidate the **four** keep-policy resolvers — two here plus `radarr/repair/anomaly` and `sonarr/cache/episode_files` *(declared P-E)* | M | 🔵 Open |
| `GLD-CLS-06` | Assert the Radarr collection-field read against a live payload each run *(see §3.3 precedent)* | S | 🔵 Open |
| `GLD-CLS-07` | Report franchise-protection coverage — real entry / de-facto anchor / none *(P-D)* | S | 🔵 Open |
| `GLD-CLS-08` | Read + document `guards.py` (10.7 KB) and `library_classifier.py` (42.4 KB) — not covered this pass | M | 🔵 Open |
| `GLD-CLS-09` | Confirm whether the de-facto anchor fired during the v4/v5 outage — partial protection or none? | S | 🔵 Open |
| `GLD-CLS-10` | Surface the resolved universe name per title | S | 🔵 Open |

### 4.38 ML discovery

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-DIS-01` | Report shelf outcomes per rollover — graduated / purged / cancelled. The five-state model exists to make this measurable and nothing reports it | S | 🔵 Open |
| `GLD-DIS-02` | Warn on a long-`deferred` slot — a seed obligation that never clears shrinks the shelf to nothing, silently *(P-D)* | S | 🔵 Open |
| `GLD-DIS-03` | Detect an orphaned occupancy table — grabbed trials with no tracking entry are **never purged**; the one path where a bounded feature becomes unbounded | M | 🔵 Open |
| `GLD-DIS-04` | 🎯 **Articulate the fail-direction rule in `DOCS_CONVENTIONS.md` §7** — *"on unknown input, fail toward the outcome that changes nothing."* Reconciles four modules and isolates `GLD-CLS-02` as the outlier | S | 🔵 Open |
| `GLD-DIS-05` | Report missed rollovers — a multi-week gap self-heals silently; it is a scheduler problem worth surfacing | S | 🔵 Open |
| `GLD-DIS-06` | Read + document `gems.py` (29.4 KB), `shelf.py`, `candidates.py`, `window.py` — ~48 KB unread | M | 🔵 Open |
| `GLD-DIS-07` | Surface the shelf + its `why` strings in the web UI | M | 🔵 Open |
| `GLD-DIS-08` | **Make `floor` a real threshold** — it defaults to `0`, so the "hard fail-closed floor" excludes only *unscored* candidates | S | 🔵 Open |
| `GLD-DIS-09` | Route the shelf floor through `thresholds/registry` — a hand-set watchability cutoff absent from `THRESHOLD_SPECS` | S | 🔵 Open |
| `GLD-DIS-10` | Report pre-roll misses — weeks where the Saturday window never had a run | S | 🔵 Open |

### 4.39 ML playlists

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-PLY-01` | ✅ **CLOSED session 61** — `is_spoiler_safe` **is** exercised: `test_ordering.py:198` and `test_timeline.py:30` both assert it. Session-20 hypothesis #1 was correct — it is used as an assertion helper rather than having a file named for it. No `test_spoiler.py` needed | S | ✅ Closed |
| `GLD-PLY-02` | 🟡 **DOWNGRADED 🔴→🟡 session 65.** `is_spoiler_safe` has no runtime caller, and `writeback.py`'s **seven P0 safety rails** cover none of episode ordering — but the guarantee **is structural**: `tv_resolver.py` imports `order_items` from `machine_learning.playlists.ordering` and documents the pipeline as *"→ **order_items (brain)** → PlaylistPlan"*. So GROUP→WITHIN→ACROSS **is** what orders the plan, and `(season, episode)` makes spoilers structurally impossible. The check remains worth adding beside rail 4 — belt-and-braces against a future refactor, **not a present hole**. *I rated this 🔴 on "no check" without confirming the guarantee first* | S | 🔵 Open | [plex/playlists §2.1](./managers/services/plex/playlists/DESIGN.md) |
| `GLD-PLY-07` | ⚠️ **CORRECTED session 65 — the 0.18 ratio is misleading.** `builder.py`'s docstring: *"The I/O gather is defensive … **the tested core is `_build_for_users` (pure given its inputs)**."* The untested bulk is I/O plumbing; the decision core **is** covered. Re-scoped to *"confirm `_build_for_users` coverage is complete"* rather than treating 119.6 KB / 21.2 KB as a gap | S | 🔵 Open | [plex/playlists §7](./managers/services/plex/playlists/DESIGN.md) |
| `GLD-PLY-12` | 🎯 **Cite the ratingKey-decay finding as the reference identity measurement** — *"Observed on a real heavy watcher: **only 11/117** of one show's watched episodes still matched by ratingKey, but **117/117** matched by (series, season, episode)."* A **90.6 % failure rate measured on live data**, fixed by multi-identity matching. **Eighth** identity instance and the first about *temporal* decay — a key correct when written and wrong later. Paired with `builder.py`'s **APPEND-ONLY** `_RK_CROSSWALK_KEY` so retired keys keep resolving | S | 🔵 Open | [plex/playlists §2.2](./managers/services/plex/playlists/DESIGN.md) |
| `GLD-PLY-08` | 🎯 **Cite `writeback_armed`'s call-log test as the reference byte-identical opt-in** — ten such opt-ins found in the sweep; **this is the only one proven by TEST rather than by construction** (*"behaviour is byte-identical to today, asserted by a call-log test"*). Construction arguments are re-derived by every reader; a call-log test fails on the commit that breaks it | S | 🔵 Open | [plex/playlists §3](./managers/services/plex/playlists/DESIGN.md) |
| `GLD-PLY-03` | 🟡 **Add `test_caps.py`** — skip-don't-stop regresses silently under a `break` refactor, and a test asserting `len(kept) <= max` would not catch it | S | 🔵 Open |
| `GLD-PLY-04` | Report truncation in the run summary — the dropped count is returned; surfacing may be missing | S | 🔵 Open |
| `GLD-PLY-05` | Assert `kept`-is-not-a-prefix in a test — the metadata-alignment hazard is documented, not guarded | S | 🔵 Open |
| `GLD-PLY-06` | Fix the `￧` mojibake in `__init__.py`'s crown-jewel rule — it sits in the sentence stating the package's most important invariant | S | 🔵 Open |
| `GLD-PLY-07` | Add `test_engagement.py` — 8.8 KB untested | S | 🔵 Open |
| `GLD-PLY-08` | Resolve `test_recency.py`'s missing source module — no `recency.py` exists | S | 🔵 Open |
| `GLD-PLY-09` | Surface the `why` strings in a preview before publishing | M | 🔵 Open |
| `GLD-PLY-10` | Verify the determinism claim with a shuffle-input property test — G3 asserts input-order independence and nothing pins it | S | 🔵 Open |

### 4.40 ML acquisition

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ACQ-01` | **Guard the `popularity` input** — the cold-start branch adds `popularity` instead of 0 for a no-history user, but a missing/zero popularity silently reinstates the exact penalty it exists to prevent | S | 🔵 Open |
| `GLD-ACQ-02` | Report demand and `t` per acquisition decision — the whole breadth model is invisible in run output | S | 🔵 Open |
| `GLD-ACQ-03` | 🟡 **Move `genre_match` to `affinity/`** — `acquisition` and `quality_analytics` both import it from `playlists`, a presentation package | S | 🔵 Open |
| `GLD-ACQ-04` | Confirm the two `0.15` thresholds (`demand`, `likely_viewers`) are deliberately aligned | S | 🔵 Open |
| `GLD-ACQ-05` | Read + document `pilot_stepping` (24.3 KB), `next_episode_planner` (13.3 KB), `enrichment_prioritizer` (7 KB) — ~45 KB unread | M | 🔵 Open |
| `GLD-ACQ-06` | Resolve `test_pilot_interactive.py`'s missing source twin | S | 🔵 Open |
| `GLD-ACQ-07` | Validate `ready_by_days = 7` against real grab times — the ramp peak is timed to download completion and never measured | M | 🔵 Open |
| `GLD-ACQ-08` | Surface resumption candidates before acquisition | M | 🔵 Open |
| `GLD-ACQ-09` | Route the demand threshold through `thresholds/registry` | S | 🔵 Open |
| `GLD-ACQ-10` | Test the `t = 0` byte-identity claim — I1 makes the roomy regime safe and nothing pins it | S | 🔵 Open |

### 4.41 ML next_watch

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-NXW-01` | 🟡 **Move the source-strength table into the brain**, have `services/acquisition/scorer` import it. `INTENT_SOURCE_STRENGTH` is a hardcoded copy of `_SOURCE_SCORE ÷ 100` — and **brain purity makes delegation impossible**, since `ml → services` imports are forbidden. Fixes the cause *(P-E)* | S | 🔵 Open |
| `GLD-NXW-02` | Warn on an unknown feed reaching `INTENT_SOURCE_STRENGTH` — a new upstream feed contributes 0.0 silently | S | 🔵 Open |
| `GLD-NXW-03` | Warn when `trakt_member` is unresolved — costs both the ladder position and the shield anchor | S | 🔵 Open |
| `GLD-NXW-04` | Report intent-index coverage — titles per source, members resolved, dated vs undated | S | 🔵 Open |
| `GLD-NXW-05` | 🎯 **Record the refuse-to-fabricate precedent in `DOCS_CONVENTIONS.md`** — the strongest counter-example to P-C in the repo, currently in one docstring | S | 🔵 Open |
| `GLD-NXW-06` | Surface `INTENT_STALE_FLOOR` in this package's docs — the curve is documented where its floor is invisible | S | 🔵 Open |
| `GLD-NXW-07` | Re-measure the solo distribution — 258/259 justified the 0.60 base and is a moving number | S | 🔵 Open |
| `GLD-NXW-08` | Revisit decay once Plex exposes `addedAt` — the code is ready, the data is not | S | 🔵 Open |
| `GLD-NXW-09` | Warn on items with neither tmdb nor tvdb rather than dropping silently | S | 🔵 Open |
| `GLD-NXW-10` | Validate `people_cooccurrence` at 0.60 — the only self-generated feed, graded like a third-party recommendation | M | 🔵 Open |

### 4.42 ML challenger

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CHL-01` | 🟡 **Add tests** — 14.7 KB covering temporal splitting, isotonic calibration, artifact persistence and a runtime path, with **zero test files**. The shadow guarantee protects the library, not the **conclusion** drawn from a divergence report | M | 🔵 Open |
| `GLD-CHL-02` | **Define promotion criteria** — what `valid_auc_pr` against `valid_base_rate`, over how many runs, earns influence? **Closes `GLD-ML-08`**; every metric needed is already persisted | M | 🔵 Open |
| `GLD-CHL-03` | Persist divergence reports rather than only logging them — ρ and the top disagreements are the experiment's actual output | S | 🔵 Open |
| `GLD-CHL-04` | 🟡 Fix the cache comment or code — `_MODEL_CACHE.clear()` keeps one model **total**, not *"one per service"* | S | 🔵 Open |
| `GLD-CHL-05` | Leave missing features as `np.nan` — LightGBM splits on missingness natively; `0.0` makes absent and zero identical *(P-C)* | S | 🔵 Open |
| `GLD-CHL-06` | Warn when enabled but no model is present — currently a silent no-op | S | 🔵 Open |
| `GLD-CHL-07` | Report challenger coverage — rows scored, model age, degenerate flag | S | 🔵 Open |
| `GLD-CHL-08` | Track ρ over time — a single run's ρ is not interpretable; a trend is | M | 🔵 Open |
| `GLD-CHL-09` | Re-verify `num_leaves = 15` if `n` grows materially | S | 🔵 Open |
| `GLD-CHL-10` | Train per-media models, or confirm one per service is right | M | 🔵 Open |

### 4.43 ML people_matrix

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-PPL-01` | 🟡 **Measure the role-weight ordering** — the docstring calls it *"the claim being made"* (directors/leads ≫ editors). Seven hand-set numbers feeding Group B + C4 and therefore **every** decision, never measured. `labels/` + `eval/` + the `sig_*` columns already hold the data | M | 🔵 Open |
| `GLD-PPL-02` | 🟡 Share the role classification with `bucket_merge` rather than mirroring it — reclassify a crew role there and the id-graph silently disagrees with the name columns *(P-E)* | S | 🔵 Open |
| `GLD-PPL-03` | Report graph coverage — credits with ids vs without. A title with no id-bearing credits contributes nothing to C4 while looking fully enriched *(P-C/P-D)* | S | 🔵 Open |
| `GLD-PPL-04` | Validate the 0.25 billing-decay constant against household rewatch behaviour | M | 🔵 Open |
| `GLD-PPL-05` | 🎯 **Record the anti-saturation principle** in `DOCS_CONVENTIONS.md` — *"a signal that saturates carries no information"*, independently rediscovered in `likelihood`, `next_watch` and here | S | 🔵 Open |
| `GLD-PPL-06` | Rename the package or note in-source that **no matrix exists** — a reader looking for one may read it as unfinished *(P-G shape)* | S | 🔵 Open |
| `GLD-PPL-07` | Read + document the remaining ~13 KB of `build.py` | M | 🔵 Open |
| `GLD-PPL-08` | Surface co-occurrence queries — *"films with A and B"* is built and has no interface | M | 🔵 Open |
| `GLD-PPL-09` | Extend the graph to Sonarr — the relational table cited is Radarr-only | M | 🔵 Open |
| `GLD-PPL-10` | Validate `people_cooccurrence`'s 0.60 intent weight jointly with `GLD-NXW-10` | M | 🔵 Open |

### 4.44 ML updates — *legacy*

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-UPD-01` | 🔴 **Delete `updates/` after a caller check** — two dead scripts that read a **nonexistent field** (`entry.get("started")`), target a **defunct cache layout**, and use three approaches the codebase has since documented as wrong (name-keyed people, undecayed counts, unthresholded `watched_status`) | S | 🔵 Open |
| `GLD-UPD-02` | 🎯 **Cite `updates/` as the worked example in `GLD-ML-03`** — an import-only guard **passes** two modules doing `open()`, `json.dump` and `df.to_csv` inside the brain | S | 🔵 Open |
| `GLD-UPD-03` | Add the structural tell to the sweep checklist — *no `__init__.py` + no docstring + no test* reliably marks pre-migration code | S | 🔵 Open |
| `GLD-UPD-04` | Extend `brain_purity` to forbid `factories.*`, or amend `ARCHITECTURE.md` rule 3 — `dataset_builder` imports `factories.cache.make_json_safe`, breaching *"`ml → contracts` only"* | S | 🔵 Open |
| `GLD-UPD-05` | Record in `MIGRATION.md` that `updates/` was superseded, and by what | S | 🔵 Open |

### 4.45 Plex service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-PLX-01` | 🔴 **Raise `plex.watchlist.snapshot_retention` above the label horizon** — retention is **~7 hours** (8 files observed) against a **14-day** measurement window, so a snapshot is gone ~48× before it can be evaluated. **Forward validation cannot produce a result** and reports a legitimate-looking `n_snapshots: 0`. **Unblocks `GLD-EVA-03`, `GLD-EVA-09`** | S | 🔵 Open |
| `GLD-PLX-02` | Warn when retention < `ml.thresholds.horizon_days` at config load *(P-D)* | S | 🔵 Open |
| `GLD-PLX-03` | 🎯 **Cite `plex/run_stats` as the reference implementation for coverage reporting** — eleven register items ask for exactly the counter Plex already ships (`users_pin_skipped`, `guid_network_hops`, …) | S | 🔵 Open |
| `GLD-PLX-04` | Enable P2 `on_deck` — `next_watch` already names `plex/on_deck/union` as a future input | S | 🔵 Open |
| `GLD-PLX-05` | Surface `plex/reconcile/{orphans,missing}` — computed when enabled, no reader *(possible P-A)* | S | 🔵 Open |
| `GLD-PLX-06` | Surface `plex/debug/unresolved_guids` — unresolved titles drop out of every id-join invisibly | S | 🔵 Open |
| `GLD-PLX-07` | Expand `test_security.py` — **898 B** against a *"non-negotiable, post-incident"* posture covering the collision map, scrubber-at-mint and the forbidden token key | M | 🔵 Open |
| `GLD-PLX-08` | Detect upstream endpoint drift — v2/Discover are community-documented and one path has already moved | M | 🔵 Open |
| `GLD-PLX-09` | Document the 13 Plex subpackages — the parent is well documented, the children are not | L | 🔵 Open |
| `GLD-PLX-10` | `parent_name` resolves to `"Services"` — cosmetic, shared with MAL | S | 🔵 Open |

### 4.46 Acquisition service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-ACQS-01` | 🎯 **Port the renormalise-out pattern to the watchability scorer** — `AcquisitionScorer` *already* drops absent signals from the weighted average at the **top level**, with a dynamic denominator. **`GLD-ML-04` is a port, not a design task** | M | 🔵 Open |
| `GLD-ACQS-02` | 🟡 **Replace `_CURRENT_YEAR = 2026` with an injected `now`** — already 2 months stale; inflates recency pool-wide every January, undetected | S | 🔵 Open |
| `GLD-ACQS-03` | Record the **aggregator-inversion precedent** — *"a mean over matched items penalises additional evidence"*, with its measured symptom | S | 🔵 Open |
| `GLD-ACQS-04` | Verify the all-signals-absent path — what does an empty denominator score? | S | 🔵 Open |
| `GLD-ACQS-05` | Verify the unknown-feed default — `_SOURCE_LABEL` has a `50: "feed"` entry with no `_SOURCE_SCORE` key; `next_watch` contributes **0.0** for unknown feeds. The two may agree on all nine known feeds and disagree on every new one | S | 🔵 Open |
| `GLD-ACQS-06` | Track the component matrix across runs — rendered and discarded | M | 🟡 **Partly done** — now rendered as a contribution table + persisted to `acquisition/breakdown`; cross-run history still missing (key overwritten each run) → `GLD-ACQS-12` |
| `GLD-ACQS-10` | 🔴 **Split the add budget per medium** — `max_adds_per_run` caps ONE score-ordered pool; `svc` is derived per candidate *after* the cap, so shows and movies compete for the same slots. Observed 2026-08-19: 10/10 went to movies/anime films, **zero** new series, starving the pilot pipeline | M | ⏸ **Superseded by `GLD-ACQS-13`** (operator ruling 2026-08-20: bytes, not counts — under the budget the cap no longer binds and the competition dissolves; latent only for count-cap deployments) |
| `GLD-ACQS-13` | ✅ **Byte-priced acquisition space budget** — selection funds candidates in priority order out of `max(0, free-U)` minus in-flight, skip-and-continue; the 4K companion is priced LIVE in the add loop (planned after selection; the largest file class in the system); fail direction INVERTED — incomplete information collapses to the bounded count cap, never unlimited (cap 0 → fallback 10); unknown sizes price at defaults, never 0; `shared_pool` MIN-headroom default because one Unraid array backs every instance | M | ✅ **Fixed** — §0.1 #58 |
| `GLD-ACQS-14` | ✅ **Committed-bytes ledger** (`acquisition/space_budget/committed`) — nets committed-but-unlanded bytes out of the next run's budget (the GLD-RST-05 phantom-headroom shape, sign flipped); commit on `added` only; deferred adds commit at FLUSH time with the price riding the queue record's `gb`; 72h TTL reconcile, prune-on-write, LOUD write-failure warning | M | ✅ **Fixed** — §0.1 #58 |
| `GLD-ACQS-15` | **hasFile-based ledger reconciliation** — v1 is TTL-only, strictly conservative (early imports and dead grabs both under-grab until expiry); clearing entries when the *arr record gains a file tightens accuracy and shrinks the one wrong-direction TTL case (a download still in flight PAST the TTL) | M | 🔵 Open |
| `GLD-ACQS-16` | 🟡 **Bytes committed outside the budget's view** — saga/universe walks (`ensure_owned_and_grab`/`ensure_show_owned_and_grab`) and rehome re-adds band-gate per add but never write the ledger; undercounted in-flight → over-commit (wrong direction), bounded today by the band headroom those paths already respect. A universe cold-start would not be absorbed | M | 🔵 Open |
| `GLD-ACQS-17` | **Fairness/demand under budget mode** — `_reserve_fairness` still receives the COUNT cap, so its guarantee is positional, not byte-aware: a reserved 40 GB pick can be refused while cheap titles fund. Moot while `demand.enabled=false` (this deployment); decide before any tester runs demand + budget together | M | 🔵 Open |
| `GLD-ACQS-18` | ✅ **First live run: the budget could never arm itself** — `GlobalCacheManager.get` returns `{}` for a MISSING key ({} is the documented missing sentinel in its own compat wrapper), and the context builder's strict `raw is not None` check read that sentinel as CORRUPT → count-cap fallback → and because fallback never persists the ledger, every subsequent first read would repeat it: a permanent deadlock disguised as a safety fallback. **The tell that should have caught it**: the deferred-queue read six lines above does the tolerant `isinstance` coercion for exactly this cache behaviour, and the exec-harness GC stub returned `None` for missing keys — a fidelity gap that let all 8 mode paths pass against the wrong sentinel. Fixed (`raw and not isinstance(raw, list)`); truthy non-lists and raising reads still fall back; re-verified against a stub with the REAL `{}` semantics | S | ✅ **Fixed** — §0.1 #60 |
| `GLD-ACQS-19` | 🔵 **Non-Home per-user affinity is computed and never consumed** (the Stacee case, P-A) — Tautulli builds per-user matrices for 8 users (13 accounts seen), but `_per_user_affinities` walks `PlexUsersManager.tracked_users` = `/api/v2/home/users` only (6 tracked of 7 Home), so a shared friend's matrix at `tautulli/users/<safe>/affinity` is never read for acquisition, fairness, or playlists. Proposed: explicit `extra_tracked_users` config merged post-Home-enumeration (mirrors `ignored_users`' inverse; keeps her off the device switcher), or discover-and-report with manual promote; full auto-tracking needs an activity bar (plays+distinct titles+recency), dwell hysteresis, fail-closed roster persistence, and an explicit rating-group (a discovered user currently matches the memberless-group wildcard → unrestricted). Delete-guard trace still undone: acquiring for a user the retention pass does not count makes her acquisitions first-culled | M | 🔵 **Open — operator decision** (roster shape) |
| `GLD-SPC-01` | ✅ **Change-plan ledger omitted every acquisition** — Phase 0 of the space-economy program ([`SPACE_ECONOMY.md`](./SPACE_ECONOMY.md) §8). `decision_ledger.stamp` writes onto PARQUET ROWS, so a title being added for the first time is structurally invisible to the grid — the 19:23 run showed `acquire 19 / -25.2 GB`, no `> movies` row at all, and `TOTAL +242.7 GB` against a true net of ~-68 GB. New rowless `ledger/pending_plan` folds into the same accumulators; verified in production at 20:23. This was the Phase 0 measurement the rest of the program waits on: the byte flow per path is now visible in the table the operator already reads, rather than needing new instrumentation | M | ✅ **Fixed** — §0.1 #68 |
| `GLD-ACQS-20` | ✅ **`hard_max_adds` refusals reported as "0 refused"** — the hard-max branch in `select()` returns BEFORE `try_charge`, so it touched no counter in `ctx.stats`, and the summary line reads those counters. The 2026-08-20 20:23 run printed *"10 funded (~33 GB), **0 refused**"* two lines above *"skipped: … space_budget_hard_max×122"*. A headline stating nothing was turned away, while 122 titles were, is worse than no headline — and the two lines only happen to sit near each other. Self-inflicted in §0.1 #58; caught reading the first run that actually used the cap. `skipped_hard_max` now counted in `ctx.stats` and surfaced as *"N capped (hard_max_adds)"*, omitted entirely when the cap is off so uncapped runs are byte-identical | S | ✅ **Fixed** — §0.1 #68 |
| `GLD-ACQS-11` | 🟡 **Measure the people-signal cohort gap** — every MAL-sourced candidate scored WITHOUT `people_affinity`, every Trakt-watchlist one WITH it. Dynamic denominator ⇒ the two cohorts are normalised over different signal sets, then compete for one capped budget | M | 🟡 **Direction confirmed in production** (2026-08-20 run, first with the coverage tables): Cast present 14 / absent 11 across the 25 acted-on; cohort roll-up: MAL 0% people-scored vs Trakt WL and Plex WL 100%; MAL mean score **74.0 vs Trakt 66.8** and MAL took 11 of 25 slots (incl. all 7 shows). The fewer-signal cohort outranks, as hypothesised. Magnitude/causality still open — the mean gap conflates genre affinity (anime-heavy household) with the denominator effect; separating them needs the per-signal contributions from the breakdown frame across runs (`GLD-ACQS-12`) | 
| `GLD-ACQS-12` | **Durable sink for the breakdown frame** — one global-cache key, overwritten every run. A run-keyed parquet makes `GLD-ACQS-06` and `GLD-ACQS-07` fall out free | S | 🔵 Open |
| `GLD-ACQS-07` | Re-measure the genre distribution post-noisy-OR — the fix's own success metric (131/663, 8-of-10) has no watcher | S | 🔵 Open |
| `GLD-ACQS-08` | Document `__init__.py` (54.5 KB), `resolver.py` (28.9 KB), `candidates.py`, `gateway.py`, `adder.py` — ~105 KB unread | L | 🔵 Open |
| `GLD-ACQS-09` | Route `_GENRE_SATURATION` + source tiers through `thresholds/registry` | S | 🔵 Open |

### 4.47 Coordinator service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-COORD-01` | 🔴 **Rename or unify the pressure fallback constant** — ⚠️ **session 54: a FOURTH site, with a different NAME.** `space_targets.py` `PRESSURE_FALLBACK_GB` **25.0** · `sonarr/series/space_pressure.py` `PRESSURE_FALLBACK_GB` **25.0** · `radarr/quality/space_pressure.py` **`PRESSURE_THRESHOLD_GB`** **25.0** · `coordinator/space_coordinator.py` `PRESSURE_FALLBACK_GB` **1000.0**. **Three of four agree at 25**; the coordinator is the **value** outlier and Radarr the **naming** one — so a grep for the common name misses Radarr entirely | S | 🔵 Open |
| `GLD-COORD-02` | 🟡 **Repoint the `space_targets` import** from the `support/utilities` shim to `machine_learning.space` — **MIGRATION Step 10 cannot complete while live callers use the shims**, and nothing lists who they are. ⚠️ **Four live callers now found, all by accident in unrelated reads**: `coordinator/space_coordinator.py`, `sonarr/orchestration/series.py`, `sonarr/series/quality.py`, `sonarr/series/space_pressure.py`. **This needs an import search, not a one-line repoint** | S | 🔵 Open |
| `GLD-COORD-03` | Note in `foundation/` that `downgrade_credit` is **skipped by default** — its equivalence test guards a path most installs never take | S | 🔵 Open |
| `GLD-COORD-04` | **Correct `space/DESIGN.md` §3.1** — the pressure band is *downgrade-active, delete-inactive*, not *"hold steady"*. Affects how `GLD-SPA-05` must measure | S | 🔵 Open |
| `GLD-COORD-05` | Report which stage ran and what each reclaimed — answers D24's empirical half and `GLD-SPA-05` in one counter | S | 🔵 Open |
| `GLD-COORD-06` | Warn when the 1000 GB fallback is used *(P-D)* | S | 🔵 Open |
| `GLD-COORD-07` | Surface the FORK-D ledgers — a title can sit evicted-awaiting-recovery indefinitely, invisibly | S | 🔵 Open |
| `GLD-COORD-08` | 🎯 **Cite FORK-D as the pattern for `GLD-LED-08`** — proper cross-run, timestamped, per-instance ledgers already exist in this repo | S | 🔵 Open |
| `GLD-COORD-09` | Document `hybrid_universe_acquisition.py` + `saga_retention_producer.py` — 33 KB unread | M | 🔵 Open |
| `GLD-COORD-10` | Verify single-service pool degradation if one service's candidate build fails | S | 🔵 Open |

### 4.48 Writeback service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-WB-01` | 🔴 **Route the watched predicate through `lifecycle.play_is_watched`** — writeback uses `watched_status == 1` **OR** `pct ≥ 85`; production uses **verdict-wins**. A row Tautulli marked **unwatched** or **partial** is pushed to the user's **permanent Trakt history** if its percentage clears 85. The **seventh** bar, and the only one whose output **leaves the system** | S | 🔵 Open |
| `GLD-WB-02` | 🔴 **Add tests** — the only package writing to third parties has **zero**, while `coordinator/` (which deletes, but tracks and restores) has **61 KB** | M | 🔵 Open |
| `GLD-WB-03` | 🟡 **Strengthen `dry_run` to kwargs → parent → Main; refuse to default** — currently two-level, defaulting to **`False` = live writes**, in the least reversible module. `coordinator/` has the stronger form to copy | S | 🔵 Open |
| `GLD-WB-04` | Warn when the 20,000-entry page cap is hit — `fetch_history` already accepts an unused `logger` *(P-D)* | S | 🔵 Open |
| `GLD-WB-05` | Check POST responses — a rejected batch may look like success | S | 🔵 Open |
| `GLD-WB-06` | Record what was pushed — nothing logs what left the system, so an incorrect push cannot be found, let alone reversed | M | 🔵 Open |
| `GLD-WB-07` | Add a public POST wrapper to `TraktAPI` — the one write path calls `_make_request` | S | 🔵 Open |
| `GLD-WB-08` | Route `watched_threshold` through `thresholds/registry` | S | 🔵 Open |
| `GLD-WB-09` | Document `mal_list.py` + `trakt_collection.py` — 7.6 KB unread | M | 🔵 Open |
| `GLD-WB-10` | Dry-run diff before first live enable — the first live run of a push-only sync is the one that cannot be undone | M | 🔵 Open |

### 4.49 Backup service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-BKP-01` | 🟡 **Distinguish "validated" from "not attempted" at the gate** — `system/backup_gate == True` means *writes permitted*, not *a backup exists*. `"ok"` and `"disabled"` are **both armed** *(P-C)*. The `reason` field carries it; verify `effective_dry_run` reads it | S | 🔵 Open |
| `GLD-BKP-02` | Report the rollback point's **age and origin** — reused vs fresh, *arr-scheduled vs ours. A reuse can be 24 h old and nothing says so | S | 🔵 Open |
| `GLD-BKP-03` | Suppress the disarm warning when no *arr instances are configured — fail-safe but warns every run | S | 🔵 Open |
| `GLD-BKP-04` | 🎯 **Cite in `DOCS_CONVENTIONS.md` as the run-scope instance of the D36 rule** — *cannot prove a rollback point ⇒ the **entire run** becomes read-only*. The strongest safety property in the codebase, in 14 KB | S | 🔵 Open |
| `GLD-BKP-05` | Warn when a backup is reused close to the age limit — 23.9 h differs materially from 1 h | S | 🔵 Open |
| `GLD-BKP-06` | Record gate state in the ledger so a plan carries whether its run was protected | S | 🔵 Open |
| `GLD-BKP-07` | **Verify every destructive primitive actually reads the gate** — the guarantee is only as broad as its readers, and nothing enumerates them. ✅ **Session 43: the biggest one confirmed** — `sonarr/cache/episode_files.py` (which owns `build_delete_candidates` and the episode-file delete path) imports `effective_dry_run`. Still unenumerated, but the answer is now "at least the highest-volume destructive path" | M | 🔵 Open |
| `GLD-BKP-08` | Consider covering Glidearr's own Parquet caches — a restored *arr with a stale local cache is partially rolled back | M | 🔵 Open |
| `GLD-BKP-09` | Surface backup size trend — 64 KB catches garbage; nothing catches a 300 MB → 40 MB drop | S | 🔵 Open |
| `GLD-BKP-10` | 🔴→✅ **The gate degraded a LIVE run over a backup that had actually succeeded** — 2026-08-20 20:33. `radarr/ultra` (0.4 MB) and `sonarr/standard` (132.6 MB) finalised; `radarr:standard` logged NOTHING and the run degraded. The *arr marks the Backup command `completed` BEFORE the zip is flushed and listed, so the single immediate `_list_backups` is a race lost in proportion to DB size — the 218.8 MB file appeared moments later and was happily reused by the very next run (timestamp `20.34.00`, i.e. after the manager had already moved on). Fixed with `_await_new_backup`, polling the LISTING for up to `LISTING_SETTLE_S` (90s). Deliberately not a bigger `BACKUP_TIMEOUT_S`: the command genuinely had completed, so waiting on the command would never have helped | S | ✅ **Fixed** — §0.1 #69 |
| `GLD-BKP-11` | 🔴→✅ **The failure reason was computed and thrown away** (P-A) — `_backup_one` returns a precise `detail` on every failure path (`"Backup command did not complete within 300s"`, `"no backup file appeared"`, `"no base_url / api key in config"`, the exception) and `ensure_backups` built its warning from the result KEYS alone. The operator was told the run degraded and given no way to find out why — in the gate that blocks every live run. The wording was also an ambiguous either/or ("FAILED **or** not loadable") when the code already knew which | S | ✅ **Fixed** — §0.1 #69 |
| `GLD-BKP-12` | ✅ **Stale-backup fallback** (operator ruling: *"degrade only if there is no usable backup from the past 24-72 hours"*) — the gate guarantees a restorable ROLLBACK POINT, not a brand-new one, so when creation fails a still-valid backup inside `backup_fallback_max_age_hours` (72h default) arms the gate instead of costing a whole live run. Two things deliberately NOT relaxed: the candidate is validated exactly as a fresh one is (a 0-byte file inside the window still disarms), and it is a WARNING naming the age, because arming destructive writes against an N-hour-old rollback point is a real trade | S | ✅ **Fixed** — §0.1 #69 |

### 4.50 MDBList service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-MDB-01` | 🔴 **Bound the 429 path** — `max_calls` counts **successes only**, so a sustained 429 walks **every** queued id at **30 s each** with zero progress. Presents as the enrichment daemon hanging, not failing. A caller without a `stop` sentinel has no escape | S | 🔵 Open |
| `GLD-MDB-02` | 🟡 **Make `budget()` fail toward inaction** — it returns `(0, 25000)` on error, i.e. **full headroom**, then `GLD-MDB-01` absorbs the resulting 429s badly. The two compound | S | 🔵 Open |
| `GLD-MDB-03` | 🟡 Update the package docstring — *"First slice: AUTH … only"* predates 4.8 KB of live rating enrichment *(P-G)* | S | 🔵 Open |
| `GLD-MDB-04` | Report cache coverage — resolved / confirmed-absent / unfetched per media type; `age_for` cannot answer it | S | 🔵 Open |
| `GLD-MDB-05` | 🎯 **Record confirmed-absent-vs-transient as a positive pattern** — four independent instances, none referencing the others. The **inverse of P-C** | S | 🔵 Open |
| `GLD-MDB-06` | Warn on a corrupt cache instead of returning `{}` — recovery silently re-spends the whole budget | S | 🔵 Open |
| `GLD-MDB-07` | Add `test_age_cache.py` — resume-state, id-space and atomic-write invariants are untested | S | 🔵 Open |
| `GLD-MDB-08` | Include the age caches in the backup scope — expensive to rebuild, trivially lost | S | 🔵 Open |
| `GLD-MDB-09` | Replace `parents[3]` with an anchored path | S | 🔵 Open |
| `GLD-MDB-10` | Document `client.py` — 12.9 KB unread | M | 🔵 Open |

### 4.51 Calendar service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-CAL-01` | 🟡 **Adopt the strong `dry_run` form** (kwargs → parent → Main, never defaulted) — **third** manager found with the weak two-level form, and it issues `monitored=true` PUTs to both *arrs. The strong form exists in **one** manager, the weak in at least **two**, and the split does not track blast radius | S | 🔵 Open |
| `GLD-CAL-02` | **Document the three effective watchability ranges** — V2 (0–58 observed), LEGACY (0–36), unowned/calendar (**0–25**) — so a threshold is never read against the wrong one | S | 🔵 Open |
| `GLD-CAL-03` | Try `title_en` in the MAL→library join — already captured; a retitled anime silently goes unmonitored, which is the exact failure the module exists to prevent | S | ⚠️ **Re-aimed session 33** — `mal/id_bridge` **already** tries `title` + `alternative_titles.en` + synonyms against three library fields. The item becomes *"should calendar use the bridge's matcher?"* — see `GLD-MAL-01` |
| `GLD-CAL-04` | Test the Trakt calendar path — the module's **primary purpose** and the half that **writes** is untested; only the pure MAL scorer has coverage | M | 🔵 Open |
| `GLD-CAL-05` | Report monitor-ahead outcomes — matched / monitored / missed by the join *(P-D)* | S | 🔵 Open |
| `GLD-CAL-06` | Confirm the `anime` genre default lands on a real affinity key | S | 🔵 Open |
| `GLD-CAL-07` | Reconsider `calendar.mal` defaulting **on** — the one place a MAL-configured install gains behaviour unasked | S | 🔵 Open |
| `GLD-CAL-08` | Document the remaining ~10 KB of `__init__.py` — Trakt fetch, cache write, monitor-ahead | M | 🔵 Open |

### 4.52 Instance tiering (Sonarr + Radarr)

**The shape today.** `gateway.categorized_instance(label)` maps a tier label to an
instance and falls back to `default_instance()` when unmapped. It works, and it has
exactly **two callers, both in `uhd_reconcile`** — so the tier map drives one
question only: *which instance holds the 4K copy*. That is a TWO-tier promote/demote
axis (`standard ↔ ultra`), not a general tiering system.

Every other pass takes `instance` as a parameter from a caller that iterates whatever
instances exist — `run_downgrades(instance, free)`, `apply_quality_actions(instance)`,
`refresh_scores(instance)`. They are instance-AWARE but tier-BLIND: nothing asks
"is this the 1080 box or the 720 box?", so nothing routes a 1080 acquisition to the
1080 instance. An operator who fills in `radarr_instances_categorized` sees it consulted
by `uhd_reconcile` and by nothing else — a config surface that is 90% decorative *(P-A)*.

`sonarr_instances_categorized` is `{}` and has **no consumer at all**. There is no TV
equivalent of `uhd_reconcile`, so TV tiering is unimplemented rather than partial.

**The requirement.** Three deployment shapes must all work, and be switchable without
editing code:

| shape | Radarr | Sonarr | notes |
|---|---|---|---|
| **single** | one instance, untiered roots | one instance | the SHIPPED default — must stay byte-identical to today |
| **two** | `standard` (720+1080) + `ultra` (4K) | one instance | the previous deployment here; `uhd_reconcile`'s native shape |
| **three** | `radarr-720` / `-1080` / `-2160` | `sonarr-720` / `-1080` / `-2160` | the current deployment; TV side has no machinery yet |

The count is not knowable at build time and must be **derived from config**, not
declared: an operator adding a fourth instance, or collapsing three to one, should
change only `*_instances` + `*_instances_categorized`.

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-INS-01` | 🎯 **Make acquisition tier-aware — the smallest change that makes a tier map mean anything.** A new title is added to `default_instance()` regardless of the quality it was funded at, so `radarr_instances_categorized` cannot influence where anything lands. Route to `categorized_instance(tier)` with the tier taken from the funded quality. **This is the piece that turns the config from decorative into functional**, and it is a third caller of an existing, working function rather than new machinery | S | 🔵 Open |
| `GLD-INS-02` | 🔴 **A tier LADDER, not a single hop.** `uhd_reconcile` knows one transition (`standard ↔ ultra`). Three tiers means 720→1080→2160 with a rung-by-rung walk, and the make-before-break rule — *verify the lower copy survives before deleting the higher* — must hold at EVERY rung, not just the top one. Generalising `_demote_overqualified_4k` is delete-path work and gets delete-path care: a ladder that drops a rung silently loses the only copy | M | 🔵 Open |
| `GLD-INS-03` | 🔴 **Sonarr has no cross-instance machinery whatsoever.** `uhd_reconcile` is ~1,262 lines of Radarr-specific move/dedup/demote; TV needs its equivalent for the "720 pushes 1080/2160 out to the other instances" model. Not a port — episodes differ from movies in ways that matter (a season can be split across tiers, and an episode file can back several episodes). **Project-sized; should not start until `GLD-INS-01`/`-02` prove the model on the Radarr side** | L | 🔵 Open |
| `GLD-INS-04` | 🎯 **Derive the deployment shape rather than declaring it.** `factories/onboarding/tier_pairing.py` already infers a tier per instance from ROOT-FOLDER PATHS (the arr reports them and they cannot drift), falling back to name/URL, and refuses to guess house-style names — `4k`/`uhd` are unambiguous, `ultra`/`standard`/`hd` are not. Wire `pair_instances()` into the run so every pass can ask "what tier is this instance?" without a hard-coded table. **Returns `single: True` for a one-of-each install, so the shipped default never sees tier logic at all** | S | 🔵 Open |
| `GLD-INS-05` | 🔴 **`sonarr_instances_categorized` is declared, documented, and read by nothing** *(P-A)*. Either give it the Sonarr consumer `GLD-INS-03` implies, or mark it explicitly unimplemented in the schema — a key an operator can fill in that silently does nothing is worse than an absent one | S | 🔵 Open |
| `GLD-INS-06` | 🔴 **A tier with no instance on one side is a legitimate state and must not be an error.** This deployment has `sonarr-1080` with no matching Radarr, and had `radarr-1080` with no Sonarr before tonight. `pair_instances` reports these in `ask` rather than inventing a partner; every consumer must handle "this tier exists for movies but not TV" without falling back to the wrong instance. **Fail direction: skip the tier, never substitute** | S | 🔵 Open |
| `GLD-INS-07` | 🔴 **Instance keys are CACHE PATH COMPONENTS**, so a rename orphans `movie_files.parquet`, `movie_score_memo.json`, `stepdown_cooldown.json`, `jit/inflight_qp/<instance>.json` and the pilot ledgers. Two of those are not caches — the cooldown ledger is the rate limit added after *Edge of Tomorrow was deleted six times in one day*, and inflight-QP is what restores a series left at a BUMPED profile by a crashed job. Neither rebuilds from anything. Add a migration path (or refuse the rename) so an operator renaming an instance is not silently discarding two ledgers whose whole job is remembering what the live system forgot | S | 🔵 Open |
| `GLD-INS-08` | 🔴 **An EMPTY instance is untested territory.** `radarr-1080` has zero movies. Every pass that reads "no rows" must treat it as an empty library, not a failed fetch — the `absent vs empty` conflation *(P-C)* that has produced three live bugs in this codebase. Audit the instance-iterating passes against a zero-row instance before relying on a three-tier deployment | S | 🔵 Open |
| `GLD-INS-09` | Report the detected shape ONCE per run — `"3 Radarr tiers (720/1080/2160), 1 Sonarr, 2 tiers unpaired"` *(P-D)*. Tiering that silently does nothing looks identical to tiering that is working, which is exactly how `radarr_instances_categorized` stayed 90% decorative | S | 🔵 Open |
| `GLD-INS-10` | ✅ **RETIRED: tiering 720↔1080 across instances buys nothing — 4K is the only tier that earns cross-instance.** Operator built `sonarr-1080`/`-2160` and `radarr-1080`, then asked whether a parquet crawl should move 1080/2160 files into them. Measured first: **movies are 1:1** (219 at 1080p in `radarr-720`, one file per record, cleanly movable) but **TV is 1:many** — of 5,249 series holding files, **105 mix two or three resolutions**, and Sonarr owns the SERIES, not the episode. A crawl moving "all 1080p episodes to sonarr-1080" would split those 105 across instances, leaving neither with a complete view. **The decisive argument is not the split, it is the purpose**: `uhd_reconcile` exists because 4K is a **DUAL** — a 1080 baseline that always plays PLUS a 2160 copy for capable clients, genuinely two files. 720 vs 1080 is ONE file at one quality, and "which quality should this file be" is what a QUALITY PROFILE answers, inside one instance, with no second record, no second root and no split series. ⚠️ Confirmed by the backlog it would have chased: **5,676 episodes (43%) sit below the 720p floor**, every one flagged `cutoff_not_met` by Sonarr — but their series have a **median watchability of 10** and **zero** reach the upgrade tier (866 of 995 score <20). The upgrade passes are not failing to reach them, they are correctly declining to spend on cold content; 90 are already marked for DELETION and 941 are unengaged pilots. Tiering would have added machinery to move files the scorer has already decided not to invest in. **Config retired**: `radarr-1080` removed, `1080p` re-pointed at `radarr-720` (it had been routing to an EMPTY instance — a live misroute), `radarr-2160` kept for the dual. Containers retained for multi-instance testing of the shipped install. ⚠️ **`GLD-INS-01`/`-02`/`-03` are superseded for the 720↔1080 case** and now apply only to the 4K dual | M | ✅ **Decided — retired** §0.1 #82 |

---

### 4.53 MAL service

| ID | Item | Effort | Status |
|---|---|---|---|
| `GLD-MAL-01` | 🟡 **Reconcile the two title matchers** — `calendar._library_ids_by_title` and `mal/id_bridge` share `norm_title` but not the strategy; the bridge adds pool restriction + ambiguity-drop *(P-E)*. **Re-aims `GLD-CAL-03`** from "add aliases" to "should calendar use the bridge's matcher?" | S | 🔵 Open |
| `GLD-MAL-02` | 🎯 **Apply ambiguity-drop to `labels/labeling`** — same no-id constraint, **no guards today**. Converts silent **mis-attribution** into silent **omission** — strictly better, and available before `GLD-LAB-02` lands | S | 🔵 Open |
| `GLD-MAL-03` | 🟡 Audit `animeGenres` consumers — it is **Sonarr-shaped** and contributes nothing to the Radarr movie pool; an operator editing it sees no effect | S | 🔵 Open |
| `GLD-MAL-04` | Report bridge resolution rate — resolved / ambiguity-dropped / unmatched per media type *(P-D)* | S | 🔵 Open |
| `GLD-MAL-05` | Surface ambiguity drops specifically — a **fixable** miss (add an alternate title in the *arr) vs an unfixable one | S | 🔵 Open |
| `GLD-MAL-06` | Reconsider the network refusal for a **non-deletion** path — D5's reasoning is path-specific, not lookup-specific | M | 🔵 Open |
| `GLD-MAL-07` | Document `api/` + `instances/` — two subdirectories unread | M | 🔵 Open |
| `GLD-MAL-08` | 🎯 **Cite the Hunter x Hunter example as the model for documenting a design decision** — a real collision, both resolutions, and why the choice falls out with no heuristic | S | 🔵 Open |

---

## 5. Blocking decisions

Several enhancements are blocked on a decision, not on effort. These need an
answer before the work can start.

| # | Question | Blocks |
|---|---|---|
| D1 | ~~Whose watch history counts as "watched" for auto-prune — any household member, or a designated primary?~~ **Answered, session 7:** `lifecycle/watched_definition.py` is the one definition — any play clearing Tautulli's own `watched_status` verdict (or `percent_complete ≥ watched_threshold.percent`, default 85) counts. Per-member protection is separate, via `household_blocked` and `viewer_protected`. | ✅ Resolved |
| D2 | What fraction of a **series** is "watched" — last aired episode, 90%, all? The per-play bar does **not** answer this; no series-level rollup exists. | `GLD-LIF-01`, `GLD-SVC-01`, `GLD-SON-06` |
| D3 | Should `movieRootFolders` be derived from classification, or configured per bucket? | `GLD-CFG-03`, `GLD-ONB-10`, `GLD-STEP-05` |
| D4 | Does the central `dry_run` gate live in `orchestration/` or `factories/`? | `GLD-ORCH-01/02/03/06` |
| D5 | Web framework: stdlib, Flask, or FastAPI? *(leaning Flask)* | all Phase-1 `GLD-WEB-*` |
| D6 | Does the web layer read Parquet directly, or through a read-only ledger service? | `GLD-WEB-02`, `GLD-WEB-03` |
| D7 | When Trakt and Tautulli disagree on watched state, which wins? | `GLD-TRKT-08` |
| D8 | Should score confidence gate destructive decisions? | `GLD-ML-11` |
| D9 | Should auto-prune be automatic, or propose-and-confirm? | `GLD-SVC-01`, `GLD-TRKT-05` |
| D10 | Are the legacy flat brain modules migrated or deleted? Some may be superseded. | `GLD-ML-01` |
| D11 | Should the device codec matrix be wired up or dropped? | `GLD-TAUT-01`, `GLD-ML-12` |
| D12 | Should `translations` stay in `DEFAULT_SCOPE`? | `GLD-DMN-09` |
| D13 | ~~For each duplicated module pair, which copy is authoritative?~~ **Answered, session 5:** six of eight are documented re-export shims — the subpackage copy is authoritative in every case, and the shims are deleted at `MIGRATION.md` Step 10. The two residual root modules are impure orchestration wrappers, not duplicates *(P-F)*. | ✅ Resolved |
| D18 | Is the brain's impure-root / pure-subpackage split intentional and permanent, or a migration artifact? If permanent, `brain_purity.py` needs a documented tier, not a wider walk. | `GLD-ML-17`, `GLD-ML-01`, `GLD-ML-02` |
| D14 | Are `SonarrQualityManager` and `SonarrSyncManager` superseded (delete) or regressed (re-wire)? They may have different answers. | `GLD-SON-01`, `GLD-SON-11` |
| D15 | Is Sonarr-side config sync happening via `orchestration`, or not at all? Determines whether D14 is cleanup or an outage. | `GLD-SON-01` |
| D16 | For Radarr add-if-absent: direct API add, or tag-for-auto-add? The README leans direct add. | `GLD-RAD-01` |
| D17 | What does make-before-break instance migration look like — add to target, verify, then remove from source? | `GLD-RAD-01` |
| D19 | Should the standalone routers move to repo-root execution, removing the dual-import special case? | `GLD-SUP-01` |
| D20 | Which `tools/` scripts are destructive, and should they share a `--dry-run` harness? | `GLD-SUP-02` |
| D21 | Should `None` on a feature row distinguish "not fetched" from "genuinely absent"? A sentinel would let renormalise-out apply automatically to the first and contribute-zero to the second — the P-C fix expressed as a type. | `GLD-CON-02`, `GLD-CON-03`, `GLD-ML-04` |
| D22 | ~~**What does "4K requires score ≥ 70" actually refer to?**~~ ✅ **RESOLVED, session 10.** `thresholds/registry.py` states it outright — the `uhd` block is headed `# ── uhd (75 live / 70 documented) ──` and the `uhd_dual` spec notes *"the shipped config value 0 means 'unset' (`cfg or DEFAULT_UHD_SCORE`), so the effective constant is **75, NOT 70**."* The 70 traces to `SCORE_4K_THRESHOLD = 70` in `universe.py`, which the registry records as **removed — dead, no reader anywhere**. **Live value is 75.** Docs repeating 70 are stale. | ✅ Resolved — execute `GLD-SCO-01` |
| D27 | Should the LEGACY axis be unified onto the persisted `watchability_score` column? It drops the live `has_credits` deferral and makes the prune depend on `refresh_scores` running in-process — a real behaviour change to demote/restore/monitor. ✅ **Session 51: Radarr-only** — Sonarr's same-named `anomaly.py` does pure set-differences with no scoring. ✅ **Session 80: THE LOAD-BEARING CLAIM IS VERIFIED IN CODE.** `_score_owned` calls `score_movie` with **no `transcode_profile`, no `platform_usage`, no `target_resolution`, no `video_codec`** — confirmed by reading the call. The inline comment goes further than the registry: *"D1 = D3 = 0.0 and D2 sits on its flat +2.0 unknown-codec branch — before AND after the change."* **So the LEGACY axis genuinely never moved under SCORER_REVISION 4, and 20-not-17 is correct.** Measured: anomaly axis **mean 7.4 / max 36** vs persisted **mean 9.3 / max 58**, agreeing on ~84% of delete-eligibility calls. ⚠️ **Unifying now carries a new constraint** — see `GLD-RAN-06`: `movie_demote` and `movie_restore` must move **together** or a permanent delete/re-acquire loop opens | `GLD-THR-03`, `GLD-THR-01`, `GLD-RAN-06` |
| D28 | **Which of the four "watched" bars are deliberately different questions, and which are drift?** Production 85 (or `watched_status`) · eval 0.9 · labels-movie 90 (documented as *"≈ the completions threshold 0.9"*, so trying to agree and off by 5) · labels-episode 50 (*"continued engagement, not per-episode completion"* — clearly a different question). | `GLD-LAB-01`, `GLD-EVA-01` |
| D29 | Should `foundation` be the adoption target, or should tools import the delegates directly? A package nothing imports may be answering a question nobody asked — porting one tool settles it. | `GLD-FND-02` |
| D30 | Implement `select_instance` **before** `GLD-RAD-01`'s remaining sub-tasks, giving the tier logic one landing place — or after, once the semantics settle? Implementing first avoids adding tier logic to `gateway`, `resolver` and `selector` separately and migrating three call sites later. | `GLD-ROU-01`, `GLD-RAD-01` |
| D31 | Is `features/episode_features.py` unmigrated because it is *unfinished*, or too costly at episode volume? ⚠️ **Sessions 44–45 — a third answer tested and rejected.** I proposed it might be *deliberate*, since the Parquet emits flat ML-ready columns. **Flat, yes; safe, no** — the Parquet gives `NaN` where the contract gives typed defaults. ✅ **And the scope is now precise**: `EpisodeFeatureRow` is a 19-field strict subset carrying **no Group D inputs**, because episodes aren't scored — series are. The adapter is narrow, but every field feeds a delete/keep decision. **The cost question remains open.** | `GLD-FEA-01`, `GLD-CON-06` |
| D32 | **What `half_life_days` is right?** Decay is built and off. But evidence is thin (n≈931, n_pos≪100) so an aggressive half-life discards most of it — **and enabling decay is an axis translation**, since likelihood consumes `watchability_score` at gain 1.0. It would need the same three-boundary re-anchor as Group D v2. ⚠️ **Session 70 — the config surface is `scoring.affinity_half_life_days`**, read in `tautulli/users._affinity_half_life` and passed to `aggregate_affinity`; `None`/`0` = legacy raw counts. **The key and the thresholds it would invalidate live in different packages with nothing linking them** | `GLD-AFF-01`, `GLD-AFF-07`, `GLD-LIK-01`, `GLD-TUS-01` |
| D33 | **Is logging permitted inside the brain?** Three modules state "no logging" as part of being pure; `sizing/storage_estimator.py` logs while claiming purity. Settle the standard, then make `brain_purity` enforce it. | `GLD-SIZ-02`, `GLD-SIZ-06`, `GLD-ML-17` |
| D34 | ✅ **ANSWERED session 61 — it IS called, but only into a REPORT.** The sole caller of `choose_codec_profile` is `quality_analytics/codec_report.py:119`. Every other hit is prose: `radarr/quality/space_pressure.py:2271` and `sonarr/cache/episode_files.py:1143` **mention** it in comments; neither invokes it. So the per-viewer codec selector feeds a **codec-routing report** (tested at `radarr/quality/test_codec_routing_report.py`) and is **not wired into profile assignment** in either service — both explicitly defer to *"the device→codec stage"* that only reports. **Per-viewer transcode reduction is advisory, not acting.** | ✅ Resolved — `GLD-QAN-05` becomes "act on the report, or say it is advisory" |
| D35 | Should `min_coverage` be non-zero by default? Pure argmin accepts that a minority viewer transcodes — correct when transcoding is merely inefficient, wrong when that viewer's device **cannot decode** the codec at all. | `GLD-QAN-08` |
| D36 | **Should a protection guard fail open or closed on missing inputs?** 🎯 **Framing found, session 19.** Neither — the four modules that differ all agree under one rule: **on unknown input, fail toward the outcome that changes nothing.** `watched_definition` assume-watched → nothing deleted ✓ · `file_comparison` no-opinion → nothing changed ✓ · `discovery/scoring` exclude → nothing spent ✓ · `build_franchise_file_ids` no-protection → **things get deleted** ✗. The last is the sole outlier, and the only one that caused an outage. | `GLD-CLS-02`, `GLD-DIS-04` |
| D37 | `discovery/scoring`'s floor defaults to **0**, so the "HARD fail-closed floor" excludes only candidates with **no score at all** — every scored candidate passes. Mechanism is fail-closed; the configured value is close to a null gate. Should it be raised, and routed through `thresholds/registry`? | `GLD-DIS-08`, `GLD-DIS-09` |
| D38 | ✅ **ANSWERED sessions 61 + 65.** **(a) Tested? YES** — `test_ordering.py:198` and `test_timeline.py:30` both assert `is_spoiler_safe`; `GLD-PLY-01` closed. **(b) Re-checked before the write? NO** — no service caller, and `writeback.py`'s seven P0 rails cover none of ordering. **(c) But does it matter? LESS THAN I THOUGHT** — `tv_resolver.py` imports `order_items` from the brain, so the GROUP→WITHIN→ACROSS `(season, episode)` sort **is** what orders the plan and spoilers are structurally impossible. The runtime check guards against a future refactor, not a present hole | `GLD-PLY-02` 🟡 |
| D39 | Should `genre_match` move from `playlists/per_user` to `affinity/`? Two brain packages now import an affinity primitive from a presentation package. Not a defect — but a third consumer makes it one. | `GLD-ACQ-03` |
| D40 | Should the feed source-strength table live in the **brain**, with `services/acquisition/scorer` importing it? Today it is copied into `next_watch` because the layering forbids the reverse import — so a correct architectural rule is manufacturing an unguarded duplicate. | `GLD-NXW-01` |
| D41 | **What would have to be true for the challenger to earn influence?** Calibrated P, AP against its base rate, both Brier scores, a degeneracy flag and rank-level divergence are all persisted — what is missing is a **stated bar**. Without one the shadow pass runs indefinitely, accumulates evidence, and never resolves. | `GLD-CHL-02`, `GLD-ML-08` |
| D42 | **Does the role-weight ordering hold empirically?** `people_matrix` states outright that cast/directors ≫ writers ≫ composers ≫ producers/DPs ≫ editors is *"the claim being made"* — seven asserted numbers feeding Group B and C4, and therefore every delete, upgrade and acquisition. Coverage (`GLD-PPL-03`) must be known first, since id-less credits likely correlate with title obscurity. | `GLD-PPL-01`, `GLD-PPL-03` |
| D43 | Does anything import `TautulliMLDatasetBuilder` / `TautulliFeatureAggregator`? One grep is all that stands between `updates/` and deletion — successors exist, methods are documented as wrong, paths are dead. | `GLD-UPD-01` |
| D44 | **What snapshot retention does forward validation need?** `horizon + margin`, or a fixed snapshot count? Today — ~7 h retention, 14-day window — the answer is "more than it has," and the cheap diagnostic is Q2: has `aggregate_forward` **ever** returned a non-zero `n_snapshots`? | `GLD-PLX-01`, `GLD-EVA-03` |
| D45 | Do `_SOURCE_SCORE` and `INTENT_SOURCE_STRENGTH` treat an **unknown** feed the same way? All nine known feeds agree exactly (verified session 27), but `next_watch` contributes 0.0 for unknown feeds while `scorer` appears to carry a default tier of 50. The divergence would first appear on a new upstream feed. | `GLD-ACQS-05`, `GLD-NXW-02` |
| D46 | Should the coordinator's last-resort floor be **1000 GB**, or a fraction of the mount as `space_targets` uses? Both are reached under the *identical* condition (mount total unreadable) — but if the total is unreadable a percentage is unavailable too, and 1000 GB is then an arbitrary absolute on an unknown drive that could be most of it. | `GLD-COORD-01` |
| D47 | **Is writeback's OR-semantics deliberate, or drift?** Alone among the seven watched bars it states no rationale — and it is the only one that writes outside the system. Related: should `writeback.dry_run` default to **True**? Every other `dry_run` guards a local, reversible, ledgered action; this one guards an unrecorded push with no undo, which is exactly the D36 case. ✅ **Session 57 — in-repo precedent found**: `CrossInstanceMove.__init__` defaults `dry_run: bool = True`, in the module that moves 60 GB files across instances | `GLD-WB-01`, `GLD-WB-03`, `GLD-RS-07` |
| D48 | Should Glidearr's own Parquet caches and `size_model/calibration` be part of the pre-destructive rollback point? Today only the *arr DBs are backed up, so a restore leaves a stale local cache against a rolled-back library — a partially rolled-back system. | `GLD-BKP-08` |
| D49 | Should `mdblist.budget()` fail **closed** (`(limit, limit)` = assume spent) rather than open (`(0, 25000)` = full headroom)? The D36 rule says yes; the counter-argument is that over-running costs only 429s — except `fetch_into` handles those by sleeping 30 s per id without a failure counter, so the two failures compound. | `GLD-MDB-02`, `GLD-MDB-01` |
| D23 | Should F3/G4 keep reading the wall clock, or take an injected `now`? The latter makes the scorer genuinely pure and replay byte-exact. | `GLD-SCO-02`, `GLD-ML-05` |
| D24 | With `space_exhaustive_downgrade` on by default, is the delete path ever actually reached? If not, the delete-gating machinery protects a path that does not execute. ✅ **Partially answered, session 28:** reachable **by design** — *"Stage 2 can therefore only ever fire after the downgrade pool is exhausted"* — and the credit-skip (§3.2) exists **specifically** to stop the backstop being *"permanently suppressed."* Narrows to the empirical half: how often is it reached? | `GLD-SPA-05`, `GLD-COORD-05` |
| D25 | Should `eval/` honour Tautulli's `watched_status`, or is the completion leg sufficient for a *historical* reconstruction? (The verdict may not have existed at the replayed time.) | `GLD-EVA-01` |
| D26 | **Is the ledger a plan snapshot or a decision record?** As built it is three columns on the item's own row — no run id, no history, and a deleted item takes its decision with it. Four registered items assume otherwise. | `GLD-LED-01`, `GLD-SCO-03`, `GLD-LIK-09`, `GLD-EVA-05`, `GLD-EVA-07` |

---

## 6. Statistics

| Metric | Count |
|---|---|
| Total enhancements registered | **240** |
| Confirmed defects (🔴) | 8 |
| Tabled | 3 |
| Resolved decisions | 1 (D13) |
| Effort `S` | 127 |
| Effort `M` | 90 |
| Effort `L` | 23 |
| Blocking decisions | 20 open |
| Recurring defect patterns (§8) | 6 |
| Areas documented so far | 22 of ~60 |

**Coverage note:** covers the entry point, the full factory layer, the three
layer roots, four services (Tautulli, Trakt, Radarr, Sonarr), `support/` and
`machine_learning/contracts/`. Plex, acquisition, coordinator, routing,
writeback, calendar, MAL, MDBList, backup and 24 further ML subpackages remain.

---

## 7. Related

- [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md) — §10 registration rule
- [`DESIGN.md`](./DESIGN.md) — top-level architecture
- Each folder's `DESIGN.md` §9 — the in-context version of these rows

---

## 8. Field notes — recurring defect patterns

These are **standing observations**, not work items. Each was found independently
in two or more folders, which makes them classes rather than incidents. Check
every remaining folder against all five.

### P-A — Signal computed, never consumed

A cache or derived artifact is built on every run, described as important, and
read by nothing.

| Instance | Cost | ID |
|---|---|---|
| `tautulli/device_codec_matrix` — "keystone signal for per-device profile selection" | Full derivation every run | `GLD-TAUT-01` |
| Trakt `translations` bucket — in `DEFAULT_SCOPE`, comment states no consumer exists | +1 Trakt call/movie × ~18k titles ≈ 14% of enrichment budget | `GLD-DMN-09` |
| **`RegistryCLI._is_expected_path`** — a written, working path-validation helper the anomaly column never called, while that column ran a substring rule that was exactly inverted. It also took a bare PATH while the only datum available was `"<file>:<line> in <func>()"`, so **no caller could have held the shape it wanted** — a producer/consumer mismatch that guaranteed it stayed unwired | The one check that could have caught a stale-mirror import, dark since it was written | `GLD-REG-11` |
| **`RegistryConfigSync.auto_hot_swap_from_config`** — called on EVERY manager init with `config.raw_data`, a dict the method cannot act on. Not merely unconsumed: **the call itself is the no-op**, ~40 times a run | Config-driven hot swap has never worked and nothing said so | `GLD-REG-12` |
| **`SonarrQualityManager`** — built, documented, filtered out of `component_dependencies` | An entire subsystem | `GLD-SON-01` |
| **`SonarrSyncManager`** — same. Pushes custom formats/naming/folders/tags into Sonarr; Radarr's equivalent *does* run | An entire subsystem | `GLD-SON-01` |
| **`_recycle_to_fund_acquisition._why`** — per-series refusal reasons, computed for EVERY refused series but emitted only under `if not funded:`; the first partial fund discarded all four (See, AoT, TBBT, Abbott & Costello). **Inside the P-A-prevention scaffold itself** — the pattern's most self-referential instance yet | The one diagnostic a partial fund needs | `GLD-ACQ-21` |
| ✅ **`expected_size_gb`** — computed by the resolver for every acquisition candidate, described in the decision table's `~size` column, and consumed by exactly ONE thing: `_size_str`, a display formatter. The one field a space-aware acquisition budget needs, priced and thrown away on every run — until session 91, when it became the byte budget's currency (`GLD-ACQS-13`). The consumption is the fix: the signal now gates selection, prices the 4K companion, and rides the deferred queue to its flush-time ledger commit | Every acquisition ran size-blind while carrying sizes | `GLD-ACQS-13` |

**Escalation:** the first two instances were unread cache keys. The Sonarr pair
are **whole subsystems** — imported, typed, documented, and disconnected. The
pattern is not limited to data; it applies to code paths.

**Why it recurs:** producers are written before consumers, the consumer slips,
and nothing links the two. The producer looks healthy forever.

**How to detect:** for every cache key written, grep for a reader. A key with
zero readers is either dead or waiting — and the doc must say which.

### P-B — Guard narrower than it appears

A mechanism exists and is trusted, but its actual coverage is smaller than its
name implies.

| Instance | Gap | ID |
|---|---|---|
| `brain_purity.py` — guarded-subpackage walk | Flat top-level brain modules (`watchhistoryaggregator.py`, `storage_estimator.py`, `transcode_analyzer.py`, `genre_predictor.py`, `penalty.py`, `upgrade.py`, `plan_summary.py`, `size_calibration.py`) are outside it entirely | `GLD-ML-01` |
| `_GUARDED_SUBPACKAGES` list | `foundation`, `thresholds`, `discovery`, `labels`, `challenger`, `updates` are genuinely pure but absent | `GLD-ML-02` |
| `brain_purity.py` — scope of the rule | Guards **imports only**. A brain module could still write to disk | `GLD-ML-03`, `GLD-HOOK-04` |
| 🎯 **…demonstrated, session 25** | `machine_learning/updates/` does `open()`, `os.makedirs`, `json.dump` and `df.to_csv` — importing only `json`, `os`, `pathlib`, `pandas`. **An import-only guard passes it.** It also imports `factories.cache.make_json_safe`, breaching `ARCHITECTURE.md`'s *"`ml → contracts` only"* — also unguarded. The abstract gap, made concrete | `GLD-UPD-02`, `GLD-UPD-04` |
| `dry_run` | Distributed invariant, no central enforcement — every manager individually remembers | `GLD-ORCH-01` ⏸ |
| 🎯 **ROOT CAUSE FOUND, session 76** | `tautulli/__init__.py` states it outright: *"dry_run must be captured explicitly — **`BaseManager` does NOT**, so without this every submanager built from `init_args` would silently default to False (the documented propagation footgun)."* **The invariant is distributed because the base class does not capture it.** ✅ **FIXED session 77**: `BaseManager.__init__`'s parent-link block already inherited `logger`/`config`/`global_cache`/`validator` — `dry_run` was the one field omitted. Now resolved with explicit-kwarg → pre-`super()` → parent → `False` precedence. ⚠️ Three subclasses still **overwrite** it after `super()` (`GLD-ORCH-02`) | ✅ `GLD-ORCH-01` |
| 🔴 **…and it has already failed, session 34** | `sonarr/__init__.py` still carries the inline record: `dry_run=self.dry_run,  # without this the cache (and its episode-file ops: acquisition, sync, JIT) **ran LIVE even in dry_run=True sessions**`. The invariant failed in this exact constructor, and the fix is a hand-passed kwarg with a comment — not a check | `GLD-SON-03`, `GLD-ORCH-01` |
| 🔴 **…with a measured cost, session 29** | Two `dry_run` resolutions of markedly different rigour, and **the weaker one guards the less reversible operation**. `coordinator/` (deletes files, but tracks + restores every one): kwargs → parent → Main, *"never silently default"*, plus an independent `backup_gate`. `writeback/` (pushes to third-party accounts, no undo): kwargs → parent, **defaults `False` = live writes** | `GLD-WB-03` |
| 🔴 **…and it is systemic, session 32** | `calendar/` has the **same weak two-level form** and issues `monitored=true` PUTs to both *arrs. **Three data points: the strong form appears in ONE manager, the weak in TWO, and the split does not track blast radius.** Not a writeback-local fix | `GLD-CAL-01`, `GLD-WB-03` |
| **Scoring axis ↔ likelihood `untouched_base`** | Welded at gain 1.0 with **no detector**. Group D v2 translated the axis −13 points and untouched titles reaching 1080p collapsed **456 → 8 (−98.2%)**. Caught by manual measurement, not by any check | `GLD-LIK-01` |
| Pre-commit hooks | Exit 0 when Python is absent — advisory, not guaranteed | `GLD-HOOK-02` |
| **`playlists/spoiler.py`** | Written *"so it can be pinned by property tests"* — **no `test_spoiler.py` exists**. `caps.py` (skip-don't-stop), `engagement.py` and `models.py` are also untested. The presence of a verification function is not the same as its being run | `GLD-PLY-01`, `GLD-PLY-03` |
| **Test coverage is uncorrelated with risk** | `acquisition/` has a test per module, each ≥ its source. `discovery/` has a test per module. `challenger/` has **14.7 KB and zero tests**; `playlists/` leaves the spoiler invariant, the size cap, engagement and models untested. Coverage is per-package habit, not risk-driven | `GLD-CHL-01`, `GLD-PLY-01` |
| 🎯 **…and naming compounds it, session 40** | `sonarr/series/`'s seven tests each name a **behaviour** — `test_exhaustive_downgrade`, `test_monitor_by_watchability`, `test_quality_upgrade_floor`, `test_space_pressure_realize` — so a reader can tell what is guaranteed without opening them. Contrast `test_client.py` / `test_build.py`, which name the file. **The model to cite when a coverage item is worked** | `GLD-SER-04` |
| Service purity (no scoring in `services/`) | Review-enforced only, unlike brain purity | `GLD-MGR-01` |
| `SonarrManager.critical_keys` | Declares `"quality"` critical, but `quality` is filtered out of `all_component_classes` — the "critical" label guarantees nothing | `GLD-SON-11` |
| **"Pure" means three different things in the brain** | `affinity/genre_affinity.py` and `features/completion_stats.py` say *"no logging"*; `sizing/size_model.py` says *"no logger, cache, or registry"*; `sizing/storage_estimator.py` claims *"Pure forecasting"* while importing `LoggerManager` and logging. `brain_purity` catches none of it — a logger is not an HTTP client, a service import, or a `*_api` module | `GLD-SIZ-02`, `GLD-SIZ-06` |

**Why it recurs:** a guard is written for the case that prompted it, then trusted
as though it were general.

**How to detect:** for every guard, ask what it does **not** cover and write that
down in §6 of the folder's DESIGN.

### P-C — Absent conflated with empty

Missing data is treated as zero/empty rather than unknown, which converts a
fetch failure into a confident wrong answer.

| Instance | Consequence | ID |
|---|---|---|
| Trakt returning `[]` on 429 | Pruner reads "watchlist is empty" and prunes everything | fixed; audit others via `GLD-TRKT-02` |
| **The registry's two entry shapes** — `register()` writes `{instance, origin, parent_name}`, `set()` writes the bare object. `find_by_attr` and `load_config_and_propagate` `getattr`/`hasattr` the **wrapper**, so both matched **nothing, always** — and an empty result is indistinguishable from "nothing has that attribute" | Two whole lookup facilities that could never return a row, silently | `GLD-REG-13` |
| `score is None` treated as 4K-eligible | Every unscored title grabbed UHD | fixed; invariant I4 |
| **`getattr(global_cache, 'memory_cache', {})`** — the attribute is `.memory`; `memory_cache` has never existed. The `{}` default made a wrong attribute name **indistinguishable from an empty cache**, so `cache_keys` reported `[]` on every manager forever. **The default is the whole defect** — `getattr` with no default would have raised on the first run | The init summary's cache field was structurally incapable of being non-empty | `GLD-MGR-11` |
| `load_summary` — absent vs failed | False `❌` for `instance_manager`, `radarr_cache` | `GLD-MIX-01` |
| ✅ **Fixed precedent, session 7** | `is_watched` was `watch_count > 0`, incremented per Tautulli row **regardless of completion**. A 30-second sample marked a file watched, started the 3-hour grace clock, bought an engagement floor of 50, and counted as a rewatch. Measured: **9.8% of episode plays, 36.2% of movie plays** were samples. Fixed by `lifecycle/watched_definition.py` — **the reference example of this pattern being caught and closed** | ✅ Done |
| **Eval's own watched bar** | `eval/replay.py` + `eval/forward.py` default `watched_threshold=0.9` and never import `lifecycle.watched_definition` (production: 85, preferring Tautulli's `watched_status`). The module created to be "the ONE definition" has a second definition living beside it | `GLD-EVA-01` |
| **…and a third and fourth, session 11** | `labels/labeling.py` uses `movie_pct_min=90` (documented as *"≈ the completions threshold 0.9"*) and `episode_pct_min=50`. **Four bars total.** `watched_definition` is imported by the two producers and by nothing else in the brain | `GLD-LAB-01` |
| **…and a fifth, session 14** | `features/completion_stats.py` hardcodes `percent_complete >= 90` in **both** `series_completion_stats` and `episode_completion_stats`. **Five bars total** — and **two modules now count *episode* completion at different bars** (90 here, 50 in `labels`) | `GLD-FEA-03` |
| **…and a sixth, session 15** | `affinity/group_completion.py` — `completion_threshold` **0.9**, `grace_threshold` **0.7** for designated `grace_members`. **Six bars total.** This one is arguably the *most* principled (per-group, per-member, configurable, models members who stop before the credits) — which reframes unification: the single bar may be the under-specified one | `GLD-AFF-03` |
| 🔴 **…and a seventh, session 29 — and it differs in SEMANTICS** | `writeback/trakt_history.py` uses `watched_status == 1` **OR** `pct ≥ 85`. Production **prefers the verdict**: `watched_status` decides when present, `pct` is only a fallback. So a row Tautulli marked **unwatched (0)** or **partial (0.5)** with `pct ≥ 85` is **not watched** internally but **is pushed to the user's permanent Trakt history** — where it then feeds back as evidence next run. 🎯 **Session 67 — the rule is stated at the PRODUCER.** `tautulli/watch_history`'s `watched_status` admission note: consumers *"can honour the server's answer **instead of inventing a second, disagreeing definition**."* Writeback's divergence **contradicts a contract written where the field enters the system** | `GLD-WB-01` |
| ✅ **Fixed precedent, session 15** | `affinity/genre_affinity.build_library_index` exists because Tautulli's *sampled* metadata index leaves holes: *"a low-volume, movie-only profile whose handful of rating_keys never made the sampled index would otherwise score **affinity=0 and collapse to the flat household ranking**."* Absent metadata reading as "no taste" — **caught and fixed** with a stable title join | ✅ Done |
| ✅ **Fixed precedent, session 17** | `quality_analytics/transcode.device_codec_matrix` exists *specifically* to disambiguate the event-only tally, *"which can't tell '0 transcodes = safe' from '= never tried'"* — by tracking `direct` **and** `transcode` counts. Partnered with `codec_direct_play_rate` returning `None` on no sample, and `none_p=0.5` so *"a cold viewer neither vetoes nor forces a codec."* **The most P-C-disciplined package in the brain** | ✅ Done |
| ✅ **Fixed precedent, session 18** — *and the most consequential yet* | `classification/franchise.py`: Radarr v4/v5 renamed `collection.name` → `collection.title`. `coll.get("name")` returned `None`, **no collection registered, no movie was a franchise entry, and category-1 franchise protection was inert on every modern Radarr** — silently. Fixed by reading both keys. Absent-as-empty **turning a protection off entirely** | ✅ Done |
| ✅ **Fixed precedent, session 21** | `acquisition/demand.py` distinguishes **absent** affinity from **weak** affinity: a user with no history contributes the **popularity prior**, a user whose match is below 0.15 contributes **0**. The naive one-call implementation would return ~0 for both, making every household with a new account systematically under-value every candidate — indistinguishable from lower demand | ✅ Done |
| 🎯 **…applied to a SCHEMA MIGRATION, session 67** | `tautulli/watch_history` admitting `watched_status`: *"**ABSENT** on rows cached before this key was admitted … `watched_by_tautulli` falls back to `percent_complete` (which those rows DO carry), so **the transition is silent: no historical row reads as unwatched merely because the cache has not cycled yet**."* Adding a field to a projection is exactly where P-C bites — absent-as-zero would have marked the **entire watch history** unwatched. Repeated immediately below for `video_decision`/`audio_decision` | ✅ Done |
| 🎯 **…and expressed in a type signature, session 45** | `contracts/feature_rows.py` distinguishes **binary** from **tri-state** deliberately: `is_watched: bool = False` (*"nobody watched it"* is a real answer) vs `all_household_watched: bool \| None = None` (*"has everyone watched it"* is **unanswerable** with no household configured, and `False` would wrongly assert "not everyone has"). The Parquet column carries no such distinction — `NaN` for both | `GLD-FEA-01` |
| 🎯 **The counter-example, session 22** | `next_watch` **refuses to derive a plausible value.** Plex's watchlist union has no per-item timestamp; the rolling snapshots exist and are timestamped, so a `min()` over them *would* produce a date — but they retain **~7 hours** on this install, so it would say every title was added yesterday: *"a FABRICATED timestamp, and a decay applied to it would be **noise dressed as evidence**."* Every ingredient for a plausible-looking column was present; the feature was left off for that source with the reason recorded. **The strongest antidote to P-C in the repo** | ✅ Done |
| Incomplete enrichment | Missing signal groups contribute zero → scores come out **uniformly lower**, indistinguishable from "the household likes this less." Silently biases every downstream decision toward deletion | `GLD-ML-04`, `GLD-TAUT-03` |
| ✅ **Refinement, session 6** | The correct fix already exists in-repo. `scoring/device_fit.py` **renormalises a missing axis out of the weighted risk** rather than scoring it zero. `GLD-ML-04` is extending a working pattern to the group level, not inventing one. Note both strategies live side by side in `feature_rows.py` Group D | `GLD-CON-02`, `GLD-CON-03` |
| 🎯 **Stronger refinement, session 27** | `services/acquisition/scorer.py` renormalises at the **TOP LEVEL**, across all six components, with a dynamic denominator: *"any signal that's unavailable for a candidate is marked 'n/a' and **dropped from the weighted average rather than counted as zero**."* That is exactly the scope `GLD-ML-04` asks for, already shipping in the sibling scorer. **`GLD-ML-04` is a port, not a design task** | `GLD-ACQS-01` |
| 🎯 **…and the RENDERER is where it bites, session 91** | `services/acquisition/scorer.py` sets `evidence["people"]` **only** when the candidate resolves in the people-matrix (`if roles:` → `people_ev`), so *absent* (never computed) and *`matched=0`* (computed, nobody qualifies) are different facts. The prose breakdown distinguished them **only by whether the `cast/crew:` line existed at all**. Converting that output to a table is what made it dangerous: **a table cell has no absent rendering by default** — blank or `0` silently asserts a check that never happened. Fixed at build time in `breakdown.py` (`people_scored` as a separate boolean; `people_matched`/`people_affinity` left `None`; absent renders the literal `not scored`) and again at the frame boundary (`to_dataframe` pins `Int64`/`Float64`/`boolean` so a stray `fillna(0)` in a website template cannot reintroduce it). 🔴 **The cohort shape is the open half**: every MAL-sourced title is unscored and every Trakt-watchlist title is scored (10/10 vs 15/15, 2026-08-19) — the forward map is Trakt-daemon-derived and does not cover MAL anime — so with a dynamic denominator the two cohorts are normalised over **different signal sets** and then compete for one capped `max_adds_per_run` budget | `GLD-ACQS-11` |
| Indexer timeout under load | Returns false `no_results` — indistinguishable from the release genuinely not existing | `GLD-SON-02` |
| 🎯 **…and "unknown" must not reset a retry budget, session 91** | The size-anomaly attempt ledger detects "did the replacement land?" by comparing the file's recorded size against its current one. An ABSENT or unparseable size is UNKNOWN — reading it as *different* would reset the budget on every run and restore the exact unbounded churn the ledger exists to stop, while reading it as *0* would look like a change every time. `_as_int` returns None for both, and only a KNOWN-and-different pair counts as changed. The same module also documents the case where conflating IS correct: an absent ledger entry and a zero-attempt entry mean the same thing (never tried), stated explicitly rather than left implicit — which is the discipline this section asks for, applied in both directions | `GLD-SON-13` |
| 🎯 **…and the fail DIRECTION is part of the type, session 91** | The acquisition byte budget (`GLD-ACQS-13`) inverts the codebase's fail-open convention on purpose. Every other space gate reads an unreadable disk as `free=inf` so a transient never blocks an add — safe, because a COUNT cap bounds the damage at N. Under an UNCAPPED byte budget the same convention means "unlimited adds, no budget": the failure the feature exists to prevent. So unreadable free space, a corrupt ledger, or a raising cache all collapse to the BOUNDED legacy count cap (and a configured cap of 0 falls back to 10, never unlimited). Three P-C calls inside one feature: absent ledger key = genuinely-first-run empty (safe, stated); present-but-wrong-typed or raising = unknown → fallback; unparseable entry sizes overstate in-flight at the default price, never 0 — in every case the unknown may only make the system LESS aggressive | `GLD-ACQS-13`, `GLD-ACQS-14` |

**Why it recurs:** `None`, `[]`, `0` and "key absent" are all falsy, and the
distinction only matters at the moment something goes wrong.

**How to detect:** for every nullable value, ask what *unknown* should do versus
what *zero* should do. If the answer is the same, say so explicitly.

**This is the most dangerous pattern in the codebase.** Three of the four
instances were live bugs. The fourth is live now.

### P-D — Failure with no detector

Something can break in a way that produces no error, no log line, and no symptom
until much later.

| Instance | Time to notice | ID |
|---|---|---|
| Enrichment daemon dies | Weeks — symptom is scores quietly not improving | `GLD-DMN-01`, `GLD-TRKT-03` |
| Parent link never resolves | Never — manager silently uses its own logger/config | `GLD-MGR-03` |
| `set_bulk()` without `save()` | Never — changes lost at exit | `GLD-CFG-02` |
| Typo'd config key | Behaves exactly like an intentional default | `GLD-CFG-01` |
| **A warning branch guarded on an attribute that does not exist** — `auto_hot_swap_from_config`'s else-branch logged only `if hasattr(self.registry, "logger")`, and `RegistryManager` never defines one. The no-op's own detector was itself unreachable, so the method failed *and* reported nothing, every run | Never — discovered only by reading the method | `GLD-REG-12` |
| Partial history pull | Affinity computed from half the data, looks valid | `GLD-TAUT-03` |
| **Partial ledger roll-up** | A skipped instance or an unstamped row makes the dry-run plan show *fewer actions* — indistinguishable from the system deciding less. For the artifact whose whole purpose is completeness | `GLD-LED-02`, `GLD-LED-03` |
| Cache key served past TTL forever | Silently outdated scores | `GLD-CACHE-04` |
| 🔴 **Snapshot retention vs measurement window** | `plex/watchlist/snapshot/` retains **~7 hours**; forward validation measures a **14-day** window. Every snapshot is gone ~48× before it can be evaluated — so the watchlist blind-spot measurement **has never been able to run**, and `aggregate_forward` honestly reports `{"n_snapshots": 0}` rather than erroring. **Two correct components, one inoperative subsystem, no detector** | `GLD-PLX-01`, `GLD-PLX-02` |
| Mis-tiered movie placement (2+ Radarr instances) | Never — 4K-worthy title sits on `standard` looking normal | `GLD-RAD-04` |
| `movieRootFolders` empty → classification discarded | Never — every movie lands in `/standard` | `GLD-RAD-05` |
| Sonarr profile/format sync not running | Never — Sonarr config simply drifts from intent | `GLD-SON-01` |

**Why it recurs:** degradation was designed for (correctly — goal G4), but
degradation without a *signal* is indistinguishable from health.

**How to detect:** for every "degrade gracefully" path in §6, ask how the
operator learns it happened.

### P-E — Duplicate implementations

> **⚠️ Corrected, session 5.** Initially logged as "eight modules in two
> locations, no authority declared." Reading the files shows that is **mostly
> wrong**. Six of the eight are documented re-export shims with a planned
> deletion. The residue is a different and more interesting finding — P-F below.

**Six documented re-export shims** — benign, tracked, deletion planned at
`MIGRATION.md` Step 10. Each carries a docstring naming its migration step and
stating there is exactly one implementation:

| Shim | Real implementation | Step |
|---|---|---|
| `support/utilities/size_model.py` | `machine_learning/sizing/size_model.py` | 1 |
| `machine_learning/storage_estimator.py` | `machine_learning/sizing/storage_estimator.py` | 1b |
| `support/utilities/watch_likelihood.py` | `machine_learning/likelihood/watch_likelihood.py` | 4 |
| `support/utilities/library_classifier.py` | `machine_learning/classification/library_classifier.py` | 5a |
| `machine_learning/plan_summary.py` | `machine_learning/ledger/plan_summary.py` | 6 |
| `support/utilities/space_targets.py` | `machine_learning/space/space_targets.py` | 7a |

**One non-shim instance, added session 12.** `foundation/formulas.py::downgrade_credit`
mirrors `services/coordinator/space_coordinator`'s arithmetic rather than
delegating — the only one of its twelve functions that does. **Mildest instance
seen**: an equivalence test asserts the two agree, so the mitigation is real, but
it is only as durable as the test. `GLD-FND-03`.

**A second, added session 22 — and the layering causes it.**
`next_watch.INTENT_SOURCE_STRENGTH` hardcodes
`services/acquisition/scorer._SOURCE_SCORE ÷ 100` with **no test and no import**.
It cannot delegate: `ARCHITECTURE.md`'s one-way rule forbids `ml → services`, and
`brain_purity` enforces it. **P-E produced by a correct architectural rule.** The
fix inverts the copy — move the constant into the brain and have the service
import it (`GLD-NXW-01`).

**A third, session 91 — caught while being created, and closed in the same edit.**
Moving the elevation breakdown into `services/acquisition/breakdown.py` gave that
module its own `_fmt_votes` / `_fmt_feed`. The manager's `AcquisitionManager`
statics of the same names had exactly one production caller — the prose emitter
that was being deleted — so leaving them would have created two live label
formatters that the log tables and the website frame could drift between. Both
were removed in the same edit, with a comment at the removal site naming
`breakdown.py` as the single source. **The only P-E instance in this register
that was authored and closed inside one change**; the pattern is normally found
long after the second copy has diverged.

They re-export with `import *` plus explicit private names, so the function
objects are identical and shared state (e.g. `size_model`'s calibration overlay)
stays consistent regardless of import path. **This is correct migration hygiene,
not drift.**

**One deletion constraint worth recording:**
`support/utilities/library_classifier.py` carries a dual-import fallback —
`scripts.managers...` first, falling back to bare `managers...` — because
`support/tools/router_show.py` and `router_movie.py` run standalone with only
`scripts/` on `sys.path`. Deleting that shim breaks both tools unless they are
updated first.

**Why it recurs:** mid-migration state. Tracked and intentional.

**A third, added 2026-08-10 — and this one is a WRITE that overwrites a correct value.**
Two registration paths write the same registry row: `BaseManager.__init__` calls
`registry.register(...)`, and then `ComponentManagerMixin.register()` calls it
**again** for the same object. Both are "correct"; the second simply runs last and
wins. Because `factories/mixins/` was missing from `utility_keywords`, the origin
walk stopped on the mixin, so the second write replaced the true origin with
`component_manager.py` — the same file, for dozens of rows. **Duplicate
implementations are usually a drift risk; this pair was a data-loss one**, and the
damage was invisible because a plausible path reads exactly like the right path
(`GLD-REG-14`).

**How to detect:** read the docstring before assuming drift. A shim says so.
And when two paths write the same field, establish which one runs LAST — not
which one is correct.

### P-F — Undocumented impure zone inside the brain

The genuine residue of P-E. Two `machine_learning/` root modules are **not**
shims — they are real implementations that perform I/O:

| Module | What it does | Why it cannot be pure |
|---|---|---|
| `machine_learning/size_calibration.py` | `SizeCalibrator` — reads `global_cache`, walks the registry, loads Parquet, persists to `size_model/calibration` | It is the orchestration half. The pure math (`fold_stats`, `compute_calibration_table`, `calibration_is_fresh`, `movie_runtime_min`) lives in `sizing/size_calibration.py` and is imported from there |
| `machine_learning/transcode_analyzer.py` | `MLTranscodeAnalyzer` — takes a `cache` handle and calls `get_or_generate_cache` | Full implementation at root; a separate `quality_analytics/transcode_analyzer.py` also exists |
| **`machine_learning/labels/first_run.py`** | Reads history + Parquet, writes an atomic marker, **dynamically loads and executes a CLI** from `support/tools/` via `importlib`, captures its stdout | The cold-start fix has to touch disk. **Inside a subpackage, not at root** — and the `importlib` load would evade an AST import-guard entirely, so `labels` would appear to pass while containing the most impure module in the brain | 

So `machine_learning/` has an **undeclared two-tier structure**:

```
machine_learning/
  <root>.py          ← impure orchestration wrappers. Do I/O. NOT purity-guarded.
  <subpackage>/      ← pure decision cores. No I/O. Purity-guarded.
```

The split is defensible, and in `size_calibration.py`'s case clearly deliberate —
pure math in the guarded subpackage, I/O wrapper at root. **But nothing documents
it.** "Brain purity" reads as an absolute, and `hooks/brain_purity.py` enforces
it only on subpackages, which makes the carve-out look like a coverage gap (P-B)
rather than a design decision.

**Consequence:** `GLD-ML-01` / `GLD-ML-02` were logged as "close the purity gap."
Part of that gap is **load-bearing** — moving `SizeCalibrator` into `sizing/`
would make it fail a guard it is currently exempt from for good reason.

**Why it recurs:** a layering rule stated absolutely, with an unstated exception.

**How to detect:** for any module exempt from a guard, ask whether the exemption
is intentional. If it is, document it as a tier, not a gap.

### P-G — Propagated unverified claim

A numeric or behavioural claim is recorded once, then repeated across many
documents without anyone checking it against the implementation. Each repetition
increases apparent confidence while adding zero evidence.

| Instance | Claim | Reality | ID |
|---|---|---|---|
| **"4K requires score ≥ 70 on the 100-point scale"** — asserted in six `DESIGN.md` files plus `DOCS_CONVENTIONS.md` §7 during this sweep | ≥ 70 | The watchability ladder's 4K entry rung is **38** (p99.5). Acquisition gates are `watch_likelihood.uhd_cutoff` **75** and `routing.movies.4k_dual_min_score` **75**, which `SCORING_GROUPS.md` says *"live on a different scale."* 70 matches neither | `GLD-SCO-01` |
| "`SonarrQualityManager` is the only filtered-out component" — from `sonarr/README.md` | one | **Two** — `SonarrSyncManager` is also absent from `component_dependencies` | `GLD-SON-01` |
| "Eight modules duplicated, no authority declared" — asserted in §8 P-E before reading the files | drift | **Six of eight are documented re-export shims** with a stated deletion step | `GLD-ML-15` |
| **"`cache/` and `quality/` each carry `test_size_anomaly_*.py`"** — asserted session 4 from a directory listing | two test pairs | **`quality/` has NO test files at all** — ten files, none tests | `GLD-SON-10` ⚠️ **retracted s36** |
| **"`filesizes.py` is dead code"** — asserted session 36 from **one** construction path (`quality/` never loads) | unreachable | **Constructed** via `orchestration/quality.py` — but `orchestration.run()` never invokes `quality`, so still **not demonstrably called**. Corrected s37, corrected **again** s38 | `GLD-SIZ-04` ⚠️ **3× revised** |
| **`ARCHITECTURE.md` subpackage map** — `routing/` row names `machine_learning/instance_selector.py` as the source to absorb | file exists | **No such file** at the ML root | `GLD-ROU-02` |
| **`brain_purity.py` docstring** — names `profile_selector.py` as one of "two legacy FLAT top-level modules" | at package root | It lives in **`quality_analytics/`** and is therefore already guarded. Only `watchhistoryaggregator.py` is genuinely flat | `GLD-ROU-06` |
| **`quality_analytics/__init__.py`** — *"existing-ML, partly stubs. Lowest priority (Step 9)"*; `ARCHITECTURE.md` lists only `transcode_analyzer` for it | mostly stubs | **7 of 8 modules implemented**, ~92 KB with six test modules — the heaviest-tested package in the brain. The one stub is the only module the map names | `GLD-QAN-03` |
| ✅ **Verified CONSISTENT, session 32** | `thresholds/registry.py`'s `mal_min_watchability` spec (constant 20, `routed=False`, *"scores UNOWNED titles, which never enter the snapshot store"*) and `services/calendar/__init__.py`'s docstring (*"default 20 … unowned entries realistically score 0–25 since the household-intent groups are all 0"*) **agree independently on both the value and the reason.** Recorded because P-G tallies the opposite | — |
| **`ARCHITECTURE.md`** — describes purity enforcement as *"a lint rule (`grep` gate in CI)"* plus a second rule forbidding old-scorer imports outside the brain | CI grep, two rules | It is a **pre-commit AST hook** over 17 subpackages, and the second rule is **not implemented** at all | `GLD-ROU-06`, `GLD-HOOK-02` |

**Why it recurs:** a convention note, a README line, or a prior summary reads as
a source. It is a *claim*. Documentation is especially prone to this because
writing a doc feels like recording knowledge rather than asserting it.

> 🎯 **The lesson from `GLD-SIZ-04`'s three revisions.** Each time I answered
> *"can this be reached?"* when the useful question was *"is this called?"* — and
> each layer of construction looked like an answer without being one. **Tracing
> constructors upward proves reachability, never invocation.** For a "does X run?"
> question, the only sufficient evidence is **finding the call site**. Reachability
> and invocation are different claims and need different evidence.

**Two of the three instances above were introduced by this documentation sweep
itself.** That is the pattern's defining property: it is generated by the act of
documenting, not discovered in the code.

**How to detect:** before repeating a number, a count, or an "only X does Y"
claim, open the file. If the source is another document rather than code, mark
it unverified rather than restating it as fact.

**How to prevent:** a numeric invariant in a doc should cite the constant it
comes from — `uhd_cutoff` (75), not "the 4K threshold." A named constant is
checkable; a prose figure is not.

---

## P-H — the saturating signal

**Independently rediscovered three times**, in three unrelated subsystems, each
time as a correction to a design that would have pinned a signal at its ceiling:

| Package | The saturation avoided | Quote |
|---|---|---|
| `likelihood/` | Gain change pinning untouched titles at `affinity_cap` | *"titles pinned at the cap are indistinguishable — a constant"* |
| `next_watch/` | Solo watchlisters at full cap, on a **258-of-259 solo** distribution | *"the member term is invisible (every title already at cap)"* |
| `people_matrix/` | Undecayed cast billing | *"floods the affinity vector with people the household has no opinion about"* |
| `services/acquisition/` | Uncapped noisy-OR — one strong genre would drive every multi-genre title to the ceiling | `_GENRE_SATURATION = 0.80` *"leaves headroom for corroborating genres to push toward 100"* |

**The principle:** *a signal that saturates carries no information.* If most of
the population sits at the ceiling, the term has become a constant and the
distinction it was added to draw is unrepresentable.

**How to spot it:** ask what fraction of the real distribution lands at the
maximum. Above ~90% and the signal is a constant with extra steps.

**Unlike P-A–P-G, this is not a defect pattern — it is a *design instinct the
codebase already has*.** All three instances are the author catching it before
it shipped. It is recorded so the fourth subsystem inherits the reasoning instead
of rediscovering it. `GLD-PPL-05`.

---

## The fail-direction rule (D36) — and its largest instance

> **On unknown input, fail toward the outcome that changes nothing.**

Derived in `discovery/DESIGN.md` §3.3 to reconcile four modules that appeared to
disagree. `services/backup/` is the same rule at **whole-run scope**, and the
strongest safety property in the codebase:

> cannot prove a validated rollback point exists → **the ENTIRE RUN becomes
> read-only**

Not "skip deletion" — every destructive primitive across both services reads one
gate and logs *"would …"* instead. A single unverifiable backup on one instance
disarms writes for the whole run. Validation is CRC-level, not existence-level.

The one caveat is `GLD-BKP-07`: a central gate's guarantee is only as broad as
the set of primitives that consult it, and nothing enumerates that set.

---

## Confirmed-absent vs transient — the inverse of P-C

Four subsystems independently store a *looked-up-and-genuinely-absent* answer
differently from a *transient failure*, so the first is never re-paid for and the
second is never made permanent. **None references the others.**

| Module | Confirmed absent | Transient |
|---|---|---|
| `services/plex/` | Discover miss memoised forever | Hop failure stays retryable |
| `ml/sizing/file_comparison` | `expected ≤ 0` ⇒ no opinion | — |
| `ml/quality_analytics/` | `None` on no sample | `none_p = 0.5` neutral prior |
| `services/mdblist/age_cache` | `null` cached — *"the cache is the resume state"* | Skipped, not cached |

Where **P-C** is this distinction being *missed*, this is it being *made*. Like
**P-H**, it is a design instinct the codebase already has, recorded so the fifth
subsystem inherits it rather than rediscovering it. `GLD-MDB-05`.

---

> **§8 ends here.** P-A–P-G are defect patterns. P-H, the fail-direction rule,
> and confirmed-absent-vs-transient are **design instincts the codebase already
> has** — recorded so they are inherited rather than rediscovered.

---

## P-I — built, caller unwired

**Distinct from P-A.** P-A is a *value* computed and never read. P-I is a
*capability* fully built — often tested, often documented — that **nothing
invokes**. Eight instances:

| Capability | State |
|---|---|
| `sonarr/sync/` | **Five appliers** (custom formats, naming, folders, media, tags), `dry_run`-correct, individually documented. `SonarrSyncManager` has no `run()` and is not in `component_dependencies` |
| `SonarrRepairManager` + `orchestration/repair.py` | **Two-level dormancy** — 15 sub-managers, a 15-method facade with `run_all_repairs`, and `orchestration.run()` never invokes `repair` |
| `CrossInstanceMove` | Full 4-operation dual-version actuator, 15.8 KB of tests, *"the caller owns eligibility"* — caller unlocated |
| `choose_codec_profile` | Complete, pure, heaviest-tested code in the brain. **Both** quality selectors explicitly defer to *"the device→codec stage"*; neither invokes it |
| `SonarrSeriesQualityManager` | `run()` is a log line; the real entrypoints are methods someone must call |
| `orchestration.run()` | Invokes **2 of 11** constructed sub-orchestrators |
| `playlists/spoiler.is_spoiler_safe` | Written *"so it can be … re-checked at runtime before a write"* — wiring unverified |
| `foundation/` | Zero runtime adopters *(by design — an adoption point, not a defect)* |

### 🎯 Why it matters more than it looks: P-I subsystems accumulate latent bugs

Code that never executes never fails, so errors settle into it unnoticed. **Three
of the sweep's confirmed defects were found inside P-I subsystems, and could only
have been found by reading:**

| Defect | Where |
|---|---|
| `cls(**critical_components[name]["init_kwargs"])` — subscripting a class, `TypeError` ×4 | `SonarrQualityManager` (`GLD-SQ-01`) |
| `detect_unexpected_entries()` — a method that does not exist, untrapped at step 11 of 14 | `orchestration/repair.py` (`GLD-REP-11`) |
| `sonarr::{inst}::series` — a cache-key format nothing produces | `repair/anomaly.py` (`GLD-REP-08`) |

**So P-I is a predictor.** Finding a dormant subsystem is a reason to read it
*closely*, not to skip it — the defects are there precisely because nobody has run
the code.

### How to tell P-I from genuinely dead code

Checklist **Q9**: does the module or its docstring **name its entrypoint**?

| Module | Names its caller? | Verdict |
|---|---|---|
| `series/space_pressure.run()` | ✅ `orchestration.run_space_pressure_downgrades` | Deliberate facade |
| `validator.audit_bootstrap_instances` | ✅ `SonarrInstanceManager.__init__` | Deliberate facade |
| `CrossInstanceMove` | 🟡 *"the caller supplies…"* | Actuator awaiting a caller |
| `SonarrRepairManager` | ❌ nothing | Genuinely unwired |

### The backlog implication

Three register items — `GLD-REP-07`, `GLD-RAD-01`, `GLD-SERQ-01` — were filed as
*"not built"* and resolved as *"built, caller unwired."* `GLD-RAD-01` alone dropped
from **L** to **S/M**.

**Before estimating any "implement X" item, check whether X already exists.** The
remaining effort is frequently a wiring change, not a build.

---

## Identity discipline — seven instances, one gap

Every module that joins two systems faces the same question: *what is the stable
key?* Seven have answered it; the answers vary with what was available.

| Module | Key | Guards |
|---|---|---|
| `ml/discovery/occupancy` | ownership id | *"never a title (remake/same-name collisions)"* |
| `ml/people_matrix` | `person_tmdb_id` | *"a name-keyed graph could not feed it at all"*; bool rejection |
| `services/plex` | Plex uuid | Sanitised-name collision map, **fail-closed attribution** |
| `services/mdblist` | tmdb + **separate files per id-space** | Movie/show tmdb ids overlap |
| **`services/mal/id_bridge`** | **title — no id exists** | **Pool restriction (–84% / –93%) + 6 aliases + ambiguity-drop** |
| `services/calendar` (MAL) | title | ❓ weaker — `GLD-MAL-01` |
| `ml/labels/labeling` | series title | ❌ **none** |

**The lesson is in row 5.** The first four had an id to fall back on. `id_bridge`
did **not** — and built three composing guards anyway, with a worked example
(`hunter x hunter`, tvdb 79076 vs 252322) showing the ambiguous alias dropped and
the unambiguous one resolving correctly.

So *"no id is available"* does not entail *"match naively."* `labels/` has the
same constraint and no guards; `GLD-MAL-02` proposes porting ambiguity-drop
there, which converts silent **mis-attribution** into silent **omission** —
strictly better, and available today without waiting for `GLD-LAB-02`.

### Reusable audit checklists produced so far

| Checklist | Where | Applies to |
|---|---|---|
| **The three Trakt bugs** — (1) paginate past 100, (2) send `extended=full` where ratings/votes are needed, (3) return `None` not `[]` on failure | [`trakt/DESIGN.md`](./managers/services/trakt/DESIGN.md) §3.3 | Every Trakt endpoint. Three were found in one manager; the rest are unaudited — `GLD-TRKT-02` |
| **FETCH / CACHE / APPLY** classification | [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md) §6 | Every module. Predicts blast radius |
| **Domain invariants** — 720p floor, 4K ≥ 70, `None` ⇒ mid-tier, `keep-universe` never deleted, cursor-in-`finally` | [`DOCS_CONVENTIONS.md`](./DOCS_CONVENTIONS.md) §7 | Every doc must not contradict these |

---

### Per-folder sweep checklist

Six questions, one per pattern. Ask all six of every remaining folder — they turn
the rest of the sweep from discovery into confirmation.

| # | Ask | Catches | If yes → |
|---|---|---|---|
| 1 | For every cache key or artifact written here, is there a reader anywhere? | P-A | Log it; say in §6 whether it is dead or waiting |
| 2 | Does a guard covering this folder actually cover *all* of it? | P-B | Write the gap into §6 explicitly |
| 3 | For every nullable value, does *unknown* behave differently from *zero/empty*? | P-C | If not, state why in §5 as an invariant |
| 4 | For every "degrades gracefully" path in §6, how does the operator learn it happened? | P-D | If nothing tells them, that's an enhancement |
| 5 | Does any module here exist somewhere else too? | P-E | Add to the `GLD-ML-15` table; note which is authoritative |
| 6 | Does each list endpoint paginate, request full extension, and return `None` (not `[]`) on failure? | P-C / Trakt trio | Add to the `GLD-TRKT-02` audit |
| 7 | Is every numeric claim in the doc traceable to a named constant in code? | P-G | Cite the constant, or mark the claim unverified |
| 8 | For every component you say "runs", have you found the **call site** — not just the constructor? | — | Constructed ≠ invoked. Say "constructed" until you have the caller |
| 9 | Is this manager a `run()` participant, or a **facade** whose methods a specific caller invokes? | — | No `run()` is not evidence of dead code. Ask whether the docstring names its entrypoint |
| 10 | When you attribute a defect to a method, does the caller reach **that** method — or a similarly-named sibling? | — | Read the caller. `refresh_all_series` and `get_all_series_chunked` both "fetch series"; only one is wired |

**Q4 is the highest-yield question.** Every folder documents graceful
degradation — goal G4 requires it — and almost none document how the operator
finds out. That gap is P-D, and it is where most remaining findings will come
from.

**Q8 was added session 41, after five instances.** `SonarrQualityManager`
(constructed by a dead parent), `orchestration.quality` (constructed, not invoked
by `run()`), `SonarrSeriesQualityManager.run()` (a no-op log line), the
`choose_codec_profile` selector, and `playlists/spoiler.is_spoiler_safe`. It also
cost `GLD-SIZ-04` **three** revisions. Each layer of construction reads like an
answer and is not one.

**Session 42 refined it.** `space_pressure.run()` is *also* a no-op — but it
**names its caller** (`orchestration.run_space_pressure_downgrades`) and states
why (the pass must run *after* `refresh_scores`). So a no-op `run()` is often
deliberate: component-iteration is the wrong trigger for a pass with a phase-order
constraint. **Ask which it is before logging a defect** — the tell is whether the
method names its real entrypoint.

**Session 63 added a fourth state.** `GLD-RAD-01` passed through *not built* →
*built, unwired* → *built and wired* → and settled on **built, wired, and
deliberately gated off pending validation** — self-declared `EXPERIMENTAL` in the
module header. So the full ladder for "does X run?" is:

| State | Tell |
|---|---|
| Not built | No module |
| Built, no caller | Q9: nothing names an entrypoint |
| Built, caller exists, caller unreached | Trace the caller's caller |
| **Built, wired, gated off on purpose** | **A gate default or an `EXPERIMENTAL` note** |

**Read the module header before concluding anything about whether a capability
runs.** Three of four states are indistinguishable from the call graph alone.

---

## ⚠️ Calibration — this register has been systematically pessimistic

Across sessions 55–65, **every finding that got a deeper read came back less
severe**, not more:

| Item | Filed as | Turned out to be |
|---|---|---|
| `GLD-RAD-01` | 🔴 L — *"migration remains"* | ✅ Built, wired, gated off, self-declared `EXPERIMENTAL` |
| `GLD-PLY-01` | 🔴 — *"no `test_spoiler.py`"* | ✅ Tested by two files, as an assertion helper |
| `GLD-PLY-02` | 🔴 — *"spoilers unguarded"* | 🟡 Structurally impossible; the check is belt-and-braces |
| `GLD-PLY-07` | 🟡 — *"0.18 test ratio"* | — The untested bulk is I/O; the pure core **is** tested |
| `GLD-RS-03` | 🟡 — *"may be built"* | ✅ Built, with a test suite of near-equal size |
| `GLD-SIZ-04` | 🟡 — *"dead duplicate"* | ⚠️ Constructed and reachable (3 revisions) |
| `GLD-SON-10` | 🟡 — *"duplicate test pairs"* | ✅ Mis-attributed to the wrong service |

**Seven downgrades, zero upgrades.** The bias has one cause: a finding is logged
when something *looks* absent, and absence is the cheapest thing to observe and
the most expensive to verify. A missing call, a missing test, a missing module —
each takes one read to suspect and three to disprove.

### What to do about it

1. **Treat every 🔴 filed on an absence as provisional** until the deeper read is
   done. Absence of a check is not presence of a hole (`GLD-PLY-02`); absence of a
   caller is not absence of a design (`GLD-RAD-01`); absence of a same-named test
   file is not absence of coverage (`GLD-PLY-01`).
2. **Weight the confirmed defects accordingly.** The ones that survived a deeper
   read are the real backlog — `GLD-SQ-01` (broken construction loop),
   `GLD-REP-11` (nonexistent method), `GLD-STO-01` (fixed), `GLD-RQ-10` (type
   mismatch), `GLD-SPLIT-02` (accidental filter). All were found by reading code,
   not by noticing something missing.
3. **The codebase is in better shape than the register's severity marks suggest.**
   A reader working top-down by severity would start with items that mostly
   dissolve on inspection.

### Session 75 — two more, and a distinct cause

`GLD-SRF-01` and `GLD-SRF-02` bring it to **nine downgrades, zero upgrades**. But
these two failed differently from the first seven, which is worth separating.

The first seven were **absence errors**: something looked missing, and absence is
cheap to observe and expensive to disprove.

These two were **attribution errors**: the defect was real, and I pinned it to the
wrong method. `refresh_all_series` and `get_all_series_chunked` sit in one file
and both "fetch series" — I traced the call site by **name similarity** rather
than reading the caller, then rated the severity as if the wired path carried the
bug. Exactly the shape that cost `GLD-SIZ-04` three revisions.

**Checklist Q10 exists for this.** Q8 asks whether a thing runs; Q10 asks whether
*this particular* thing is the one that runs.

A useful consequence: the two error classes want different remedies. An absence
finding needs a **deeper read of the same file**; an attribution finding needs a
read of a **different** file — the caller.

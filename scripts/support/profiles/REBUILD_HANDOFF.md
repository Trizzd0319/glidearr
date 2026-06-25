# Quality-rebuild + watchability-tuning — session handoff

**Purpose:** resume the Radarr/Sonarr quality-profile rebuild + watchability/codec/universe tuning
work cold. Everything designed, decided, built, and measured in the session is here.
**Last worked:** 2026-06-24. **Resume at:** §8 (next steps) — the universe-credit prototype.

---

## 0. ✅ LIVE REBUILD COMPLETE (2026-06-24)

Both live `standard` instances rebuilt: **radarr 20 profiles / 83 CFs**, **sonarr 15 / 88**. 5,854 movies +
3,511 series reassigned. **Quality ladder intact** — base profiles were PUT-updated in place, so ids 3–10
(HD-720p…UHD Bluray+WEB) are preserved; `radarr_quality_ladder` needs no rewrite. Rollback snapshots at
`profiles/<svc>/standard/_pre_apply_snapshot/`. Codec overlays verified (AV1 variant prefers AV1, H264 blocks it).

**Gotcha handled manually (fold into the executor next):** deleting old profiles failed until their
references were repointed — Sonarr had **import lists** on Any/SD; Radarr had **8 Collections** on
SD/English-HD-720p/English-HD-Bluray+WEB. `arr_rebuild.apply()` reassigns movies/series but does NOT yet
repoint Collections (`/collection` bulk editor) or import lists (`/importlist/{id}`) before profile-delete —
add that so it's hands-off. Also: existing CF *defs* still aren't refreshed (PUT 400; existing kept, net-new added).

## 1. TL;DR — where we are

- **Built + working:** export tool, TRaSH fetch, blueprint generator, and `arr_rebuild.py` with **dry-run +
  `--validate` (both services PASS)**. The 5 net-new codec CFs are **installed live** (inert). The apply
  EXECUTOR body is the only thing left to write (§7); `--apply` validates + refuses to write.
- **Designed, not built (4 pieces):** space-budgeted allocator · frequency-graded engagement floors ·
  device→codec selector + transcode-accuracy feedback · universe watch-propagation.
- **Nothing destructive has run.** Your live Radarr/Sonarr are untouched (besides the earlier
  *playlist poster + titleSort* work, which is separate and already live).
- **Restore points if/when we wipe:** your own backups + my export snapshot (`profiles/<svc>/standard/`)
  + the apply tool's planned rollback ledger.

---

## 2. Decisions locked this session

- **35 profiles total** — Radarr 20 (8 base + 12 codec variants), Sonarr 15 (6 base + 3 anime + 6 codec).
- **5 audience tiers:** Agnostic (TRaSH compat-first) · Universal-H264 · HEVC (HDR10, no DV) · HEVC+DV · AV1.
  - Codec variants land on the 4 main tiers: 1080p tiers get {H264, HEVC, AV1}; 4K tiers get {HEVC, HEVC+DV, AV1} (no H264 at 4K).
- **Drop the English- clones + `Any`/`SD`.** English priority becomes universal CF scoring
  (`English Audio +1500`, `Dual Audio +500`, `Language: Not Original −`), not parallel profiles.
  Anime stays soft (sub-only still grabs).
- **Merge rule:** CF *definitions* from canonical TRaSH (re-pulled) **+ our codec CFs (ours win on conflict)**;
  CF *scores* preserved from your tuned profiles + codec/English overlays. ("Don't let TRaSH clobber our items.")
- **Keep the HDR/DV plan** (device-aware tiers use the HDR/DV CFs for scoring). "minus DV/HDR10" only
  meant "don't overwrite our items," NOT strip HDR.
- **Full dedupe on the primary + rewrite `radarr_quality_ladder`** to the new IDs (shown as a dry-run diff first).
- **Default tier = efficient 1080p (HEVC, good bitrate ~6–8 GB).** 4K only for the exceptional.
  Low likelihood is sticky (doesn't climb fast).

---

## 3. Built artifacts (paths + how to run)

Tools — `scripts/support/tools/`:
| Tool | What | Run |
|---|---|---|
| `arr_profiles_export.py` | snapshot QPs+CFs → JSON + parquet | `python -m scripts.support.tools.arr_profiles_export --all` |
| `arr_trash_fetch.py` | pull canonical TRaSH set (240/235 CFs) | `python -m scripts.support.tools.arr_trash_fetch` |
| `arr_profiles_build.py` | generate the 35-profile blueprint | `python -m scripts.support.tools.arr_profiles_build` |
| `arr_rebuild.py` | **wipe+apply DRY-RUN** (--apply gated) | `PYTHONIOENCODING=utf-8 python -m scripts.support.tools.arr_rebuild [--service radarr]` |

Data — `scripts/support/profiles/`:
- `DEVICE_CODEC_MATRIX.md` — full per-device direct-play matrix (Samsung/LG/Roku/Fire/Apple/etc → T0/T1/T2).
- `blueprint/custom_formats.json` — our codec CFs (AVC/x264, HEVC, HDR or DV, Dolby Vision steer, Language: Not Original).
- `blueprint/{radarr,sonarr}_profiles.json` — the 35-profile spec.
- `{radarr,sonarr}/standard/` — the export snapshot (restore point).
- `trash/{radarr,sonarr}/` — canonical TRaSH cache.

---

## 4. Empirical findings (DON'T recompute — these took live API + parquet work)

**Library size / distribution (Radarr):** 24,668 movies; **98% on a 720p profile** (18,610 HD-720p +
5,607 English-HD-720p); only ~2,000 have files. Sonarr: 11,969 series.

**Space (your real file medians):** 720p ≈ **4.37 GB**, 1080p ≈ 20 GB, 4K ≈ 42 GB.
- All movies at 720p ≈ **79 TB** (18.6k) / **103 TB** (24.2k).
- Disk: **7.96 TB free / 29.1 TB total** on the movies volume → full library at 720p is ~3–4× total capacity.
- In 8 TB free: ~1,800 more at 720p, ~400 at 1080p-remux, ~190 at 4K (≈1,100–1,300 at HEVC-1080p ~6–8 GB).

**Watchability (1,960 *owned* movies — the rest are unowned/wanted, no history):**
- untouched 1,842 (94%) · watched 83 (4%) · **rewatched 35 (2%, avg 6.06 plays)**.
- 4K band watches **~10× the 1080p band** (6.06 vs 0.59 avg plays) → your "5×" bar is easily met; cutoff at wc≥2.
- **Formula is bimodal:** watched→50, rewatched→90; the 62–88 middle is empty → won't use the middle rungs as-is.

**Universe propagation (real data):** 283 owned in universes/collections; **hot groups: MCU (10 rewatched →
24 siblings), Fast (3→9)**; 196 siblings boost-eligible. ⚠ a catch-all `"universe"` group has 167 members →
propagation MUST be scoped to tight collections/curated universes (cap group size), or it over-promotes.

---

## 5. The watchability/ladder reshape — ✅ DONE (piece #1 of 3), 2026-06-24

Implemented + tested (7/7) + verified on the live parquet. `watch_likelihood.py`: graded engagement
floors (`floor = watched_floor + (wc-1)*rewatch_step`, cap `rewatch_floor`) → 1×=50, 2×=64, 3×=78, 4×+=90;
`uhd_cutoff 70→77` (now ABOVE `affinity_cap` 75 so taste never reaches 4K); `_DEFAULT_RADARR_LADDER`
rewritten to `[[0,3],[40,4],[45,7],[55,8],[77,5],[85,9],[90,10]]`. Config.json updated to match (live).
**Verified:** owned movies → 720p 1784 / 1080p 163 / 4K 13 (was 35); watched-once = all 1080p; 4K only wc≥3;
untouched 97% at 720p, none at 4K. Tests: `likelihood/test_watch_likelihood.py`.

**Piece #2 — ✅ DONE (2026-06-25).** Sonarr `run_active_watcher_upgrades` rewritten in
`services/sonarr/series/quality.py`: `select_upgrade_targets` + new `capped_target` EXCLUDE codec
variants (`_codec_variant`), and each series' target is CAPPED by the recalibrated likelihood
(`watch_likelihood` → `resolution_cap_for_likelihood`) — single watch → 1080p agnostic, regular
rewatch / hot-universe → 2160p agnostic; upgrade-only (never downgrades). `aggregate_series_signals`
now surfaces `watch_count` / `watchability_score` / `universe_credit` (max per series). The
`universe_credit` row field is READ and **now POPULATED** by the Sonarr franchise-join (§6), so a
single-watch member of a hot saga elevates immediately; non-franchise / cold-saga shows stay gated on
their own rewatch (0 credit → byte-identical). Tests: `series/test_active_watcher_gating.py` (+
recalibration updates to space tests). Fixes the Abbott & Costello (1 watch → 1080) + TMNT (no 1 TB
AV1 backfill) over-promotes from the log review.

**Piece #3 — device→codec selector still to do:** Stage-2 re-points each title to the matching codec
variant at its resolution, from per-show Tautulli device history (handoff §6). (The Sonarr
franchise-join that populates `universe_credit` is now DONE — see §6.)

## (old) 5. The watchability/ladder reshape (agreed target)

Replace the bimodal floors with **frequency-graded** floors so all rungs fill:

| Watch behavior | Today | New | Tier |
|---|---|---|---|
| watched 1× | 50 | 50 | 1080p WEB *(default)* |
| watched 2× | 90 | 65 | 1080p BD |
| watched 3× | 90 | 78 | Remux / 2160-WEB |
| watched 4×+ | 90 | 90 | **4K** |

- Proposed bands: 0–40 → 720p · 40–62 → 1080p WEB · 62–80 → 1080p BD · 80–88 → Remux/2160-WEB · 88–100 → 4K.
- Raise 720p→1080p threshold 20→~40 (steep low end). Affinity cap stays 75 (keeps taste-only out of 4K).
- 4K cutoff calibrated empirically to the ~5× watch-rate point (wc≥2 ≈ 10×; tune down slightly for exactly 5×).

---

## 6. Universe watch-propagation — ✅ MECHANISM DONE (wiring pending), 2026-06-24

The pure core is built + tested (`likelihood/test_watch_likelihood.py`, 12/12):
- `explain_likelihood` now adds a `universe_credit` row field to wc → `effective_wc`, so **1 real watch +
  ~2 universe credit ⇒ effective 3 ⇒ 4K immediately**; a plain single watch stays 1080; credit alone on an
  unwatched title only reaches 1080 (needs effective 3 for 4K).
- `universe_credit(rewatched_siblings, group_size, days_since_watch=…)` helper: `heat = rewatched/size`
  (self-dilutes loose mega-groups — MCU 10/34 → ~full 2.0, the 167-bucket 4/167 → ~0.16), full at
  `universe_heat_full` (0.30), recency-halved every `universe_recency_halflife_days` (30), cap
  `universe_credit_cap` (2.0). Config knobs added to `watch_likelihood` defaults. 0 until injected → byte-identical off.

**Wiring — pre-pass that POPULATES `universe_credit`:**
- **Sonarr:** ✅ DONE 2026-06-24. `episode_files.refresh_scores` → `_apply_universe_credit` joins TV-franchise
  membership (`tv_group_maps_from_series` over `kometa_franchises.json`) → per-series watch_count + days-since →
  `series_universe_credits(...)` → broadcasts the `universe_credit` column onto every episode row (persisted to
  parquet). 0 everywhere when no franchise heat → byte-identical off.
- **Sonarr downgrade factor:** ✅ DONE 2026-06-24. `plan_series_downgrades` now PROTECTS a series whose
  `universe_credit ≥ UNIVERSE_PROTECT_MIN` (1.0) from space-pressure step-down (`skipped_universe` stat +
  `hot-universe` log row). The credit is recency-decayed, so a stale saga's members fall below the threshold
  and become droppable again — the user's "recency bias drop." Tests: `space/test_downgrade_planner.py`
  (hot-saga protected, stale-saga droppable).
- **Radarr:** ✅ DONE 2026-06-24. `quality/space_pressure.refresh_scores` → `_apply_universe_credit` builds saga
  membership from the **TMDB `collection_name`** (automatic — NO keep tag needed, so it works for every user)
  **∪ `universe_name`** (curated, when keep-tagged) → per-movie watch_count + days-since →
  `movie_universe_credits(...)` (pipe-sep, a film keeps its HOTTEST group) → broadcasts `universe_credit`.
  CRITICAL lesson (adversarial review + user): `universe_name` is set ONLY for keep-universe/`universe`-TAGGED
  movies, so deriving membership from it made the downgrade guard dead for the common (untagged) case — the
  fix is `collection_name`, which every user has automatically.
- **Radarr consumers (BOTH up & down):**
  - *Untagged* saga movies (the common case) flow through space-pressure: `plan_movie_downgrades` PROTECTS a row
    with `universe_credit ≥ UNIVERSE_PROTECT_MIN` (`skipped_universe`/`hot-universe`), and `plan_movie_upgrades`
    already feeds the whole row to `watch_likelihood` so the credit elevates the upgrade tier automatically.
  - *Keep-tagged* universe movies are owned by the **universe manager** (`radarr/quality/universe.py`): upgrade
    via `watch_likelihood(df.loc[idx])`; downgrade now passes that credit-bearing likelihood to
    `universe_quality.downgrade_target`, which **floors the step-down at `resolution_cap_for_likelihood`** when
    `universe_credit ≥ MIN` (a hot saga resists to its earned tier; a stale one decays to floor 1 and drops).
  - Math fixes from the review: `universe_credit` clamps `days_since ≥ 0` (future-dated watch can't exceed cap);
    `movie_universe_credits` dedupes repeated pipe tokens. Tests: `test_watch_likelihood.py`,
    `test_downgrade_planner.py` (untagged), `test_universe_quality.py` (manager floor).
- **Borrowed-vs-earned ranking** (drop soft/universe tiers FIRST, finer than the binary protect guard): record
  `borrowed_fraction` and feed the space-pressure downgrade ranker — still to do (future refinement).

## (design) 6. Universe watch-propagation design (the piece we said YES to)

- **Heat → credit:** per tight group, `group_heat = Σ members min(wc,cap) × recency_weight(last_watched)`,
  normalized by group size. Each sibling gets `universe_credit` (borrowed, scaled, capped).
- **Asymmetric threshold:** `effective_wc = own_watch_count + universe_credit` → "twice in a hot universe"
  ≈ effective 4 → reaches a 4K floor that'd normally need 4 direct watches.
- **Recency decay:** `universe_credit *= decay(days since group's last watch)` (uses `last_watched_at`).
- **Borrowed vs earned:** `borrowed_fraction = universe_credit / effective_wc` → space-pressure downgrade
  ranker drops **soft (borrowed) tiers first**; direct rewatches (Tangled, OT) are earned and stick.
- **Scope guard:** only TMDB collections + curated universes, group-size cap + coherence weight (the 167-bucket lesson).
- **Hooks:** pre-pass writes `universe_credit` + `borrowed_fraction` columns → `explain_likelihood` adds the
  credit → space-pressure ranker reads `borrowed_fraction`. All inputs already in the parquet
  (`universe_name`, `collection_name`, `watch_count`, `last_watched_at`).

---

## 7. The wipe/apply status + remaining gaps

**Progress 2026-06-24 (end of session):**
- `arr_rebuild.py --validate` **PASSES on BOTH services** — every blueprint profile builds against the live
  schema (cutoff + language resolve), all 83/88 CFs resolve, and the reassignment map is total. The whole
  destructive plan is proven correct.
- **The 5 net-new codec CFs are INSTALLED LIVE** on both instances (AVC/x264, HEVC, HDR or DV, Dolby Vision
  (steer), Language: Not Original — Sonarr already had the last). They're **inert** until a profile scores
  them, so no behavior change yet.
- **Reassignment default chosen:** `Any`/`SD`/orphans → **`HD - 720p/1080p`** (in code; change the `default`
  var in `arr_rebuild._reassign_map` call if you want by-file-quality instead).
- **Custom-group lesson:** your profiles have hand-made quality groups (e.g. Sonarr `[Anime] Remux-1080p`
  cutoff = custom group "Bluray 1080p" id 1004, NOT in the default schema). So **apply must CLONE each source
  profile's `items` + `cutoff` verbatim** (preserving custom groups) and overlay only name/language/thresholds/
  formatItems-scores — do NOT rebuild items from `/qualityprofile/schema`.

**EXECUTOR BUILT + VALIDATED END-TO-END (2026-06-24).** `arr_rebuild.apply()` ran a full rebuild against the
new **`radarr/test`** instance (port 8484, 35 isolated movies) and produced **exactly the blueprint: 20
profiles + 83 CFs**, with the codec overlays verified correct (e.g. `…(AV1)` → AV1 +1500/HEVC +800/x264 +300;
`…(H264)` → x264 +1500, AV1/HEVC/x265 all −35000). Order: snapshot → upsert CFs → upsert profiles (PUT
existing-by-name, POST new; clone source items + full formatItems) → reassign items off dropped profiles
(bulk `…/editor`) → delete dropped profiles → delete dropped CFs. Rollback snapshot at `profiles/<svc>/<inst>/_pre_apply_snapshot/`.

**Live-run command** (when ready, with attention): `python -m scripts.support.tools.arr_rebuild --service radarr --instance standard --apply --i-have-backups` (then `--service sonarr`).

**Remaining nits before the live run:**
1. **CF-definition refresh doesn't apply to EXISTING CFs.** The PUT-update of an existing CF is 400-rejected by
   the API (TRaSH spec format doesn't round-trip), so the executor **keeps the existing def** (id still scores
   fine) and only POSTs net-new CFs. On `test`: 7 added, 76 kept-as-is, 38 old deleted → 83. The QP rebuild is
   perfect; only existing CF *regex/specs* aren't refreshed to latest-TRaSH (they're already TRaSH-derived, so
   minor). To truly refresh: fix the PUT payload (likely a spec `fields`/`implementationName` shape) or
   delete+recreate existing CFs (careful — they're referenced by live profiles mid-flight).
2. **Ladder rewrite** still prints-only (not yet patching `radarr_quality_ladder`) — patch ONLY that array in
   raw `config.json` (never write the loaded config back — secrets would leak).
3. **Live `standard` is 24,668 movies** — the reassignment uses the bulk editor (fast), but run with attention.

---

## 8. NEXT STEPS — resume order

1. **Universe-credit prototype** (← start here): a read-only pass over `movie_files.parquet` that computes
   `group_heat` + `universe_credit` and shows **which of the 196 siblings move tiers and by how much**. No engine changes.
2. **Frequency-graded floors** (§5) + re-run the band distribution to confirm the spread across all rungs.
3. **Space-budgeted allocator** + a dry-run showing, for 24k movies in 8 TB, how many land on each rung.
4. **Device→codec selector + transcode-accuracy feedback** (Tautulli already has `device_codec_matrix` +
   `codec_direct_play_rate` — measures direct-play vs transcode).
5. **Finish `arr_rebuild --apply`** (reassignment map + payload builder), validate on a test instance, then the live wipe.

---

## 9. Open decisions waiting on you

- `Any`/`SD` reassignment default (§7.1).
- Exact 4K cutoff to hit *exactly* 5× (wc≥2 ≈ 10×).
- Universe propagation strength / decay half-life / group-size cap.
- **Git:** 5 of *your* commits are unpushed (ahead of `origin/main` from before this session). A `git push`
  would mass-push them — I held it. Decide: commit the `arr_*`/`profiles/` tooling on its own and you push
  yours first, or push everything together. Profile tooling is all **untracked** right now (nothing committed by me).

---

## 10. Key integration points in the engine (for the unbuilt pieces)

- Ladder selector: `scripts/managers/machine_learning/likelihood/watch_likelihood.py` →
  `profile_id_for_likelihood()` (threshold-walk), `explain_likelihood()` (the floors), config `radarr_quality_ladder`.
- Watchability score: persisted in `scripts/support/cache/radarr/standard/movie_files.parquet`
  (`watchability_score`, `watch_count`, `percent_complete`, `is_watched`, `universe_name`, `collection_name`, `last_watched_at`).
- Space: `scripts/managers/machine_learning/space/space_targets.py` (`(T floor, U headroom)`),
  `size_model.estimate_gb_for_profile()`, `services/radarr/quality/space_pressure.py` (apply/downgrade/delete ranker),
  `services/coordinator/space_coordinator.py`.
- Transcode/device data: `machine_learning/quality_analytics/transcode.py`
  (`device_codec_matrix`, `codec_direct_play_rate`), Tautulli history fields `platform`, `transcode_decision`,
  `stream_video_codec` (`services/tautulli/watch_history/`).

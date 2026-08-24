# lifecycle — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **lifecycle**

**Package** — `scripts.managers.machine_learning.lifecycle`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`space/DESIGN.md`](../space/DESIGN.md) · [`machine_learning/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

"Watched" sounds like a boolean. It is the hardest definition in the system, and
getting it wrong was a real, measured bug.

**The bug this package exists to fix**, quoted from
[`watched_definition.py`](./watched_definition.py):

> `is_watched` was `watch_count > 0`, and `watch_count` was incremented once per
> Tautulli history row **regardless of completion** — in BOTH producers.

A 30-second sample therefore:

- marked the file watched,
- started the 3-hour grace clock that queues it for deletion,
- bought it an engagement floor of 50 in `watch_likelihood` (≈ WEB-1080p),
- and counted as a rewatch toward scoring signal A3 once it happened twice.

Measured on the live cache: **50 of 511 episode plays (9.8 %)** and **141 of 389
movie plays (36.2 %)** were below the bar. Over a third of movie "watches" were
samples — and each one started a deletion clock.

Two further problems follow:

1. **Two producers, two definitions.** Radarr's `_fetch_watch_map` and Sonarr's
   `_fetch_tautulli_episode_history` each inlined the increment, so they could
   and did drift.
2. **Changing the definition strands state.** Tightening the bar flips rows
   `True → False`, and any state derived from the old verdict — a
   `marked_for_deletion` flag from a previous run — becomes an orphan justified
   by a definition that no longer holds.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One definition of "watched", used by every producer. |
| G2 | Agree with what Plex and Tautulli show the household. |
| G3 | Fail open — a guard must never shrink on missing data. |
| G4 | Tightening the definition must not retroactively delete anything. |
| G5 | Raw playback facts stay raw; only verdicts are thresholded. |
| G6 | Pure — stdlib and pandas only. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Defining "watched" for a *series* | This is per-play. Series-level rollup is a separate question (§10 Q2). |
| N2 | Owning the completion threshold | Tautulli owns it; this honours it (G2). |
| N3 | Executing deletion | Emits `GracePlan` / decisions; services apply. |
| N4 | Reconciling Trakt vs Tautulli history | Tautulli is this package's input. |

---

## 3. Architecture

### 3.1 The precedence chain

```
play_is_watched(history_row, threshold_pct)
   │
   ├─ watched_status present?
   │     numeric  → val >= 1.0            (1 watched · 0.5 partial · 0 unwatched)
   │     string   → "true"/"watched" → True
   │                "false"/"unwatched"/"partial"/"" → False
   │     ← PREFERRED: it is the OPERATOR'S verdict, already reflecting whatever
   │       completion threshold they configured in Tautulli
   │
   ├─ percent_complete present?
   │     → pct >= threshold_pct           (default 85)
   │     ← FALLBACK for rows cached before watched_status entered the Tautulli
   │       projection whitelist. Those rows DO carry percent_complete, so the
   │       transition is SILENT — no historical row reads as unwatched merely
   │       because the cache has not cycled
   │
   └─ neither → True                      ← FAIL OPEN (G3)
                                            "a play is a play"
```

The default fallback is **85.0**, matching Tautulli's and Plex's shipped
threshold, deliberately: *"the percentage leg and the `watched_status` leg agree
on a default install, so a cache that has not yet cycled `watched_status` in
produces the same verdicts as one that has."*

`resolve_watched_percent` reads `watched_threshold.percent`, falls back to the
legacy `episode_retention.watched_percent` alias, then to 85. An unparseable
value **falls through to the next source rather than to 0**, because a bar of 0
would silently restore the original bug.

### 3.2 What stays raw, and why (G5)

| Field | Thresholded? | Reason |
|---|---|---|
| `is_watched`, `watch_count` | **Yes** | Verdicts |
| `percent_complete` | **No** | Raw fact — how far the furthest play got |
| `last_watched_at` | **No** | Raw fact — when it was last played |

If `percent_complete` were thresholded, `watch_likelihood.explain_likelihood`
could no longer grade a sub-threshold play as *started* (20–90 %) versus
*abandoned* (<20 %). A title abandoned at 15 % would fall through to the
UNTOUCHED branch (`untouched_base` 25 + score) — so **abandoning a show could
raise its quality target.** The exact inverse of the intent.

`last_watched_at` also carries the "was this tried at all?" bit for the one case
`percent_complete` cannot express: a play Tautulli reported at 0 %. Two such rows
exist on the live cache (*The Big Bang Theory* S02E03, *The Seven Deadly Sins*
S01E01); without that bit they would land on UNTOUCHED and score **higher** after
being abandoned.

Consumers reading `last_watched_at` alone use it **protectively** — don't delete
something played recently, don't call a series cold — so leaving it raw keeps the
tightened definition from becoming a stealth *widening* of deletion.

### 3.3 🎯 `not is_watched` returns `'clear'`, not `'skip'` (G4)

The most consequential line in the package, and a genuinely subtle piece of
migration safety.

Grace marking is the **only** thing that sets `marked_for_deletion`, so an
unwatched row carrying that flag is an impossible state. Returning `'skip'`
would *preserve* it.

That was inert while `is_watched` could only go `False → True`. Under the global
watched bar, a row whose only plays were samples flips `True → False` — and
`'skip'` would have left the previous run's mark standing and **deleted a file
the current definition says was never watched.**

> Five live rows are in exactly that state (*Fallout* S01E06, *See* S01E06,
> *DuckTales* S01E01, *Blue Bloods* S01E11/E12); without this they would be
> deleted on the first pass after the change.

Five files, on the first run after a definition change, on a justification that
no longer existed. This is the class of bug that is nearly impossible to find
afterwards — the files are simply gone, and the logs say they were watched.

### 3.4 Guard precedence

**Movies** — [`movie_grace_decision`](./grace_policy.py):

```
clear  ← franchise entry · franchise-protected file · keep_forever/keep_movie/universe
       · NOT WATCHED                                     ← §3.3
skip   ← watched but no last-watched timestamp
mark   ← otherwise
```

**Episodes** — [`episode_grace_decision`](./grace_policy.py):

```
clear  ← pilot or next-episode        (cleared even when unwatched)
clear  ← NOT WATCHED                                     ← §3.3
skip   ← watched but no last-watched timestamp
clear  ← viewer_protected · fid_protected · keep_series
       · keep_season_current · recent_aired · household_blocked
mark   ← otherwise
```

Order matches the service guards exactly. `viewer_protected` is evaluated first
among the post-skip clears purely so it is the one attributed in the log
("held for Aiden / Raina") — the ordering there is cosmetic, since every branch
returns the same `'clear'`.

### 3.5 Per-viewer retention

[`viewer_retention.py`](./viewer_retention.py) protects an interval around each
account's position:

```
[ position − backward_buffer , position + pace × horizon ]
```

Pace-scaled, so a fast watcher's forward buffer is proportionally larger.

### 3.6 Grace-window scaling

`grace_window_multiplier` scales a row's grace window by its
`watchability_percentile` — favourites keep files longer, forgettables shorter.

**Default is exactly `1.0`**, so `grace_td × 1.0 == grace_td` and marking is
byte-identical until the ramp is explicitly enabled. Null/NaN/absent percentile
also returns 1.0. Configured, it interpolates `low_mult`…`high_mult` across
percentile 0–100.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | One module owns "watched" | G1 — two inlined producers drifted, which is what caused the bug | Per-producer logic |
| D2 | Prefer Tautulli's `watched_status` | G2 — it is the operator's own verdict; a second definition guarantees disagreement with Plex | Always use our own threshold |
| D3 | Fallback default 85 matches Tautulli/Plex | A cache mid-transition produces the same verdicts as a cycled one | Independent default |
| D4 | Fail open on missing data | G3 — this gates delete guards | Fail closed |
| D5 | Unparseable threshold falls through, never to 0 | A bar of 0 silently restores the original bug | Clamp to 0 |
| D6 | `percent_complete` / `last_watched_at` stay raw | G5 — thresholding them would let abandonment *raise* a quality target | Threshold everything |
| D7 | `not is_watched` ⇒ `'clear'` | G4 — prevents five real files being deleted on a mark the new definition doesn't support | `'skip'` |
| D8 | Pilot/next cleared **before** the watched check | They are protected regardless of watch state | Uniform ordering |
| D9 | `viewer_protected` first among post-skip clears | Log attribution only; behaviourally identical | Arbitrary order |
| D10 | Grace multiplier defaults to exactly 1.0 | Byte-identical until explicitly enabled | Default ramp on |
| D11 | Legacy config key kept as an alias | The knob outgrew its original name (`episode_retention`) once it governed movies too | Rename and break |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Exactly one definition of "watched" — every producer calls `play_is_watched`. |
| I2 | Tautulli's `watched_status` wins when present. |
| I3 | Missing data ⇒ watched ⇒ guard stays wide. |
| I4 | `percent_complete` and `last_watched_at` are never thresholded. |
| I5 | An unwatched row is never left `marked_for_deletion`. |
| I6 | Grace marking is the only setter of `marked_for_deletion`. |
| I7 | Grace multiplier is exactly 1.0 unless explicitly enabled. |
| I8 | The retention rule and the global watched bar resolve through the same function. |
| I9 | This package performs no I/O. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal to operator? |
|---|---|---|---|---|
| History row missing both fields | Precedence rule 3 | Treated as watched — guard stays wide | Safe | ❌ **None** |
| `watched_status` an unknown string | String branch | Unrecognised ⇒ falls to `percent_complete` | Safe | ❌ **None** |
| Threshold config unparseable | `_as_float` → next source | Falls through to 85, never 0 | Safe | ❌ **None** |
| Definition tightens, rows flip `True→False` | `'clear'` branch | Stale marks cleared before deletion | **Safe — 5 real files** | ❌ **None** |
| Anchor timestamp unparseable | `grace_mark` → `(None, None)` | Row left unchanged | Safe | ❌ **None** |
| Percentile absent for the ramp | Multiplier → 1.0 | Neutral | Safe | ✅ Byte-identical |
| Tautulli threshold changed by operator | **None** | Glidearr silently follows | 🟡 Verdicts shift with no record | ❌ **None** |
| Series-level "watched" needed | **Not implemented** | No per-series definition exists | 🟡 Blocks auto-prune | ✅ Known gap |

Every degradation is toward **not deleting** — the correct direction. But **none
of the eight rows produces an operator signal**, and row 7 matters: changing the
Tautulli threshold silently re-verdicts the entire library on the next run, with
nothing recording that it happened.

---

## 7. Configuration surface

| Key | Default | Effect |
|---|---|---|
| `watched_threshold.percent` | `85.0` | Completion bar when `watched_status` is absent |
| `episode_retention.watched_percent` | — | Legacy alias; new key wins |
| grace ramp `enabled` / `low_mult` / `high_mult` | disabled ⇒ 1.0 | Percentile-scaled grace window |
| viewer retention `backward_buffer` / `horizon` | — | Per-viewer protected interval |

Constants: `DEFAULT_WATCHED_PERCENT` 85.0.

---

## 8. Implemented capabilities

- ✅ Single system-wide definition of "watched", honouring Tautulli's own verdict
- ✅ Silent transition for rows predating `watched_status`
- ✅ Fail-open on missing data
- ✅ Threshold-free `percent_complete` / `last_watched_at`
- ✅ Migration-safe `'clear'` semantics protecting five real files
- ✅ Movie and episode grace precedence matching service guards exactly
- ✅ Per-viewer retention with pace-scaled forward horizon
- ✅ Percentile-scaled grace window, byte-identical by default
- ✅ Saga retention across series boundaries
- ✅ Monitor, restore and stale-prune policies
- ✅ Restore identity (`GLD-RST-01`..`-07`) — `RELEASE_FIELDS` lifted off the parquet
  row at delete time, history enrichment, infohash→magnet reconstruction, and
  credential-safe grab-URL archiving. Indexed centrally in
  [`ENHANCEMENTS.md`](../../../ENHANCEMENTS.md) §4.54
- ✅ Grab-URL redaction hardened against MALFORMED urls (`GLD-RST-13`) — params
  appended with `&` and no `?` land in `path`, where urlparse leaves `query` empty;
  a live Newznab apikey reached 266 archive rows that way. Malformed tails are now
  split back into a query before the allowlist runs
- ✅ **Restore is SEARCH-based, not URL-based** (`GLD-RST-15`/`-16`, operator ruling
  2026-08-24). Sonarr masks indexer api keys — `GET /indexer` returns `apiKey` as
  `********` — so an archived download URL can never be refilled from Sonarr's own
  config and is forensic only. Recovery runs `match_release` against
  `scene_name` + `release_group` + `quality_name` + `resolution` from the
  `deleted_episodes` ledger, which makes that release record the ENTIRE restore
  capability rather than an enhancement. Both delete paths now feed one ledger
- ✅ Behavioural auto-rater
- ✅ Legacy config alias

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-LIF-01` | **Series-level watched definition** — the per-play bar exists; the per-*series* rollup does not | Blocks Trakt auto-prune (`GLD-SVC-01`) and per-series retention. **The single remaining gap in the "watched" story** | M | D2 |
| `GLD-LIF-02` | **Detect and report a changed watched threshold** — record the effective bar per run, warn when it moves | §6 row 7: an operator changing Tautulli's threshold silently re-verdicts the whole library | S | — |
| `GLD-LIF-03` | **Report `'clear'` reason counts** per run — how many rows each guard protected | Makes guard effectiveness visible; the attribution ordering already exists for this purpose | S | `GLD-SPA-05` |
| `GLD-LIF-04` | **Report stale-mark clears** — how many `marked_for_deletion` flags were cleared by a definition flip | §3.3 protected five files silently; that should be visible | S | `GLD-LIF-02` |
| `GLD-LIF-05` | **Warn on unrecognised `watched_status` values** rather than silently falling through | §6 row 2 | S | — |
| `GLD-LIF-06` | **Enable the grace ramp by default** once its effect is measured | Currently built, defaulted off, so favourites and forgettables share a window | S | `GLD-LIF-03` |
| `GLD-LIF-07` | **Reconcile Tautulli and Trakt watch history** into one watched-set with documented precedence | Two sources, no stated authority | M | `GLD-TRKT-08`, D7 |
| `GLD-LIF-08` | **Per-viewer retention visibility** — which accounts are holding which episodes | The log attributes it; nothing aggregates it | S | `GLD-WEB-04` |
| `GLD-LIF-09` | **Re-measure the sample rate** — 9.8 % episodes / 36.2 % movies was measured once; confirm the bar still holds | The figure justified the change and is now unmonitored | S | `GLD-LIF-02` |
| `GLD-LIF-10` | **Auto-rater confidence** — behavioural ratings feed A4 alongside real Trakt ratings | Inferred and declared ratings currently look alike downstream | M | `GLD-ML-11` |
| `GLD-RST-08` | ✅ **Record release identity on the DELETE paths, not just the step-down** — done in §0.1 #86/#87. All four delete paths plus the consent gate now archive; `GLD-RST-02`'s descriptor was wired to the step-down ONLY, so the path that held 271 marked rows recorded nothing | Closed. Verified in production: 271/271 rows carrying identity, reason, path, class, pid, profile, episode_id and a redacted grab descriptor | M | ✅ Done |
| `GLD-RST-09` | **Retire `_SECRET_QS_KEYS`** on `SonarrSeriesSpacePressureManager` | Dead constant since `GLD-RST-07` replaced the denylist with an allowlist; nothing reads it *(P-A)*. Left in place pending a grep for external callers | S | `GLD-RST-07` |
| `GLD-RST-14` | **Build the redaction corpus from PRODUCTION urls, not from imagination** — harvest distinct `downloadUrl` shapes out of live `*arr` history and assert on every one | `GLD-RST-13` leaked a live apikey because all nine corpus urls were WELL-FORMED: the adversarial cases covered base64-nested passkeys, path tokens and netloc userinfo, and never a url that simply does not parse. The corpus tested an imagined adversary | S | `GLD-RST-13` |
| `GLD-RST-18` | **Detector for a restore-set write that silently does nothing** — assert `deleted_episodes` grew by the expected series count after an armed pass, and surface it in the deletion summary | `_persist_restore_set` skips under `dry_run` BY DESIGN, so it is structurally impossible to dry-run and executes for the first time on a live armed run. Its failure mode is the worst one in the system: files gone, no restore. Errors are logged, but nothing yet CHECKS the write landed *(**P-D**)* | S | `GLD-RST-15` |
| `GLD-RST-19` | **Audit `push_payload` / `magnet_from_hash` — built by `GLD-RST-04`, never called** | Dead code (**P-A**): the only two consumers of an archived `download_url`, and nothing invokes them. With `GLD-RST-16` settling restore as search-based they may simply be deleted; a torrent-side magnet restore is the only case that would justify wiring them | S | `GLD-RST-16` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ~~Whose watch history counts as "watched"?~~ **Answered:** any play clearing Tautulli's own verdict counts; `household_blocked` and `viewer_protected` handle per-member protection separately. *(D1 resolved)* | ✅ |
| Q2 | What fraction of a **series** is watched — last aired episode, 90 % of aired, or all? The per-play bar does not answer this. *(= D2)* | `GLD-LIF-01`, `GLD-SVC-01` |
| Q3 | Should the grace ramp be on by default? | `GLD-LIF-06` |
| Q4 | When Tautulli and Trakt disagree, which wins? *(= D7)* | `GLD-LIF-07` |
| Q5 | Should a changed watched threshold require re-confirmation before the next delete pass? | `GLD-LIF-02` |

## 11. Related designs

- [`space/DESIGN.md`](../space/DESIGN.md) — the delete gate these guards feed
- [`likelihood/`](../likelihood/) — `explain_likelihood`, which depends on raw `percent_complete`
- [`scoring/SCORING_GROUPS.md`](../scoring/SCORING_GROUPS.md) — A2/A3 consume these verdicts
- [`services/tautulli/DESIGN.md`](../../services/tautulli/DESIGN.md) — the history source
- [`DESIGN_series_saga_resumption.md`](../DESIGN_series_saga_resumption.md)

# plex/playlists — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [plex](../README.md) › **playlists**

**Managers** — `PlexPlaylistBuilderManager`, `PlexPlaylistWritebackManager`, and the per-family builders
**Status** — ✅ Implemented · 🔵 Writeback default-off · 🔴 No runtime spoiler check
**Existing docs** — [`DESIGN_personal_playlists.md`](../DESIGN_personal_playlists.md) at the Plex root

> **Coverage:** `writeback.py` read IN FULL (session 66) and modified —
> `GLD-PLY-13/14/15`. Plus the directory inventory. **`builder.py` (119.6 KB) and
> `universe_order.py` (48.0 KB) are unread.**

---

## 1. Scale — the largest folder in the repo

**622.7 KB.** Source ~296 KB, tests **~206 KB**, plus 121 KB of generated JSON
(`tv_franchises.generated.json`, `universe_timeline.json`).

| Module | Size | Its test |
|---|---|---|
| [`builder.py`](./builder.py) | **119.6 KB** | 21.2 KB — **ratio 0.18** |
| [`universe_order.py`](./universe_order.py) | 48.0 KB | 35.9 KB |
| [`writeback.py`](./writeback.py) | 44.2 KB | 35.8 KB |
| [`movie_builder.py`](./movie_builder.py) | 20.8 KB | **43.6 KB — ratio 2.10** |
| `tv_resolver` · `movie_resolver` · `combined_builder` | 16.1 / 15.5 / 14.0 KB | 10.6 / 18.4 / 13.0 KB |

**The TV builder is the least-tested module relative to size in the folder; the
movie builder is the most.** An eleven-fold difference in coverage ratio between
two modules doing the same job for different media.

That matters here specifically: **episode ordering is a TV concern.** The spoiler
invariant lives on the side with the thinnest tests. `GLD-PLY-07`.

---

## 2. 🟡 `GLD-PLY-02` — downgraded: the guarantee **is** structural

`writeback.py`'s docstring enumerates **seven numbered safety rails, "all P0"**:

| # | Rail |
|---|---|
| 1 | Fail-closed arm gate (config **AND** not `dry_run`) |
| 2 | Per-server write token, **never owner-for-managed**, assert-checked |
| 3 | Persisted managed-anchor map — ratingKey first, title-adoption only as a 404 fallback and only when that user owns it |
| 4 | ratingKey **re-resolution vs the FRESH owned inventory** before writing; drift counted, large-drift user skipped |
| 5 | In-place add/remove/move diff; delete+recreate only as last resort, **create-new-then-delete-old**; steady state is a no-op |
| 6 | Orphan cleanup against the **live Home roster** |
| 7 | Armed/disarmed banner every run + an audit line per write |

**None of the seven concerns episode ordering** — which is what sessions 61 and 64
established, and why I carried this at 🔴.

### 2.1 ✅ But session 65 settles Q1: the brain's ordering **is** used

[`tv_resolver.py`](./tv_resolver.py) — the module `builder.py` delegates plan
construction to — imports the ordering directly:

```python
from scripts.managers.machine_learning.playlists.expansion import NEXT_UNWATCHED, expand_show
from scripts.managers.machine_learning.playlists.models     import PlaylistInput
from scripts.managers.machine_learning.playlists.ordering   import order_items
```

and states the pipeline outright:

> `PlaylistInput[] → expand_show (cap per series) → **order_items (brain)** →
> PlaylistPlan (+ resolution stats)`

So the crown-jewel GROUP→WITHIN→ACROSS rule **is** what orders the plan, and
[`playlists/DESIGN.md`](../../../machine_learning/playlists/DESIGN.md) §3.3's
argument holds: the `(season, episode)` sort makes a spoiler **structurally
impossible** regardless of air-date quality.

**`GLD-PLY-02` drops from 🔴 to 🟡.** The runtime check is belt-and-braces — it
would catch a *future* refactor that swapped the ordering, not a present hole.
Still worth adding beside rail 4, which already re-resolves at the right moment,
but it is not the only thing standing between the household and a spoiler.

I was wrong to rate it 🔴 on "no runtime check" without first confirming whether
the structural guarantee held. Checklist Q8's lesson, in a new dress: *absence of
a check is not presence of a hole.*

---

## 2.2 🎯 The best-measured identity finding in the repo

`watched_episode_keys` returns a set **mixing three identity kinds**:

| Identity | When it works |
|---|---|
| Episode `ratingKey` (str) | Exact — *"when Plex hasn't re-scanned"* |
| `(series, season, episode)` | *"numeric, the most reliable"* |
| `(series, episode_title)` | Fallback when indices are missing |

> Tautulli records the ratingKey **as it was at play time**, so a Plex library
> re-scan / re-match / duplicate leaves the historical ratingKey pointing at a
> now-stale item. **Observed on a real heavy watcher: only 11/117 of one show's
> watched episodes still matched by ratingKey, but 117/117 matched by
> (series, season, episode).**

**A 90.6 % failure rate, measured on live data**, fixed by matching on *any*
identity while keeping the exact path for fresh rows.

This is the **eighth** identity-discipline instance and the first about *temporal*
decay rather than cross-system joins — the key is correct when written and wrong
later. It pairs with `builder.py`'s complementary fix:

```python
# Durable Plex ratingKey -> native id. APPEND-ONLY: a key Plex retires on a re-scan
# keeps resolving, which is the only way a play recorded before that re-scan stays
# attributable.
_RK_CROSSWALK_KEY = "tautulli/rating_key_crosswalk"
```

One problem, two solutions at different layers: **append-only crosswalk** so old
keys keep resolving, **multi-identity matching** so the join survives when they
don't.

And the drop policy is explicit: *"Episodes that don't resolve to a ratingKey are
dropped and **COUNTED (never guessed)** — the brain only ever sees resolvable,
playable items."*

---

## 3. 🎯 The best byte-identical opt-in in the repo

> **DEFAULT-OFF / FAIL-CLOSED is the whole contract.** With
> `plex.playlists.writeback.enabled` false (the default) **OR** `dry_run` true,
> `writeback_armed` returns False and the manager runs the full
> preview/diff/re-resolution but performs **ZERO** Plex writes — behaviour is
> **byte-identical to today (asserted by a call-log test)**.

Ten byte-identical opt-ins have turned up in this sweep. Every other one is
byte-identical **by construction** (a multiplier of 1.0, an exponent of 0, a
weight of 0.0). This one is byte-identical **by assertion** — a test that records
the call log and proves no write verb fired.

That is the stronger form, because construction arguments are re-derived by every
reader while a call-log test fails on the commit that breaks it.

Note the split: *"The build/preview gate is the existing `_cap_enabled`; **ONLY
the actual write verbs consult `writeback_armed`**."* So the plan is always
computed and previewable, and only the writes are gated — the same
`log_only`-style separation [`services/routing/`](../../routing/DESIGN.md) §3
makes.

---

## 4. Two "never" invariants about writing to the wrong account

> We **NEVER** fall back to the owner token for a non-admin (that would create the
> playlist on the owner's account), and we **NEVER** delete a playlist that is not
> OUR managed anchor.

Both failures are silent and personal: a member's playlist appearing on the
owner's account, or Glidearr deleting a playlist a household member made
themselves. Rail 2 is **assert-checked** rather than merely intended.

This is the same fail-closed attribution discipline
[`plex/DESIGN.md`](../DESIGN.md) §3.2 documents for the per-user token map — the
package that handles per-member credentials treats mis-attribution as the primary
hazard in both places.

---

## 4.1 🔴 The fallback that ate the primary path (`GLD-PLY-13`)

Rail 5 promises *"an IN-PLACE add/remove/move diff (stable ratingKeys), delete+recreate
only as a last resort"*. The persisted anchor map exists and works. But the branch that
chooses between them was reading a metric on the wrong scale:

```python
move = survivors if survivors != cur_order else []   # all-or-nothing
...
n_changes = len(add) + len(remove) + len(move)
if n_changes > max(len(desired_rks), 1) * _RECREATE_RATIO:   # 1.0
```

`move` was never a count of displaced items — it was the ENTIRE survivor list the moment
the order changed at all. With `D` desired, `a` adds, `r` removes:

| Case | `n_changes` | Recreates when |
|---|---|---|
| Order unchanged | `a + r` | `a + r > D` |
| Order changed | `D + r` | **`r >= 1`** |

So once the brain re-ranks anything, the recreate trigger collapses to *"was a single item
removed?"* — which for an Up Next list is not an exception, it is the steady state.

**Measured, not inferred.** `playlists-4.log` and `playlists-5.log` agree exactly:

```
'Up Next' would be RECREATED (100 item(s))   x4
'Up Next' would update (+22/-0/~78)          x1
```

22 + 0 + 78 = 100 against `D = 100`. The one list that survived in place cleared the
threshold **by a margin of zero**, and only because nothing had been watched off it yet.

The fix makes `move` the complement of the longest already-correctly-ordered subsequence,
so the threshold finally measures what its comment claims. Re-simulated on the live shapes:
a `+22/-3` moderate re-rank drops 103 → 52, a FULL reshuffle 103 → 87 (both in-place),
while a genuine `+60/-40` overhaul still recreates.

**A new pattern shape, or P-B inverted.** Every P-B instance so far is a *guard narrower
than it appears*. This is the mirror: a **fallback BROADER than it appears**, where the
escape hatch quietly becomes the main road. Worth a §8 field-note row of its own — the
tell is the same in both directions (the metric in the comparison is not on the scale the
threshold assumes), so the sweep question generalises: *for every ratio test, is the
numerator measured in the same units as the denominator?*

---

## 5. Three patterns recurring, with new instances

**Make-before-break — third instance.** Rail 5's *"delete+recreate only as a last
resort and **create-new-then-delete-old**"*, after `CrossInstanceMove`'s copy-not-move
and the TV step-down's realize-before-drop.

**Confirmed-vs-absent — sixth instance.** Rail 6: orphan cleanup runs against the
**live Home roster**, so *"a PIN-mint failure leaves the playlist alone — present
in the roster, absent from `tracked_users`."* A user Glidearr failed to
authenticate is not an orphan, and naive cleanup would delete their playlist.

**A shipped bug recorded at its fix site — fourth instance.**

> `_BRAND_SCHEME = "2"` … v1 used the wrong `/playlists/{rk}/posters` endpoint
> (**404, silently cached as done**); v2 is `/library/metadata/{rk}/posters` +
> **2xx-verified**.

A 404 indistinguishable from success and then cached permanently — P-C and P-D in
one. The fix adds response verification *and* a scheme token so stale "done"
markers from the broken version no longer match.

---

## 6. Smaller observations

| Observation | Note |
|---|---|
| `_SORT_PREFIX = "!"` | *"'!' (ASCII 0x21) sorts ahead of every digit/letter (**verified live** — '_' would sort AFTER capitals, so it would NOT pin to the front)."* Empirically checked, rejected alternative recorded |
| Titles carry no username | *"each list lives on that member's OWN account, so the owner is unambiguous (and the title stays non-identifying)"* — privacy by construction |
| Display title vs sort key | The `!` lives **only** in Plex's `titleSort`, so the visible name stays clean |
| 🟡 **Mojibake, second instance** | `carry NO username �� each list…` — same class as `GLD-PLY-06`'s `￧`, different file |
| Nine plan families | Up Next · The Long Glide · Touch & Go · Fresh Arrivals · Anniversary Picks · On This Week · Hidden Gems, plus tv/movie/combined |
| `anon_label` / `_TIER_NAMES` | De-identified handles (`'T - adult 1'`) mirrored from the builders so logs correlate without naming members |

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-PLY-02` | 🟡 **Call `is_spoiler_safe` before the write** — `writeback.py` documents **seven P0 safety rails** and none covers ordering. ✅ **But session 65 confirms the guarantee is structural**: `tv_resolver.py` imports `order_items` from the brain, so GROUP→WITHIN→ACROSS *is* what orders the plan. **Downgraded 🔴→🟡** — belt-and-braces against a future refactor, not a present hole. Rail 4 is the natural home | S | `GLD-PLY-01` ✅ |
| `GLD-PLY-07` | ⚠️ **CORRECTED session 65 — the 0.18 ratio is misleading.** `builder.py`'s docstring: *"The I/O gather is defensive … **the tested core is `_build_for_users` (pure given its inputs)**."* So the untested bulk is I/O plumbing and the decision core **is** covered. Re-scope to: confirm `_build_for_users` coverage is genuinely complete, rather than treating 119.6 KB / 21.2 KB as a gap | S | — |
| `GLD-PLY-08` | 🎯 **Cite `writeback_armed`'s call-log test as the reference byte-identical opt-in** — ten such opt-ins found; this is the only one proven by **test** rather than by construction | S | `GLD-AFF-06` |
| `GLD-PLY-09` | **Fix the second mojibake** — `writeback.py`'s *"carry NO username ��"*, same class as `GLD-PLY-06` | S | `GLD-PLY-06` |
| `GLD-PLY-10` | **Read `builder.py` (119.6 KB) and `universe_order.py` (48.0 KB)** — the ordering logic the spoiler guarantee rests on, and the crown-jewel GROUP→WITHIN→ACROSS rule | L | `GLD-PLY-02` |
| `GLD-PLY-11` | **Surface rail 4's drift counter** — a large-drift user is *"skipped with a re-run note"*; whether that reaches the run summary is unverified | S | `GLD-PLX-03` |
| `GLD-PLY-13` | ✅ **FIXED session 66 — rail 5's "last resort" was the DEFAULT path.** `_diff` reported the WHOLE survivor list as `move` on any re-rank, so `n_changes` = `len(desired) + removes` and `_RECREATE_RATIO` tripped whenever a single item was removed. Live logs: **4 of 6 "Up Next" lists recreated every run.** `move` is now the minimum displaced set (`_min_moves`, LIS complement) | M | — |
| `GLD-PLY-14` | ✅ **FIXED session 66 — titleSort lost on every recreate.** `_recreate` never re-titled the new ratingKey and the title gate is keyed on `anchor_id`, which survives a recreate → the fresh list inherits a stale "already titled" marker and never gets its `!` front-pin. Same shape as the `_BRAND_KEY` bug; branding was fixed, the title half was missed | S | `GLD-PLY-13` |
| `GLD-PLY-15` | ✅ **ADDED session 66 — once-a-day rewrite cadence.** Item writes rate-limited to `min_interval_hours` (default 20) with a churn escape hatch `churn_override_ratio` (default 0.40) measured on adds/removes only, never moves. `recreated` + `deferred` counters added to the banner | M | `GLD-PLY-13` |
| `GLD-PLY-16` | **Make `_apply_diff` issue only the moves it reports.** The metric is now the minimum displaced set, but the applier still re-walks the FULL desired order (D `move_playlist_item` calls, deterministic + idempotent). Aligning it would cut ~100 calls per list to ~30 — but the reordering proof is fiddly and this is a live write path, so it was deliberately left alone. Do it only with a replay test | M | `GLD-PLY-13` |

## 8. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | ✅ **ANSWERED session 65** — **yes.** `tv_resolver.py` imports `order_items` from `machine_learning.playlists.ordering` and states the pipeline *"→ order_items (brain) → PlaylistPlan"*. Spoiler safety **is** structural; `GLD-PLY-02` downgrades to 🟡 | ✅ Closed |
| Q2 | How often does rail 4's re-resolution find drift large enough to skip a user? | `GLD-PLY-11` |
| Q3 | ✅ **ANSWERED session 66 — no.** Every line in `playlists-2/3/4/5.log` is `[disarmed]`; there is no `ARMED` banner in any rotation and `audit.log` carries no playlist write. So the whole `GLD-PLY-13` recreate storm was **preview only** — it never touched a member's account. Fixed before arming | ✅ Closed |

**Q1 is the one that decides `GLD-PLY-02`'s severity.** If `builder.py` orders by
`(season, episode)` via the brain's `order_within_group`, spoiler safety is
structural and the runtime check is belt-and-braces — worth adding, not urgent. If
the builder sorts by air date anywhere, the check is the only thing that would
catch it, and it is not running.

## 9. Related designs

- [`machine_learning/playlists/DESIGN.md`](../../../machine_learning/playlists/DESIGN.md) §3.3–3.4 — the crown-jewel ordering rule and `is_spoiler_safe`
- [`plex/DESIGN.md`](../DESIGN.md) §3.2 — the fail-closed attribution §4 mirrors
- [`services/routing/DESIGN.md`](../../routing/DESIGN.md) §3 — the same plan-always / act-only-when-armed split
- [`radarr/storage/DESIGN.md`](../../radarr/storage/DESIGN.md) §4.2 — make-before-break, first instance
- [`DESIGN_personal_playlists.md`](../DESIGN_personal_playlists.md) — the feature's own design note

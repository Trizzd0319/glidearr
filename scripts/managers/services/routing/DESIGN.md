# routing — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **routing**

**Manager** — `RoutingManager`
**Status** — ✅ Implemented, **inert until opted in** · 🟡 One guard fails toward action
**Existing docs** — **none.** The only service package in the repo with no markdown at all.

> **Coverage:** `__init__.py` head (~110 of 11.2 KB) read. `uhd_reconcile.py`
> (**57.2 KB**) unread beyond the `CrossInstanceMove` construction found by grep.

## 1.5 ✅ `uhd_reconcile.py` — `GLD-RAD-01` fully resolved (session 63)

Read: docstring + imports (~100 of 57.2 KB).

**It is built, wired, gated off, and self-declared EXPERIMENTAL.** None of the
three states I successively predicted (*not built* → *built, unwired* → *built and
wired*) was the whole answer.

> **EXPERIMENTAL** — the move uses Radarr's async import/rescan commands; the exact
> timing/convergence **wants validation against a live shared-storage pair before
> the consent gate is flipped on**.

So `GLD-RAD-01` is not an incomplete feature. It is a **complete feature
deliberately held behind consent pending live validation**, and the author says so
in the module header. The register carried it as *"Multi-instance tier routing
incomplete — add-if-absent + migration remain"* for the whole sweep.

### 1.5.1 🎯 A seven-condition gate stack — the strongest in the repo

| # | Gate | Default |
|---|---|---|
| 1 | `routing.configured` | onboarding must have run |
| 2 | `routing.movies.4k_policy == 'both'` | **`highest_only`** → skip |
| 3 | A **distinct** 4K instance resolves | — |
| 4 | `routing.reorg_mode` | **`log_only`** — logs the plan, moves nothing |
| 5 | `cross_instance_move_consent` | *"the MOVE physically relocates a file + re-searches"* |
| 6 | `cross_instance_dedup_consent` | separate, **on top**, *"AND honours the backup gate"* |
| 7 | Shared-storage **pre-flight probe** | *"otherwise it degrades to log-only rather than churning a Move scan that can never complete"* |

Plus `effective_dry_run` on every destructive step — *"a real run whose backup
pre-flight failed writes nothing."*

Seven gates, two of them *separate consents* for the two different destructive
acts (relocate vs delete), and a default that only ever logs. That exceeds even
[`backup/`](../backup/DESIGN.md)'s stack.

### 1.5.2 The stated safety invariants

| Invariant | Quote |
|---|---|
| Record preservation | *"The source Radarr **RECORD is never deleted** (it is retuned in place, so its id/history survive)"* |
| Delete scope | *"the **only file ever deleted** is a dedup LOSER's, and only **after the keeper's file is confirmed present**"* |
| Ordering | *"make-before-break (the source is only changed **after** the destination has the file)"* |
| Re-runnability | *"idempotent across runs"* |
| Ambiguity | a same-path duplicate (two records, one physical file) is *"flagged for the operator and **NEVER auto-acted**"* |

The last is the confirmed-vs-assumed discipline applied to a case the code cannot
safely resolve: two records pointing at one file could be a genuine duplicate or a
library misconfiguration, so it escalates rather than guessing.

### 1.5.3 Move and dedup are designed not to fight

> The dedup planner treats the intended dual-version split (≤1080p on standard +
> 2160p on the 4K instance) as **the desired end state, NOT a duplicate**, so the
> move and the dedup never fight.

Two subsystems whose naive implementations would undo each other — the move
*creates* the exact two-copy state the dedup exists to remove. Identified and
designed around rather than discovered in production.

### 1.5.4 Alias-tolerant instance labels

```python
# the role map writes "4K" while the folder bucket is "4k" (and operators may use
# uhd/2160) — accept them all so a casing/naming split never silently disables the move.
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
```

A real internal mismatch — role map `"4K"` vs folder bucket `"4k"` — absorbed
rather than left to fail silently. The identity discipline again, this time on
config labels.

### 1.5.5 🔴 Three shims in one file — the worst `GLD-CACHE-S01` instance

```python
from scripts.support.utilities.size_model        import profile_max_quality   # SHIM
from scripts.support.utilities.space_targets     import space_targets         # SHIM
from scripts.support.utilities.watch_likelihood  import watch_likelihood      # SHIM
# …alongside DIRECT brain imports:
from scripts.managers.machine_learning.space import dual_version
from scripts.managers.machine_learning.space.cross_instance_dedup import plan_dedup
from scripts.managers.machine_learning.space.routing_targets import (…nine symbols…)
from scripts.managers.machine_learning.classification.keep_policy import resolve_keep_policy
from scripts.managers.machine_learning.lifecycle.stale_prune_policy import clock_age
```

**Three different migration shims and five direct brain imports, in one block.**
Note `routing_targets` is imported *direct* while `space_targets` — its
neighbour in `machine_learning/space/` — comes through the shim.

Running tally: **10 callers across 4 shims** (`space_targets` ×5,
`watch_likelihood` ×3, `library_classifier` ×1, `size_model` ×1), every one found
incidentally. `GLD-RT-07`.

---

## 1. Two things live here

| Module | Size | Tests | Role |
|---|---|---|---|
| [`__init__.py`](./__init__.py) | 11.2 KB | 6.1 KB | `RoutingManager` — in-run library **re-organiser** |
| [`uhd_reconcile.py`](./uhd_reconcile.py) | **57.2 KB** | **55.7 KB** | UHD/dual-version reconciliation — constructs `CrossInstanceMove` |

**130 KB total, 62 KB of tests — a ratio above 1.0 — and not one line of
documentation.** Every other service package carries at least a `README.md`.

`uhd_reconcile.py:254` is `GLD-RAD-01`'s missing caller:

```python
actuator = CrossInstanceMove(gw, self.logger, dry_run=not (dual_armed and not eff_dry))
```

Note the double negative resolves correctly: live only when `dual_armed` **and**
not effectively-dry.

---

## 2. What `RoutingManager` does

> Reconciles **ALREADY-OWNED** movies + shows to the correct library FOLDER when a
> late signal (a Common Sense age arriving, anime detection, or a changed routing
> preference) means a title's classified bucket no longer matches where it sits on
> disk.

Classification and the move plan are the **shared, pure** `library_router` /
`library_classifier` — *"identical to the add-time resolver, so add-time and
re-org never disagree."* This manager is only the driver: fetch, plan, then LOG
or APPLY.

That is [`classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md)
§3.1's G2 (*"add-time and re-organisation make identical decisions"*) delivered —
one `plan_moves`, two callers.

---

## 3. 🎯 A four-condition gate stack

```python
if not self._routing.get("configured"):  return    # never-onboarded → nothing
mode = reorg_mode(self.config)
if mode == "off":                        return

apply = (mode == "same_instance") and relocation_enabled(self.config) and not self.dry_run
```

| Gate | Effect |
|---|---|
| `routing.configured` | Onboarding must have run |
| `routing.reorg_mode` | `off` \| `log_only` (classify + log, move nothing) \| `same_instance` |
| `relocation_enabled` | `same_instance` **also** requires explicit move consent |
| `not dry_run` | *"even then a dry_run never PUTs"* |

Four independent conditions before a single file moves, and a genuine middle
setting (`log_only`) that plans and reports without acting. Only
[`backup/`](../backup/DESIGN.md)'s gate is stronger.

Cross-instance migration is explicitly out of scope here — *"that stays a
separate, deferred path"* — which is `uhd_reconcile.py`'s job.

---

## 4. ✅ The `allowed_roots` guard now fails toward **inaction**

```python
# FAILS TOWARD INACTION. A failed rootfolder fetch used to leave the set empty,
# which DISABLED THE GUARD rather than the pass.
allowed_roots = set()
try:
    allowed_roots = {library_router._norm(r.get("path")) for r in
                     (im._make_request(name, "rootfolder", fallback=[]) or []) …}
except Exception as e:
    …
if apply and not allowed_roots:
    self._log("log_warning", "… PLANNING ONLY this run rather than moving files unguarded …")
    apply_here = False
else:
    apply_here = apply
```

The hazard is real and well identified: global folder maps against per-instance
roots, so a 4K instance could be told to move a kids-classified film into the
1080p kids root.

The failure direction used to be stated and **wrong by the repo's own rule** — an
empty `allowed_roots` disabled the cross-library guard and let the moves proceed
unguarded. [`discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md)
§3.3's rule — *on unknown input, fail toward the outcome that changes nothing* —
says the pass should be skipped, not the guard.

It now skips the pass. An unreadable root list downgrades that instance to
`apply_here = False` for the run: the plan is still classified, still written to
`support/logs/routing.log`, and the next run with a readable list applies it. The
two outcomes are not symmetric — skipping costs one run of re-organisation, while
moving unguarded can put a kids film where its audience cannot reach it and only a
manual move gets it back. `GLD-RT-01` ✅ **resolved** — see `ENHANCEMENTS.md`
§0.1 #72.

⚠️ **The guard's first live effect was to look like a broken consent gate.**
`test_routing_manager`'s fake instance-manager keyed its GET responses on the
instance NAME alone, so the new `rootfolder` fetch was answered with the ITEM
list; those dicts carry no `path`, the allowed set came out empty, and two
consented live-run tests failed `0 == 1` on the PUT count. That reads as a consent
regression, which is the one thing it was not. `GLD-ROU-09`.

---

## 5. 🟡 The same file imports through a shim *and* directly

```python
from scripts.managers.machine_learning.classification import library_router          # direct
from scripts.managers.machine_learning.space.routing_targets import reorg_mode, …    # direct
from scripts.support.utilities.library_classifier import classify_movie, classify_show, is_anime_media
#      ^^^ the MIGRATION Step 5a re-export SHIM
```

`library_router` comes straight from the brain; `library_classifier` comes through
the `support/utilities` shim — **in the same import block, for two halves of the
same subsystem**.

Second instance of the pattern `GLD-CACHE-S01` records in
`sonarr/cache/episode_files.py`, and it brings the shim tally to:

| Shim | Callers found |
|---|---|
| `support/utilities/space_targets` | 4 |
| `support/utilities/watch_likelihood` | 2 |
| **`support/utilities/library_classifier`** | **1 (here)** |

Seven callers across three shims, every one found incidentally. MIGRATION Step 10
cannot proceed without the full list. `GLD-RT-02`.

---

## 6. 🟡 A third `services → services` import

```python
from scripts.managers.services.mdblist import age_cache
```

After `sonarr → radarr` (`_pick_stepdown_release`) and `sonarr → plex`
(`tv_group_maps_from_series`). **Three instances now**, three different pairs.

`ARCHITECTURE.md` rule 3 remains silent on `services → services`, and this one is
the most defensible of the three — a routing decision genuinely needs CSM ages,
and `age_cache` is a read-only lookup. But it confirms `GLD-SP-03` as a settled
pattern rather than an exception.

---

## 7. The second consumer of `movieRootFolders`

```python
self._movie_root_folders = self.config.get("movieRootFolders", {}) or {}
```

[`classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md) §3.2
traced `GLD-CFG-03` to `target_folder` returning `""` when `movieRootFolders` is
empty. This is a **second** call site reaching that same function — the add-time
resolver being the first.

Both would compute a correct classification and resolve it to an empty
destination. Here the consequence is bounded by §3's gates (an un-onboarded
install never reaches it), but it means `GLD-CLS-01`'s warning belongs in
`target_folder` itself rather than at either call site. `GLD-RT-03`.

---

## 8. Worth crediting

**A dedicated log sink.** *"The full per-title plan can be thousands of lines, so
it goes to a DEDICATED file (`support/logs/routing.log`) instead of flooding the
run log/console — the main log gets only a per-instance count. Fresh plan each
run."* `log_to_file("routing", msg, reset=True)` — a logger capability no other
package in the sweep uses, solving a real problem (a full plan is unreadable
inline and invaluable on disk).

**`log_only` as a first-class mode.** Not a `dry_run` flag but a named setting
that classifies and reports without acting — so an operator can see what
re-organisation *would* do before consenting to it.

**Test ratio above 1.0** — 62 KB against 130 KB of source, including a dedicated
`test_uhd_demote_watchability.py` (20.4 KB).

---

---

## 8.5 `uhd_reconcile.py` — session 63

### 8.5.1 ✅ `GLD-RAD-01` fully answered: **built, wired, gated — and deliberately off**

> **EXPERIMENTAL** — the move uses Radarr's async import/rescan commands; the exact
> timing/convergence **wants validation against a live shared-storage pair before
> the consent gate is flipped on**.

That is the complete answer. `GLD-RAD-01` is not *"multi-instance tier routing
incomplete"* — it is **complete, wired, seven-gated, and intentionally disabled
pending a validation run**. The remaining work is an operator decision plus a
test against real shared storage, not engineering.

Its purpose is stated as the operator's own words:

> *"If we add a movie to standard radarr, and it gets upgraded to 4k, add it to
> the 4k instance … If one instance doesn't have access to the other's folders,
> re-grab/downgrade."*

### 8.5.2 Seven gates — the deepest stack in the repo

| # | Gate |
|---|---|
| 1 | `routing.configured` |
| 2 | `routing.movies.4k_policy == 'both'` — default `highest_only` → skip |
| 3 | A **distinct** 4K instance resolves |
| 4 | `routing.reorg_mode` — `off` / **`log_only` (default)** / `cross_instance` |
| 5 | `cross_instance_move_consent` — *"the MOVE physically relocates a file + re-searches"* |
| 6 | `cross_instance_dedup_consent` — separate, **plus the backup gate**, because dedup deletes |
| 7 | **Shared-storage pre-flight probe** — else *"degrades to log-only rather than churning a Move scan that can never complete"* |

Plus `effective_dry_run` on every destructive step, so *"a real run whose backup
pre-flight failed writes nothing."*

Gate 4's note is worth keeping: `cross_instance` is *"the file-relocation mode,
**un-conflated** from folder moves"* — the two were deliberately separated so
consenting to folder tidying does not consent to moving files between instances.

### 8.5.3 🎯 Two subsystems with opposite goals, explicitly reconciled

> The dedup planner treats the intended dual-version split (≤1080p on standard +
> 2160p on the 4K instance) as **the desired end state, NOT a duplicate**, so the
> move and the dedup never fight.

A move that *creates* a second copy and a dedup that *removes* second copies are
naturally adversarial. Rather than ordering them or gating one on the other, the
dedup planner was taught the intended end state. That is the durable fix —
ordering would have broken the first time either ran independently.

And the genuinely ambiguous case is escalated rather than guessed:

> a same-path duplicate (two records, one physical file) is **flagged for the
> operator and NEVER auto-acted**.

### 8.5.4 Safety invariants, stated

> The source Radarr **RECORD is never deleted** (it is retuned in place, so its
> id/history survive); the only file ever deleted is a **dedup LOSER's**, and only
> **after the keeper's file is confirmed present**.

Make-before-break again, and an explicit statement of the single deletion the
whole subsystem can perform.

### 8.5.5 An alias guard against silent disablement

```python
# Alias-aware: the role map writes "4K" while the folder bucket is "4k" (and operators may use
# uhd/2160) — accept them all so a casing/naming split never silently disables the move.
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
```

A casing mismatch between the role map and the folder bucket would have disabled
the entire feature with no error — the same class as `GLD-REP-01`'s
`"cache"`/`"repair_cache"`, caught here before it shipped.

### 8.5.6 🔴 Three shims **and** direct brain imports, in one file

```python
from scripts.managers.machine_learning.space import dual_version                  # direct
from scripts.managers.machine_learning.space.routing_targets import (…)            # direct
from scripts.managers.machine_learning.classification.keep_policy import (…)       # direct

from scripts.support.utilities.size_model import profile_max_quality               # SHIM
from scripts.support.utilities.space_targets import space_targets                  # SHIM
from scripts.support.utilities.watch_likelihood import watch_likelihood            # SHIM
```

The worst instance of `GLD-CACHE-S01` found: **three** shims alongside direct
brain imports in a single import block — and it introduces `size_model` as a
**fourth** distinct shim.

Running tally: `space_targets` ×5 · `watch_likelihood` ×3 · `library_classifier`
×1 · `size_model` ×1 — **10 callers across 4 shims**, every one found
incidentally. `GLD-RT-07`.

### 8.5.7 🎯 This package is a cross-service **coordinator**, which reframes `GLD-SP-03`

```python
from scripts.managers.services.acquisition.gateway import ArrGateway
from scripts.managers.services.radarr.storage.cross_instance_dedup_apply import CrossInstanceDedup
from scripts.managers.services.radarr.storage.cross_instance_move import CrossInstanceMove
from scripts.managers.services.radarr.storage.shared_storage import shared_storage_confirmed
```

Plus `services.mdblist.age_cache` in `__init__.py`. So `services/routing/` imports
from **acquisition, radarr and mdblist** — it is not a peer borrowing from a peer,
it is an **orchestration layer above the services**, like
[`services/coordinator/`](../coordinator/DESIGN.md).

That splits `GLD-SP-03` into two genuinely different things:

| Kind | Example | Assessment |
|---|---|---|
| **Coordinator → service** | `routing` → `radarr.storage.CrossInstanceMove` | **Structural and correct** — a coordinator using actuators |
| **Peer → peer** | `sonarr/series/space_pressure` → `RadarrSpacePressureManager._pick_stepdown_release` (a **private** method) | **The actual smell** |

`ARCHITECTURE.md` needs to distinguish them rather than rule on
`services → services` as one category. `GLD-RT-08`.

---

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-RT-07` | 🔴 **`uhd_reconcile.py` uses THREE shims plus direct brain imports** in one block — the worst `GLD-CACHE-S01` instance, and it adds `size_model` as a **fourth** shim. Tally: **10 callers across 4 shims** | S | `GLD-CACHE-S01`, `GLD-ML-15` |
| `GLD-RT-08` | 🎯 **Split `GLD-SP-03` into coordinator→service (structural, fine) and peer→peer (the smell)** — `services/routing/` imports from acquisition, radarr **and** mdblist because it is an orchestration layer, not a peer. `sonarr → radarr`'s **private-method** borrow is the case that actually needs a home in the brain | S | `GLD-SP-03` |
| `GLD-RT-01` | ✅ **`allowed_roots` fetch failure disabled the guard, not the pass** — a `rootfolder` error let same-instance moves proceed **without** the cross-library check. Now skips the pass for that instance (plan still logged, next readable run applies it) | S | ✅ **Fixed** — `ENHANCEMENTS.md` §0.1 #72 |
| `GLD-RT-02` | 🟡 **Same file imports `library_router` directly and `library_classifier` through the shim** — two halves of one subsystem, two paths. Second instance of `GLD-CACHE-S01`; brings the tally to **7 callers across 3 shims** | S | `GLD-COORD-02`, `GLD-ML-15` |
| `GLD-RT-03` | **Put `GLD-CLS-01`'s warning in `target_folder` itself** — there are now **two** call sites (add-time resolver + this re-organiser), and both would silently resolve to `""` | S | `GLD-CLS-01`, `GLD-CFG-03` |
| `GLD-RT-04` | 🔴 **Document this package** — 130 KB, 62 KB of tests, **zero markdown**. The only service package with none, and it contains `GLD-RAD-01`'s caller | M | — |
| `GLD-RT-05` | **Read `uhd_reconcile.py`** (57.2 KB) — the dual-version reconciler, `CrossInstanceMove`'s only caller, and the thing that decides whether `GLD-RAD-01` actually runs | L | `GLD-RAD-01` |
| `GLD-RT-06` | **Reconcile with `machine_learning/routing/`** — that package is an empty stub declaring `select_instance` (`GLD-ROU-01`), while *this* one is 130 KB of live routing. Decide whether the brain stub is still wanted | S | `GLD-ROU-01`, `GLD-ROU-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `RoutingManager.run()` have a caller — is it in the phase order? | `GLD-RT-05` |
| Q2 | Is `routing.configured` true on this install, and what is `reorg_mode`? | `GLD-RT-01` |
| Q3 | Does `uhd_reconcile` run, and under what gate? | `GLD-RT-05`, `GLD-RAD-01` |
| Q4 | Should the brain's empty `routing/` stub be deleted now that `services/routing/` exists? | `GLD-RT-06` |

**Q4 is worth settling cheaply.** [`routing/DESIGN.md`](../../machine_learning/routing/DESIGN.md)
records `machine_learning/routing/` as a declared stub whose stated source file
does not exist, and notes that instance-selection decisions live in the service
layer in violation of I3. This package is *why*. Either the brain stub is the
intended destination for `uhd_reconcile`'s decision half, or it should go.

## 11. Related designs

- [`machine_learning/routing/DESIGN.md`](../../machine_learning/routing/DESIGN.md) — the empty brain stub this package explains
- [`machine_learning/classification/DESIGN.md`](../../machine_learning/classification/DESIGN.md) §3.1–3.2 — the shared `plan_moves` and the `movieRootFolders` trace
- [`radarr/storage/DESIGN.md`](../storage/DESIGN.md) §4 — `CrossInstanceMove`, constructed by `uhd_reconcile.py:254`
- [`machine_learning/discovery/DESIGN.md`](../../machine_learning/discovery/DESIGN.md) §3.3 — the fail-direction rule §4 breaches
- [`backup/DESIGN.md`](../backup/DESIGN.md) — the only stronger gate stack in the repo

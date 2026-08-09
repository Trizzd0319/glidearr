# radarr/repair — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [services](../../README.md) › [radarr](../README.md) › **repair**

**Manager** — `RadarrRepairManager` · **Key module** — `anomaly.py`
**Status** — 🔴 The AXIS LEGACY home. Owns four delete-family thresholds and describes ~2 of ~15 responsibilities.

> **Coverage:** `anomaly.py` head (~75 lines — docstring, imports, formatters). The
> file runs past **line 1350**; `_score_owned`, the demote/restore/unmonitor legs
> and the 4K gate are unread. `RadarrRepairManager.__init__` unread.

---

## 1. Why this file matters more than its docstring suggests

`repair/anomaly.py` is named in the register more than any other single module:

| It owns | Register |
|---|---|
| **AXIS LEGACY** — `_score_owned`, re-scored per-run from raw Radarr dicts | `registry.py` delete block, D27 |
| `movie_demote` (20), `movie_restore` (20), `movie_unmonitor` (20) | `THRESHOLD_SPECS` |
| `movie_monitor` (30) | `THRESHOLD_SPECS` |
| `uhd_dual` (75) — *"the only UHD gate that compares the WATCHABILITY score"* | `THRESHOLD_SPECS` |
| The **16.5 % disagreement** with AXIS V2 on delete-eligibility (354 of 2,151 movies) | `registry.py` |

And its module docstring, in full:

```
Detects anomalous movie states in Radarr:
- Movies in the wrong instance (resolution mismatch vs. instance policy)
- Movies with missing files but still monitored
```

### 1.1 🔴 The imports tell a completely different story

```python
from …classification.keep_policy        import resolve_keep_policy
from …space.downgrade_planner           import UNIVERSE_PROTECT_MIN
from …space.dual_version                import DEFAULT_UHD_SCORE, pick_hd_profile
from …thresholds.registry               import get_threshold
from …lifecycle.monitor_policy          import release_available, triage_action
from …lifecycle.stale_prune_policy      import (budget_delete_cohort, clock_age,
                                                expedite_dwell, franchise_delete_exempt,
                                                prune_below_floor_action, prune_score_gate,
                                                restore_cooldown_active)
from …space_targets                     import (coordinator_owns_deletion, deletions_enabled,
                                                space_targets)
```

Keep-policy resolution · universe protection · **dual-version 4K routing** ·
threshold routing · monitor triage · **delete-cohort budgeting** · franchise
delete exemption · **restore cooldown** · prune floors and score gates ·
coordinator hand-off · deletion enablement.

**The docstring covers two of them.** It reads as a diagnostic reporter; the
imports describe a module that **demotes, deletes, restores, unmonitors and
routes 4K copies**.

This is the sweep's most consequential **P-G** instance. Every prior one was a
stale figure or a mis-scoped note; this one would lead a reader to believe a
file that owns four delete thresholds is read-only. `GLD-RAN-01`.

---

## 2. 🟡 `_UHD_LABELS` — a third copied vocabulary, and this one says "MUST match"

```python
# Alias-aware 4K/UHD instance labels — MUST match UhdReconcileManager._UHD_LABELS so the
# monitored-missing triage and the dual-version reconcile agree on which Radarr session is the
# dedicated 4K instance (the role map writes "4K" while the folder bucket is "4k").
_UHD_LABELS = ("4K", "4k", "uhd", "UHD", "2160p", "2160")
```

The identical tuple sits in
[`services/routing/uhd_reconcile.py`](../../routing/DESIGN.md) §1.5.4. The comment
states the requirement in capitals and **nothing enforces it**.

Third instance of the shape:

| Copy | Enforceable? |
|---|---|
| `next_watch.INTENT_SOURCE_STRENGTH` | ❌ — the brain cannot import a service (D40) |
| `radarr/quality._KID_AGE_TIERS` | ✅ — a `services → services` import exists |
| **`anomaly._UHD_LABELS`** | ✅ — **both files are services**, and `uhd_reconcile` already imports from `radarr/storage` |

Only the first is forced. This one is a copy between two service modules that
already import from each other's neighbours, with the coupling written out in the
comment. If they drift, the triage and the reconcile disagree about **which
Radarr instance is the 4K one** — silently, and in opposite directions.
`GLD-RAN-02`.

---

## 3. Confirmations

**It routes through the threshold registry.** `get_threshold` is imported, so the
four delete-family constants are registry-routed exactly as
[`series/`](../../sonarr/series/DESIGN.md) §5's `GLD-SER-05` established for the
Sonarr side. The calibration machinery covers both services.

**Sixth `space_targets` shim caller.** `from scripts.support.utilities.space_targets
import …` — running tally **11 callers across 4 shims**, every one found
incidentally. MIGRATION Step 10 still has no list.

---

## 4. 🎯 Worth crediting — the grid formatters

```python
# Keep the dry-run / repair grids self-explanatory: a Year disambiguates same-titled
# entries (e.g. several "Demon Slayer" films) and a critic Rating gives an outside
# anchor to read the watchability Score against. Both are pulled straight from the
# already-cached Radarr movie dict — no extra API calls.
```

Two small ideas worth naming:

**An independent anchor beside the model's own number.** The grid shows
watchability *next to* a critic rating, so an operator reading *"delete: score 6"*
can see whether the film is also poorly reviewed or whether the model is the
outlier. Almost nothing else in the repo presents a decision alongside a signal
the model did not produce.

**Two scales handled correctly.** IMDb → TMDb → Trakt render as `x.x` (out of 10);
Rotten Tomatoes → Metacritic as `n%` (out of 100). A naive chain would print
`85.0` beside `7.4` and invite exactly the wrong comparison.

---

## 5. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-RAN-01` | 🔴 **Rewrite `anomaly.py`'s docstring** — it claims two diagnostic responsibilities; the imports show ~15, including **demote, delete-cohort budgeting, restore cooldown, franchise exemption, unmonitor triage and dual-version 4K routing**. The file owns **four delete-family thresholds** and the whole AXIS LEGACY. **The worst P-G instance in the sweep**: a reader would believe it is read-only | S | D27 |
| `GLD-RAN-02` | 🟡 **`_UHD_LABELS` is copied from `uhd_reconcile.py` with "MUST match" and no enforcement** — and unlike `next_watch`'s forced copy, **both files are services**, so an import is available. Drift means the triage and the reconcile disagree about which instance is the 4K one | S | `GLD-RQ-02`, `GLD-SP-03` |
| `GLD-RAN-03` | **Read `_score_owned`** — the AXIS LEGACY computation itself, D27's subject, and the source of the **16.5 %** delete-eligibility disagreement with AXIS V2 | M | D27, `GLD-THR-03` |
| `GLD-RAN-04` | 🎯 **Propagate the independent-anchor idea** — showing a critic rating beside the watchability score lets an operator see when the model is the outlier. The plan ledger and the coordinator's delete table show scores with no external reference | S | `GLD-LED-05` |
| `GLD-RAN-05` | **Read `RadarrRepairManager.__init__`** — Sonarr's twin has `run_all_repairs` but no caller (`GLD-REP-07`); Radarr's has a `@timeit("run_all_repairs")` decorator, so the same question applies | S | `GLD-REP-07` |

## 6. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Does `_score_owned` still take the Group D **v1** path, as `registry.py` asserts? | `GLD-RAN-03`, D27 |
| Q2 | Is `RadarrRepairManager.run_all_repairs` invoked, or dormant like Sonarr's? | `GLD-RAN-05` |
| Q3 | Do the two `_UHD_LABELS` tuples currently agree? *(They do as read — the question is what stops them diverging.)* | `GLD-RAN-02` |

**Q1 is D27's remaining half.** `registry.py` states that `_score_owned` passes no
`transcode_profile`, so Group D takes the v1 path and the axis never moved under
Group D v2 — which is *why* these four thresholds stayed at 20 while the V2 family
went to 17. That claim is load-bearing for four live delete cutoffs and has not
been checked against the code.

## 7. Related designs

- [`machine_learning/thresholds/registry.py`](../../../machine_learning/thresholds/DESIGN.md) — the delete block, the two axes, and the 16.5 % measurement
- [`services/routing/DESIGN.md`](../../routing/DESIGN.md) §1.5.4 — the other `_UHD_LABELS`
- [`sonarr/repair/DESIGN.md`](../../sonarr/repair/DESIGN.md) — the Sonarr twin, whose `anomaly.py` shares only the name (D27, session 51)

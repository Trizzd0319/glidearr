# sonarr/series/quality.py — verification notes

> Companion to [`DESIGN.md`](./DESIGN.md). Records what a full read of
> `quality.py` (40.8 KB) established, session 41.

---

## 1. ✅ Q3 answered — the thresholds **do** route through the registry

```python
from scripts.managers.machine_learning.thresholds.registry import get_threshold
...
promote_threshold = get_threshold("series_monitor", self.config,
                                  _int_cfg("series_monitor_score_threshold", 35), logger=...)
demote_floor      = get_threshold("series_demote", self.config,
                                  _int_cfg("series_demote_score_threshold", 17), logger=...)
```

> Both band edges read through the calibrated-threshold registry; in the default
> `mode="shadow"` each returns the literal below unchanged.

So the calibration machinery **can** drive TV. `THRESHOLD_SPECS`' `file:line`
references are accurate, and the shadow-mode contract holds on this path.

### 1.1 The asymmetry is deliberate — and it confirms `thresholds/` §3.5

> **NOTE the asymmetry, and do not "fix" it:** the DEMOTE floor was re-anchored
> 20 → 17 when Group D v2 translated the score axis, but the MONITOR threshold
> stays at **35**. They act on **different populations** — the monitor gate is
> crossed only by **file-owning** series (zero stubs reach 35 either way), while
> the demote floor sweeps the **whole** series list.

[`thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.5
derived exactly this from the stub analysis (7,600 of 11,986 series own no file;
the stub axis moved −2 against the file-owning −13). The call site states the same
conclusion independently, and pre-emptively warns against "fixing" it.

Two documents, derived separately, agreeing — the second such verified
consistency after `mal_min_watchability` (§8 P-G's positive row).

---

## 2. 🎯 The fail-safe keep-tag guard — the best guard in the repo

```python
# FAIL-SAFE: the keep tag is exactly how a user pins a LOW-affinity show this policy
# would otherwise dormant, so the guard must never fail open. Pass fallback=None so we
# can tell a FAILED fetch (→ None) from a genuinely-empty catalogue (→ []).
raw_tags = self.sonarr_api._make_request(instance, "tag", fallback=None)
if raw_tags is None:
    tags_ok = False
...
if not tags_ok and unmonitor_ids:
    stats["guard_deferred"] = len(unmonitor_ids)
    self.logger.log_warning(f"  ⚠️ [{instance}] keep-tag catalogue unavailable — deferring "
                            f"unmonitor of {len(unmonitor_ids)} series this pass …")
    unmonitor_ids = []
```

Four things at once, and each is a pattern the register tracks separately:

| Property | Pattern |
|---|---|
| `fallback=None` distinguishes **failed fetch** from **empty catalogue** | Confirmed-absent vs transient — **fifth instance, first applied to a guard** |
| On failure, suppress only the **destructive** leg | D36 — fail toward the outcome that changes nothing |
| Re-monitoring climbers still proceeds | Degradation is scoped to the risky half, not the whole pass |
| `guard_deferred` is **counted and described** in the stats table | Visible degradation (`GLD-PLX-03`'s discipline) |

The reasoning is stated in full: *"the keep tag is exactly how a user pins a
LOW-affinity show this policy would otherwise dormant."* The guard protects
against its own policy's blind spot, and knows it.

---

## 3. 🟡 `run()` is a no-op — a fifth "constructed ≠ invoked" layer

```python
def prepare(self):
    self.logger.log_debug("🔧 SonarrSeriesQualityManager preparation complete (no subcomponents declared).")

def run(self):
    self.logger.log_info("🚀 Running SonarrSeriesQualityManager components...")
```

Both are log lines and nothing else. The real entrypoints —
`run_active_watcher_upgrades` and `run_monitor_by_watchability` — are **methods
someone else must call**.

`SonarrSeriesManager.run()` iterates its components and calls `comp.run()`, which
for `quality` logs and returns. So the actual quality work is invoked from
elsewhere (presumably `orchestration/series.py`, unread past line 100).

That is the same shape as `GLD-ORCH-S01` one level down, and it means the run
tree's `✅ Ran: quality` debug line records **nothing having happened**.
`GLD-SERQ-01`.

---

## 4. 🟡 A third live caller of the migration shim

```python
from scripts.support.utilities.space_targets import space_targets
```

After `coordinator/space_coordinator.py` and `orchestration/series.py`. **Three
found by accident, in three unrelated reads.**

`GLD-COORD-02` needs an import search before MIGRATION Step 10 can proceed —
the shims cannot be deleted while callers exist, and the callers are not
enumerated anywhere.

---

## 5. Confirmations of earlier findings

### 5.1 `last_watched_at` is consumed exactly as `lifecycle/` intended

```python
# REQUIRED since watch_count counts WATCHES, not plays: a series whose only plays were
# sub-threshold samples now has watch_count 0, and without the "it was played" bit it would
# read UNTOUCHED (untouched_base + score) instead of ABANDONED (ceiling 25) — i.e. a sampled
# series could earn a HIGHER resolution cap than a watched one.
```

[`lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) §3.2
argued `last_watched_at` must stay threshold-free for precisely this reason.
Here is the consumer relying on it, with the same reasoning independently stated.

### 5.2 Evidence for D34 — a named "device→codec stage" exists

```python
def _codec_variant(profile: dict) -> bool:
    """A device-audience codec variant, e.g. ``WEB-2160p (Combined) (AV1)``. NEVER an
    auto-upgrade target: the RESOLUTION ladder picks the agnostic tier; the device→codec
    stage assigns the codec."""
```

The upgrade pass **deliberately excludes** codec variants so a later stage can
assign them. That does not prove `choose_codec_profile` is wired — but it
confirms the stage is an intended part of the architecture, and that this pass
defers to it rather than competing with it.

Repeated at `capped_target`: *"the device→codec stage assigns the codec variant,
**never this upgrade pass** (so 'best by resolution' can't land on a `(AV1)`
profile any more)"* — the "any more" implying it once did.

### 5.3 `alert_unconfigured_floor` exists

```python
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
...
alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, _total_gb)
```

`GLD-SPA-01` and `GLD-COORD-06` propose warning when the space floor falls back.
A utility for it **already exists** and is called here. Both items should be
re-scoped to *"call the existing alert from the remaining sites"* rather than
*"build a warning."* `GLD-SERQ-02`.

---

## 6. Two more instances of care worth recording

**Dry-run preview stability:**

> a dwell-met candidate isn't actually unmonitored in dry_run, so keep its dwell
> clock — else the next preview resets age→0 and the series oscillates "would
> unmonitor" / quiet / "would unmonitor". *(Mirrors the Radarr twin in anomaly.py.)*

A dry run that mutated the dwell clock would produce **oscillating previews** —
a subtle way for a preview to be wrong without being incorrect about any single
title.

**Batched writes with a stated failure mode:**

> each Sonarr write + write-lock-hold stays small, well under the shared 30s HTTP
> timeout, and a large first-enablement pass is **resumable** instead of one
> oversized PUT that can time out and **mis-mark the whole set as failed** (the
> client returns the fallback on timeout).

`_BATCH = 200`. The reason is not "be polite to the API" but "a timeout would
mis-report success/failure for every id in the payload."

---

## 7. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-SERQ-01` | 🟡 **`SonarrSeriesQualityManager.run()`/`prepare()` are no-op log lines** — the real entrypoints are methods called from elsewhere, so `✅ Ran: quality` records nothing happening | §3 — fifth "constructed ≠ invoked" layer; the run tree reads as green | S | `GLD-ORCH-S01` |
| `GLD-SERQ-02` | **Re-scope `GLD-SPA-01` / `GLD-COORD-06`** — `alert_unconfigured_floor` already exists and is called here. The work is *calling it from the remaining sites*, not building it | §5.3 | S | `GLD-SPA-01`, `GLD-COORD-06` |
| `GLD-SERQ-03` | 🎯 **Cite the keep-tag guard as the reference fail-safe** — `fallback=None` to separate failed-fetch from empty, suppress only the destructive leg, count the deferral | §2. Fifth confirmed-absent-vs-transient instance and the first on a *guard* | S | `GLD-MDB-05` |
| `GLD-SERQ-04` | **Document `space_pressure.py`** — 40.2 KB, the third `THRESHOLD_SPECS` consumer (`tv_delete_ceiling`, 17) | Only `quality.py` was read this pass | M | `GLD-SER-05` |
| `GLD-SERQ-05` | **Find the caller of `run_active_watcher_upgrades` / `run_monitor_by_watchability`** | §3 — until found, neither is demonstrated to run | S | `GLD-SERQ-01` |

## 8. Related

- [`DESIGN.md`](./DESIGN.md) — the folder design this verifies
- [`machine_learning/thresholds/DESIGN.md`](../../../machine_learning/thresholds/DESIGN.md) §3.5 — the stub analysis §1.1 confirms
- [`machine_learning/lifecycle/DESIGN.md`](../../../machine_learning/lifecycle/DESIGN.md) §3.2 — the raw `last_watched_at` §5.1 consumes
- [`machine_learning/quality_analytics/DESIGN.md`](../../../machine_learning/quality_analytics/DESIGN.md) — the device→codec stage §5.2 defers to
- [`coordinator/DESIGN.md`](../../coordinator/DESIGN.md) §3.5 — the shim, third caller in §4

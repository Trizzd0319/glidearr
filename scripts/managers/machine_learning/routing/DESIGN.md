# routing — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **routing**

**Package** — `scripts.managers.machine_learning.routing`
**Status** — 🔵 Planned — declared stub, no implementation
**Related** — [README.md](./README.md) · [`ARCHITECTURE.md`](../ARCHITECTURE.md) · [`radarr/DESIGN.md`](../../services/radarr/DESIGN.md)

---

## 1. Problem statement

A household running more than one Radarr instance — `standard` for HD, `ultra`
for 4K/UHD, optionally a dedicated `anime` instance — must route every
acquisition and every upgrade to the instance whose role matches the target
tier. Get it wrong and a 4K-worthy title lands on `standard` with no path to
`ultra`.

That is a **value judgement**: it depends on the watchability score, the target
quality tier, and the title's classification. Under
[`ARCHITECTURE.md`](../ARCHITECTURE.md)'s guiding principle — *"Services SENSE
and ACT; the ML layer THINKS"* — it belongs in the brain.

It is not there. This package is the declared destination; the decision logic is
still in `services/`.

---

## 2. Design goals & non-goals

### Goals (as declared by the stub and ARCHITECTURE)

| # | Goal |
|---|---|
| G1 | `select_instance(item, instances, config) -> str` — one entrypoint. |
| G2 | Pure: consumes `contracts.*` feature rows + config; emits plain data. |
| G3 | Service keeps instance-config FETCH and the APPLY; brain decides only. |
| G4 | Migration is shimmed — importers keep calling the service method, which delegates here. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Instance configuration | Service reads it; brain receives it. |
| N2 | Executing the move | `SERVICE REMAINDER … Service keeps instance config FETCH + apply.` |
| N3 | Single-instance behaviour | With one instance there is no decision — a documented no-op. |

---

## 3. Architecture

### 3.1 What exists

```
routing/
  __init__.py            docstring only
  instance_selector.py   docstring + declared API + TODO comment
```

The stub is unusually well-formed for a placeholder. It records:

- **Migration target** and the purity contract (`NO HTTP, NO service imports, NO global_cache writes`)
- **Purpose** — *"Decide which configured instance an add/route targets"*
- **Pulls from** — the decision core it is meant to absorb
- **Public API** — the exact signature
- **Depends on** — `contracts`
- **Service remainder** — what stays behind

That is enough for someone to implement it without archaeology, which is the
point of a declared stub rather than an empty file.

### 3.2 🔴 The consequence: a live layering violation

Because this package is empty, instance selection happens in the service layer:

| Concern | Live location | Layer |
|---|---|---|
| Categorised instance lookup | `acquisition/gateway.py::categorized_instance` | service |
| Anime-movie routing | `services/acquisition/resolver.py` | service |
| Resolution-tier routing (new adds) | `support/tools/router_movie.py` | **tool** |
| Per-instance profile resolution | `services/radarr/quality/selector.py` | service |

[`managers/DESIGN.md`](../../DESIGN.md) invariant **I3** states *"`services/`
contains no scoring, ranking or threshold logic."* Instance selection by target
tier is exactly threshold logic — the tier comes from the watchability score via
the quality ladder.

So I3 is violated today, and it is violated *by design of the migration's
current position*, not by accident. `GLD-MGR-01` (the service-purity hook) would
flag it if that hook existed — which is the argument for building the hook and
this package together rather than separately.

**The tool row is the sharper one.** Resolution-tier routing for new adds
happens post-landing in `router_movie.py` because *"the file doesn't exist at
add-time"* — so a decision that should be in the brain is currently in an
operator tool, outside the run path entirely. See
[`radarr/DESIGN.md`](../../services/radarr/DESIGN.md) §3.3 and `GLD-RAD-02`.

### 3.3 🟡 The stated source does not exist

[`ARCHITECTURE.md`](../ARCHITECTURE.md) and the stub both name the source to
absorb:

> `machine_learning/instance_selector.py` (decision half)

There is no such file at the `machine_learning/` package root. The root contains
`genre_predictor.py`, `penalty.py`, `plan_summary.py`, `size_calibration.py`,
`storage_estimator.py`, `transcode_analyzer.py`, `upgrade.py`,
`watchhistoryaggregator.py` — and no `instance_selector.py`.

Three readings, and nothing distinguishes them:

1. It was deleted after the map was written, and the map is stale.
2. It never existed, and the map is aspirational.
3. The decision half lives elsewhere (the `gateway`/`resolver`/`selector`
   trio in §3.2) and the map names a file that was only ever a plan.

Reading 3 is most likely given what §3.2 found, but the map should say so.
`GLD-ROU-02`.

### 3.4 🟡 Guarded but empty

`routing` **is** in `brain_purity.py`'s `_GUARDED_SUBPACKAGES`. The guard
currently protects a file with no imports and no code.

That is harmless, and arguably correct — the guard is in place *before* the
implementation lands, so the first real import is checked. Worth noting only
because it inflates the apparent coverage of `_GUARDED_SUBPACKAGES`: one of the
seventeen guarded packages is empty, and two more (`labels`, and the root tier)
have documented purity exceptions. See
[`labels/DESIGN.md`](../labels/DESIGN.md) §3.5.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Declare the stub rather than leave the folder absent | Records the intended API, dependencies and service remainder, so implementation needs no archaeology | Create it when needed |
| D2 | Shim on migration, not a flag-day cut | G4 — importers keep calling the service method, which delegates. Consistent with the six `MIGRATION.md` shims already in place | Rewrite call sites |
| D3 | Service keeps FETCH + APPLY | G3 — instance config is I/O, and the move is an APPLY | Brain owns the whole flow |
| D4 | Guard the package before implementation | The first real import is checked | Add to the guard on landing |

---

## 5. Invariants

Nothing executes, so there are no runtime invariants. The invariants this package
would carry on implementation:

| # | Invariant |
|---|---|
| I1 | `select_instance` is pure — no HTTP, no service imports, no `global_cache` writes. |
| I2 | With one configured instance, it is a no-op returning that instance. |
| I3 | Target tier drives selection; the tier comes from the score, not from the title. |
| I4 | Anime routing falls back to the default when no anime instance exists. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius | Signal? |
|---|---|---|---|---|
| Import attempt | `ImportError` on the named symbol | Loud | Immediate | ✅ |
| **Instance selection lives in `services/`** | **None** — `GLD-MGR-01` would catch it | I3 of `managers/DESIGN.md` violated | 🟡 Architectural, not behavioural | ❌ **None** |
| Tier routing happens post-landing in a tool | **None** | A brain decision runs outside the run path | 🟡 4K-worthy upgrades land on `standard` | ❌ **None** |
| `ARCHITECTURE.md` names an absent source | **None** | Migration map is unreliable at this row | 🟡 | ❌ **None** |

None of these is a runtime fault. All four are the same shape: **the map and the
territory disagree, and nothing compares them.**

---

## 7. Configuration surface

None yet. On implementation it would consume the keys the service layer reads
today:

| Key | Effect |
|---|---|
| `radarr_instances` | The instance map |
| `radarr_instances_categorized` | Role map — resolution tiers + optional anime |
| `routing.movies.4k_dual_min_score` | The `uhd_dual` gate (75, `falsy_means_default`) |

---

## 8. Implemented capabilities

None. The package contains a docstring, a declared API, and a TODO.

What *is* in place and would be reused:

- ✅ `radarr_instances_categorized` written by onboarding (Radarr Phase 1)
- ✅ `gateway.categorized_instance` is service-aware (Phase 2)
- ✅ Anime-movie routing with default fallback (Phase 2)
- ✅ `contracts/` shapes the entrypoint would consume
- ✅ `routing` already in `_GUARDED_SUBPACKAGES`

## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| `GLD-ROU-01` | 🔴 **Implement `select_instance`** and shim the service callers to delegate | Closes the I3 layering violation and gives `GLD-RAD-01`/`02`/`03` one place to put the tier logic instead of three | M | `GLD-RAD-01`, D30 |
| `GLD-ROU-02` | **Correct `ARCHITECTURE.md`'s `routing/` row** — the named source `machine_learning/instance_selector.py` does not exist *(P-G, in the repo's own docs)* | The migration map is the thing a future implementer trusts | S | — |
| `GLD-ROU-03` | **Move tier routing out of `router_movie.py`** into the run path | §6 row 3: a brain decision currently lives in an operator tool | M | `GLD-RAD-02`, `GLD-ROU-01` |
| `GLD-ROU-04` | **Add the no-op single-instance test** before implementing | I2 is the case most likely to regress into a spurious move | S | `GLD-ROU-01` |
| `GLD-ROU-05` | **Record guarded-but-empty packages** in the purity guard's output, so `_GUARDED_SUBPACKAGES` coverage is not overstated | §3.4 — one of seventeen guarded packages is empty | S | `GLD-ML-02` |
| `GLD-ROU-06` | **Audit `ARCHITECTURE.md`'s full subpackage map** against the tree — the `routing/` row is wrong; `quality_analytics/` names `profile_selector.py` at root when it lives in `quality_analytics/` | The map is cited by every migration stub | S | `GLD-ROU-02` |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `select_instance` be implemented now, or after `GLD-RAD-01`'s add-if-absent and migration work settles the semantics? *(= D30)* | `GLD-ROU-01` |
| Q2 | Does the entrypoint take a `MovieFeatureRow`, or a resolved target tier? The stub says `item`, which is ambiguous. | `GLD-ROU-01` |
| Q3 | Should Sonarr be in scope at all? It is single-instance with no categorized map, so `select_instance` would be a permanent no-op for TV. | `GLD-ROU-01` |
| Q4 | Is the `machine_learning/instance_selector.py` reference stale, aspirational, or pointing at the `gateway`/`resolver`/`selector` trio? | `GLD-ROU-02` |

**Q1 has a defensible answer either way**, and it is worth stating: implementing
`select_instance` *first* gives `GLD-RAD-01`'s three remaining sub-tasks a single
place to land, rather than adding tier logic to `gateway`, `resolver` and
`selector` separately and migrating three call sites later. The cost is designing
the signature before the semantics are fully settled — which Q2 and Q3 are
already circling.

## 11. Related designs

- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — the subpackage map and the boundary contract this stub cites
- [`MIGRATION.md`](../MIGRATION.md) — the shim strategy (D2)
- [`radarr/DESIGN.md`](../../services/radarr/DESIGN.md) §3.3 — the live implementation and its remaining gaps
- [`managers/DESIGN.md`](../../DESIGN.md) §5 I3 — the invariant §3.2 violates
- [`orchestration/DESIGN.md`](../../orchestration/DESIGN.md) — the other declared-but-empty folder in this repo

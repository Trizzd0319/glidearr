# onboarding / steps — Design

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [factories](../../README.md) › [onboarding](../README.md) › **steps**

**Package** — `scripts.managers.factories.onboarding.steps`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`onboarding/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

First-run setup spans roughly a dozen unrelated domains — downloader instances,
watch-history sources, library paths, routing, deletion policy, daemons,
notifications, scoring defaults. Written as one procedure it has three defects:

1. **Untestable.** Testing the Trakt OAuth path requires stubbing every prompt
   that precedes it.
2. **All-or-nothing failure.** An unreachable Tautulli aborts the whole wizard,
   losing the Sonarr and Radarr configuration already gathered.
3. **No targeted reconfigure.** Changing one service means re-answering
   everything.

Decomposing into step objects with a uniform contract fixes all three. The design
question that remains is how steps share information without becoming coupled —
the arr step *discovers* root folders that the library step *needs*.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | Each step is independently testable with a fake prompter. |
| G2 | One step's failure never aborts the wizard. |
| G3 | Steps hand off data without importing each other. |
| G4 | A step works identically interactive and headless. |
| G5 | Any single step can be run alone (`only_service`). |
| G6 | Steps never persist — abort-without-save stays possible. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Declarative dependency resolution | Two real dependencies; an explicit ordered list is clearer. |
| N2 | Re-runnable mid-wizard | Restart is cheap. |
| N3 | Per-step undo | The whole wizard is atomic — save happens once at the end. |

---

## 3. Architecture

### 3.1 The step contract

```python
class Step:
    name: str
    service: str | None          # for only_service filtering

    def run(self, prompter, cfg, ctx) -> list[StepResult]: ...
```

| Parameter | Role |
|---|---|
| `prompter` | G4 — the sole input channel. A step never reads `os.environ` or calls `input()` directly. |
| `cfg` | The merged canvas from `deep_merge(empty_config(), existing)`. Mutated in place. Every key already exists, so a step assigns rather than defends. |
| `ctx` | G3 — a plain dict for handoff. Currently `{"root_folders": [...]}`. |
| returns | `list[StepResult]` for the summary table. |

`StepResult` carries icon / service / detail and is rendered by
`OnboardingManager._summary()` as a `log_table`.

### 3.2 Control flow

```
build_steps(logger, only_service) → ordered [Step]
      only_service set? → filter to that step's service        ← G5
   │
   for step in steps:
       try:    results += step.run(prompter, cfg, ctx)
       except KeyboardInterrupt / EOFError:  → propagate, NO save
       except Exception as e:                → warn + failed StepResult, continue  ← G2
```

The two-class exception split is the same one described in
[`onboarding/DESIGN.md`](../DESIGN.md) §3.2, and it is what distinguishes
"the operator left" from "one service is down."

### 3.3 The `ctx` handoff

```
arr.py     probes Sonarr/Radarr → GET /rootfolder
             ctx["root_folders"] = [...discovered paths...]
                    │
library.py  offers ctx["root_folders"] as menu choices
             rather than asking the operator to retype paths
```

This is the only genuine inter-step data dependency, and `ctx` keeps it from
becoming an import. `library.py` reads a dict key; it does not know `arr.py`
exists. A future step can publish into `ctx` without any consumer changing.

### 3.4 Ordering

Two dependencies are real:

| Step | Needs | Reason |
|---|---|---|
| `library` | `arr` | Consumes `ctx["root_folders"]` |
| `routing` | `arr` | Instances must exist to be routing targets |

The rest is ordered as an operator narrative: downloaders → watch-history
sources → library shape → policy → background workers → notifications → tunables.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Uniform `run(prompter, cfg, ctx)` signature | G1 — one fake prompter tests any step | Bespoke signatures |
| D2 | Mutate `cfg` in place | The canvas already has every key from `deep_merge`; returning partial dicts would need merging logic per step | Return a patch |
| D3 | `ctx` dict rather than step-to-step imports | G3 — publishers and consumers stay decoupled | Direct references |
| D4 | Steps never save | G6 — persistence in one place is what makes abort-without-save work | Save per step |
| D5 | All input via `prompter` | G4 — a step cannot accidentally work only interactively | Direct `input()` / `os.environ` |
| D6 | `StepResult` rows rather than logging inline | One summary table instead of a dozen scattered lines; matches the project logging standard | Per-step logging |
| D7 | Explicit ordered list in `build_steps()` | N1 — two dependencies do not justify a resolver | Topological sort |
| D8 | `service` attribute for `only_service` filtering | G5 without a registry | Name matching |
| D9 | Lazy import of the whole subpackage | A configured install never pays the import cost of `arrapi`, OAuth libs, etc. | Eager import |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A step never calls `input()` or reads `os.environ` directly — always via `prompter`. |
| I2 | A step never writes `config.json`. |
| I3 | A step never imports another step. |
| I4 | A step's exception is caught and recorded; only `KeyboardInterrupt`/`EOFError` escape. |
| I5 | Every step returns at least one `StepResult`. |
| I6 | `library` and `routing` run after `arr`. |
| I7 | Steps assign into an already-complete `cfg`; they never create missing parent dicts. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Service unreachable | Validator probe fails | Failed `StepResult`, wizard continues | That service |
| OAuth device flow times out | [`../oauth.py`](../oauth.py) | Failed `StepResult` | Trakt or MAL |
| Required headless value missing | `EOFError` | Propagates → wizard aborts, env var name reported | Whole wizard |
| Operator cancels | `KeyboardInterrupt` | Propagates → no save | None |
| Step raises unexpectedly | Generic `except` | Warning + failed row | That domain |
| `ctx` key absent (arr skipped/failed) | Consumer must default | `library` falls back to manual entry | Extra typing |
| Step ordering violated | **None** | `library` sees an empty `ctx` and asks manually | Degraded UX, not incorrect |
| Two steps write the same config key | **None** | Last writer wins, silently | 🟡 Unguarded |

**Row 8 is unguarded.** Nothing prevents two steps from writing the same key, and
`extras.py` legitimately touches broad tunables that overlap other domains. A
key-ownership assertion would make that visible — §9 P3.

---

## 7. Configuration surface

Steps write; they read only through `prompter`. Domain → config block:

| Step | Writes |
|---|---|
| [`arr.py`](./arr.py) | `sonarr_instances`, `radarr_instances` |
| [`library.py`](./library.py) | `rootFolders` |
| [`media.py`](./media.py) | `plex`, `tautulli`, `tvdb` |
| [`routing.py`](./routing.py) | Routing / instance-selection keys |
| [`trakt.py`](./trakt.py) | `trakt` |
| [`mal.py`](./mal.py) | `mal` |
| [`mdblist.py`](./mdblist.py) | `mdblist` |
| [`nextep.py`](./nextep.py) | Next-episode planning keys |
| [`english_dub.py`](./english_dub.py) | English-dub preference keys |
| [`deletions.py`](./deletions.py) | Deletion policy, protected tags |
| [`daemons.py`](./daemons.py) | `daemons.enrich.enabled`, pilot-daemon keys |
| [`notifications.py`](./notifications.py) | `notifications.discord` |
| [`extras.py`](./extras.py) | `free_space_limit`, `dry_run`, `animeGenres`, `documentaryGenres`, scoring tunables |

`RECOMMENDARR_*` mapping lives in [`../env_map.py`](../env_map.py).

---

## 8. Implemented capabilities

- ✅ Uniform step contract with `StepResult` reporting
- ✅ Fourteen domain steps covering the full config surface
- ✅ `ctx`-based root-folder handoff from arr → library
- ✅ Per-step failure isolation
- ✅ `only_service` filtering for targeted reconfigure
- ✅ Interactive / headless parity through the prompter abstraction
- ✅ Live connectivity validation per service
- ✅ Trakt device-flow and MAL OAuth
- ✅ Custom-format sync during arr setup (tested)
- ✅ Plex pin/auth flow (tested)
- ✅ Lazy subpackage import

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Declared `depends_on`** per step, asserted by `build_steps()` | Makes §6 row 7 detectable instead of silently degrading | S | — |
| P2 | **Per-step re-run** (`--step trakt`) finer than `only_service` | Some services span several concerns | S | [`onboarding/`](../DESIGN.md) P2 |
| P3 | **Key-ownership assertion** — each step declares the keys it owns; overlaps fail loudly | Closes §6 row 8 | M | — |
| P4 | **Step-level dry run** — report what would be written | Inspection before commit | S | [`onboarding/`](../DESIGN.md) P7 |
| P5 | **`movieRootFolders` capture** in [`library.py`](./library.py) | The key is empty today, so movie classification buckets are discarded | S | Schema decision |
| P6 | **Schema-derived steps** — generate prompts from a typed schema | Adding a config key currently means editing a step | M | Typed schema |
| P7 | **Web renderers** for each step | Shares the P6 schema work with the web config editor | L | [`web/`](../web/DESIGN.md), P6 |
| P8 | **Idempotent re-run** — a step detects existing valid values and offers "keep" | Re-running currently re-asks everything | M | — |
| P9 | **Progress indicator** (`step 4/14`) | Fourteen steps with no sense of remaining effort | S | — |
| P10 | **Per-step validation summary** — what was probed and what it returned | Failures report *that* something failed, not what was tried | S | [`../validators.py`](../validators.py) |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should `extras.py` be split? It is a grab-bag spanning space, genres, dry-run and scoring — the widest overlap risk for P3. | P3 |
| Q2 | Should scoring tunables be in onboarding at all, given they inflate a first run and are better tuned against real data? | P7 |
| Q3 | Should a failed step block dependents (`arr` fails ⇒ skip `routing`), or let them degrade? | P1 |
| Q4 | Is `ctx` the right handoff, or should discovered data live in `cfg` and be read back? | — |

## 11. Related designs

- [`onboarding/DESIGN.md`](../DESIGN.md) — the wizard that drives these steps
- [`config/DESIGN.md`](../../config/DESIGN.md) — where the collected config lands
- [`web/DESIGN.md`](../../web/DESIGN.md) — P7
- [`base.py`](./base.py) — `StepResult` definition

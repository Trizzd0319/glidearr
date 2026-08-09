# web — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **web**

**Package** — `scripts.managers.factories.web`
**Status** — 🔵 Planned (no code yet — this document is the specification)
**Related** — [README.md](./README.md) · [`factories/DESIGN.md`](../DESIGN.md) · [`scripts/DESIGN.md`](../../../DESIGN.md)

---

## 1. Problem statement

Glidearr today has no operator surface. Every interaction is one of:

| Task | Today |
|---|---|
| Change a setting | Hand-edit `support/config/config.json`, restart |
| Review what a run would do | Read scrollback, or open the Parquet ledger in Python |
| Trigger a run | `python scripts/main.py` on the host |
| Understand why a title was deleted | Grep logs, cross-reference the ledger by hand |
| Check whether a daemon is alive | Inspect sentinel files / `Get-CimInstance` |
| Tune a threshold | Edit JSON, guess, run, compare |

This produces four concrete costs:

1. **The dry-run plan is the product, and it is unreadable.** Goal G3 in
   [`scripts/DESIGN.md`](../../../DESIGN.md) says a dry run must produce the
   complete plan. It does — into Parquet. Nobody reviews Parquet by eye, so the
   most valuable output of the system is effectively write-only.

2. **Config editing is hostile and unvalidated.** `config.json` mixes typed
   scalars, instance maps, nested threshold blocks and blank secret leaves. A
   typo'd key fails silently at first read, often many phases into a run.

3. **Decisions are opaque after the fact.** The ledger records *what* was decided
   and the signals behind it, but there is no way to ask "why was
   *The Fellowship of the Ring* downgraded?" without writing a query.

4. **Tuning is a blind loop.** Changing a weight means edit → full run → read logs
   → compare. There is no way to preview the effect of a threshold change against
   the existing ledger.

The web layer exists to close all four, and to do so **without becoming a second
place where policy lives.**

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | **Read-first.** Every view works before any write capability exists. |
| G2 | **Zero new policy.** The web layer renders and edits; it never scores, ranks, or decides. All logic stays in [`machine_learning/`](../../machine_learning/README.md). |
| G3 | **Schema-driven config UI.** Forms are generated from the onboarding schema, not hand-built — so a new config key produces a form field for free. |
| G4 | **Explainability.** Every decision row links to the signals that produced it. |
| G5 | **Safe by default.** Binds to localhost. Destructive actions require explicit confirmation and are gated identically to `dry_run`. |
| G6 | **Never blocks the run.** The web process must be independently startable, killable, and crashable without affecting `main.py`. |
| G7 | **No secret ever reaches the browser.** Secret leaves render as status only (`env` / `keyring` / `missing`), never as values. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Replacing Plex/Sonarr/Radarr UIs | They own their domains. Glidearr's UI is about *intent and rationale*. |
| N2 | Multi-user accounts, RBAC | Single operator. §9 P11 if that changes. |
| N3 | Internet exposure | Localhost bind. Remote access is the user's reverse-proxy problem, deliberately out of scope. |
| N4 | A mobile app | Responsive web is sufficient. |
| N5 | Re-implementing scoring in JS for live preview | Violates G2. Preview calls the Python brain. |
| N6 | Streaming media | Not a media server. |

---

## 3. Architecture

### 3.1 Where it sits

`web/` lives under `factories/` — **not** `services/` — for a specific reason:
it is infrastructure, and it must obey the factory-layer rule of holding no media
policy. It reads the same `global_cache`, `ConfigManager` and ledger every other
manager uses, and calls the brain for previews. It is a *view over* the system,
not a participant in it.

```
┌────────────────────────────────────────────────────────────┐
│  Browser (localhost:8787)                                  │
│  HTML + vanilla JS + SSE. No build step.                    │
└───────────────┬────────────────────────────────────────────┘
                │ HTTP / JSON / Server-Sent Events
┌───────────────▼────────────────────────────────────────────┐
│  WebManager(BaseManager)          app.py                   │
│    ├── routes/      thin HTTP handlers, no logic           │
│    ├── views/       read models — shape data for display    │
│    ├── forms/       schema → form, validate → config write │
│    ├── actions/     run trigger, daemon control            │
│    ├── stream/      SSE broadcast of run events            │
│    └── static/      css, js, templates                     │
└───┬─────────┬──────────┬──────────┬──────────┬─────────────┘
    │         │          │          │          │
    ▼         ▼          ▼          ▼          ▼
 Config   GlobalCache  Ledger   Registry   Brain (preview only,
 Manager  (snapshots)  (Parquet) (health)   pure calls)
```

### 3.2 Module layout

| Module | Role | Status |
|---|---|---|
| `__init__.py` | Exports `WebManager` | 🔵 Planned |
| `app.py` | `WebManager(BaseManager)` — server lifecycle, route registration, graceful shutdown | 🔵 Planned |
| `server.py` | HTTP server adapter; isolates the framework choice behind one seam | 🔵 Planned |
| `routes/dashboard.py` | Landing view: last run, health, space, pending plan | 🔵 Planned |
| `routes/config.py` | Config browse + edit | 🔵 Planned |
| `routes/plan.py` | Dry-run plan review, per-item rationale | 🔵 Planned |
| `routes/ledger.py` | Decision-ledger browse, filter, search | 🔵 Planned |
| `routes/library.py` | Library explorer with scores and tags | 🔵 Planned |
| `routes/runs.py` | Run history, trends, trigger | 🔵 Planned |
| `routes/health.py` | Service reachability, daemons, cache stats | 🔵 Planned |
| `routes/tuning.py` | Threshold sandbox — replay the ledger under altered weights | 🔵 Planned |
| `views/*.py` | Read models. Pure transforms: cache/ledger → display dicts | 🔵 Planned |
| `forms/schema_form.py` | Generates form descriptors from [`onboarding/schema.py`](../onboarding/schema.py) | 🔵 Planned |
| `forms/validate.py` | Reuses [`onboarding/validators.py`](../onboarding/validators.py) | 🔵 Planned |
| `actions/run_trigger.py` | Spawns a run as a detached subprocess | 🔵 Planned |
| `actions/daemon_control.py` | Start/stop/status via [`daemons/supervisor.py`](../daemons/supervisor.py) | 🔵 Planned |
| `stream/events.py` | SSE broadcaster fed by the event bus | 🔵 Planned |
| `auth.py` | Localhost gate + optional shared-secret token | 🔵 Planned |
| `static/` | CSS, JS, HTML templates | 🔵 Planned |

### 3.3 Control flow — the three interaction classes

**Read** (the majority, and all of Phase 1):
```
GET /plan → routes/plan → views/plan_view
              → ledger.read_latest()        (Parquet, read-only)
              → global_cache.get(...)       (snapshots)
           → render → HTML
```
No locks, no writes, safe during a live run.

**Write config**:
```
POST /config → forms/validate (reuses onboarding validators)
             → ConfigManager.set_bulk(...) → .save()
                  (atomic 0600, secrets stripped — factories/DESIGN.md D9)
             → RegistryConfigSync.load_config_and_propagate()   [if P8 landed]
```

**Action**:
```
POST /run  → actions/run_trigger
              → check no run in flight (MAIN_ACTIVE_SENTINEL)
              → subprocess.Popen(detached), record run id
              → SSE stream begins emitting
```
The web process **never runs the pipeline in-process** (G6). A crash in the UI
must not kill a half-finished run, and a long run must not block the UI thread.

### 3.4 Data contracts

| Source | Access | Payload |
|---|---|---|
| `ConfigManager` | read + write | Settings tree, secrets redacted to status only |
| `GlobalCacheManager` | **read-only** | Library snapshots, run stats, timestamps |
| Decision ledger (Parquet) | **read-only** | Decision rows + signal breakdown |
| `RegistryManager` | read-only | Live manager tree, flags, component health |
| [`onboarding/schema.py`](../onboarding/schema.py) | read-only | Field types, labels, validators → forms (G3) |
| Brain modules | pure calls | Threshold preview, score explanation |
| [`daemons/supervisor.py`](../daemons/supervisor.py) | control | Daemon start/stop/status |

**The web layer never writes to `global_cache` and never writes the ledger.**
That is the single rule that keeps it a view rather than a participant.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Under `factories/`, not `services/` | It is infrastructure with no media policy. Under `services/` it would inevitably accrete decisions (G2). | `services/web/` |
| D2 | Read-only Phase 1 | Ships value immediately at near-zero risk, and validates the read models before any write path exists (G1). | Full CRUD from day one |
| D3 | Runs are subprocesses, never in-process | G6. Also gives free isolation: an OOM in the run doesn't take the UI with it. | Threaded in-process run |
| D4 | Server-Sent Events, not WebSockets | One-directional server→client is the entire need. SSE is ~20 lines, auto-reconnects, and needs no extra dependency. | WebSockets |
| D5 | No frontend build step | A Python project with an npm toolchain has two dependency trees and two upgrade cadences, for a single-operator UI. | React/Vue + bundler |
| D6 | Forms generated from the onboarding schema | G3 — a new config key yields a form field with no UI work, and validation stays in one place | Hand-built forms |
| D7 | Framework behind `server.py` | The HTTP framework is the most likely thing to be swapped; isolating it keeps that a one-file change | Framework calls throughout |
| D8 | Localhost bind by default | G5. Remote access is a reverse-proxy concern the operator opts into. | Bind `0.0.0.0` |
| D9 | Secrets render as status, never value | G7. The browser is the least trustworthy place a secret can be. | Masked-but-present |
| D10 | Threshold preview calls the Python brain | G2 — a JS re-implementation would drift from the real model, which is exactly the failure this architecture exists to prevent | Client-side preview |
| D11 | Ledger read via a dedicated read model, not raw Parquet in routes | Keeps routes thin and gives one place to handle schema evolution | Query Parquet in handlers |
| D12 | `WebManager` subclasses `BaseManager` | Inherits logger/config/cache/registry uniformly; consistent with every other manager | Standalone module |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | The web layer contains **no scoring, ranking, or threshold logic**. |
| I2 | It **never writes** `global_cache` or the ledger. |
| I3 | No secret value is ever serialised into an HTTP response. |
| I4 | Runs execute as detached subprocesses, never in the web process. |
| I5 | Default bind is `127.0.0.1`. |
| I6 | Every destructive action requires an explicit confirm step. |
| I7 | The web process can crash or be killed with zero effect on an in-flight run. |
| I8 | Config writes go through `ConfigManager.save()` — atomic, `0600`, secrets stripped. |
| I9 | All read views tolerate a missing/empty ledger and a cold cache. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Port in use | Bind error at startup | Log and exit with the port in the message | Web only |
| Ledger absent (never run) | Empty read | Empty-state view with "run first" guidance | Plan/ledger views |
| Cache cold | Key miss | Render what exists; mark stale sections | Cosmetic |
| Run already in flight | `MAIN_ACTIVE_SENTINEL` present | Trigger refused with the running PID + start time | Prevents concurrent runs |
| Run subprocess dies | Exit code via poll | Marked failed; stderr tail surfaced | Web display only |
| Config write fails | `ConfigManager.save()` raises | Error surfaced, in-memory state untouched, form re-rendered with input preserved | No partial write (I8) |
| Invalid config submitted | Validators reject | Field-level errors, nothing persisted | None |
| SSE client disconnects | Broken pipe | Drop the subscriber, keep broadcasting | One client |
| Web process crashes mid-run | — | Run continues; UI reconnects and re-reads state from disk | None (I7) |
| Concurrent config edit + run | Both touch `config.json` | Run already loaded config at start; edit applies next run | Confusing, not corrupting — §10 Q3 |

---

## 7. Configuration surface

Proposed keys, namespaced under `web.`:

| Key | Type | Default | Effect |
|---|---|---|---|
| `web.enabled` | bool | `false` | Master gate |
| `web.host` | str | `"127.0.0.1"` | Bind address. Non-localhost logs a warning. |
| `web.port` | int | `8787` | Bind port |
| `web.auth.mode` | str | `"localhost"` | `localhost` \| `token` \| `none` |
| `web.auth.token` | secret | — | Shared secret when `mode="token"`. Stored via `SecretStore`, never rendered. |
| `web.readonly` | bool | `true` | Disables all write routes. **Default-on** until Phase 2 is proven. |
| `web.allow_run_trigger` | bool | `false` | Permits `POST /run` |
| `web.allow_config_edit` | bool | `false` | Permits config writes |
| `web.allow_daemon_control` | bool | `false` | Permits daemon start/stop |
| `web.ledger_page_size` | int | `100` | Ledger pagination |
| `web.sse_heartbeat_s` | int | `15` | SSE keepalive interval |

Note the deliberately conservative defaults: with `web.enabled=true` and nothing
else set, you get a **read-only** dashboard. Every write capability is opt-in
individually.

---

## 8. Implemented capabilities

None. This document is the specification; no code exists yet.

The following already-shipped pieces are the substrate it will build on:

- ✅ Decision ledger in Parquet ([`machine_learning/ledger/`](../../machine_learning/ledger/README.md))
- ✅ `PlanSummary` roll-up ([`machine_learning/plan_summary.py`](../../machine_learning/plan_summary.py))
- ✅ `ConfigManager` with atomic save + secret stripping ([`config/`](../config/README.md))
- ✅ Onboarding schema + validators ([`onboarding/`](../onboarding/README.md)) — the basis for G3
- ✅ Registry health + component status ([`registry/health.py`](../registry/health.py))
- ✅ Daemon supervisor + sentinels ([`daemons/`](../daemons/README.md))
- ✅ `run_stats` cache keys per service

## 9. Planned additions

Phased so each phase is independently shippable and useful.

### Phase 1 — Read-only (highest value, lowest risk)

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Dashboard** — last run, service health, disk space, pending plan size | One glance replaces reading scrollback | M | — |
| P2 | **Plan review** — the dry-run plan as a sortable, filterable table with per-item rationale | Makes the primary output of a dry run actually reviewable. **The single highest-value item in this document.** | M | Ledger read model |
| P3 | **Ledger browser** — filter by action, service, title, date; full signal breakdown per row | Answers "why was this deleted?" in seconds | M | P2 |
| P4 | **Library explorer** — every title with score, tags, quality, size, watch data | Makes the 100-point score visible and sanity-checkable | M | Cache read models |
| P5 | **Health view** — reachability, daemon liveness, cache size, last-updated timestamps | Replaces sentinel-file archaeology | S | `registry/health.py` |
| P6 | **Config viewer** (read-only) with secrets shown as status only | Safe way to confirm what is actually loaded | S | Schema |

### Phase 2 — Write

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P7 | **Config editor** — schema-generated forms, validated, atomic save | Removes the JSON-editing barrier entirely | L | P6, G3 |
| P8 | **Run trigger** with dry-run toggle and live SSE log | Run from anywhere on the LAN | M | P1 |
| P9 | **Daemon control** — start/stop/restart, status | Replaces manual process management | S | P5 |
| P10 | **Per-item overrides** — pin, protect, force-upgrade, exclude a title | Human judgement where the model is wrong, without editing tags by hand | M | P4 |

### Phase 3 — Analysis

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P11 | **Threshold sandbox** — replay the existing ledger under altered weights and diff the outcome | Turns blind tuning into a measured comparison | L | Brain preview API |
| P12 | **Run history + trends** — library health, space, score distribution over time | Detects drift | M | Run history store |
| P13 | **Score explainer** — per-title waterfall of every signal's contribution | Makes the model legible; likely to surface real scoring bugs | M | P4, brain breakdown |
| P14 | **Watchlist manager** — review Trakt watchlist, prune, see why an item persists | Surfaces the auto-prune decisions from P2 in `scripts/DESIGN.md` §9 | M | Trakt service |
| P15 | **Playlist preview** — render the generated playlists before writeback | Catches bad playlists pre-publish | M | Playlist brain |
| P16 | **Space simulator** — "if I add 2TB, what changes?" | Capacity planning | M | Space brain |

### Phase 4 — Platform

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P17 | **Auth beyond localhost** — sessions, or reverse-proxy header trust | Remote access | M | N2 revisited |
| P18 | **Mobile-responsive layout** | Approve plans from a phone | S | P1–P3 |
| P19 | **Webhook receiver** — Sonarr/Radarr/Plex events trigger targeted work | Event-driven rather than purely batch | L | Event bus |
| P20 | **Notification centre** — in-app alternative to Discord | Consolidates alerts | S | Event bus |
| P21 | **Export** — ledger/plan to CSV/JSON | Offline analysis | S | P3 |
| P22 | **Dark mode** | It is a media tool used at night | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Framework: stdlib `http.server` (zero deps, painful), Flask (familiar, +1 dep), or FastAPI (async, typed, +several deps)? Leaning **Flask** — the dependency cost is modest and the routing/templating ergonomics carry Phase 2. | P1 |
| Q2 | Does the web layer read Parquet directly, or through a read-only ledger service? Direct is simpler; a service seam protects against schema drift. | P2 |
| Q3 | How should a config edit during an in-flight run behave — refuse, queue, or apply-next-run with a banner? | P7 |
| Q4 | Where do per-item overrides (P10) persist — config, a dedicated Parquet table, or `*arr` tags? Tags are visible in Sonarr/Radarr, which argues for them. | P10 |
| Q5 | Should the run trigger honour `dry_run` from config, or always force an explicit per-invocation choice? Explicit seems safer. | P8 |
| Q6 | Is SSE sufficient for live log streaming, or is polling the ledger simpler given runs are minutes-long? | P8 |
| Q7 | Should the threshold sandbox write its experiments anywhere, or stay ephemeral? | P11 |
| Q8 | Does the web process need its own logger sink, or does it share the run log? Sharing risks interleaving. | P1 |

## 11. Related designs

- [`factories/DESIGN.md`](../DESIGN.md) — parent layer, esp. §9 P1 and P8/P9
- [`scripts/DESIGN.md`](../../../DESIGN.md) §9 P1, P7, P8
- [`scripts/DESIGN_auto_run_triggering.md`](../../../DESIGN_auto_run_triggering.md) — overlaps P8
- [`config/DESIGN_secrets_backend.md`](../config/DESIGN_secrets_backend.md) — constrains G7/D9
- [`onboarding/README.md`](../onboarding/README.md) — schema source for G3
- [`machine_learning/ledger/README.md`](../../machine_learning/ledger/README.md) — the ledger P2/P3 render

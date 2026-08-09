# web

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **web**

**Package** — `scripts.managers.factories.web`
**Run position** — Out of band. A separate process, independently startable, never part of `main.py`'s run path.
**One-liner** — The operator surface: a localhost web interface for reviewing dry-run plans, browsing the decision ledger, editing config and triggering runs — a view over the system, never a participant in it.

---

## Status

🔵 **Planned — no code yet.** [`DESIGN.md`](./DESIGN.md) is the full specification.

This folder is intentionally created ahead of implementation so the design is
reviewable and so the parent layer's docs have something real to link to.

---

## Purpose

Glidearr's most valuable output — the complete plan a dry run produces — is
currently written to Parquet and never read by a human. The web layer's core
job is to fix that, and four related gaps:

| Gap | Closed by |
|---|---|
| Dry-run plans are write-only | Plan review (Phase 1, P2) |
| Config editing is hand-edited JSON | Schema-generated forms (Phase 2, P7) |
| "Why was this deleted?" needs a Python query | Ledger browser (Phase 1, P3) |
| Tuning is edit → run → grep → guess | Threshold sandbox (Phase 3, P11) |
| Daemon liveness needs sentinel archaeology | Health view (Phase 1, P5) |

---

## The one rule

**This package holds no policy.**

It lives under [`factories/`](../README.md) rather than
[`services/`](../../services/README.md) specifically to bind it to the
factory-layer contract: infrastructure only, no media decisions. It renders what
the brain decided and lets you change the inputs. It never scores, ranks, or
picks.

A scoring helper appearing in this folder is a design violation — it would create
a second model, which is the exact failure
[`scripts/DESIGN.md`](../../../DESIGN.md) §1 goal G1 exists to prevent.

---

## Planned script inventory

Nothing here yet. Planned layout, per [`DESIGN.md`](./DESIGN.md) §3.2:

| Module | Role | Status |
|---|---|---|
| `__init__.py` | Exports `WebManager` | 🔵 Planned |
| `app.py` | `WebManager(BaseManager)` — lifecycle, routes, shutdown | 🔵 Planned |
| `server.py` | HTTP framework adapter (one swappable seam) | 🔵 Planned |
| `auth.py` | Localhost gate + optional shared-secret token | 🔵 Planned |
| `routes/` | Thin HTTP handlers — dashboard, config, plan, ledger, library, runs, health, tuning | 🔵 Planned |
| `views/` | Read models: cache/ledger → display dicts | 🔵 Planned |
| `forms/` | Schema → form generation; validation reused from onboarding | 🔵 Planned |
| `actions/` | Run trigger, daemon control | 🔵 Planned |
| `stream/` | Server-Sent Events broadcaster | 🔵 Planned |
| `static/` | CSS, JS, templates — no build step | 🔵 Planned |

---

## Planned entry points

| Symbol | Role |
|---|---|
| `WebManager` | `BaseManager` subclass owning the server lifecycle |
| `WebManager.serve()` | Blocking start |
| `python -m scripts.managers.factories.web` | Standalone launch |

---

## Data in / data out

| Direction | Source/Sink | Access | Payload |
|---|---|---|---|
| IN | Decision ledger (Parquet) | **read-only** | Decision rows + signals |
| IN | [`GlobalCacheManager`](../cache/README.md) | **read-only** | Snapshots, run stats, timestamps |
| IN | [`ConfigManager`](../config/README.md) | read | Settings (secrets as status only) |
| IN | [`RegistryManager`](../registry/README.md) | read-only | Live tree, flags, component health |
| IN | [`onboarding/schema.py`](../onboarding/schema.py) | read-only | Field types → generated forms |
| IN | Brain modules | pure calls | Threshold preview, score breakdown |
| OUT | `ConfigManager.save()` | write | Atomic `0600`, secrets stripped |
| OUT | Detached subprocess | spawn | A run, never in-process |
| OUT | [`daemons/supervisor.py`](../daemons/supervisor.py) | control | Daemon start/stop |
| OUT | Browser | HTTP + SSE | HTML, JSON, live events |

**Never writes** `global_cache` or the ledger. See [`DESIGN.md`](./DESIGN.md) §5 I2.

---

## Configuration

Defaults are deliberately conservative — `web.enabled=true` alone yields a
**read-only** dashboard, and every write capability is opt-in individually.

| Key | Default | Effect |
|---|---|---|
| `web.enabled` | `false` | Master gate |
| `web.host` | `"127.0.0.1"` | Bind address |
| `web.port` | `8787` | Bind port |
| `web.readonly` | `true` | Disables all write routes |
| `web.allow_run_trigger` | `false` | Permits `POST /run` |
| `web.allow_config_edit` | `false` | Permits config writes |
| `web.allow_daemon_control` | `false` | Permits daemon start/stop |

Full table: [`DESIGN.md`](./DESIGN.md) §7.

---

## Test coverage

None yet. Planned:

| Test | Covers |
|---|---|
| `test_readonly_enforcement.py` | Write routes refuse when `web.readonly=true` |
| `test_secret_redaction.py` | No secret value appears in any response (I3) |
| `test_run_trigger_concurrency.py` | Trigger refuses while `MAIN_ACTIVE_SENTINEL` exists |
| `test_schema_form_generation.py` | A new schema key yields a form field |
| `test_empty_state.py` | All views render against a cold cache and absent ledger |

---

## Navigation

- **Up:** [`factories/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Related:** [`config/`](../config/README.md) · [`onboarding/`](../onboarding/README.md) · [`daemons/`](../daemons/README.md) · [`machine_learning/ledger/`](../../machine_learning/ledger/README.md)

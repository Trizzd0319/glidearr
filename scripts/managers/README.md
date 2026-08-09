# managers

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **managers**

**Package** — `scripts.managers`
**Run position** — Everything below [`main.py`](../main.py). Constructed during `Main.__init__`, executed during `Main.run()`.
**One-liner** — The three architectural layers — factory (infrastructure), service (I/O adapters) and brain (pure decisions) — plus a thin orchestration helper.

---

## Purpose

`managers/` is where the layering rule from [`scripts/DESIGN.md`](../DESIGN.md) §3.1
is physically expressed. The folder split *is* the architecture:

| Folder | Layer | May do I/O? | May decide? |
|---|---|---|---|
| [`factories/`](./factories/README.md) | Infrastructure | Local only (config, cache, keyring) | No |
| [`services/`](./services/README.md) | Adapters | **Yes** — this is their job | No |
| [`machine_learning/`](./machine_learning/README.md) | Brain | **No** — enforced by [`hooks/brain_purity.py`](../hooks/brain_purity.py) | **Yes** |
| [`orchestration/`](./orchestration/README.md) | Glue | No | No |

The two directions that must never happen:

- A **service** that scores, ranks, or picks. That duplicates the model and breaks
  goal G1 (one scoring model).
- A **brain** module that fetches, writes, or caches. That makes the module
  unreplayable and breaks the [`eval/`](./machine_learning/eval/README.md) harness.

The second direction is machine-enforced at commit time. The first is not — it
relies on review. See [`DESIGN.md`](./DESIGN.md) §9.

---

## Subpackages

| Folder | Role | Docs |
|---|---|---|
| [`factories/`](./factories/README.md) | Logger, config, secrets, registry, cache, metrics, daemons, onboarding, mixins, base classes | [README](./factories/README.md) · [DESIGN](./factories/DESIGN.md) |
| [`services/`](./services/README.md) | Radarr, Sonarr, Plex, Tautulli, Trakt, MAL, MDBList, acquisition, coordinator, routing, writeback, calendar, backup | [README](./services/README.md) · [DESIGN](./services/DESIGN.md) |
| [`machine_learning/`](./machine_learning/README.md) | Scoring, likelihood, lifecycle, space, sizing, thresholds, discovery, playlists, eval, ledger | [README](./machine_learning/README.md) · [DESIGN](./machine_learning/DESIGN.md) |
| [`orchestration/`](./orchestration/README.md) | Cross-manager coordination helpers | [README](./orchestration/README.md) · [DESIGN](./orchestration/DESIGN.md) |

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`__init__.py`](./__init__.py) | — | Package marker (empty) | ✅ Implemented |

---

## The dependency rule

```
                    orchestration/
                          │  (may call across services)
                          ▼
   factories/  ◄──────  services/  ──────►  machine_learning/
   (injected                │                   (pure, called
    downward)               │                    with plain data)
                            ▼
                     external APIs
```

- `factories/` imports **nothing** from `services/` or `machine_learning/`.
- `services/` imports from `factories/` and calls into `machine_learning/`.
- `machine_learning/` imports from `factories/contracts` shapes only — never
  `services/`, never an HTTP client, never a `*_api` module.
- `orchestration/` is the only place allowed to coordinate two services.

---

## Navigation

- **Up:** [`scripts/`](../README.md)
- **Down:** [`factories/`](./factories/README.md) · [`services/`](./services/README.md) · [`machine_learning/`](./machine_learning/README.md) · [`orchestration/`](./orchestration/README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)

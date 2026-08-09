# routing

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **routing**

**Package** — `scripts.managers.machine_learning.routing`
**Run position** — **None.** Nothing here is implemented.
**One-liner** — The intended home for instance selection — which Sonarr/Radarr instance a title belongs to. Currently a migration placeholder with no implementation.

---

## Status: 🔵 Planned — a declared stub

[`instance_selector.py`](./instance_selector.py) contains a docstring, a
`PUBLIC API (to implement)` section, and:

```python
# TODO(ml-migration): move the decision core(s) listed above here.
# Until migrated, importers should keep calling the existing service
# method (which will be shimmed to delegate here per MIGRATION.md).
```

No function bodies. [`__init__.py`](./__init__.py) is a docstring only.

This is an **honest** stub — it declares its intent, its public API, its
dependencies and its migration status. That is materially better than an empty
file. But it means the decision logic it is meant to own is currently living in
the service layer. See [`DESIGN.md`](./DESIGN.md) §3.2.

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`instance_selector.py`](./instance_selector.py) | `select_instance(item, instances, config) -> str` — **declared, not implemented** | 🔵 Planned |
| [`__init__.py`](./__init__.py) | Package docstring only | ✅ Implemented |

No tests.

---

## What it is meant to own

From [`ARCHITECTURE.md`](../ARCHITECTURE.md)'s subpackage map:

| Package | Owns | Source it absorbs |
|---|---|---|
| `routing/` | instance selection (which Sonarr/Radarr instance) | `machine_learning/instance_selector.py` (decision half) |

⚠️ **That stated source does not exist.** There is no `instance_selector.py` at
the `machine_learning/` package root. See [`DESIGN.md`](./DESIGN.md) §3.3.

---

## The declared contract

Per the stub's own docstring:

```
PUBLIC API (to implement):
  - select_instance(item, instances, config) -> str

DEPENDS ON: contracts
SERVICE REMAINDER (stays in the service as the thin adapter):
  Service keeps instance config FETCH + apply.
```

Which matches [`ARCHITECTURE.md`](../ARCHITECTURE.md) rule 2 — a brain entrypoint
consumes feature rows and config, and emits plain data a service adapter applies.

---

## Where instance selection actually happens today

| Concern | Live location |
|---|---|
| Categorised instance lookup | `acquisition/gateway.py::categorized_instance` |
| Anime-movie routing | [`services/acquisition/resolver.py`](../../services/acquisition/README.md) |
| Resolution-tier routing for new adds | [`support/tools/router_movie.py`](../../../support/tools/router_movie.py) — post-landing |
| Per-instance profile resolution | `services/radarr/quality/selector.py` |

All four are in the **service** layer. See
[`radarr/DESIGN.md`](../../services/radarr/DESIGN.md) §3.3 for the full state of
that subsystem.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Contract:** [`ARCHITECTURE.md`](../ARCHITECTURE.md) · [`MIGRATION.md`](../MIGRATION.md)
- **Live implementation:** [`services/radarr/DESIGN.md`](../../services/radarr/DESIGN.md) §3.3

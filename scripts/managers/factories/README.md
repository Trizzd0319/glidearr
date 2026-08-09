# factories

> Breadcrumb: [glidearr](../../..) › [scripts](../../README.md) › [managers](../README.md) › **factories**

**Package** — `scripts.managers.factories`
**Run position** — **First.** Built during `Main.__init__` before any service manager exists. [`onboarding/`](./onboarding/README.md) runs even earlier — before `ConfigManager`.
**One-liner** — The infrastructure layer: base classes, registry, config + secrets, cache, metrics, mixins, daemons and onboarding. Knows nothing about media.

---

## Purpose

Everything in `factories/` is media-agnostic plumbing. If a module here mentions
a movie, a series, or a quality tier, it is in the wrong folder.

The layer supplies six things every service manager depends on:

| Concern | Provided by |
|---|---|
| Identity + DI + parent linking | [`base_manager.py`](./base_manager.py) |
| Multi-instance handling | [`base_instance_manager.py`](./base_instance_manager.py) |
| Service directory + flags + health | [`registry/`](./registry/README.md) |
| Settings + secrets | [`config/`](./config/README.md) |
| Persistence (JSON / Parquet / memory) | [`cache/`](./cache/README.md) |
| Declarative subcomponent loading | [`mixins/`](./mixins/README.md) |

Plus two lifecycle concerns that sit outside the run: [`onboarding/`](./onboarding/README.md)
(first-run setup) and [`daemons/`](./daemons/README.md) (background workers).

---

## Bootstrap order

This order is load-bearing. See [`main.md`](../../main.md).

```
0. OnboardingManager.run_if_needed()   ← BEFORE ConfigManager, so
                                          SecretBootstrap sees a provisioned
                                          keyring and never double-prompts
1. LoggerManager                       ← support/utilities/logger
2. ConfigManager  → .reload()          ← config/
3. RegistryManager                     ← registry/
4. GlobalCacheManager                  ← cache/
5. MetricsLogger                       ← metrics.py
6. BaseManager.__init__                ← base_manager.py; parent auto-link
```

---

## Script inventory

| Script | Doc | Role | Status |
|---|---|---|---|
| [`base_manager.py`](./base_manager.py) | [`base_manager.md`](./base_manager.md) | Singleton base class: DI, registry self-registration, parent auto-linking, `prepare()`/`run()` contract | ✅ Implemented |
| [`base_instance_manager.py`](./base_instance_manager.py) | [`base_instance_manager.md`](./base_instance_manager.md) | Base for multi-instance services (Radarr `standard`, Sonarr `720`); `prepare()` is a deliberate no-op | ✅ Implemented |
| [`metrics.py`](./metrics.py) | — | `MetricsLogger` — run-level counters and timings | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | Package marker | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_base_manager_parent_link.py`](./test_base_manager_parent_link.py) | Parent auto-linking and deferred resolution |
| [`test_disk_free_failopen.py`](./test_disk_free_failopen.py) | Free-space check fails **open**, never blocking a run |

---

## Subpackages

| Folder | Role | Docs |
|---|---|---|
| [`cache/`](./cache/README.md) | `GlobalCacheManager` + JSON / Parquet / timestamp / memory / audit / diff / compression handlers | [README](./cache/README.md) · [DESIGN](./cache/DESIGN.md) |
| [`config/`](./config/README.md) | `ConfigManager`, loader, resolver, sanitizer, validator, secret store + bootstrap, CLI | [README](./config/README.md) · [DESIGN](./config/DESIGN.md) |
| [`daemons/`](./daemons/README.md) | Supervisor, daemon paths/sentinels, pilot jobs, bucket merge | [README](./daemons/README.md) · [DESIGN](./daemons/DESIGN.md) |
| [`mixins/`](./mixins/README.md) | `ComponentManagerMixin`, ordered components, queue cancel | [README](./mixins/README.md) · [DESIGN](./mixins/DESIGN.md) |
| [`onboarding/`](./onboarding/README.md) | First-run wizard: schema, env map, OAuth, prompts, validators, per-domain steps | [README](./onboarding/README.md) · [DESIGN](./onboarding/DESIGN.md) |
| [`registry/`](./registry/README.md) | `RegistryManager` = core + trace + CLI + injection + health + config-sync | [README](./registry/README.md) · [DESIGN](./registry/DESIGN.md) |
| `web/` *(🔵 planned)* | Web interface for config, plan review, run triggering, ledger browsing | [DESIGN](./web/DESIGN.md) |

---

## Entry points

| Symbol | From | Used by |
|---|---|---|
| `BaseManager` | [`base_manager.py`](./base_manager.py) | Every manager in the tree |
| `BaseInstanceManager` | [`base_instance_manager.py`](./base_instance_manager.py) | Radarr / Sonarr instance managers |
| `ConfigManager` | [`config/`](./config/README.md) | `Main`, then injected everywhere |
| `RegistryManager` / `get_registry()` | [`registry/`](./registry/README.md) | `BaseManager`, `ComponentManagerMixin` |
| `GlobalCacheManager` | [`cache/`](./cache/README.md) | Every service manager |
| `MetricsLogger` | [`metrics.py`](./metrics.py) | `Main` |
| `OnboardingManager.run_if_needed()` | [`onboarding/`](./onboarding/README.md) | `main.py` `__main__` block |
| `EnrichDaemonSupervisor` | [`daemons/supervisor.py`](./daemons/supervisor.py) | `main.py` `__main__` block |

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | `support/config/config.json` | Settings tree |
| IN | OS keyring / `RECOMMENDARR_*` env | Secret leaves |
| IN | `support/cache/**` | JSON + Parquet snapshots |
| OUT | `support/config/config.json` | Atomic `0600` write, secrets stripped |
| OUT | OS keyring | Secret leaves |
| OUT | `support/cache/**` | Snapshots, timestamps, run stats |
| OUT | stdout / log files | One summary line per manager |

**No external HTTP.** The only network access originating in this layer is
[`onboarding/oauth.py`](./onboarding/oauth.py) during first-run setup.

---

## Navigation

- **Up:** [`managers/`](../README.md)
- **Down:** [`cache/`](./cache/README.md) · [`config/`](./config/README.md) · [`daemons/`](./daemons/README.md) · [`mixins/`](./mixins/README.md) · [`onboarding/`](./onboarding/README.md) · [`registry/`](./registry/README.md) · [`web/`](./web/DESIGN.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)

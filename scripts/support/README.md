# support

> Breadcrumb: [glidearr](../..) › [scripts](../README.md) › **support**

**Package** — `scripts.support`
**Run position** — Mixed. [`utilities/`](./utilities/README.md) is imported throughout the run; [`daemons/`](./daemons/README.md) run detached; [`tools/`](./tools/README.md) are operator-invoked and never in the automated path.
**One-liner** — Everything that isn't a manager: shared utilities, operator tools, background daemon bodies, notifications, on-disk config and profile data, and the cache/log roots.

---

## Purpose

`support/` is the catch-all, and that is deliberate rather than sloppy — it holds
four genuinely different kinds of thing that share only the property of not being
part of the manager tree:

| Kind | Folder | Run path? |
|---|---|---|
| **Shared code** | [`utilities/`](./utilities/README.md) | ✅ Imported everywhere |
| **Daemon bodies** | [`daemons/`](./daemons/README.md) | ✅ Detached processes |
| **Operator tools** | [`tools/`](./tools/README.md) | ❌ Manual invocation only |
| **Setup scripts** | [`setup/`](./setup/README.md) | ❌ First-run / maintenance |
| **Notifications** | [`notifications/`](./notifications/README.md) | ✅ End of run |
| **Data** | `config/` · `profiles/` · `cache/` · `logs/` · `assets/` | ✅ Read/written |
| **Reference** | [`knowledge/`](./knowledge/README.md) | ❌ Human docs |

---

## Subpackages

| Folder | Role | Docs |
|---|---|---|
| [`utilities/`](./utilities/README.md) | Logger, bootstrap, auth validator, decorators, progress, registry helpers, string normalisation, migration shims | [README](./utilities/README.md) · [DESIGN](./utilities/DESIGN.md) |
| [`tools/`](./tools/README.md) | ~60 operator scripts: profile build/export, ML train/eval, cache reset, routers, audits, one-off repairs | [README](./tools/README.md) · [DESIGN](./tools/DESIGN.md) |
| [`daemons/`](./daemons/README.md) | `enrich_daemon.py`, `pilot_search_daemon.py` — the bodies the factory supervisor spawns | [README](./daemons/README.md) · [DESIGN](./daemons/DESIGN.md) |
| [`setup/`](./setup/README.md) | `onboarding.py`, `setup_secrets.py`, `migrate_secrets.py`, `install_hooks.py` | [README](./setup/README.md) · [DESIGN](./setup/DESIGN.md) |
| [`notifications/`](./notifications/README.md) | Discord notifier, run-summary collector | [README](./notifications/README.md) · [DESIGN](./notifications/DESIGN.md) |
| [`knowledge/`](./knowledge/README.md) | Human reference notes on playlists and transcoding | — |
| `config/` | `config.json`, `default_config.json`, backups, cache keys, reference keys | — |
| `profiles/` | TRaSH guide data, per-instance profile snapshots, device codec matrix | — |
| `cache/` | 🚫 Runtime cache root. Not source. | — |
| `logs/` | 🚫 Runtime log root. Not source. | — |
| `assets/` | Static assets | — |

---

## Script inventory

| Script | Role | Status |
|---|---|---|
| [`plex_stresstest.py`](./plex_stresstest.py) | Plex load/stress harness | 🧪 Tooling |
| [`safe_cache_clear.py`](./safe_cache_clear.py) | Guarded cache wipe | 🧪 Tooling |
| [`cache_cleanup.ps1`](./cache_cleanup.ps1) | PowerShell cache cleanup | 🧪 Tooling |
| [`manager_process_flow_template.txt`](./manager_process_flow_template.txt) | The reusable prompt template the manager architecture was designed against | 📄 Reference |
| [`PERF_BASELINE.md`](./PERF_BASELINE.md) | Performance baseline measurements | 📄 Reference |
| [`__init__.py`](./__init__.py) | Package marker | ✅ Implemented |

---

## The migration shims

Four modules in [`utilities/`](./utilities/README.md) are **re-export shims**, not
implementations. Each moved to the brain layer and left a compatibility import
behind, scheduled for deletion at `MIGRATION.md` Step 10:

| Shim | Real implementation | Step |
|---|---|---|
| [`utilities/size_model.py`](./utilities/size_model.py) | `machine_learning/sizing/size_model.py` | 1 |
| [`utilities/watch_likelihood.py`](./utilities/watch_likelihood.py) | `machine_learning/likelihood/watch_likelihood.py` | 4 |
| [`utilities/library_classifier.py`](./utilities/library_classifier.py) | `machine_learning/classification/library_classifier.py` | 5a |
| [`utilities/space_targets.py`](./utilities/space_targets.py) | `machine_learning/space/space_targets.py` | 7a |

They re-export the same function objects, so shared state (e.g. `size_model`'s
calibration overlay) stays consistent regardless of import path. **Do not treat
these as duplicates** — see [`ENHANCEMENTS.md`](../ENHANCEMENTS.md) §8 P-E.

⚠️ **Deletion constraint:** [`utilities/library_classifier.py`](./utilities/library_classifier.py)
has a dual-import fallback because [`tools/router_show.py`](./tools/router_show.py)
and [`tools/router_movie.py`](./tools/router_movie.py) run standalone with only
`scripts/` on `sys.path`. Deleting the shim breaks both unless they are updated first.

---

## Data in / data out

| Direction | Path | Payload |
|---|---|---|
| IN/OUT | `config/config.json` | Settings, secrets stripped |
| IN | `config/default_config.json` | Blank template |
| IN | `profiles/trash/` | TRaSH guide custom formats + quality profiles |
| IN/OUT | `profiles/{radarr,sonarr}/<instance>/` | Profile snapshots, `_pre_apply_snapshot` |
| OUT | `cache/**` | Every runtime cache |
| OUT | `logs/**` | Run log, daemon logs |
| OUT | Discord | Run summary |

---

## Navigation

- **Up:** [`scripts/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Down:** [`utilities/`](./utilities/README.md) · [`tools/`](./tools/README.md) · [`daemons/`](./daemons/README.md) · [`setup/`](./setup/README.md) · [`notifications/`](./notifications/README.md)
- **Backlog:** [`ENHANCEMENTS.md`](../ENHANCEMENTS.md)

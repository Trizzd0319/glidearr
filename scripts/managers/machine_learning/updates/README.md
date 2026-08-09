# updates

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **updates**

**Package** — `scripts.managers.machine_learning.updates` *(not a package — no `__init__.py`)*
**Run position** — **None.** Two standalone CLI scripts.
**One-liner** — Pre-migration relics: a Tautulli dataset builder and a name-keyed feature aggregator, both superseded by `labels/`, `affinity/` and `people_matrix/`.

---

## ⚠️ Status: legacy, recommended for deletion

Every structural marker separates this folder from the rest of the brain:

| | Rest of `machine_learning/` | `updates/` |
|---|---|---|
| `__init__.py` | ✅ every subpackage | ❌ **absent** |
| Module docstring | ✅ extensive, often 20–80 lines | ❌ **none** |
| Tests | ✅ every subpackage | ❌ **none** |
| I/O | ❌ pure by contract | ✅ `open()`, `os.makedirs`, `json.dump`, `df.to_csv` |
| Logging | via injected logger | `print()` |
| Entry point | called by a manager | `if __name__ == "__main__":` with hardcoded relative paths |

See [`DESIGN.md`](./DESIGN.md) for what supersedes each, and for the three
registered findings this folder confirms.

---

## Script inventory

| Script | Size | What it does | Superseded by |
|---|---|---|---|
| [`dataset_builder.py`](./dataset_builder.py) | 2.8 KB | Joins Tautulli history + metadata into a flat JSON dataset | [`labels/snapshots.py`](../labels/README.md) + [`labels/labeling.py`](../labels/README.md) |
| [`feature_aggregator.py`](./feature_aggregator.py) | 2.9 KB | Per-user raw counts of genres/actors/directors/etc → CSV | [`affinity/genre_affinity.py`](../affinity/README.md) + [`people_matrix/`](../people_matrix/README.md) |

---

## Why they are stale, not just old

Both scripts read fields and paths that no longer exist:

| Reference | Reality |
|---|---|
| `entry.get("started")` | [`labels/labeling.py`](../labels/README.md) documents `date` as *"**the only timestamp field present; there is no `started`**"* |
| `cache/tautulli/watch_history/watch_history_default.json` | The live path is `tautulli/history/all.json` |
| `cache/tautulli/metadata_libraries/metadata_libraries_default.json` | No such cache in the current layout |

And both use approaches the brain has since explicitly rejected:

- **Name-keyed people** (`f"actor_{actor}"`) — [`people_matrix`](../people_matrix/README.md) states a name-keyed graph *"could not feed [C4] at all."*
- **Raw counts, no decay or weighting** — [`affinity/`](../affinity/README.md) has temporal decay; [`people_matrix/`](../people_matrix/README.md) has role weights and billing decay.
- **`watched_status` captured untresholded** — the exact bug [`lifecycle/watched_definition.py`](../lifecycle/README.md) exists to fix.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Successors:** [`labels/`](../labels/README.md) · [`affinity/`](../affinity/README.md) · [`people_matrix/`](../people_matrix/README.md)

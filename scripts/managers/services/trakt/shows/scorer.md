# `trakt/shows/scorer.py` (re-export shim — no manager class)

**File** — `scripts/managers/services/trakt/shows/scorer.py`
**One-liner** — A backward-compatibility re-export shim that forwards the old `trakt.shows.scorer` import path to the TV watchability engine now living in the machine_learning brain layer.

> This file defines **no `*Manager` class**. It is documented here only because it sits directly in the in-scope directory and because the procedure asks that delegations into `machine_learning/` be *noted* (never documented). The brain module it forwards to is out of scope.

## What it does (for a senior Python engineer)

The TV scoring engine (`score_show`, the 0–100 watchability scorer with its Group A–G structure and critic boost) was moved to `scripts.managers.machine_learning.scoring.show_scorer` during ML-migration Step 2. To avoid breaking existing imports — chiefly `from scripts.managers.services.trakt.shows.scorer import score_show` used by the Sonarr `episode_files` cache manager — this module re-exports the brain symbols:

```python
from scripts.managers.machine_learning.scoring.show_scorer import *
from scripts.managers.machine_learning.scoring.show_scorer import (
    QUALITY_PROFILE_THRESHOLDS,
    score_show,
    score_to_profile,
    score_to_sonarr_profile_id,
)
```

FETCH / CACHE / APPLY: none — it is a name forwarder. No config keys, no `global_cache` / Parquet keys, no API endpoints, no `dry_run` behavior, no singleton/threading concerns. The module docstring notes it is scheduled for deletion at `MIGRATION.md` Step 10.

## How it functions

Import-time only: the two `import` lines run when the module is first loaded, binding `score_show`, `score_to_profile`, `score_to_sonarr_profile_id`, `QUALITY_PROFILE_THRESHOLDS` (and whatever `*` exports) into this module's namespace. There is no class, no `__init__`, no `load_components`, and no runtime control flow.

**Delegation note:** all behavior is delegated to the brain module `scripts.managers.machine_learning.scoring.show_scorer`. Per scope, that module is intentionally not documented here.

## Criteria & examples

No thresholds or guards are defined in this file — they are re-exported. For example `QUALITY_PROFILE_THRESHOLDS` is a constant that originates in the brain module; the shim merely makes the name importable from the old path. Concrete scoring rules live in the brain and are out of scope.

## In plain English

Imagine a TV show's "how much is this worth keeping?" rating used to be calculated in this room, but the calculator got moved to a different room (the "brain"). Rather than make everyone learn the new address, this file is a forwarding sticky-note on the old door: knock here and you're automatically sent to the right place. Eventually the old door will be removed entirely, but for now nothing that used to work breaks.

## Interactions

- **Forwards to:** `scripts.managers.machine_learning.scoring.show_scorer` (brain — not documented here).
- **Primary caller:** `scripts/managers/services/sonarr/cache/episode_files.py`, which imports `score_show` through this path to compute per-series watchability scores.
- **Sibling:** `cache.py` (`TraktShowCacheManager`) supplies the people / ratings / related data the scorer consumes.

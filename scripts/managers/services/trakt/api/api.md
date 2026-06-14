# api.py (deprecated shim)

- **File** — `scripts/managers/services/trakt/api/api.py`
- **One-liner** — A deprecated import-compatibility shim that re-exports `TraktAPIManager` under the legacy name `TraktAPI`; it defines no manager class of its own.

## What it does (for a senior Python engineer)

This module contains no class definition. It exists solely for backward import compatibility: the Trakt API layer was consolidated into `TraktAPIManager` (in `scripts/managers/services/trakt/api/__init__.py`), and this file re-exports that class under the old alias so legacy imports such as `from scripts.managers.services.trakt.api.api import TraktAPI` continue to resolve.

The full body is:

```python
from scripts.managers.services.trakt.api import TraktAPIManager as TraktAPI  # noqa: F401

__all__ = ["TraktAPI"]
```

No FETCH / CACHE / APPLY behavior, no config keys, no cache keys, no API endpoints, no dry_run logic — it is a pure aliasing re-export. All real behavior lives in `TraktAPIManager`; see `README.md` in this directory.

## How it functions

Importing the module triggers the single re-export line. `TraktAPI` is bound to the `TraktAPIManager` symbol. There is no lifecycle, no `__init__`, and no `load_components`.

## Criteria & examples

None — there are no thresholds, guards, or selection rules. Example: existing code that did `TraktAPI(...)` now constructs a `TraktAPIManager` instance unchanged.

## In plain English

This is just a forwarding address. The "Trakt API" department moved into a new office (`TraktAPIManager`), and this slip of paper makes sure any old mail addressed to the previous name still gets delivered to the new one. It does no work itself.

## Interactions

- Re-exports `TraktAPIManager` from `scripts/managers/services/trakt/api/__init__.py`. Nothing else references it functionally; it is retained only so older import paths keep working.

"""machine_learning — the decision/brain layer.

Services SENSE and ACT (fetch / cache / apply); this layer THINKS. Every
value judgement (watchability, earned quality tier, delete/downgrade/upgrade,
protection, grace, next-episode, size, library routing) lives here, organised
BY CONCERN, not by service. See ARCHITECTURE.md for the boundary contract and
MIGRATION.md for the incremental, test-gated extraction plan.

Hard rule: nothing under this package may import requests / *_api / _make_request
or write to global_cache. Brain entrypoints are pure: plan(features, ctx, config).
"""

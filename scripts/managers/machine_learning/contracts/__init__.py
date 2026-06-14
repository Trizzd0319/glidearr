"""contracts — the typed service<->ML boundary.

Plain, frozen dataclasses flowing in both directions: feature rows (service ->
ML) and plans/decisions (ML -> service). No logic lives here — only shapes.
Re-exports the common types for convenience.
"""

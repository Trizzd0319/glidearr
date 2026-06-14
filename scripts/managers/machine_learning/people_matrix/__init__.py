"""people_matrix — pure person↔media co-occurrence graph for watchability.

THINKS only (brain layer): a service manager reads the daemon people buckets and
passes decoded credits dicts to :func:`build_index`; the scorer / candidate layers
read the resulting inverted index + forward map. See ``build.py`` and the design doc
``machine_learning/DESIGN_people_matrix.md``.
"""
from __future__ import annotations

from scripts.managers.machine_learning.people_matrix.build import (
    PERSON_ROLE_WEIGHTS,
    ROLES,
    build_index,
    co_occurring,
    deserialize_forward,
    films_with_all,
    invert_forward,
    route_people,
    serialize_forward,
)

__all__ = [
    "PERSON_ROLE_WEIGHTS", "ROLES", "build_index", "co_occurring",
    "deserialize_forward", "films_with_all", "invert_forward", "route_people",
    "serialize_forward",
]

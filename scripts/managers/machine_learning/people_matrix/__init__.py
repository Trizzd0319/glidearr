"""people_matrix — pure person↔media co-occurrence graph for watchability.

THINKS only (brain layer): a service manager reads the daemon people buckets and
passes decoded credits dicts to :func:`build_index`; the scorer / candidate layers
read the resulting inverted index + forward map. See ``build.py`` and the design doc
``machine_learning/DESIGN_people_matrix.md``.
"""
from __future__ import annotations

from scripts.managers.machine_learning.people_matrix.build import (
    BILLED_ROLES,
    PERSON_BILLING_DECAY,
    PERSON_ROLE_WEIGHTS,
    RELATION_ROLE_TYPES,
    ROLES,
    billing_weight,
    build_index,
    co_occurring,
    deserialize_forward,
    deserialize_names,
    films_with_all,
    forward_from_relations,
    invert_forward,
    merge_forward,
    route_people,
    route_people_names,
    serialize_forward,
    serialize_names,
)

__all__ = [
    "BILLED_ROLES", "PERSON_BILLING_DECAY", "PERSON_ROLE_WEIGHTS",
    "RELATION_ROLE_TYPES", "ROLES", "billing_weight", "build_index", "co_occurring",
    "deserialize_forward", "deserialize_names", "films_with_all",
    "forward_from_relations", "invert_forward", "merge_forward",
    "route_people", "route_people_names", "serialize_forward", "serialize_names",
]

"""The manager-inheritance contract holds across the WHOLE manager layer: every BaseManager subclass
forwards logger/config/global_cache/validator/registry + **kwargs to super() and builds its children
with that same context (canonical cache attr is global_cache, never cache). audit_manager_pattern is a
deterministic AST check; this asserts it stays at zero so a regression — a manager that drops the shared
cache/registry and makes BaseManager spin a fresh ConfigManager/RegistryManager — fails the suite."""
from __future__ import annotations

from scripts.support.tools.audit_manager_pattern import find_violations


def test_all_managers_forward_full_framework_context():
    violations = find_violations()
    assert not violations, (
        f"{len(violations)} manager-contract violation(s) — see "
        "scripts/support/tools/audit_manager_pattern.py:\n"
        + "\n".join("  " + str(v) for v in violations)
    )

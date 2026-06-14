"""
Stable dependency ordering for ``component_dependencies`` maps.

Service managers (Sonarr, Radarr, …) declare a ``component_dependencies`` dict of
``{component_name: [dependency_names]}`` and historically relied on the dict's
INSERTION ORDER to decide the order in which components are prepared and run.
That is fragile: alphabetising the dict, or moving an entry while refactoring,
silently changes execution order with nothing to catch it.

``topo_order`` makes the order explicit and self-correcting. It is a STABLE
topological sort — it scans the declared components in their existing order and
emits each one as soon as its declared dependencies have all been emitted:

  * if the insertion order is already a valid topological order (it is today for
    both *arr managers), the output is byte-identical to the insertion order, so
    wiring it in is a pure, behaviour-preserving drop-in;
  * if a later edit reorders the dict into something that violates a declared
    dependency, the sort quietly corrects it instead of running a component
    before its dependency;
  * a genuine dependency CYCLE raises ``ValueError`` loudly rather than looping.

Dependencies that are not themselves keys in the map (e.g. ``"manager"`` or
``"cache"``, which are built eagerly elsewhere) are treated as already-satisfied
and ignored, so existing maps pass through unchanged.
"""
from __future__ import annotations


def topo_order(dependencies: dict[str, list[str]]) -> list[str]:
    """Return the component names in a stable, dependency-respecting order.

    The order equals the dict's insertion order whenever that order is already
    valid; otherwise it is corrected to honour every intra-map dependency.
    Raises ``ValueError`` if the dependencies contain a cycle.
    """
    declared = list(dependencies)          # insertion order
    known = set(declared)                  # only intra-map names gate ordering
    emitted: list[str] = []
    seen: set[str] = set()

    remaining = declared
    while remaining:
        progressed = False
        for name in remaining:             # scan in declared order
            deps = dependencies.get(name) or []
            if all((dep not in known) or (dep in seen) for dep in deps):
                emitted.append(name)
                seen.add(name)
                progressed = True
        if not progressed:                 # nothing emittable → cycle
            stuck = ", ".join(n for n in remaining if n not in seen)
            raise ValueError(
                f"component_dependencies has a dependency cycle "
                f"(cannot order: {stuck})"
            )
        remaining = [n for n in remaining if n not in seen]
    return emitted

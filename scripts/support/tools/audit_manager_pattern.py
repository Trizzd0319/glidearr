"""audit_manager_pattern.py — deterministic AST guard for the manager-inheritance contract.

Every ``BaseManager`` subclass must forward the full framework context — ``logger, config,
global_cache, validator, registry`` plus ``**kwargs`` — to ``super().__init__``, and must build its
own child ``BaseManager`` subclasses with that same context. When it doesn't, ``BaseManager`` spins
up a FRESH ``ConfigManager``/``RegistryManager`` and the shared cache/registry is lost for that whole
subtree (the class of bug behind the 2026-06 "Config file not found" errors and per-instance cache
misses). The canonical shared-cache attribute is ``global_cache`` — never ``cache``.

This walks the syntax tree of every ``scripts/managers/**/*.py`` (no imports, no live calls, no LLM)
and reports ONLY real violations:

  • D1  super().__init__ DROPS a captured ``global_cache``/``validator``/``registry`` param
  • D7  super().__init__ does not forward ``**kwargs`` (while the signature captures it)
  • D5  a ``BaseManager`` subclass ``__init__`` with no ``super().__init__`` call at all
  • D3  a child ``BaseManager`` subclass built without ``global_cache``/``registry``
  • D6  the shared cache passed/read under ``cache`` instead of ``global_cache``
  • D4  a fresh ``ConfigManager()``/``RegistryManager()``/``GlobalCacheManager()`` inside a manager

It is false-positive-aware: params that flow through ``**kwargs``, non-``BaseManager`` utility/handler
classes, third-party clients whose own keyword is literally ``cache=``, child constructions that
splat ``**init_kwargs``, and ``BaseManager``'s own ``config or ConfigManager()`` fallback + the registry
factory are all recognised and NOT flagged.

Usage:
    python -m scripts.support.tools.audit_manager_pattern        # prints findings, exits 1 if any
``test_manager_pattern_contract.py`` calls ``find_violations()`` and asserts it is empty, so a manager
that drops context fails CI.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import NamedTuple, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANAGERS_DIR = _REPO_ROOT / "scripts" / "managers"

# BaseManager.__init__ positional order, and the three the subtree breaks without.
_BASE_PARAMS = ("logger", "config", "global_cache", "validator", "registry")
_REQUIRED = {"global_cache", "validator", "registry"}
_FRAMEWORK_CTORS = {"ConfigManager", "RegistryManager", "GlobalCacheManager"}
# files where instantiating a framework singleton IS legitimate (the base fallback + the factory)
_FRESH_OK = {"factories/base_manager.py", "factories/registry/__init__.py"}


class Violation(NamedTuple):
    code: str
    file: str          # path relative to scripts/managers
    line: int
    detail: str

    def __str__(self) -> str:
        return f"[{self.code}] managers/{self.file}:{self.line}  {self.detail}"


def _is_super_init(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "__init__"
            and isinstance(f.value, ast.Call) and isinstance(f.value.func, ast.Name)
            and f.value.func.id == "super")


def _call_name(call: ast.Call) -> Optional[str]:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _is_base_subclass(cls: ast.ClassDef) -> bool:
    return any(isinstance(b, ast.Name) and b.id == "BaseManager" for b in cls.bases)


def find_violations(managers_dir: Optional[Path] = None) -> list[Violation]:
    """Return every real manager-contract violation under ``managers_dir`` (default: the repo's
    ``scripts/managers``). An empty list means every BaseManager subclass forwards the full context."""
    root = Path(managers_dir) if managers_dir else _MANAGERS_DIR
    files = [p for p in root.rglob("*.py") if not p.name.startswith("test_")]

    # Pass 1: the set of real BaseManager subclass names (so we never mistake a utility class for a child).
    trees: dict[Path, ast.AST] = {}
    base_subclasses: set[str] = set()
    for p in files:
        try:
            t = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        trees[p] = t
        for c in ast.walk(t):
            if isinstance(c, ast.ClassDef) and _is_base_subclass(c):
                base_subclasses.add(c.name)

    out: list[Violation] = []
    for p, t in trees.items():
        rel = p.relative_to(root).as_posix()
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()

        def snip(node: ast.AST) -> str:
            ln = getattr(node, "lineno", 0)
            return lines[ln - 1].strip()[:120] if 0 < ln <= len(lines) else "?"

        # File-wide: cache= on super/manager-child, getattr(parent|manager,"cache"), fresh framework ctor.
        for node in ast.walk(t):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            target_is_manager = _is_super_init(node) or (name in base_subclasses)
            for kw in node.keywords:
                if kw.arg == "cache" and target_is_manager:
                    out.append(Violation("D6", rel, node.lineno, f"passes cache= (use global_cache=): {snip(node)}"))
            if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
                a0, a1 = node.args[0], node.args[1]
                if isinstance(a1, ast.Constant) and a1.value == "cache":
                    base = a0.id if isinstance(a0, ast.Name) else (a0.attr if isinstance(a0, ast.Attribute) else "")
                    if base in {"parent", "manager"}:
                        out.append(Violation("D6", rel, node.lineno, f'getattr(..., "cache") reads non-canonical attr: {snip(node)}'))
            if isinstance(node.func, ast.Name) and node.func.id in _FRAMEWORK_CTORS and rel not in _FRESH_OK:
                out.append(Violation("D4", rel, node.lineno, f"{node.func.id}(...) built fresh instead of inherited: {snip(node)}"))

        # Class-level: super() coverage + child construction, for real BaseManager subclasses only.
        for cls in [n for n in ast.walk(t) if isinstance(n, ast.ClassDef) and _is_base_subclass(n)]:
            init = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
            if init is None:
                continue
            named = {a.arg for a in init.args.args} | {a.arg for a in init.args.kwonlyargs}
            captures_kwargs = init.args.kwarg is not None
            supers = [c for c in ast.walk(init) if isinstance(c, ast.Call) and _is_super_init(c)]
            if not supers:
                out.append(Violation("D5", rel, init.lineno, f"{cls.name}.__init__ never calls super().__init__()"))
            for sc in supers:
                covered: set[str] = set()
                for i, a in enumerate(sc.args):
                    if not isinstance(a, ast.Starred) and i < len(_BASE_PARAMS):
                        covered.add(_BASE_PARAMS[i])
                forwards_kwargs = any(kw.arg is None for kw in sc.keywords)
                covered |= {kw.arg for kw in sc.keywords if kw.arg in _BASE_PARAMS}
                # A required param is REALLY dropped only when it is a NAMED param (so it is captured
                # and would NOT auto-flow through **kwargs) yet is not passed to super().
                dropped = sorted(r for r in _REQUIRED if r in named and r not in covered)
                if dropped:
                    out.append(Violation("D1", rel, sc.lineno, f"{cls.name} super() drops captured {dropped}: {snip(sc)}"))
                if captures_kwargs and not forwards_kwargs:
                    out.append(Violation("D7", rel, sc.lineno, f"{cls.name} super() does not forward **kwargs: {snip(sc)}"))

            for node in ast.walk(init):
                if not isinstance(node, ast.Call) or _is_super_init(node):
                    continue
                name = _call_name(node)
                if name not in base_subclasses or name == cls.name:
                    continue
                if any(kw.arg is None for kw in node.keywords):      # **init_kwargs splat → assume forwarded
                    continue
                kw_names = {kw.arg for kw in node.keywords}
                if "logger" not in kw_names and "config" not in kw_names:
                    continue
                missing = [k for k in ("global_cache", "registry") if k not in kw_names]
                if missing:
                    out.append(Violation("D3", rel, node.lineno, f"{cls.name} builds {name}(...) without {missing}: {snip(node)}"))

    return sorted(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic AST audit of the manager-inheritance contract.")
    ap.add_argument("--path", help="managers dir to scan (default: scripts/managers)")
    args = ap.parse_args(argv)

    violations = find_violations(Path(args.path) if args.path else None)
    if not violations:
        print("OK: every BaseManager subclass forwards the full framework context (0 violations).")
        return 0
    print(f"FOUND {len(violations)} manager-contract violation(s):\n")
    for v in violations:
        print(f"  {v}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

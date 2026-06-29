"""BaseManager's registry parent-link must inherit the parent's shared cache under the CANONICAL
attr name 'global_cache' (the line-78 fix — it used to read a non-canonical 'cache'), and must never
clobber a child's already-good cache to None when the parent's cache is None."""
from __future__ import annotations

import types

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.registry import RegistryManager


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_success(self, *a, **k): pass


def _cfg():
    # BaseManager reads self.config.raw_data (auto_hot_swap) and getattr(config,'version',...)
    return types.SimpleNamespace(raw_data={}, version="test")


def _fake_parent(global_cache):
    return types.SimpleNamespace(global_cache=global_cache, logger=_Logger(), config=_cfg(),
                                 validator=None, manager=None)


class _ChildA(BaseManager):
    pass


class _ChildB(BaseManager):
    pass


def test_child_inherits_parent_global_cache_via_registry_link():
    reg = RegistryManager()
    sentinel = object()
    reg.register("manager", "AuditFakeParent", _fake_parent(sentinel))
    child = _ChildA(logger=_Logger(), config=_cfg(), global_cache=None,
                    registry=reg, parent_name="AuditFakeParent", singleton_key="audit-inherit")
    assert child.global_cache is sentinel          # inherited the parent's shared cache (canonical attr)


def test_parent_none_cache_does_not_clobber_childs_good_cache():
    reg = RegistryManager()
    good = object()
    reg.register("manager", "AuditNoneParent", _fake_parent(None))
    child = _ChildB(logger=_Logger(), config=_cfg(), global_cache=good,
                    registry=reg, parent_name="AuditNoneParent", singleton_key="audit-noclobber")
    assert child.global_cache is good              # a None-cache parent must NOT overwrite a good cache

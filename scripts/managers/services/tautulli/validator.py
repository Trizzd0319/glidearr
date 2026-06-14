"""
tautulli/validator.py — package-level validator stub
=====================================================
``TautulliManager.__init__`` imports ``TautulliValidatorManager`` from this
module and registers it as the ``validator_manager`` component. The working
instance-level validation lives in ``tautulli.instances.validator``
(``TautulliInstanceValidatorManager``).

This stub fills that component slot (its ``validate()`` returns True).
``TautulliManager`` IS used by the startup path — ``main.py`` constructs it in
``_initialize_managers`` and calls ``prepare()``/``run()`` in Phase 1/2 — so this
module loads on every run; keep it import-safe.
"""

from scripts.managers.factories.base_manager import BaseManager


class TautulliValidatorManager(BaseManager):
    """
    Stub validator — satisfies TautulliManager's component registry.
    For real validation logic see tautulli.instances.validator.
    """

    parent_name = "TautulliManager"

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

    def validate(self) -> bool:
        return True

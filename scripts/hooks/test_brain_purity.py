"""Brain-purity guard tests — the migrated machine_learning/ subpackages must not
import HTTP / the service layer / *_api. Running it in the suite makes the invariant
a standing regression guard (not just a pre-commit hook)."""
from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("brain_purity", os.path.join(_HERE, "brain_purity.py"))
brain_purity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain_purity)


def test_migrated_brain_is_pure():
    # The real invariant: the guarded subpackages import nothing forbidden.
    assert brain_purity.main() == 0


def test_violations_flags_forbidden_imports(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import requests\n"
        "from scripts.managers.services.radarr.quality import space_pressure\n"
        "from scripts.support.utilities.radarr_api import RadarrAPI\n"
        "import pandas as pd\n"            # allowed
        "from .sibling import helper\n",   # relative — allowed
        encoding="utf-8",
    )
    v = brain_purity._violations(str(bad))
    joined = "\n".join(v)
    assert "HTTP client 'requests'" in joined
    assert "service layer" in joined
    assert "*_api" in joined
    assert len(v) == 3                      # pandas + relative import are NOT flagged

"""Importing the config models must not load the heavy numeric stack."""

import subprocess
import sys


def test_config_import_does_not_load_scipy_or_matplotlib():
    code = (
        "import sys\n"
        "import murineshiftwork.logic.config.models\n"
        "import murineshiftwork.logic.config\n"
        "heavy = [m for m in ('scipy', 'matplotlib') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

"""The scheduled workflows invoke these modules as scripts, not as packages.

``uv run src/zotero_arxiv_daily/weekly.py`` gives the module no parent
package, so a relative import there dies at startup — and no package-level
test catches it, because pytest imports the module normally.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["main.py", "weekly.py", "monthly.py"])
def test_entrypoint_runs_as_a_script(script):
    result = subprocess.run(
        [sys.executable, str(REPO / "src" / "zotero_arxiv_daily" / script), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    combined = result.stdout + result.stderr
    assert "ImportError" not in combined, combined
    assert "attempted relative import" not in combined, combined
    assert result.returncode == 0, combined

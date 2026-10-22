"""Pre-commit: pip-audit, one run per requirements file because the services
pin different versions of the same packages and a combined run rejects that."""
import subprocess
import sys
from pathlib import Path

import pytest

REQUIREMENTS = sorted(path for path in Path(".").glob("**/requirements*.txt") if "node_modules" not in path.parts)


@pytest.mark.parametrize("requirements", REQUIREMENTS, ids=str)
def test_dependency_audit(requirements):
    # shortcut: --no-deps --disable-pip audits only the pinned top-level packages
    # and needs every line pinned (==); drop both flags if ~2 min per file becomes acceptable
    result = subprocess.run([sys.executable, "-m", "pip_audit", "--no-deps", "--disable-pip",  # noqa: S603
                             "--progress-spinner", "off", "-r", str(requirements)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

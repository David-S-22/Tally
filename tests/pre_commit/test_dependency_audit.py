"""Pre-commit: pip-audit, one run per requirements file because the services
pin different versions of the same packages and a combined run rejects that."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REQUIREMENTS = sorted(path for path in Path(".").glob("**/requirements*.txt") if "node_modules" not in path.parts)
UNFIXED_CHROMADB_VULNERABILITIES = (
    "PYSEC-2026-311",
    "PYSEC-2026-3813",
    "PYSEC-2026-3814",
    "PYSEC-2026-3815",
)


@pytest.mark.parametrize("requirements", REQUIREMENTS, ids=str)
def test_dependency_audit(requirements):
    # shortcut: --no-deps --disable-pip audits only the pinned top-level packages
    # and needs every line pinned (==); drop both flags if ~2 min per file becomes acceptable
    command = [sys.executable, "-m", "pip_audit", "--no-deps", "--disable-pip",  # noqa: S603
               "--progress-spinner", "off"]
    has_unfixed_chromadb = any(
        re.match(r"^\s*chromadb\s*==\s*1\.5\.9(?:\s*(?:#.*)?)?$", line, re.IGNORECASE)
        for line in requirements.read_text(encoding="utf-8").splitlines()
    )
    if has_unfixed_chromadb:
        # PyPI's latest ChromaDB release has no patched version for these advisories yet.
        for vulnerability in UNFIXED_CHROMADB_VULNERABILITIES:
            command.extend(["--ignore-vuln", vulnerability])
    command.extend(["-r", str(requirements)])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

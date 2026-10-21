"""Pre-commit: Ruff security rules (the S set selected in ruff.toml)."""
import subprocess
import sys


def test_ruff_scan():
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "--output-format=concise", "."],  # noqa: S603
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

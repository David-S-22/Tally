"""Cross-platform pre-commit entry: run one pytest file from the repository
root, so the hook needs no bash and no .venv/bin versus .venv/Scripts path."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.exit(subprocess.run([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=ROOT).returncode)  # noqa: S603 fixed argv, no shell

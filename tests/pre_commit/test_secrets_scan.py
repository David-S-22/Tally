"""Pre-commit: detect-secrets. Fails on any finding that is not an audited
false positive in .secrets.baseline."""
import json
import subprocess
import sys


def test_secrets_scan():
    result = subprocess.run([sys.executable, "-m", "detect_secrets", "scan",  # noqa: S603
                             "--exclude-files", r"package-lock\.json$", "--exclude-files", r"\.secrets\.baseline$"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with open(".secrets.baseline", encoding="utf-8") as handle:
        baseline = json.load(handle)["results"]
    known = {(path, item["hashed_secret"]) for path, items in baseline.items() for item in items}
    new = [f"{path}:{item['line_number']} {item['type']}"
           for path, items in json.loads(result.stdout)["results"].items() for item in items
           if (path.replace("\\", "/"), item["hashed_secret"]) not in known]
    assert not new, "Potential secrets detected: " + ", ".join(new)

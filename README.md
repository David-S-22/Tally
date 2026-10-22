# ASD Spring 2026 — group 21

## Agentic loop

A shared terminal Plan → Act → Observe → Adapt review workflow lives in
`agentic_loop/` with per-student prompt assets under `prompts/`. With the
group app running (`docker compose up -d`):

```
pip install -r agentic_loop/requirements.txt
python -m agentic_loop.main
```

Run records land in `reports/` (gitignored). Details: `agentic_loop/README.md`.

## Pre-commit security scans

`.pre-commit-config.yaml` runs Ruff security rules (`ruff.toml`),
detect-secrets (`.secrets.baseline`) and pip-audit on every commit and on
pull requests to `main`, each as a pytest file in `tests/pre_commit/`.

```
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

The agentic loop's Testing Review mode runs the same scans and records the
result. Each student's scan report lives in `docs/release-2/<name>/`.

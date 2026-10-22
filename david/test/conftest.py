import os
import pathlib
import sys

# AI mode is off by default so the cloud baseline makes no model calls. The suite
# drives the planner, classifier and suggestion paths against a stubbed client,
# so switch it on for the whole run. ollama_service.ai_mode_enabled() reads the
# environment per call, so this needs no patching of the module.
os.environ.setdefault("AI_MODE_ENABLED", "true")


def add_to_path(path: pathlib.Path):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

root_dir = pathlib.Path(__file__).resolve().parent.parent.parent
david_dir = pathlib.Path(__file__).resolve().parent.parent
add_to_path(root_dir)
add_to_path(david_dir)

import backend.savings_service
import backend.savings_service.app
import backend.savings_service.classifier_service
import backend.savings_service.helpers
import backend.savings_service.ollama_service
import backend.savings_service.suggestion_service

sys.modules["savings_service"] = backend.savings_service
for sub in ["app", "helpers", "ollama_service", "classifier_service", "suggestion_service"]:
    mod = getattr(backend.savings_service, sub)
    sys.modules[f"savings_service.{sub}"] = mod

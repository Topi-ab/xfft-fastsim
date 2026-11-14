"""Import paths of the characterization bench: the repo's tests/ infrastructure, model/ and scripts/."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

for path in (HERE, REPO / "tests", REPO / "model", REPO / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def pytest_report_header(config):
    """The repo's header (venv, packages, model, configuration), from tests/conftest.py."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("repo_tests_conftest", REPO / "tests" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pytest_report_header(config)

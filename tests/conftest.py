import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def agent():
    path = Path(__file__).resolve().parents[1] / "agentd" / "agentd.py"
    spec = importlib.util.spec_from_file_location("agentd_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

import os

import pytest

from wewln_phonecaller.config import load_settings
from wewln_phonecaller.logging_setup import shutdown_logging


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """No WEWLN_* variables from the machine and no stray .env from the checkout leak into tests."""
    for key in list(os.environ):
        if key.upper().startswith("WEWLN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    shutdown_logging()  # release log files; Windows cannot delete open files


@pytest.fixture
def make_settings(tmp_path):
    def factory(**overrides):
        overrides.setdefault("data_dir", tmp_path / "data")
        return load_settings(env_file=None, **overrides)

    return factory

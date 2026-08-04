import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    override = os.environ.get("SPEC_DIR")
    directory = (
        Path(override).resolve()
        if override
        else (REPO_ROOT.parent / "api-gateway" / "spec").resolve()
    )
    if not directory.is_dir():
        raise RuntimeError(
            f"Protocol spec not found at {directory}. Check out api-gateway beside this "
            f"repo or set SPEC_DIR. Conformance must never silently skip."
        )
    return directory


@pytest.fixture(scope="session")
def load_vector(spec_dir):
    def _load(name: str) -> dict:
        return json.loads((spec_dir / "vectors" / name).read_text())

    return _load


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "home"
    monkeypatch.setenv("MEERKLY_HOME", str(directory))
    return directory

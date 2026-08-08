import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_spec_dir() -> Path:
    """Locate the api-gateway protocol spec.

    Order: explicit SPEC_DIR, then the sibling api-gateway checkout (canonical —
    used in local dev so spec drift fails immediately), then the vendored copy
    committed under spec/ (used in CI, where the private sibling isn't
    available). Keep the vendored copy fresh with scripts/sync-spec.sh.
    """
    override = os.environ.get("SPEC_DIR")
    if override:
        return Path(override).resolve()
    sibling = (REPO_ROOT.parent / "api-gateway" / "spec").resolve()
    if sibling.is_dir():
        return sibling
    return (REPO_ROOT / "spec").resolve()


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    directory = _resolve_spec_dir()
    if not directory.is_dir():
        raise RuntimeError(
            f"Protocol spec not found at {directory}. Check out api-gateway beside this "
            f"repo, set SPEC_DIR, or run scripts/sync-spec.sh. Conformance must never "
            f"silently skip."
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

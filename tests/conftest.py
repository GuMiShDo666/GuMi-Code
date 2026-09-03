from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path

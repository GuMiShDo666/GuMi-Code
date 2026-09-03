import tomllib
from pathlib import Path


def _load_pyproject() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_pyproject_project_name() -> None:
    data = _load_pyproject()

    assert data["project"]["name"] == "vibe-harness"


def test_pyproject_console_script_points_to_cli_main() -> None:
    data = _load_pyproject()

    assert data["project"]["scripts"]["vibe-harness"] == "vibe_harness.cli:main"


def test_pyproject_package_discovery_uses_src() -> None:
    data = _load_pyproject()

    assert data["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_pyproject_python_requires() -> None:
    data = _load_pyproject()

    assert data["project"]["requires-python"] == ">=3.11"

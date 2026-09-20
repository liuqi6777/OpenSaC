import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_wheels", Path(__file__).resolve().parents[1] / "scripts" / "verify_wheels.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
select_wheel = _MODULE.select_wheel


@pytest.fixture
def wheel_directory(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    (tmp_path / "dist").mkdir()
    return tmp_path


def test_wheel_selection_ignores_old_builds(wheel_directory):
    old = wheel_directory / "dist" / "opensac-0.1.0-py3-none-any.whl"
    current = wheel_directory / "dist" / "opensac-0.2.0-py3-none-any.whl"
    old.touch()
    current.touch()
    assert select_wheel(wheel_directory) == current
    with pytest.raises(ValueError):
        select_wheel(wheel_directory, old)


def test_wheel_selection_rejects_missing_or_ambiguous_builds(wheel_directory):
    with pytest.raises(ValueError):
        select_wheel(wheel_directory)
    first = wheel_directory / "dist" / "opensac-0.2.0-py3-none-any.whl"
    second = wheel_directory / "dist" / "opensac-0.2.0-1-py3-none-any.whl"
    first.touch()
    second.touch()
    with pytest.raises(ValueError):
        select_wheel(wheel_directory)
    assert select_wheel(wheel_directory, second) == second
    second.unlink()
    with pytest.raises(ValueError):
        select_wheel(wheel_directory, second)

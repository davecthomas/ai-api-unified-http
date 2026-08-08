# tests/test_version_sync.py

"""
The service version lives in exactly three places; this test fails whenever
they disagree so a partial bump cannot ship. Mirrors the convention proven in
the ai-api-unified library repo.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def _module_version() -> str:
    from ai_api_unified_http.__version__ import __version__

    return __version__


def _readme_title_version() -> str:
    first_line: str = (REPO_ROOT / "README.md").read_text().splitlines()[0]
    match = re.match(r"^# ai-api-unified-http (\d+\.\d+\.\d+)$", first_line)
    assert match, f"README title does not carry a version: {first_line!r}"
    return match.group(1)


def test_three_version_locations_agree() -> None:
    assert _pyproject_version() == _module_version() == _readme_title_version()

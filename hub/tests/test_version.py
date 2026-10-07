"""The release workflow tags from the root VERSION file; the package and the app must never disagree with it."""

import re
import tomllib
from pathlib import Path

import pytest

from acm_hub import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_package_version_is_plain_semver_and_matches_pyproject():
    pyproject = tomllib.loads((ROOT / "hub" / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)  # the release workflow tags v<this>


@pytest.mark.skipif(not (ROOT / "VERSION").exists(), reason="not running from a repo checkout")
def test_root_version_file_is_the_source_of_truth():
    """Edit VERSION, then run `python scripts/sync-version.py`; this fails if you forgot the second step."""
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == __version__

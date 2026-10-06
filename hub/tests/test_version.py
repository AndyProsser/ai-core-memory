"""The release workflow tags from pyproject.toml; the app reports __version__. They must never disagree."""

import re
import tomllib
from pathlib import Path

from acm_hub import __version__


def test_package_version_matches_pyproject_and_is_plain_semver():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert __version__ == pyproject["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)  # the release workflow tags v<this>

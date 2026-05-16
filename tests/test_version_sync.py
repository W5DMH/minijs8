"""Catch drift between version.py and pyproject.toml.

Two places have to agree on the version string. This test ensures we
notice if someone bumps one but forgets the other, before that drift
ships in an image.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from minijs8 import __version__


def test_version_matches_pyproject():
    project_root = Path(__file__).parent.parent
    pyproject = project_root / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    declared = data["project"]["version"]
    assert declared == __version__, (
        f"version mismatch: pyproject.toml says {declared!r}, "
        f"src/minijs8/version.py says {__version__!r}; "
        f"update both"
    )


def test_version_format():
    """Sanity check that the version is a non-empty string of dots and digits."""
    assert isinstance(__version__, str)
    assert re.match(r"^\d+\.\d+\.\d+(\.\w+)?$", __version__), (
        f"version {__version__!r} does not look like SemVer"
    )


def test_setup_header_version_formatter():
    """The SETUP screen title bar uses _format_version_for_header to
    trim trailing ``.0`` patch components for readability on the 240-
    px display. Pin the contract so a future re-arrangement of the
    formatter doesn't quietly start emitting 'V1.0.0' (which we
    intentionally avoid — the W5DMH spec asked for 'V1.0')."""
    from minijs8.ui.screens import _format_version_for_header
    # Marketing-friendly form when patch is .0.
    assert _format_version_for_header("1.0.0") == "V1.0"
    assert _format_version_for_header("2.3.0") == "V2.3"
    # Non-zero patch is preserved (bug-fix builds).
    assert _format_version_for_header("1.0.5") == "V1.0.5"
    assert _format_version_for_header("1.2.3") == "V1.2.3"
    # Prerelease tags pass through unchanged (don't strip the .0
    # before a ".devN" suffix — that's still meaningful).
    assert _format_version_for_header("1.0.0.dev1") == "V1.0.0.dev1"
    # Defensive: empty / weird inputs don't blow up the renderer.
    assert _format_version_for_header("") == "V?"

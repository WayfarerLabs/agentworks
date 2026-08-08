"""Unit tests for ``agentworks.path_rendering.format_host_path``: the three
branches of the spelling rule itself.

The companion ``test_operator_path_rendering.py`` guards the invariant one
level up, that every operator-facing surface actually routes through this
function. These tests pin what the function returns; that one pins who
calls it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.path_rendering import format_host_path


def test_format_host_path_uses_tilde_for_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rendered = format_host_path(tmp_path / "agentworks" / "config.toml")
    assert rendered == "~/agentworks/config.toml"


def test_format_host_path_falls_back_to_absolute_outside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    rendered = format_host_path(Path("/etc/agentworks.toml"))
    assert rendered == "/etc/agentworks.toml"


def test_format_host_path_relative_path_renders_as_is() -> None:
    """Relative ``Path`` inputs render verbatim: only absolute paths are
    candidates for the ``~/`` rewrite.
    """
    assert format_host_path(Path("config.toml")) == "config.toml"

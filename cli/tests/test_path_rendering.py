"""Unit tests for ``agentworks.path_rendering.format_host_path``: the three
branches of the spelling rule itself.

The companion ``test_operator_path_rendering.py`` guards the invariant one
level up, that every operator-facing surface actually routes through this
function. These tests pin what the function returns; that one pins who
calls it.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

import agentworks.path_rendering as path_rendering


def test_format_host_path_uses_tilde_for_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rendered = path_rendering.format_host_path(tmp_path / "agentworks" / "config.toml")
    # The separator is the host-native one by design (see the Windows case
    # below), so build the expectation the same way rather than pinning "/".
    assert rendered == str(Path("~") / "agentworks" / "config.toml")


def test_format_host_path_uses_tilde_for_home_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert path_rendering.format_host_path(tmp_path) == "~"


def test_format_host_path_uses_windows_separators_for_home(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WindowsPath(PureWindowsPath):
        @classmethod
        def home(cls) -> _WindowsPath:
            return cls("C:/Users/operator")

    monkeypatch.setattr(path_rendering, "Path", _WindowsPath)

    rendered = path_rendering.format_host_path(_WindowsPath("C:/Users/operator/agentworks/config.toml"))

    assert rendered == r"~\agentworks\config.toml"


def test_format_host_path_falls_back_to_absolute_outside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # An absolute path outside $HOME renders as the bare absolute path. Build
    # it from the host's drive anchor so it is genuinely absolute on Windows
    # too (a bare "/etc/..." has no drive there and reads as relative).
    outside = Path(tmp_path.anchor) / "etc" / "agentworks.toml"
    rendered = path_rendering.format_host_path(outside)
    assert rendered == str(outside)


def test_format_host_path_relative_path_renders_as_is() -> None:
    """Relative ``Path`` inputs render verbatim: only absolute paths are
    candidates for the home-relative rewrite.
    """
    assert path_rendering.format_host_path(Path("config.toml")) == "config.toml"

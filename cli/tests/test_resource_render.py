"""Tests for ``agentworks.resources.render`` -- the framework-shared
origin renderer that backs both ``agw resource describe`` (cross-kind)
and ``agw secret describe`` (per-kind). The renderer lives in the
framework layer so kind-specific modules don't drift from each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.resources.origin import Origin
from agentworks.resources.render import format_file_path, format_origin_line


def test_format_origin_line_handles_none() -> None:
    assert format_origin_line(None) == "unknown"


def test_format_origin_line_operator_declared_with_file_and_line() -> None:
    origin = Origin.operator_declared(file=Path("/tmp/config.toml"), line=42)
    rendered = format_origin_line(origin)
    assert rendered.startswith("operator-declared (")
    assert rendered.endswith(":42)")


def test_format_origin_line_operator_declared_without_file_returns_bare_label() -> (
    None
):
    """The defensive path for an operator-declared origin with no file
    information (e.g. a singleton-omitted Config default) still returns
    a meaningful single-cell label.
    """
    origin = Origin.operator_declared(file=None, line=0)
    assert format_origin_line(origin) == "operator-declared"


def test_format_origin_line_auto_declared_with_source() -> None:
    origin = Origin.auto_declared(source=("vm_template", "default"))
    assert format_origin_line(origin) == "auto-declared (vm_template:default)"


def test_format_origin_line_code_declared_with_source() -> None:
    origin = Origin.code_declared(source="framework:always-materialize")
    assert (
        format_origin_line(origin)
        == "code-declared (framework:always-materialize)"
    )


def test_format_origin_line_raises_on_unknown_variant() -> None:
    """A future ``Origin`` variant must be wired through the renderer
    explicitly; failing loudly catches the silent-drift case.
    """
    fake = type(
        "_BogusOrigin",
        (),
        {"variant": "made-up", "file": None, "line": 0, "source": None},
    )()
    with pytest.raises(AssertionError):
        format_origin_line(fake)  # type: ignore[arg-type]


def test_format_file_path_uses_tilde_for_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rendered = format_file_path(tmp_path / "agentworks" / "config.toml")
    assert rendered == "~/agentworks/config.toml"


def test_format_file_path_falls_back_to_absolute_outside_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    rendered = format_file_path(Path("/etc/agentworks.toml"))
    assert rendered == "/etc/agentworks.toml"


def test_format_file_path_relative_path_renders_as_is() -> None:
    """Relative ``Path`` inputs render verbatim -- only absolute paths
    are candidates for the ``~/`` rewrite.
    """
    assert format_file_path(Path("config.toml")) == "config.toml"

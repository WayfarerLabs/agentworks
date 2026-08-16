"""Tests for ``agentworks.resources.render`` shared origin formatting.

The renderer backs resource inventory and ``agw secret describe``. It lives
in the framework layer so kind-specific modules do not drift from each other.

The host-path spelling these renderers embed is tested next to the rule
itself, in ``test_path_rendering.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.origin import Origin
from agentworks.resources.render import format_origin_line


def test_format_origin_line_handles_none() -> None:
    assert format_origin_line(None) == "unknown"


def test_format_origin_line_operator_declared_with_file_and_line() -> None:
    origin = Origin.operator_declared(file=Path("/tmp/config.toml"), line=42)
    rendered = format_origin_line(origin)
    assert rendered.startswith("operator-declared (")
    assert rendered.endswith(":42)")


def test_format_origin_line_operator_declared_without_file_returns_bare_label() -> None:
    """The defensive path for an operator-declared origin with no file
    information (e.g. a singleton-omitted Config default) still returns
    a meaningful single-cell label.
    """
    origin = Origin.operator_declared(file=None, line=0)
    assert format_origin_line(origin) == "operator-declared"


def test_format_origin_line_auto_declared_with_source() -> None:
    origin = Origin.auto_declared(source=("vm-template", "default"))
    assert format_origin_line(origin) == "auto-declared (vm-template/default)"


def test_format_origin_line_built_in_with_source() -> None:
    origin = Origin.built_in(source="framework:always-materialize")
    assert format_origin_line(origin) == "built-in (framework:always-materialize)"


def test_format_origin_line_system_plugin_with_source() -> None:
    origin = Origin.system_plugin(plugin="apt", source="agentworks.plugins.apt")
    assert format_origin_line(origin) == "system-plugin apt (agentworks.plugins.apt)"


def test_format_origin_line_system_plugin_without_source_returns_bare_label() -> None:
    """The defensive path for a system-plugin origin with no source. The
    factory always sets ``source``, so this exercises a hand-built
    ``Origin`` (a malformed producer, or a future caller bypassing the
    factory) the same way the operator-declared defensive test does.
    """
    origin = Origin(variant="system-plugin", plugin="apt", source=None)
    assert format_origin_line(origin) == "system-plugin apt"


def test_format_origin_line_system_plugin_without_plugin_degrades_gracefully() -> None:
    """A malformed system-plugin origin missing its ``plugin`` name degrades
    to the bare label rather than rendering the literal ``"None"``. The
    factory always sets ``plugin``, so this exercises a hand-built ``Origin``;
    the branch guards ``plugin`` for the same defensive reason it guards
    ``source``.
    """
    origin = Origin(variant="system-plugin", plugin=None, source="agentworks.plugins.apt")
    assert format_origin_line(origin) == "system-plugin (agentworks.plugins.apt)"


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

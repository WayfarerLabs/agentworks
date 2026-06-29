"""Framework-layer rendering helpers shared by every kind's CLI describe
view. ``format_origin_line`` and ``format_file_path`` live here (not in
any kind module) because the cross-kind ``agw resource describe`` and
the per-kind commands (``agw secret describe``, future ``agw vm
describe`` ...) all render the same ``Origin`` shape; defining the
renderer next to ``Origin`` keeps the layer correct.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources.origin import Origin


def format_origin_line(origin: Origin | None) -> str:
    """Render an ``Origin`` as a single-line parenthetical:
    ``"operator-declared (~/path:42)"``, ``"auto-declared (kind:name)"``,
    ``"code-declared (source)"``. ``"unknown"`` when ``origin`` is None
    (defensive for Resources constructed outside the framework path).

    Raises ``AssertionError`` on an unknown ``Origin`` variant -- a loud
    failure here catches the case where a future variant is added to
    ``Origin`` without a corresponding renderer update.
    """
    if origin is None:
        return "unknown"
    if origin.variant == "operator-declared":
        if origin.file is not None and origin.line:
            return f"operator-declared ({format_file_path(origin.file)}:{origin.line})"
        return "operator-declared"
    if origin.variant == "auto-declared":
        source = origin.source
        if isinstance(source, tuple) and len(source) == 2:
            return f"auto-declared ({source[0]}:{source[1]})"
        return "auto-declared"
    if origin.variant == "code-declared":
        source = origin.source
        return f"code-declared ({source})" if source else "code-declared"
    raise AssertionError(f"unhandled Origin variant: {origin.variant!r}")


def format_file_path(file: Path) -> str:
    """Render a file path operator-friendly: ``~/path`` when under
    ``$HOME``, else the bare absolute path. Relative paths render as-is.
    """
    if file.is_absolute():
        try:
            return f"~/{file.relative_to(Path.home())}"
        except ValueError:
            return str(file)
    return str(file)

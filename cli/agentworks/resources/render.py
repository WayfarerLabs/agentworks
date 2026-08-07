"""Framework-layer rendering helpers shared by every kind's CLI describe
view. ``format_origin_line`` lives here (not in any kind module) because
the cross-kind ``agw resource describe`` and the per-kind commands
(``agw secret describe``, future ``agw vm describe`` ...) all render the
same ``Origin`` shape; defining the renderer next to ``Origin`` keeps the
layer correct.

``format_file_path`` is re-exported from ``agentworks.source_location``,
which is where it moved so the schema error bridge can render a path the
same way without importing this package: importing anything under
``agentworks.resources`` runs that package's ``__init__``, which loads
every kind module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.source_location import format_file_path

if TYPE_CHECKING:
    from agentworks.origin import Origin
    from agentworks.resources.reference import ReferenceEntry


def format_reference_entry(entry: ReferenceEntry) -> str:
    """One "Referenced by:" line: who points here, what for, and where the
    name was actually written when those differ.

    An inheriting row publishes the runtime needs of its MERGED
    declaration (FR17), so "vm-template/kid: the BASE env var" can be
    entirely true and still send an operator to a file with no such env
    var in it. The tail names the template that wrote it.

    Shared by ``agw resource describe`` and ``agw secret describe``, which
    render the same list and must not drift.
    """
    line = f"{entry.source[0]}/{entry.source[1]}: {entry.usage}"
    if entry.declared_by is None or entry.declared_by == entry.source:
        return line
    return f"{line} (inherited from {entry.declared_by[0]}/{entry.declared_by[1]})"


def format_origin_line(origin: Origin | None) -> str:
    """Render an ``Origin`` as a single-line parenthetical:
    ``"operator-declared (~/path:42)"``, ``"auto-declared (kind:name)"``,
    ``"built-in (source)"``, ``"system-plugin <plugin> (source)"``.
    ``"unknown"`` when ``origin`` is None (defensive for Resources
    constructed outside the framework path).

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
            return f"auto-declared ({source[0]}/{source[1]})"
        return "auto-declared"
    if origin.variant == "built-in":
        source = origin.source
        return f"built-in ({source})" if source else "built-in"
    if origin.variant == "system-plugin":
        label = f"system-plugin {origin.plugin}" if origin.plugin else "system-plugin"
        return f"{label} ({origin.source})" if origin.source else label
    raise AssertionError(f"unhandled Origin variant: {origin.variant!r}")


def format_origin_location(origin: Origin | None) -> str:
    """Render an ``Origin`` as a bare source location for inline error
    framing, dropping the variant prefix ``format_origin_line`` carries
    for the describe / doctor views. An operator-declared row renders as
    ``~/path:42`` (an operator reading a config error already knows it is
    their config, so the ``operator-declared`` prefix is redundant noise
    inside the message). Other variants fall back to the full
    ``format_origin_line`` rendering: a built-in ``source`` or an
    auto-declared ``kind/name`` carries no bare file location, so the
    labelled form stays the informative one.
    """
    if origin is not None and origin.variant == "operator-declared" and origin.file is not None and origin.line:
        return f"{format_file_path(origin.file)}:{origin.line}"
    return format_origin_line(origin)


__all__ = ["format_file_path", "format_origin_line", "format_origin_location"]

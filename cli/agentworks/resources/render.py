"""Framework-layer rendering helpers for resource inspection views.

``sanitize_fact_line`` is the shared boundary for dynamic scalar facts that
must remain on one terminal-safe line. ``format_origin_line`` lives here (not
in any kind module) because the resource inventory and kind-specific commands
such as ``agw secret describe`` render the same ``Origin`` shape; defining the
renderer next to ``Origin`` keeps the layer correct.

The host paths these renderers embed are spelled by
``agentworks.path_rendering.format_host_path``, the repo-wide rule, which
lives in its own top-level leaf so the schema error bridge can render a
path the same way without importing this package: importing anything
under ``agentworks.resources`` runs that package's ``__init__``, which
loads every kind module. Import it from there rather than from here.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import yaml

from agentworks.path_rendering import format_host_path
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from agentworks.machine_output import JsonObject
    from agentworks.origin import Origin
    from agentworks.resources.reference import ReferenceEntry


_UNSAFE_LINE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def sanitize_fact_line(value: str) -> str:
    """Remove terminal controls and unsafe Unicode categories from a fact line."""
    sanitized = sanitize_terminal_output(value)
    return "".join(
        character for character in sanitized if unicodedata.category(character) not in _UNSAFE_LINE_CATEGORIES
    )


def yaml_document_lines(value: JsonObject) -> tuple[str, ...]:
    """Serialize one JSON-compatible object as terminal-safe block YAML lines."""
    document = yaml.safe_dump(
        value,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    )
    return tuple(document.rstrip("\n").split("\n"))


def format_reference_entry(entry: ReferenceEntry) -> str:
    """One "Referenced by:" line: who points here, what for, and where the
    name was actually written when those differ.

    An inheriting row publishes the runtime needs of its MERGED
    declaration (FR17), so "vm-template/kid: the BASE env var" can be
    entirely true and still send an operator to a file with no such env
    var in it. The tail names the template that wrote it.

    This is the secret describe view's inbound-reference formatter. Graph
    output projects the authoritative references as typed edges instead.
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
            return f"operator-declared ({format_host_path(origin.file)}:{origin.line})"
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


__all__ = ["format_origin_line", "sanitize_fact_line", "yaml_document_lines"]

"""Representations of resolved secrets produced by shell argument quoting."""

from __future__ import annotations

import shlex


def secret_redaction_forms(value: str) -> tuple[str, ...]:
    """Include the escaped interior of a quoted assignment or larger argument.

    Quoting ``KEY=value`` puts the surrounding quotes before the key, so a
    separately quoted ``value`` is not necessarily a substring of the command.
    """
    if not value:
        return ()
    quoted = shlex.quote(value)
    interior = quoted[1:-1] if quoted.startswith("'") and quoted.endswith("'") else quoted
    return tuple(dict.fromkeys((value, quoted, interior)))

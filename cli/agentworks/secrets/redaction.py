"""Conventional shell and Python representations of resolved secrets."""

from __future__ import annotations

import shlex

# The bytes bash's ``$'...'`` quoting spells as a backslash and a letter.
_ANSI_C_LETTERS = {0x07: "a", 0x08: "b", 0x09: "t", 0x0A: "n", 0x0B: "v", 0x0C: "f", 0x0D: "r", 0x1B: "E"}


def secret_redaction_forms(value: str) -> tuple[str, ...]:
    """Include the escaped interior of a quoted assignment or larger argument.

    Quoting ``KEY=value`` puts the surrounding quotes before the key, so a
    separately quoted ``value`` is not necessarily a substring of the command.
    Python diagnostics can also emit ``repr`` of a value or a shell command.
    A value with leading or trailing line breaks is also matched without them,
    as a command substitution or a message that quotes it leaves it, and a
    value with a byte bash will not print raw also as bash's ``$'...'`` spelling
    of it (``printf %q``, ``declare -p``).
    """
    if not value:
        return ()
    quoted = shlex.quote(value)
    interior = quoted[1:-1] if quoted.startswith("'") and quoted.endswith("'") else quoted
    forms = (value, quoted, interior)
    represented = tuple(repr(form) for form in forms)
    trimmed = value.strip("\r\n")
    extra = (trimmed,) if trimmed and trimmed != value else ()
    ansi_c = tuple(spelling for text in (value, *extra) if (spelling := _bash_ansi_c_quote(text)) is not None)
    return tuple(dict.fromkeys((*forms, *represented, *(form[1:-1] for form in represented), *extra, *ansi_c)))


def _bash_ansi_c_quote(text: str) -> str | None:
    """bash's ``$'...'`` spelling of ``text``, or ``None`` where bash would not use it.

    bash quotes with ``$'...'`` only a string holding a byte it will not print
    raw. This is its spelling in the C locale, and in any locale for ASCII
    text; in a UTF-8 locale bash prints a printable non-ASCII character raw,
    which this does not attempt.
    """
    data = text.encode("utf-8", "surrogateescape")
    if all(0x20 <= byte < 0x7F for byte in data):
        return None
    spelled: list[str] = []
    for byte in data:
        if byte in _ANSI_C_LETTERS:
            spelled.append("\\" + _ANSI_C_LETTERS[byte])
        elif byte in (0x5C, 0x27):
            spelled.append("\\" + chr(byte))
        elif 0x20 <= byte < 0x7F:
            spelled.append(chr(byte))
        else:
            spelled.append(f"\\{byte:03o}")
    return "$'" + "".join(spelled) + "'"

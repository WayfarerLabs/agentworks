"""Conventional shell and Python representations of resolved secrets."""

from __future__ import annotations

import shlex
import unicodedata

# The bytes bash's ``$'...'`` quoting spells as a backslash and a letter.
_ANSI_C_LETTERS = {0x07: "a", 0x08: "b", 0x09: "t", 0x0A: "n", 0x0B: "v", 0x0C: "f", 0x0D: "r", 0x1B: "E"}

# The categories glibc's UTF-8 ``iswprint`` counts as printable and ``str.isprintable`` does not.
_GLIBC_PRINTABLE_EXTRA = frozenset({"Zs", "Cf", "Co"})


def secret_redaction_forms(value: str) -> tuple[str, ...]:
    """Include the escaped interior of a quoted assignment or larger argument.

    Quoting ``KEY=value`` puts the surrounding quotes before the key, so a
    separately quoted ``value`` is not necessarily a substring of the command.
    Python diagnostics can also emit ``repr`` of a value or a shell command.
    A value with leading or trailing line breaks is also matched without them,
    as a command substitution or a message that quotes it leaves it, and a
    value with a character bash will not print raw also as bash's ``$'...'``
    spelling of it (``printf %q``, ``declare -p``), in the C locale and in a
    UTF-8 one.
    """
    if not value:
        return ()
    quoted = shlex.quote(value)
    interior = quoted[1:-1] if quoted.startswith("'") and quoted.endswith("'") else quoted
    forms = (value, quoted, interior)
    represented = tuple(repr(form) for form in forms)
    trimmed = value.strip("\r\n")
    extra = (trimmed,) if trimmed and trimmed != value else ()
    ansi_c = tuple(
        spelling
        for text in (value, *extra)
        for utf8 in (False, True)
        if (spelling := _bash_ansi_c_quote(text, utf8=utf8)) is not None
    )
    return tuple(dict.fromkeys((*forms, *represented, *(form[1:-1] for form in represented), *extra, *ansi_c)))


def _bash_ansi_c_quote(text: str, *, utf8: bool) -> str | None:
    """bash's ``$'...'`` spelling of ``text``, or ``None`` where bash would not use it.

    bash quotes with ``$'...'`` only a string holding a character it will not
    print raw, and spells such a character's bytes in octal (a letter for
    the common controls). In the C locale that is every non-ASCII character.
    In a UTF-8 locale (the guest's ``C.UTF-8``) bash prints a printable
    non-ASCII character raw. glibc's
    ``iswprint`` there is Python's ``str.isprintable`` widened to spaces,
    format characters and private use (``_GLIBC_PRINTABLE_EXTRA``); the two
    still differ for a code point one Unicode version assigns and the other's
    does not, so that spelling is exact only outside those.
    """
    spelled: list[str] = []
    quoted = False
    for character in text:
        if 0x20 <= ord(character) < 0x7F:
            spelled.append("\\" + character if character in "\\'" else character)
            continue
        if utf8 and ord(character) >= 0x80 and _glibc_printable(character):
            spelled.append(character)
            continue
        quoted = True
        for byte in _utf8(character):
            letter = _ANSI_C_LETTERS.get(byte)
            spelled.append("\\" + letter if letter is not None else f"\\{byte:03o}")
    return "$'" + "".join(spelled) + "'" if quoted else None


def _glibc_printable(character: str) -> bool:
    return character.isprintable() or unicodedata.category(character) in _GLIBC_PRINTABLE_EXTRA


def _utf8(character: str) -> bytes:
    """The bytes ``character`` stands for: an undecodable byte for a ``surrogateescape`` surrogate."""
    try:
        return character.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return character.encode("utf-8", "surrogatepass")

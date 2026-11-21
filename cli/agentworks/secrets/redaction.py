"""Conventional shell and Python representations of resolved secrets."""

from __future__ import annotations

import shlex


def secret_redaction_forms(value: str) -> tuple[str, ...]:
    """Include the escaped interior of a quoted assignment or larger argument.

    Quoting ``KEY=value`` puts the surrounding quotes before the key, so a
    separately quoted ``value`` is not necessarily a substring of the command.
    Python diagnostics can also emit ``repr`` of a value or a shell command.
    """
    if not value:
        return ()
    quoted = shlex.quote(value)
    interior = quoted[1:-1] if quoted.startswith("'") and quoted.endswith("'") else quoted
    forms = (value, quoted, interior)
    represented = tuple(repr(form) for form in forms)
    # A larger value can make repr escape apostrophes that repr(secret) leaves raw.
    single_quote_interiors = tuple(form[1:-1].replace("'", "\\'") for form in represented if form.startswith('"'))
    return tuple(dict.fromkeys((*forms, *represented, *(form[1:-1] for form in represented), *single_quote_interiors)))

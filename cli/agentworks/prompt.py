"""Interactive prompt utilities."""

from __future__ import annotations

import sys

import click


def prompt_secret(label: str, *, hint: str | None = None) -> str:
    """Prompt for a secret value with masked confirmation.

    Shows the hint (if provided), prompts with hidden input, then
    overwrites the prompt line with asterisks to confirm entry.
    Rejects empty values.
    """
    if hint:
        click.echo(f"  {hint}", err=True)

    while True:
        value = str(click.prompt(label, err=True, default="", hide_input=True))
        if value.strip():
            break
        sys.stderr.write(f"\x1b[1A\r\x1b[2K{label}: (empty, try again)\n")
        sys.stderr.flush()

    # Confirm entry with masked placeholder
    mask = "*" * min(len(value), 20)
    sys.stderr.write(f"\x1b[1A\r\x1b[2K{label}: {mask}\n")
    sys.stderr.flush()

    return value

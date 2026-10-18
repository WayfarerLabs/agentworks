"""Fixed finite-input entry selected only by prepared DIRECT delivery."""

from __future__ import annotations

from ._inline_guest import _main


def main(nonce: str) -> int:
    """Run the separately bounded prepared request through the ordinary helper."""
    return _main(nonce, prepared_input=True)

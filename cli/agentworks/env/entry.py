"""EnvEntry: one env var declaration, either a plaintext value or a secret reference."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvEntry:
    """One env var declaration.

    Exactly one of ``value`` or ``secret`` is set. ``value`` carries a plaintext
    value to export; ``secret`` carries the name of a declared secret
    (``[secrets.<name>]``) that the resolver looks up at command time.

    ``key`` is the env var name; validation lives in the config loader so this
    type can stay pure data with no regex coupling.
    """

    key: str
    value: str | None = None
    secret: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and self.secret is None:
            raise ValueError(
                f"EnvEntry for {self.key!r} must set exactly one of value or secret",
            )
        if self.value is not None and self.secret is not None:
            raise ValueError(
                f"EnvEntry for {self.key!r} cannot set both value and secret",
            )

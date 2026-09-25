"""Caller-authored execution invocation values."""

from __future__ import annotations

import builtins
from dataclasses import KW_ONLY, dataclass, field
from enum import Enum

from agentworks.errors import ValidationError


class Shell(Enum):
    """Explicit application script interpreter selection."""

    SH = "sh"
    BASH = "bash"
    USER_DEFAULT = "user_default"


class Lifetime(Enum):
    """How long execution may outlive its initiating operation."""

    OPERATION = "operation"
    INDEPENDENT = "independent"


@dataclass(frozen=True, repr=False)
class Input:
    """Finite application input; empty bytes represent explicit EOF."""

    data: builtins.bytes
    is_sensitive: bool = False

    def __post_init__(self) -> None:
        if type(self.data) is not builtins.bytes or type(self.is_sensitive) is not bool:
            raise ValidationError("Execution input requires bytes and a sensitivity choice")

    @classmethod
    def eof(cls) -> Input:
        return cls(b"")

    @classmethod
    def bytes(cls, data: builtins.bytes, *, sensitive: bool = False) -> Input:
        return cls(data, sensitive)

    @classmethod
    def sensitive(cls, data: builtins.bytes) -> Input:
        return cls(data, True)


@dataclass(frozen=True)
class Output:
    """Bounded capture or discarded application output."""

    max_bytes: int | None

    def __post_init__(self) -> None:
        if self.max_bytes is not None and (type(self.max_bytes) is not int or self.max_bytes < 0):
            raise ValidationError("Execution capture requires a nonnegative byte bound")

    @classmethod
    def capture(cls, max_bytes: int = 4_096) -> Output:
        if type(max_bytes) is not int:
            raise ValidationError("Execution capture requires a nonnegative integer byte bound")
        return cls(max_bytes)

    @classmethod
    def discard(cls) -> Output:
        return cls(None)


@dataclass(frozen=True)
class Command:
    """Literal application argv, omitted from diagnostic representations."""

    argv: tuple[str, ...] = field(repr=False)

    def __init__(self, argv: list[str] | tuple[str, ...]) -> None:
        """Normalize the caller-owned argv sequence at the public value boundary."""
        if not isinstance(argv, (list, tuple)):
            raise ValidationError("Command argv must be a list or tuple of strings")
        if not argv or not all(isinstance(argument, str) for argument in argv) or not argv[0]:
            raise ValidationError("A literal command requires a nonempty argv of strings")
        object.__setattr__(self, "argv", tuple(argv))


@dataclass(frozen=True)
class Script:
    """Source and explicit shell startup choices, independent of application stdin."""

    source: str = field(repr=False)
    shell: Shell
    _: KW_ONLY
    login: bool = False
    interactive: bool = False

    def __post_init__(self) -> None:
        """Validate untyped callers without retaining caller payloads in failures."""
        if not isinstance(self.source, str):
            raise ValidationError("Script source must be text")
        if not isinstance(self.shell, Shell):
            raise ValidationError("Script shell must be a Shell value")
        if not isinstance(self.login, bool) or not isinstance(self.interactive, bool):
            raise ValidationError("Script startup flags must be booleans")

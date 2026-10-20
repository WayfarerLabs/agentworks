"""Caller-authored execution invocation values."""

from __future__ import annotations

import builtins
import inspect
from dataclasses import KW_ONLY, dataclass, field
from enum import Enum

from agentworks.errors import ValidationError

# Runtime introspection of public values resolves these annotation names.
from .carrier import ByteSink, ByteSource  # noqa: TC001


def _endpoint(endpoint: object, method: str) -> None:
    """Check external endpoint shape without invoking caller descriptors or I/O."""
    if not callable(inspect.getattr_static(endpoint, method, None)):
        raise ValidationError("Execution live I/O requires nonblocking byte endpoints")


class Shell(Enum):
    """Explicit application script interpreter selection."""

    SH = "sh"
    BASH = "bash"
    USER_DEFAULT = "user_default"


class Lifetime(Enum):
    """How long execution may outlive its initiating operation."""

    OPERATION = "operation"
    INDEPENDENT = "independent"


@dataclass(frozen=True, slots=True)
class JobRef:
    """Credential-free reference to one exact persisted managed run."""

    run_id: str

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or len(self.run_id) != 32
            or any(character not in "0123456789abcdef" for character in self.run_id)
        ):
            raise ValidationError("Job reference requires a canonical run identity")


@dataclass(frozen=True, repr=False)
class Input:
    """Finite input or an explicitly borrowed nonblocking application source."""

    data: builtins.bytes | None
    is_sensitive: bool = False
    source: ByteSource | None = field(default=None, repr=False, kw_only=True)
    allow_staging: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        """Validate caller-authored input and its independent storage policy."""
        if type(self.is_sensitive) is not bool or (self.data is not None and type(self.data) is not builtins.bytes):
            raise ValidationError("Execution input requires bytes and a sensitivity choice")
        if type(self.allow_staging) is not bool:
            raise ValidationError("Execution input staging choice must be a boolean")
        if self.data is None:
            _endpoint(self.source, "try_read")
        elif self.source is not None:
            raise ValidationError("Execution input requires exactly one source")

    @classmethod
    def eof(cls) -> Input:
        return cls(b"")

    @classmethod
    def bytes(cls, data: builtins.bytes, *, sensitive: bool = False, allow_staging: bool = True) -> Input:
        return cls(data, sensitive, allow_staging=allow_staging)

    @classmethod
    def sensitive(cls, data: builtins.bytes, *, allow_staging: bool = True) -> Input:
        return cls(data, True, allow_staging=allow_staging)

    @classmethod
    def live(cls, source: ByteSource, *, sensitive: bool = False) -> Input:
        return cls(None, sensitive, source=source)


@dataclass(frozen=True)
class Output:
    """Bounded capture, discard, or explicitly borrowed application sinks."""

    max_bytes: int | None
    stdout: ByteSink | None = field(default=None, repr=False, kw_only=True)
    stderr: ByteSink | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.max_bytes is not None and (type(self.max_bytes) is not int or self.max_bytes < 0):
            raise ValidationError("Execution capture requires a nonnegative byte bound")
        if self.stdout is not None or self.stderr is not None:
            if self.max_bytes is not None:
                raise ValidationError("Execution live output cannot also capture bytes")
            _endpoint(self.stdout, "try_write")
            _endpoint(self.stderr, "try_write")

    @classmethod
    def capture(cls, max_bytes: int = 4_096) -> Output:
        if type(max_bytes) is not int:
            raise ValidationError("Execution capture requires a nonnegative integer byte bound")
        return cls(max_bytes)

    @classmethod
    def discard(cls) -> Output:
        return cls(None)

    @classmethod
    def live(cls, stdout: ByteSink, stderr: ByteSink) -> Output:
        _endpoint(stdout, "try_write")
        _endpoint(stderr, "try_write")
        return cls(None, stdout=stdout, stderr=stderr)


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

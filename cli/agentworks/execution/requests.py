"""What a caller asks a VM to do.

Requests are plain data. Nothing here validates, encodes or sends: the one
function that turns a request into bytes, ``envelope.encode``, owns validation,
so every rule sits at the boundary where operator-authored input becomes a
wire message. Fields that can hold secrets or script text are ``repr=False``
so a request never leaks through a traceback or a log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

AccountShell = Literal["sh", "dash", "bash", "zsh"]
"""The login shells a managed account may use: the four the constant first-stage
command line is verified to parse under. The template ``shell`` fields use this
type, so the restriction is enforced where operator config is loaded."""


class Shell(StrEnum):
    """The interpreter a :class:`Script` runs under.

    ``SH`` and ``BASH`` are the fixed paths ``/bin/sh`` and ``/bin/bash``.
    ``USER_DEFAULT`` is the shell in the identity's passwd entry.
    """

    SH = "sh"
    BASH = "bash"
    USER_DEFAULT = "user_default"


@dataclass(frozen=True, kw_only=True)
class RequestOptions:
    """Options every request takes.

    ``env`` is added to the guest's rebuilt environment and may override any
    of it. ``cwd`` must be absolute, and ``None`` means the home directory.
    ``deadline`` bounds the local wait in seconds; it never claims the guest
    command stopped.
    """

    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None
    deadline: float | None = None


@dataclass(frozen=True)
class Command(RequestOptions):
    """A literal argv, run with no shell. ``argv[0]`` is searched in the guest's ``PATH``."""

    argv: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Script(RequestOptions):
    """Source text run by an interpreter that reads it from a file descriptor.

    The text is in no argument vector, and the interpreter's stdin stays the
    caller's. ``login`` adds ``-l`` so the interpreter runs the account's
    login startup first.
    """

    source: str = field(repr=False)
    shell: Shell
    login: bool = False


@dataclass(frozen=True)
class LoginShell(RequestOptions):
    """The identity's default shell as an interactive login shell.

    Valid only for ``attach``: ``run`` and ``stream`` refuse it, and ``attach``
    refuses one that carries a ``deadline``. A ``Script`` with an empty source
    would run the shell on an empty file and exit, which is not a session.
    """


Request = Command | Script | LoginShell


@dataclass(frozen=True)
class Forward:
    """One local-to-remote port forward for ``VMAccess.forward``."""

    local_port: int
    remote_port: int
    bind_address: str = "localhost"

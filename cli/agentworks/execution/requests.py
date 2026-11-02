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
    from collections.abc import Mapping, Sequence

AccountShell = Literal["sh", "dash", "bash", "zsh"]
"""The login shells a managed account may use: the four the constant first-stage
command line is verified to parse under. The template ``shell`` fields use this
type, so the restriction is enforced where operator config is loaded."""


def account_shell_basename(value: object) -> object:
    """Reduce a shell path to its basename, so ``/usr/bin/zsh`` is read as ``zsh``.

    The before-validator of the template ``shell`` fields. Anything that is not
    a path passes through unchanged for :data:`AccountShell` to judge, so a
    shell outside the list is refused by name at the config boundary.
    """
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else value


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
    """

    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None


@dataclass(frozen=True, kw_only=True)
class BoundedRequestOptions(RequestOptions):
    """The options of a request that runs to a result, which a deadline can bound.

    ``deadline`` bounds the local wait in seconds; it never claims the guest
    command stopped.
    """

    deadline: float | None = None


@dataclass(frozen=True)
class Command(BoundedRequestOptions):
    """A literal argv, run with no shell. ``argv[0]`` is searched in the guest's ``PATH``."""

    argv: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Script(BoundedRequestOptions):
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

    Valid only for ``attach``, and it takes no deadline: the session lasts as
    long as the operator keeps it. A ``Script`` with an empty source would run
    the shell on an empty file and exit, which is not a session.
    """


def shell_command_line(args: Sequence[str]) -> str:
    """Several arguments as one shell command line, every word single-quoted.

    Quoting every word, the first included, keeps the shell from reading any of
    them as syntax: ``shlex.quote`` leaves ``=ls`` bare, which zsh expands to a
    path, and a bare ``if``, ``time`` or ``FOO=bar`` is a keyword or an
    assignment. A quoted first word is still looked up as a builtin, function or
    program, so ``command -v`` keeps working.
    """
    return " ".join("'" + arg.replace("'", "'\"'\"'") + "'" for arg in args)


RunRequest = Command | Script
"""The requests ``run`` and ``stream`` take."""

Request = RunRequest | LoginShell
"""Every request kind; ``LoginShell`` is the one ``attach`` takes."""


@dataclass(frozen=True)
class Forward:
    """One local-to-remote port forward for ``VMAccess.forward``."""

    local_port: int
    remote_port: int
    bind_address: str = "localhost"

"""What a caller asks a VM to do.

Requests are plain data. Nothing here validates, encodes or sends: the one
function that turns a request into bytes, ``envelope.encode``, owns validation,
so every rule sits at the boundary where operator-authored input becomes a
wire message. Fields that can hold secrets or script text are ``repr=False``
so a request never leaks through a traceback or a log line.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CAPTURE_LIMIT_BYTES = 1_048_576
"""What :class:`CaptureOutput` keeps by default: 1 MiB of one stream."""

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


@dataclass(frozen=True)
class NoInput:
    """No input: the payload's stdin is at end of file once the request is sent."""


@dataclass(frozen=True)
class Bytes:
    """Input the client writes after the request and then closes: the payload's stdin, at its first byte.

    ``data`` is not in the ``repr``, and no operation log record or error
    holds it. Nothing bounds it but memory.
    """

    data: bytes = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class BoundedRequestOptions(RequestOptions):
    """The options of a request that runs to a result, which a deadline can bound.

    ``deadline`` bounds the local wait in seconds; it never claims the guest
    command stopped. ``elevated`` runs the whole operation as root under
    ``sudo -n``: it is the prefix of the connection's first-stage command, not
    part of the request message, and an agent-bound access refuses it. A
    :class:`LoginShell` has neither, so an elevated attach cannot be written.

    ``stdin`` is the payload's input under ``run``; ``stream`` takes the
    operator's stdin as its own argument and refuses a request that carries
    input. ``log_streams=False`` keeps both of the payload's streams out of
    the operation log, for a payload whose output is file content or a
    command line that must not be retained.
    """

    deadline: float | None = None
    elevated: bool = False
    stdin: NoInput | Bytes = NoInput()
    log_streams: bool = True


@dataclass(frozen=True)
class Command(BoundedRequestOptions):
    """A literal argv, run with no shell. ``argv[0]`` is searched in the guest's ``PATH``."""

    argv: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Script(BoundedRequestOptions):
    """Source text run by an interpreter that reads it from a file descriptor.

    The text is in no argument vector, and the interpreter's stdin stays the
    caller's. ``login`` adds ``-l`` so the interpreter runs the account's
    login startup first. ``interactive`` adds ``-i`` and has the interactive
    shell source the script, so its interactive startup runs first too.
    """

    source: str = field(repr=False)
    shell: Shell
    login: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class PythonProgram(BoundedRequestOptions):
    """Core's own Python source, run by the guest's ``/usr/bin/python3 -I -S`` from a file descriptor.

    The source is in no argument vector, and ``args`` are the program's
    arguments, ``sys.argv[1:]``. ``login`` and ``interactive`` run the
    identity's default shell with that startup first, which then execs the
    interpreter, so the program sees the environment a login or interactive
    shell builds.
    """

    source: str = field(repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    login: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class LoginShell(RequestOptions):
    """The identity's default shell as an interactive login shell.

    Valid only for ``attach``, and it takes no deadline: the session lasts as
    long as the operator keeps it. A ``Script`` with an empty source would run
    the shell on an empty file and exit, which is not a session.
    """


def shell_command_line(args: Sequence[str]) -> str:
    """Several arguments as one shell command line, as ``shlex.join`` writes it.

    The line is the one the account shell has always been given, so builtins,
    functions, keywords (``time``), aliases and a leading ``NAME=value``
    assignment keep working. The one hardening is for a word starting with
    ``=``: ``shlex.quote`` leaves ``=ls`` bare, which zsh expands to a path, so
    such a word is always single-quoted.
    """
    return " ".join(
        "'" + arg.replace("'", "'\"'\"'") + "'" if arg.startswith("=") else shlex.quote(arg) for arg in args
    )


RunRequest = Command | Script | PythonProgram
"""The requests ``run`` and ``stream`` take."""


@dataclass(frozen=True)
class CaptureOutput:
    """A ``run`` sink that keeps up to ``limit`` bytes of its stream; more is ``Unknown(OUTPUT_LIMIT)``."""

    limit: int = CAPTURE_LIMIT_BYTES


@dataclass(frozen=True)
class DiscardOutput:
    """A ``run`` sink that keeps nothing. The stream is still read to its closing marker, so the outcome stands."""


OutputSink = CaptureOutput | DiscardOutput
"""Where ``run`` puts one of the payload's streams."""

Request = RunRequest | LoginShell
"""Every request kind; ``LoginShell`` is the one ``attach`` takes."""


@dataclass(frozen=True)
class Forward:
    """One local-to-remote port forward for ``VMAccess.forward``."""

    local_port: int
    remote_port: int
    bind_address: str = "localhost"

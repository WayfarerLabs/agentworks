"""The operation log: one file per operation on a VM, and the record of each request in it.

:class:`OperationLogFile` is the log a command writes, under :data:`LOG_DIR`
as ``<vm>-<timestamp>-<stem>.log`` unless the caller names its path, and
:func:`vm_logs` finds a VM's logs by that exact shape. Every byte the file
receives passes through one sink that removes the secrets registered when
the log was built, raw, shell-quoted and normalized.

:func:`format_record` turns a request and its result into the block the log
writes. It reads no environment value, script or program source or input,
so no code path can write them: a record shows a request's shape, never its
secrets. A request with ``log_streams=False`` has neither of its streams
written either.
"""

from __future__ import annotations

import logging
import re
import shlex
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, NotDelivered
from agentworks.path_rendering import format_host_path
from agentworks.secrets.redaction import secret_redaction_forms

if TYPE_CHECKING:
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Completion, Result

LOG_DIR = Path.home() / ".config" / "agentworks" / "logs"
"""Where an operation log is written unless its caller names a path."""

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# The digits and the uppercase T and Z of _TIMESTAMP_FORMAT. A VM name is
# lowercase, so no name can contain this, and it anchors where one ends.
_TIMESTAMP_PATTERN = r"[0-9]{8}T[0-9]{6}Z"


def vm_logs(vm_name: str, *, stem: str | None = None) -> list[Path]:
    """``vm_name``'s logs under :data:`LOG_DIR`, newest first, or only its ``stem`` logs when given.

    A file counts only when its whole name is ``<vm_name>-<timestamp>-<stem>.log``,
    so VM ``dev`` never claims VM ``dev-2``'s logs, and only when it is a file
    or a link, which ``vm delete`` can unlink, never a directory so named. A
    missing directory has none.
    """
    if not LOG_DIR.is_dir():
        return []
    shape = re.compile(
        rf"{re.escape(vm_name)}-{_TIMESTAMP_PATTERN}-{re.escape(stem) if stem is not None else '.+'}\.log", re.ASCII
    )
    return sorted(
        (path for path in LOG_DIR.iterdir() if shape.fullmatch(path.name) and (path.is_file() or path.is_symlink())),
        reverse=True,
    )


class _PropagatingFileHandler(logging.FileHandler):
    """File handler that preserves the log's write-failure contract: a failed write raises."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802, ARG002
        raise


class OperationLogFile:
    """One operation's log, written to disk on every call so a partial log survives a crash.

    The secrets passed as ``redactions`` are registered before the header is
    written and cannot be added later, so no write can precede them. Writers
    in this process that share one log, such as stop-all's concurrent
    teardowns, take turns.
    """

    def __init__(
        self,
        vm_name: str,
        command_stem: str,
        *,
        redactions: tuple[str, ...] = (),
        path: Path | None = None,
    ) -> None:
        started = datetime.now(tz=UTC)
        self._path = path or LOG_DIR / f"{vm_name}-{started.strftime(_TIMESTAMP_FORMAT)}-{command_stem}.log"
        # Each write opens, writes and closes its own handler under the lock.
        # It orders nothing across processes.
        self._write_lock = threading.Lock()
        self._secrets = tuple(dict.fromkeys(secret for secret in redactions if secret))
        forms = dict.fromkeys(form for secret in self._secrets for form in secret_redaction_forms(secret))
        # Prefer the longest representation so an overlapping shorter token
        # cannot rewrite part of it before the complete value is matched.
        self._redact = tuple(sorted(forms, key=len, reverse=True))
        self._warnings: list[str] = []
        self._label = command_stem

        self._write(f"# Log: {vm_name} ({command_stem})\n# Started: {started.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")

    @property
    def path(self) -> Path:
        """The file this log writes."""
        return self._path

    @property
    def display_path(self) -> str:
        """:attr:`path` spelled the way an operator reads it (``~/...``), for a message that names the log.

        The log's body stays a verbatim transcript; only the name is rendered.
        """
        return format_host_path(self._path)

    @property
    def redactions(self) -> tuple[str, ...]:
        """The secrets registered at construction, for text this operation reports outside its log.

        A guest diagnostic quoted in an error never passes through the log's
        sink, so code that builds one sanitizes it with these.
        """
        return self._secrets

    def sanitize(self, text: str) -> str:
        """``text`` with every form of every registered secret replaced by ``[REDACTED]``."""
        for secret in self._redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def step(self, name: str) -> None:
        """Log the start of a named step, which labels the requests :meth:`record` writes after it."""
        self._label = name
        self._write(f"--- [{_clock()}] {name} ---\n")

    def record(self, request: RunRequest, result: Result) -> None:
        """Log one ``agentworks.execution`` request and its result, labeled with the latest step.

        The callable a command hands ``VMAccess`` as its ``log``. The record
        holds no environment value, script source or stdin, and goes through
        the same redaction as every other line.
        """
        self._write(f"[{_clock()}] " + format_record(request, result, label=self._label))

    def output(self, text: str) -> None:
        """Log general output."""
        if text:
            self._write(text if text.endswith("\n") else text + "\n")

    def warning(self, msg: str) -> None:
        """Keep ``msg`` for :attr:`warnings` and log its sanitized form."""
        self._warnings.append(msg)
        self._write(f"[{_clock()}] WARNING: {msg}\n")

    def log_error(self, msg: str) -> None:
        """Log an error message."""
        self._write(f"[{_clock()}] ERROR: {msg}\n")

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def has_warnings(self) -> bool:
        return bool(self._warnings)

    def close(self) -> None:
        """Write the footer, after the traceback of the exception in flight, if any.

        Operations close their log from inside their ``except`` blocks, so the
        error that ended an operation lands in its own log rather than only in
        the shared ``error.log``, which is the fallback for failures before
        any operation log exists.
        """
        exc_type, exc, exc_tb = sys.exc_info()
        if exc is not None:
            tb_text = "".join(traceback.format_exception(exc_type, exc, exc_tb))
            self._write(f"[{_clock()}] EXCEPTION:\n{tb_text}\n")

        lines = [f"\n# Finished: {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"]
        if self._warnings:
            lines.append(f"# Warnings: {len(self._warnings)}")
        self._write("\n".join(lines) + "\n")

    def _write(self, text: str) -> None:
        # Every byte that reaches the file handler passes through this choke
        # point. The guarantee covers the complete redaction set supplied at
        # construction, even when a caller passes those values in raw text.
        # It cannot cover a secret the caller failed to register before the
        # first incremental write. Raw and shell-quoted forms are registered
        # together, and late registration is intentionally unsupported.
        #
        # Keep the sanitized LogRecord and transient FileHandler together in
        # this function. CodeQL's clear-text-storage query recognizes this
        # local data flow; moving either side behind a helper reopens the alert
        # even though the runtime behavior would be equivalent.
        record = logging.LogRecord(
            name="agentworks.execution.oplog",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=self.sanitize(text),
            args=(),
            exc_info=None,
        )
        with self._write_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handler = _PropagatingFileHandler(self._path, mode="a", encoding="utf-8", errors="replace")
            try:
                handler.setFormatter(logging.Formatter("%(message)s"))
                handler.terminator = ""
                handler.handle(record)
            finally:
                handler.close()


def _clock() -> str:
    return datetime.now(tz=UTC).strftime("%H:%M:%S")


def format_record(request: RunRequest, result: Result, *, label: str) -> str:
    """The block for one request: its label and shape, the completion, both streams and ssh's diagnostics.

    ``label`` is the caller's step heading. The shape is the argv for a
    :class:`Command`, the shell and startup flags for a ``Script``, and the
    startup flags and arguments for a ``PythonProgram``, then whether it was
    elevated, the working directory, the environment's names
    and the deadline. A request with ``log_streams=False`` has a line saying
    so in place of its streams.
    """
    lines = [f"request ({label}): {_shape(request)}", f"outcome: {_completion(result.completion)}"]
    if not request.log_streams:
        lines.append("streams: not logged")
    else:
        for name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
            if data:
                # Only the stream's own last newline gives way to the line join, so the
                # block holds the stream's text whole: the sink's redaction matches a
                # secret that ends in line breaks only if every one of them is there.
                text = data.decode("utf-8", errors="replace")
                lines += [f"{name}:", text.removesuffix("\n")]
    if result.client_log_tail:
        lines += ["diagnostics:", result.client_log_tail]
    return "\n".join(lines) + "\n\n"


def _shape(request: RunRequest) -> str:
    if isinstance(request, Command):
        what = f"argv {shlex.join(request.argv)}"
    elif isinstance(request, Script):
        what = f"script ({', '.join([request.shell.value, *_startup(request.login, request.interactive)])})"
    else:
        startup = _startup(request.login, request.interactive)
        what = "python program" + (f" ({', '.join(startup)})" if startup else "")
        what += f", args {shlex.join(request.args)}" if request.args else ""
    parts = [
        what,
        *(["elevated"] if request.elevated else []),
        f"cwd {request.cwd or 'home'}",
        f"env [{', '.join(sorted(request.env))}]",
        "no deadline" if request.deadline is None else f"deadline {request.deadline:g}s",
    ]
    return ", ".join(parts)


def _startup(login: bool, interactive: bool) -> list[str]:
    return [*(["login"] if login else []), *(["interactive"] if interactive else [])]


def _completion(completion: Completion) -> str:
    if isinstance(completion, Completed):
        return f"completed, exit {completion.status}"
    if isinstance(completion, NotDelivered):
        code = f": {completion.code}" if completion.code else ""
        return f"not delivered ({completion.reason.value}{code})"
    return f"unknown ({completion.reason.value})"

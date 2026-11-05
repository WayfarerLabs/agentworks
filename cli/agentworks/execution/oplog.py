"""The operation log's record of one request.

:func:`format_record` turns a request and its result into the block an
operation log writes. It reads no environment value, script source or stdin,
so no code path can write them: a record shows a request's shape, never its
secrets. A request with ``log_streams=False`` has neither of its streams
written either. Writing the block, and redacting the secrets an operation
registered, is the log's own job, at its one sink.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, NotDelivered

if TYPE_CHECKING:
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Completion, Result


def format_record(request: RunRequest, result: Result, *, label: str) -> str:
    """The block for one request: its label and shape, the completion, both streams and ssh's diagnostics.

    ``label`` is the caller's step heading. The shape is the argv for a
    :class:`Command`, and the shell and login flag for a ``Script``, then
    whether it was elevated, the working directory, the environment's names
    and the deadline. A request with ``log_streams=False`` has a line saying
    so in place of its streams.
    """
    lines = [f"request ({label}): {_shape(request)}", f"outcome: {_completion(result.completion)}"]
    if not request.log_streams:
        lines.append("streams: not logged")
    else:
        for name, data in (("stdout", result.stdout), ("stderr", result.stderr)):
            if data:
                lines += [f"{name}:", data.decode("utf-8", errors="replace").rstrip("\n")]
    if result.client_log_tail:
        lines += ["diagnostics:", result.client_log_tail]
    return "\n".join(lines) + "\n\n"


def _shape(request: RunRequest) -> str:
    if isinstance(request, Command):
        what = f"argv {shlex.join(request.argv)}"
    else:
        what = f"script ({request.shell.value}{', login' if request.login else ''})"
    parts = [
        what,
        *(["elevated"] if request.elevated else []),
        f"cwd {request.cwd or 'home'}",
        f"env [{', '.join(sorted(request.env))}]",
        "no deadline" if request.deadline is None else f"deadline {request.deadline:g}s",
    ]
    return ", ".join(parts)


def _completion(completion: Completion) -> str:
    if isinstance(completion, Completed):
        return f"completed, exit {completion.status}"
    if isinstance(completion, NotDelivered):
        code = f": {completion.code}" if completion.code else ""
        return f"not delivered ({completion.reason.value}{code})"
    return f"unknown ({completion.reason.value})"

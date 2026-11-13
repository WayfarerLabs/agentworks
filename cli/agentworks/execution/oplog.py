"""The operation log's record of one request.

:func:`format_record` turns a request and its result into the block an
operation log writes. It reads no environment value, script or program source
or input, so no code path can write them: a record shows a request's shape,
never its secrets. A request with ``log_streams=False`` has neither of its
streams written either. Writing the block, and redacting the secrets an
operation registered, is the log's own job, at its one sink.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, NotDelivered

if TYPE_CHECKING:
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Completion, Result


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
                lines += [f"{name}:", data.decode("utf-8", errors="replace").rstrip("\n")]
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

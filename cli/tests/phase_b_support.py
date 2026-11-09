"""Phase B steps under test: a recording admin access whose file operations run on this machine.

:class:`StepGuest` holds a ``RecordingVMAccess`` and its ``Files`` (bounded as
Phase B bounds them). A file operation runs the real file program on the
test's own paths (``run_file_program_locally``), so a test reads what a step
left on disk; every other request is recorded and answers ``Completed(0)``
unless the test scripts it with :meth:`StepGuest.respond`, matched by
equality with the request the step builds. :class:`StepLog` stands in for the
operation log. Tests that run the file program are Linux only
(:data:`requires_file_program`).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.execution import files as files_module
from agentworks.execution.requests import PythonProgram
from agentworks.vms.initializer.steps import STEP_DEADLINE
from tests.vm_access_support import Admin, Call, RecordingVMAccess, run_file_program_locally

if TYPE_CHECKING:
    from agentworks.execution.files import Files
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result

requires_file_program = pytest.mark.skipif(
    sys.platform != "linux", reason="the file program walks paths with Linux's O_PATH"
)


@dataclass
class StepLog:
    """The operation log a step writes (``InitLog``): steps, output lines and warnings, kept in memory."""

    steps: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    redactions: tuple[str, ...] = ()
    display_path: str = "phase-b.log"

    def step(self, name: str) -> None:
        self.steps.append(name)

    def output(self, text: str) -> None:
        self.outputs.append(text)

    def record(self, request: object, result: object) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warned.append(msg)

    def sanitize(self, text: str) -> str:
        for secret in self.redactions:
            text = text.replace(secret, "[REDACTED]")
        return text

    @property
    def warnings(self) -> list[str]:
        return list(self.warned)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warned)


def is_file_operation(request: object) -> bool:
    # Read through the module: the suite's conftest sends the program with a private lock.
    return isinstance(request, PythonProgram) and request.source == files_module.program_source()


class StepGuest:
    """The admin's access and ``Files`` for a step under test."""

    def __init__(self, *, local_files: bool = True) -> None:
        """``local_files=False`` records file operations like any other request, running nothing."""
        self.access = RecordingVMAccess([], Admin())
        if local_files:
            self.access.answer = self._answer
        self.files: Files = self.access.files(deadline=STEP_DEADLINE)
        self.log = StepLog()

    def _answer(self, call: Call) -> Result | BaseException | None:
        return run_file_program_locally(call)

    def respond(self, request: RunRequest, outcome: Result | BaseException) -> None:
        """Answer ``request`` with ``outcome`` whenever the step asks exactly it; the latest answer wins."""
        self.access.responses.insert(0, (request, outcome))

    def commands(self) -> list[RunRequest]:
        """Every request but file operations, in order."""
        return [request for request in self.access.requests() if not is_file_operation(request)]

    def file_operations(self) -> list[tuple[str, dict[str, Any]]]:
        """``(operation, parameters)`` for every file operation, in order."""
        return [
            (request.args[0], json.loads(request.args[1]))
            for request in self.access.requests()
            if isinstance(request, PythonProgram) and is_file_operation(request)
        ]

    def writes(self) -> list[str]:
        """The paths of every operation that may change a file, in order."""
        return [params["path"] for operation, params in self.file_operations() if operation not in ("read", "stat")]

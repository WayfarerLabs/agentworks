"""The guest file program's test harness, shared by ``test_file_program.py`` and its sibling suites.

Each operation runs the program's ``main`` in its own interpreter, under each
interpreter the L lane uses, with its lock in a sticky, world-writable directory
of the test's own, so operations and the test can contend. Each suite imports
the ``guest`` and ``as_root`` fixtures by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import file_program, files
from tests.execution.conformance.lanes import python_interpreters, sudo_granted, unavailable

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAIN = (
    "import sys; sys.path.insert(0, sys.argv[1]); import file_program\n"
    "{patch}\n"
    "sys.exit(file_program.main(sys.argv[4:], lock_path=sys.argv[2], lock_wait=float(sys.argv[3])))"
)


@dataclass
class Guest:
    """The program, its lock and a working tree under one test's directory."""

    root: Path
    python: str
    lock_wait: float = 5.0

    @property
    def lock(self) -> Path:
        return self.root / "lock" / "agentworks-files.lock"

    @property
    def tree(self) -> Path:
        return self.root / "tree"

    def start(
        self,
        operation: str,
        params: dict,
        *,
        stdin: bytes = b"",
        patch: str = "",
        sudo: bool = False,
        umask: int | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start ``operation``, its ``SEALED`` parameters sent ahead of ``stdin`` as ``Files`` sends them."""
        sealed = file_program.SEALED.get(operation, ())
        if sealed:
            stdin = json.dumps({name: params[name] for name in sealed}).encode() + b"\n" + stdin
            params = {name: value for name, value in params.items() if name not in sealed}
        argv = [
            self.python,
            "-I",
            "-S",
            "-c",
            _MAIN.format(patch=patch),
            str(self.root / "program"),
            str(self.lock),
            str(self.lock_wait),
            operation,
            json.dumps(params),
        ]
        process = subprocess.Popen(
            ["sudo", "-n", "--", *argv] if sudo else argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=None if umask is None else (lambda: os.umask(umask)),  # noqa: PLW1509
        )
        assert process.stdin is not None
        process.stdin.write(stdin)
        process.stdin.close()
        return process

    def run(self, operation: str, params: dict, **options: object) -> subprocess.CompletedProcess[bytes]:
        process = self.start(operation, params, **options)  # type: ignore[arg-type]
        assert process.stdout is not None and process.stderr is not None
        # The program's streams are small, so reading one and then the other cannot stall it.
        stdout, stderr = process.stdout.read(), process.stderr.read()
        process.wait(timeout=30)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def write_if(
        self, path: Path, data: bytes, *, expected: str | None = None, **overrides: object
    ) -> subprocess.CompletedProcess[bytes]:
        params = {
            "path": str(path),
            "expected": expected,
            "mode": 0o600,
            "group": None,
            "parent_mode": 0o700,
            "preserve": False,
            "size": len(data),
            "sha256": digest(data),
        }
        options = {key: overrides.pop(key) for key in ("patch", "sudo", "umask") if key in overrides}
        return self.run("write_if", {**params, **overrides}, stdin=data, **options)

    def write(self, path: Path, data: bytes, **overrides: object) -> subprocess.CompletedProcess[bytes]:
        params = {
            "path": str(path),
            "mode": 0o600,
            "owner": None,
            "group": None,
            "parent_mode": 0o700,
            "size": len(data),
            "sha256": digest(data),
        }
        options = {key: overrides.pop(key) for key in ("patch", "sudo", "umask") if key in overrides}
        return self.run("write", {**params, **overrides}, stdin=data, **options)

    def ensure_directory(
        self, path: Path, *, mode: int, owner: str | None = None, group: str | None = None, **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        params = {"path": str(path), "mode": mode, "owner": owner, "group": group}
        return self.run("ensure_directory", params, **options)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def temporaries(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.startswith(".agentworks-tmp-"))


@pytest.fixture(params=sorted(python_interpreters()))
def guest(request: pytest.FixtureRequest, tmp_path: Path) -> Guest:
    program = tmp_path / "program"
    program.mkdir()
    (program / "file_program.py").write_text(files.program_source())
    (tmp_path / "lock").mkdir()
    (tmp_path / "lock").chmod(0o1777)
    (tmp_path / "tree").mkdir()
    return Guest(tmp_path, python_interpreters()[request.param])


@pytest.fixture
def as_root(guest: Guest) -> Iterator[None]:
    """The sudo lane: required where passwordless sudo is granted (CI), skipped elsewhere; root's files go after."""
    if not sudo_granted():
        unavailable("the sudo lane needs passwordless sudo")
    yield
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(guest.root)], check=False)

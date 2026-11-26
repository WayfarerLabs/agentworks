"""Builders, hand-written records and subprocess holders for the native lock primitive's tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agentworks.native_locks import (
    Entity,
    LockMode,
    LockRequest,
    account_entity,
    operation_label,
    session_entity,
    workspace_entity,
)
from agentworks.native_locks._files import holders_directory
from agentworks.native_locks._os import try_lock, unlock
from agentworks.native_locks._records import RECORD_LOCK_OFFSET

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

VM = "dev"

# A holder in another process: it acquires one request, prints "held", and
# holds it until its stdin closes or it is killed.
_HOLDER_SCRIPT = """
import json, sys
from pathlib import Path
from agentworks import native_locks as nl

spec = json.loads(sys.argv[2])
vm = spec["vm"]
workspace = nl.workspace_entity(vm, spec["workspace"])
if spec["session"] is None:
    entity = workspace
else:
    account = nl.account_entity(vm, spec["account"])
    entity = nl.session_entity(spec["session"], workspace=workspace, account=account)
if spec["mode"] == "exclusive":
    request = nl.LockRequest(exclusive=(entity,))
else:
    request = nl.LockRequest(shared=(entity,))
nl.operation_label.set(tuple(spec["label"]))
with nl.native_locks(Path(sys.argv[1]), request):
    print("held", flush=True)
    sys.stdin.read()
"""

# Every release since v0.19.0, inline: the vm file locked exclusive with
# flock, or with msvcrt.locking on byte 0 after seek(0). It prints "held",
# or "busy <errno>" when refused.
_OLD_RELEASE_SCRIPT = """
import hashlib, os, sys
from pathlib import Path

database = Path(sys.argv[1]).resolve()
directory = database.parent / f".{database.name}.native-locks"
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
lock_file = (directory / hashlib.sha256(sys.argv[2].encode()).hexdigest()).open("a+b")
if lock_file.seek(0, os.SEEK_END) == 0:
    lock_file.write(b"\\0")
    lock_file.flush()
lock_file.seek(0)
try:
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as error:
    print("busy", error.errno, flush=True)
    sys.exit(0)
print("held", flush=True)
sys.stdin.read()
"""


def workspace(name: str = "w") -> Entity:
    return workspace_entity(VM, name)


def account(name: str = "a") -> Entity:
    return account_entity(VM, name)


def session(name: str = "s", *, workspace_name: str = "w", account_name: str = "a") -> Entity:
    return session_entity(name, workspace=workspace(workspace_name), account=account(account_name))


def request_for(entity: Entity, mode: LockMode) -> LockRequest:
    if mode is LockMode.EXCLUSIVE:
        return LockRequest(exclusive=(entity,))
    return LockRequest(shared=(entity,))


def records(database: Path, vm: str = VM) -> list[Path]:
    return sorted(holders_directory(database.resolve(), vm).glob("*.json"))


@contextmanager
def labelled(*words: str) -> Iterator[None]:
    token = operation_label.set(("agw", *words))
    try:
        yield
    finally:
        operation_label.reset(token)


@contextmanager
def fake_record(database: Path, payload: object, *, live: bool, name: str = "0123456789abcdef") -> Iterator[Path]:
    """A record written by hand: held open and locked by this test when ``live``, as a live holder would.

    A record that is not live is left closed, as a dead holder leaves it.
    """
    directory = holders_directory(database.resolve(), VM)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    path.write_bytes(content)
    if not live:
        yield path
        return
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        assert try_lock(fd, LockMode.EXCLUSIVE, offset=RECORD_LOCK_OFFSET)
        try:
            yield path
        finally:
            unlock(fd, offset=RECORD_LOCK_OFFSET)
    finally:
        os.close(fd)


def record_payload(key: str, mode: str, *, acquired: float, command: tuple[str, ...] = ("agw", "fake")) -> object:
    return {"pid": 1, "command": list(command), "acquired": acquired, "entities": [{"key": key, "mode": mode}]}


@contextmanager
def holder_process(
    database: Path,
    *,
    mode: LockMode,
    workspace_name: str = "w",
    session_name: str | None = None,
    label: tuple[str, ...] = ("agw", "other", "holder"),
) -> Iterator[subprocess.Popen[str]]:
    """Run a holder of one workspace or session in another process until the block ends."""
    spec = {
        "vm": VM,
        "workspace": workspace_name,
        "session": session_name,
        "account": "a",
        "mode": "exclusive" if mode is LockMode.EXCLUSIVE else "shared",
        "label": list(label),
    }
    with _script(_HOLDER_SCRIPT, str(database), json.dumps(spec)) as process:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        yield process


@contextmanager
def old_release_process(database: Path) -> Iterator[subprocess.Popen[str]]:
    """Run today's single-lock algorithm on the VM's file in another process."""
    with _script(_OLD_RELEASE_SCRIPT, str(database), VM) as process:
        yield process


@contextmanager
def _script(script: str, *arguments: str) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        process.kill()
        process.communicate(timeout=15)

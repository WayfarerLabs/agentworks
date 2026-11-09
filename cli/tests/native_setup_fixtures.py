"""Actual native setup helpers exercised against isolated local files and CLIs."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.artifacts.session import HOME_PROBE
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.db import VMRow
from agentworks.execution.access import VMAccess
from agentworks.execution.files import Files
from agentworks.execution.oplog import format_record
from agentworks.execution.requests import Bytes, Command, PythonProgram, Script, Shell
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.model import NativeClaim, SetupRecord
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeTool
from agentworks.ssh import SSHResult
from agentworks.transports import Transport
from tests.vm_access_support import (
    Admin,
    Call,
    RecordingVMAccess,
    account_shell,
    check_result,
    is_command_check,
    local_vm_access,
    run_file_program_locally,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.requests import RunRequest


class LocalFixtureTransport(Transport):
    """Execute only fixture commands under a newly constructed child environment."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        (root / "tmp").mkdir()
        # A fixture-owned login shell sources only this fixture's profile. Using
        # the host login shell here could read the operator's startup files.
        login_shell = root / "login-shell"
        login_shell.write_text(
            '#!/bin/bash\n[ "$1" = "-lc" ] || exit 9\n. "$HOME/.profile"\nexec /bin/bash --noprofile --norc -c "$2"\n'
        )
        login_shell.chmod(0o700)
        (self.home / ".profile").write_text('export PATH="$HOME/.local/bin:$PATH"\n')
        self.environment = {
            "PATH": str(root / "bin") + ":/usr/bin:/bin",
            "HOME": str(self.home),
            "SHELL": str(login_shell),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "TMPDIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.logger = None
        self.default_timeout = 30
        self.logged_output: list[str] = []

    def describe(self) -> str:
        return "isolated local native fixture"

    def run(self, command: str, **kwargs) -> SSHResult:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.home,
            env={**self.environment, **(kwargs.get("env") or {})},
            input=kwargs.get("input_text", kwargs.get("input_data", "")),
            text=True,
            capture_output=True,
            timeout=kwargs.get("timeout") or 30,
        )
        hidden = kwargs.get("input_text") is not None or kwargs.get("discard_output")
        stdout = "" if hidden else result.stdout
        stderr = "" if hidden else result.stderr
        self.logged_output.extend([stdout, stderr])
        return SSHResult(returncode=result.returncode, stdout=stdout, stderr=stderr)

    def copy_to(self, local_path, remote_path, **kwargs) -> None:
        shutil.copyfile(local_path, remote_path)

    def copy_from(self, remote_path, local_path, **kwargs) -> None:
        shutil.copyfile(remote_path, local_path)

    def _interactive(self, command: str, **kwargs) -> int:
        raise AssertionError("interactive operations are outside fixture scope")

    def call_streaming(self, command: str, **kwargs) -> int:
        raise AssertionError("streaming operations are outside fixture scope")

    def answer(self, call: Call) -> Result | None:
        """Answer an access's request as this fixture's account would, or ``None`` for the default outcome.

        File operations run their real program here (``run_file_program_locally``),
        the session home probe reads this fixture's home, and every required
        command is present. Any other ``Command``, a bash or sh ``Script`` and
        a ``PythonProgram`` run here, in the request's working directory or the
        fixture's home, with the fixture's environment and the request's over
        it; a login program runs after the fixture's login shell reads its
        ``.profile``. Each answered request's operation log record joins
        ``logged_output``.
        """
        request = call.request
        if is_command_check(request):
            return check_result(request, 0)
        if request == HOME_PROBE:
            return Result(Completed(0), str(self.home).encode())
        result = run_file_program_locally(call)
        if result is None and isinstance(request, Command):
            result = self._spawn(list(request.argv), request)
        elif result is None and isinstance(request, Script) and request.shell in (Shell.BASH, Shell.SH):
            result = self._spawn([request.shell.value, "-c", request.source], request)
        elif result is None and isinstance(request, PythonProgram):
            program = shlex.join([sys.executable, "-I", "-S", "-c", request.source, *request.args])
            result = self._spawn(
                [self.environment["SHELL"], "-lc", program] if request.login else shlex.split(program), request
            )
        if result is not None:
            self.logged_output.append(format_record(request, result, label="fixture"))
        return result

    def _spawn(self, argv: list[str], request: Command | Script | PythonProgram) -> Result:
        completed = subprocess.run(
            argv,
            cwd=request.cwd or self.home,
            env={**self.environment, **request.env},
            input=request.stdin.data if isinstance(request.stdin, Bytes) else b"",
            capture_output=True,
            timeout=request.deadline or 120,
            check=False,
        )
        return Result(Completed(completed.returncode), completed.stdout, completed.stderr)

    def access(self) -> RecordingVMAccess:
        """A recording access to this fixture's account, answered by :meth:`answer`."""
        access = RecordingVMAccess([], Admin())
        access.answer = self.answer
        return access

    def files(self) -> Files:
        """File operations on this fixture's files, through :meth:`access`."""
        return self.access().files()


def fixture_access(
    monkeypatch: pytest.MonkeyPatch,
    transport: LocalFixtureTransport,
    *,
    log: Callable[[RunRequest, Result], None] | None = None,
) -> VMAccess:
    """A real ``VMAccess`` to the fixture's account: its home, and a login shell that reads only its ``.profile``."""
    return local_vm_access(monkeypatch, home=transport.home, shell=account_shell(transport.root), log=log)


def invocation(
    transport: LocalFixtureTransport,
    claims: list[tuple[NativeClaim, ...]],
    *,
    prior=None,
    env=None,
    access: VMAccess | None = None,
):
    """User setup of the fixture's account, through ``access`` (the fixture's recording access by default)."""
    access = access or transport.access()
    return UserSetupInvocation(
        vm=cast(VMRow, object()),
        access=access,
        files=access.files(),
        prior=prior,
        checkpoint=claims.append,
        username="fixture",
        home=str(transport.home),
        environment=env or {},
    )


def native_cli(
    transport: LocalFixtureTransport, tool: NativeTool, config_root: Path | str, *, env=None, access=None
) -> NativeCLI:
    """A native CLI for the fixture's account, to use as a context manager."""
    return NativeCLI(tool, invocation(transport, [], env=env, access=access), config_root=str(config_root))


def record(claims: tuple[NativeClaim, ...], tool: NativeTool) -> SetupRecord:
    return SetupRecord(
        component="agent",
        integration="claude-code" if tool == "claude" else "codex",
        destination_id="a" * 64,
        declaration={},
        claims=claims,
    )


def market_fixture(transport: LocalFixtureTransport, tool: NativeTool) -> Path:
    binary = shutil.which(tool)
    if binary is None:
        pytest.skip(f"{tool} CLI is not installed")
    bindir = transport.root / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / tool).symlink_to(Path(binary).resolve())
    # npm-installed launchers need Node in the fixture's deliberately small PATH.
    node = shutil.which("node")
    if node is not None and not (bindir / "node").exists():
        (bindir / "node").symlink_to(Path(node).resolve())
    root = transport.root / "market"
    manifest = root / (".agents/plugins/marketplace.json" if tool == "codex" else ".claude-plugin/marketplace.json")
    manifest.parent.mkdir(parents=True)
    entries = []
    for name in ["one", "two"]:
        plugin = root / "plugins" / name / (".codex-plugin" if tool == "codex" else ".claude-plugin") / "plugin.json"
        plugin.parent.mkdir(parents=True)
        plugin.write_text(json.dumps({"name": name, "version": "1.0.0"}))
        entries.append(
            {
                "name": name,
                "source": {"source": "local", "path": f"./plugins/{name}"} if tool == "codex" else f"./plugins/{name}",
            }
        )
    manifest.write_text(json.dumps({"name": "fixture-market", "owner": {"name": "Fixture"}, "plugins": entries}))
    return root

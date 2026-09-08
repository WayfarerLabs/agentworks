"""Actual native setup helpers exercised against isolated local files and CLIs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from agentworks.capabilities.harness_integration.native_cli import NativeTool
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
from agentworks.db import VMRow
from agentworks.harness_setup.model import NativeClaim, SetupRecord
from agentworks.ssh import SSHResult
from agentworks.transports import Transport


class LocalFixtureTransport(Transport):
    """Execute only fixture commands under a newly constructed child environment."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        (root / "tmp").mkdir()
        self.environment = {
            "PATH": str(root / "bin") + ":/usr/bin:/bin",
            "HOME": str(self.home),
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
        self.commands: list[str] = []
        self.logged_output: list[str] = []

    def describe(self) -> str:
        return "isolated local native fixture"

    def run(self, command: str, **kwargs) -> SSHResult:
        self.commands.append(command)
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.home,
            env={**self.environment, **kwargs.get("env", {})},
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


def invocation(transport: LocalFixtureTransport, claims: list[tuple[NativeClaim, ...]], *, prior=None, env=None):
    return UserSetupInvocation(
        vm=cast(VMRow, object()),
        runner=transport,
        prior=prior,
        checkpoint=claims.append,
        username="fixture",
        home=str(transport.home),
        environment=env or {},
    )


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

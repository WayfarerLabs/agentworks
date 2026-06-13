"""Tests for SSH SetEnv argument construction.

The env-and-secrets SDD's Phase 3 SetEnv pivot routes env injection through
the SSH protocol's SetEnv mechanism (`-o SetEnv=KEY=VALUE` per pair on the
client side, accepted by the remote sshd's `AcceptEnv *` directive). These
tests pin the client-side construction.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from agentworks.ssh import ExecTarget, SSHTarget, _ssh_base_args, interactive, run


def _target() -> SSHTarget:
    return SSHTarget(host="vm.tailnet", user="agentworks")


def test_base_args_no_env_omits_set_env_flag() -> None:
    args = _ssh_base_args(_target())
    assert not any(a.startswith("SetEnv=") for a in args)
    assert "-o" in args
    # Sanity: the base args still include the host as the last positional.
    assert args[-1] == "agentworks@vm.tailnet"


def test_base_args_with_env_emits_one_set_env_per_pair() -> None:
    args = _ssh_base_args(
        _target(),
        env={"AGENTWORKS_SESSION": "s1", "EDITOR": "nvim"},
    )
    set_env_args = [
        args[i + 1] for i, a in enumerate(args)
        if a == "-o" and i + 1 < len(args) and args[i + 1].startswith("SetEnv=")
    ]
    assert "SetEnv=AGENTWORKS_SESSION=s1" in set_env_args
    assert "SetEnv=EDITOR=nvim" in set_env_args


def test_base_args_set_env_precedes_host() -> None:
    """SetEnv -o flags must appear before the user@host positional; otherwise
    OpenSSH parses them as part of the remote command."""
    args = _ssh_base_args(_target(), env={"K": "v"})
    host_index = args.index("agentworks@vm.tailnet")
    set_env_indices = [i for i, a in enumerate(args) if a.startswith("SetEnv=")]
    assert all(i < host_index for i in set_env_indices)


def test_base_args_empty_env_dict_omits_set_env_flag() -> None:
    """``env={}`` is treated the same as ``env=None``: no SetEnv flags."""
    args = _ssh_base_args(_target(), env={})
    assert not any(a.startswith("SetEnv=") for a in args)


def test_base_args_with_env_value_containing_spaces() -> None:
    """SetEnv values containing spaces flow through verbatim; OpenSSH receives
    them as a single argv element (no shell parsing happens at this layer)."""
    args = _ssh_base_args(_target(), env={"GREET": "hello world"})
    set_env_values = [a for a in args if a.startswith("SetEnv=")]
    assert "SetEnv=GREET=hello world" in set_env_values


def test_run_threads_env_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh.run with env=... ends up with SetEnv flags on the subprocess argv."""
    captured: dict[str, Any] = {}

    class _CompletedStub:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(args: list[str], **kwargs: object) -> _CompletedStub:
        captured["args"] = args
        return _CompletedStub()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    run(_target(), "true", env={"AGENTWORKS_VM": "vm-1"})
    assert "SetEnv=AGENTWORKS_VM=vm-1" in captured["args"]


def test_interactive_threads_env_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh.interactive with env=... ends up with SetEnv flags on the
    subprocess argv (matches ssh.run shape)."""
    captured: dict[str, Any] = {}

    def fake_call(args: list[str]) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    interactive(_target(), "tmux attach", env={"AGENTWORKS_SESSION": "s1"})
    assert "SetEnv=AGENTWORKS_SESSION=s1" in captured["args"]


def test_exec_target_run_threads_env_to_ssh_run() -> None:
    """ExecTarget.run(env=...) passes env down to ssh.run, which materializes
    SetEnv flags on the SSH command line."""
    target = ExecTarget(ssh=_target())
    captured_env: dict[str, dict[str, str] | None] = {}

    class _CompletedStub:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(args: list[str], **kwargs: object) -> _CompletedStub:  # noqa: ARG001
        captured_env["argv"] = args  # type: ignore[assignment]
        return _CompletedStub()

    with patch.object(subprocess, "run", fake_subprocess_run):
        target.run("true", env={"AGENTWORKS_AGENT": "claude"})

    args = captured_env["argv"]
    assert "SetEnv=AGENTWORKS_AGENT=claude" in args  # type: ignore[operator]

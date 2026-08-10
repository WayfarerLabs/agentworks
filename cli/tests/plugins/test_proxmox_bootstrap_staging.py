"""Private, bounded Proxmox guest-agent bootstrap staging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ProvisioningError
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

_AUTH_KEY = "tskey-proxmox-file-sentinel"
_SCRIPT = "#!/bin/sh\nTAILSCALE_AUTH_KEY='" + _AUTH_KEY + "'\n"
_PATH = "/tmp/agentworks-bootstrap-" + "b" * 32 + ".sh"
_SUCCESS = object()


def _assert_exception_graph_is_value_free(failure: BaseException) -> None:
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert _AUTH_KEY not in repr(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


class _GuestAgent:
    def __init__(
        self,
        *,
        write_failure: BaseException | None = None,
        execute_result: object = _SUCCESS,
        removal_failure: BaseException | None = None,
    ) -> None:
        self.write_failure = write_failure
        self.execute_result = execute_result
        self.removal_failure = removal_failure
        self.files: dict[str, str] = {}
        self.modes: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def guest_agent_exec_wait(
        self,
        node: str,
        vmid: int,
        command: str,
        args: list[str] | None = None,
        *,
        timeout: int = 60,
    ) -> dict[str, object] | None:
        del node, vmid, timeout
        argv = tuple(args or ())
        self.calls.append((command, argv))
        if command == "/usr/bin/install":
            path = argv[-1]
            self.files[path] = ""
            self.modes[path] = 0o600
            return {"exitcode": 0, "out-data": ""}
        if command == "/bin/bash":
            if isinstance(self.execute_result, BaseException):
                raise self.execute_result
            if self.execute_result is _SUCCESS:
                return {
                    "exitcode": 0,
                    "out-data": "##STEP## Tailscale\n##SUCCESS## tailscale-ip=100.64.0.7\n",
                }
            assert self.execute_result is None or isinstance(self.execute_result, dict)
            return self.execute_result
        if command == "/bin/sh":
            if self.removal_failure is not None:
                raise self.removal_failure
            path = argv[-1]
            self.files.pop(path, None)
            self.modes.pop(path, None)
            return {"exitcode": 0, "out-data": ""}
        raise AssertionError(f"unexpected command: {command}")

    def guest_agent_file_write(self, node: str, vmid: int, path: str, content: str) -> None:
        del node, vmid
        assert self.modes[path] == 0o600
        if self.write_failure is not None:
            raise self.write_failure
        self.files[path] = content


def _platform(monkeypatch: pytest.MonkeyPatch, api: _GuestAgent) -> ProxmoxPlatform:
    monkeypatch.setattr(
        "agentworks.plugins.proxmox.platform.uuid.uuid4",
        lambda: SimpleNamespace(hex="b" * 32),
    )
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: api)
    return ProxmoxPlatform(
        "pve",
        {
            "api_url": "https://pve.example:8006",
            "node": "pve1",
            "token_id": "agw@pam!token",
            "template_vmid": 9000,
        },
    )


def test_success_stages_mode_0600_and_removes_verified_path(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _GuestAgent()

    result = _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert result == "100.64.0.7"
    assert api.files == {}
    assert api.modes == {}
    assert api.calls[0] == ("/usr/bin/install", ("-m", "600", "/dev/null", _PATH))
    assert api.calls[1] == ("/bin/bash", (_PATH,))
    assert api.calls[2] == (
        "/bin/sh",
        (
            "-c",
            'rm -f -- "$1" && test ! -e "$1"',
            "agentworks-bootstrap-cleanup",
            _PATH,
        ),
    )
    assert _AUTH_KEY not in repr(api.calls)


def test_write_failure_is_safe_and_removes_private_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _GuestAgent(write_failure=ProxmoxAPIError(f"reflected {_AUTH_KEY}"))

    with pytest.raises(ProxmoxAPIError) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert str(caught.value) == "Proxmox guest-agent bootstrap file write failed"
    assert api.files == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_exception_graph_is_value_free(caught.value)


def test_failure_output_is_not_repeated_to_observable_error_text(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    api = _GuestAgent(
        execute_result={
            "exitcode": 1,
            "out-data": f"##STEP## Tailscale\n##ERROR## rejected {_AUTH_KEY}\n",
            "err-data": f"stderr reflected {_AUTH_KEY}",
        }
    )

    with pytest.raises(ProvisioningError, match=r"bootstrap failed \(exit 1\)") as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert warnings == []
    assert _AUTH_KEY not in repr(warnings)
    _assert_exception_graph_is_value_free(caught.value)
    assert api.files == {}


def test_forged_success_ip_is_not_returned_or_repeated(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    api = _GuestAgent(
        execute_result={
            "exitcode": 0,
            "out-data": f"##STEP## Tailscale\n##SUCCESS## tailscale-ip={_AUTH_KEY}\n",
            "err-data": "",
        }
    )

    with pytest.raises(ProvisioningError, match=r"bootstrap failed \(exit 0\)") as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert warnings == []
    assert _AUTH_KEY not in repr(warnings)
    _assert_exception_graph_is_value_free(caught.value)
    assert api.files == {}


@pytest.mark.parametrize(
    "execute_result",
    [
        ProxmoxAPIError("execution failed"),
        None,
        KeyboardInterrupt("stop"),
        SystemExit("exit"),
        GeneratorExit("close"),
    ],
    ids=("failure", "timeout", "interrupt", "system-exit", "generator-exit"),
)
def test_execute_unwind_removes_stage(
    monkeypatch: pytest.MonkeyPatch,
    execute_result: dict[str, object] | None | BaseException,
) -> None:
    api = _GuestAgent(execute_result=execute_result)
    platform = _platform(monkeypatch, api)

    if isinstance(execute_result, BaseException):
        with pytest.raises(type(execute_result)) as caught:
            platform._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())
        assert caught.value is execute_result
    elif execute_result is None:
        with pytest.raises(ProvisioningError, match="bootstrap timed out"):
            platform._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())
    else:
        raise AssertionError("unexpected non-exception execute result")

    assert api.files == {}
    assert api.modes == {}


def test_standalone_removal_failure_surfaces_and_models_residue(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _GuestAgent(removal_failure=ProxmoxAPIError("remove failed"))

    with pytest.raises(ProxmoxAPIError) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert str(caught.value) == "could not verify removal of the Proxmox bootstrap staging file"
    assert api.files == {_PATH: _SCRIPT}
    assert _AUTH_KEY not in repr(caught.value)


@pytest.mark.parametrize(
    "removal_failure",
    [
        KeyboardInterrupt("cleanup interrupted"),
        SystemExit("cleanup exited"),
        GeneratorExit("cleanup closed"),
    ],
    ids=("interrupt", "exit", "generator-exit"),
)
def test_standalone_removal_control_flow_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    removal_failure: BaseException,
) -> None:
    api = _GuestAgent(removal_failure=removal_failure)

    with pytest.raises(type(removal_failure)) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert caught.value is removal_failure
    assert api.files == {_PATH: _SCRIPT}


def test_removal_failure_does_not_mask_primary(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    failure = ProxmoxAPIError("execution failed")
    api = _GuestAgent(
        execute_result=failure,
        removal_failure=ProxmoxAPIError("remove failed"),
    )

    with pytest.raises(ProxmoxAPIError) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert caught.value is failure
    assert api.files == {_PATH: _SCRIPT}
    assert warnings == [
        "could not verify removal of the Proxmox bootstrap staging file; "
        "primary failure unchanged; plaintext may remain"
    ]
    assert _AUTH_KEY not in repr(warnings)


@pytest.mark.parametrize(
    "removal_failure",
    [
        KeyboardInterrupt("cleanup interrupted"),
        SystemExit("cleanup exited"),
        GeneratorExit("cleanup closed"),
    ],
    ids=("interrupt", "exit", "generator-exit"),
)
def test_removal_control_flow_does_not_mask_primary(
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    removal_failure: BaseException,
) -> None:
    primary = ProxmoxAPIError("execution failed")
    api = _GuestAgent(
        execute_result=primary,
        removal_failure=removal_failure,
    )

    with pytest.raises(ProxmoxAPIError) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert caught.value is primary
    assert api.files == {_PATH: _SCRIPT}
    assert warnings == [
        "could not verify removal of the Proxmox bootstrap staging file; "
        "primary failure unchanged; plaintext may remain"
    ]


def test_warning_failure_does_not_mask_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = ProxmoxAPIError("execution failed")
    warning_failure = RuntimeError("warning sink failed")
    api = _GuestAgent(
        execute_result=primary,
        removal_failure=ProxmoxAPIError("remove failed"),
    )

    def _fail_warning(message: str) -> None:
        del message
        raise warning_failure

    monkeypatch.setattr("agentworks.plugins.proxmox.platform.output.warn", _fail_warning)

    with pytest.raises(ProxmoxAPIError) as caught:
        _platform(monkeypatch, api)._run_bootstrap_via_agent("pve1", 100, _SCRIPT, RunContext())

    assert caught.value is primary
    assert api.files == {_PATH: _SCRIPT}

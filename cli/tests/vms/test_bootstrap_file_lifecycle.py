"""Durable file lifecycle for the WSL2 Phase A bootstrap."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ProvisioningError
from agentworks.ssh import SSHError, SSHLogger, SSHResult
from agentworks.vms.initializer import driver

_AUTH_KEY = "tskey-bootstrap-file-sentinel"
_SUCCESS_OUTPUT = "\n".join(
    (
        "##STEP## Tailscale",
        "##SUCCESS## tailscale-ip=100.64.0.9",
        "",
    )
)


class _RecordingTransport:
    def __init__(
        self,
        *,
        copy_failure: BaseException | None = None,
        execute_failure: BaseException | None = None,
        execute_result: SSHResult | None = None,
        cleanup_failure: BaseException | None = None,
    ) -> None:
        self.copy_failure = copy_failure
        self.execute_failure = execute_failure
        self.execute_result = execute_result
        self.cleanup_failure = cleanup_failure
        self.guest_files: dict[str, bytes] = {}
        self.guest_modes: dict[str, int] = {}
        self.local_paths: list[Path] = []
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.commands.append(command)
        if command.startswith("install -m 600"):
            path = command.rsplit(" ", 1)[1]
            self.guest_files[path] = b""
            self.guest_modes[path] = 0o600
            return SSHResult(returncode=0, stdout="", stderr="")
        if command.startswith("setsid sudo"):
            if self.execute_failure is not None:
                raise self.execute_failure
            return self.execute_result or SSHResult(returncode=0, stdout=_SUCCESS_OUTPUT, stderr="")
        if command.startswith("rm -f --"):
            path = command.split(" ")[3]
            if self.cleanup_failure is not None:
                raise self.cleanup_failure
            self.guest_files.pop(path, None)
            self.guest_modes.pop(path, None)
            return SSHResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    def copy_to(self, local_path: str | Path, remote_path: str, **kwargs: object) -> None:
        del kwargs
        path = Path(local_path)
        self.local_paths.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert self.guest_modes[remote_path] == 0o600
        if self.copy_failure is not None:
            raise self.copy_failure
        self.guest_files[remote_path] = path.read_bytes()


def _call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: _RecordingTransport,
    *,
    logger: SSHLogger | None = None,
) -> str:
    public_key = tmp_path / "id.pub"
    public_key.write_text("ssh-ed25519 AAAA test\n")
    monkeypatch.setattr(
        "agentworks.vms.initializer.driver.uuid.uuid4",
        lambda: SimpleNamespace(hex="a" * 32),
    )
    return driver._run_bootstrap_script(
        logger if logger is not None else MagicMock(),
        SimpleNamespace(operator=SimpleNamespace(ssh_public_key=public_key)),
        SimpleNamespace(),
        "vm1",
        transport,  # type: ignore[arg-type]
        "agentworks",
        "wsl2--vm1",
        MagicMock(),
        tailscale_auth_key=_AUTH_KEY,
        script_swap=0,
    )


def _assert_no_residue(transport: _RecordingTransport) -> None:
    assert transport.guest_files == {}
    assert transport.guest_modes == {}
    assert transport.local_paths
    assert all(not path.exists() for path in transport.local_paths)


def test_success_uses_private_random_stages_and_removes_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordingTransport()

    assert _call(tmp_path, monkeypatch, transport) == "100.64.0.9"

    _assert_no_residue(transport)
    surfaced = repr(transport.commands)
    assert _AUTH_KEY not in surfaced
    guest_path = "/tmp/agentworks-bootstrap-" + "a" * 32 + ".sh"
    assert guest_path in surfaced
    assert transport.commands[-1] == f"rm -f -- {guest_path} && test ! -e {guest_path}"


def test_failure_output_is_redacted_from_durable_log_and_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import ssh

    monkeypatch.setattr(ssh, "LOG_DIR", tmp_path / "logs")
    logger = SSHLogger("vm1", "vm-create", redactions=(_AUTH_KEY,))
    transport = _RecordingTransport(
        execute_result=SSHResult(
            returncode=1,
            stdout=f"bootstrap failed near {_AUTH_KEY}\n",
            stderr="",
        )
    )

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport, logger=logger)
    logger.close()

    assert str(caught.value) == "Bootstrap script failed (exit 1)"
    assert _AUTH_KEY not in repr(caught.value)
    assert _AUTH_KEY not in logger.path.read_text()
    _assert_no_residue(transport)


class _FailingTempFile:
    def __init__(self, path: Path, failure: OSError) -> None:
        self.name = str(path)
        self._failure = failure
        path.touch(mode=0o600)

    def __enter__(self) -> _FailingTempFile:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def write(self, content: bytes) -> None:
        del content
        raise self._failure


def test_local_write_failure_removes_both_stages_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("write failed")
    local_path = tmp_path / "bootstrap.sh"
    monkeypatch.setattr(
        "agentworks.vms.initializer.driver.tempfile.NamedTemporaryFile",
        lambda **kwargs: _FailingTempFile(local_path, failure),
    )
    transport = _RecordingTransport()

    with pytest.raises(OSError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    assert not local_path.exists()
    assert transport.guest_files == {}


def test_copy_failure_removes_both_stages_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = SSHError("copy failed")
    transport = _RecordingTransport(copy_failure=failure)

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    _assert_no_residue(transport)


@pytest.mark.parametrize(
    "failure",
    [
        SSHError("execution failed"),
        SSHError("command timed out"),
        KeyboardInterrupt("stop"),
    ],
    ids=("failure", "timeout", "interrupt"),
)
def test_execute_unwind_removes_both_stages_and_preserves_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    transport = _RecordingTransport(execute_failure=failure)

    with pytest.raises(type(failure)) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    _assert_no_residue(transport)


@pytest.mark.parametrize(
    "cleanup_failure",
    [SSHError("remove failed"), OSError("remove failed")],
    ids=("transport", "process"),
)
def test_standalone_guest_removal_failure_surfaces_and_models_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
) -> None:
    transport = _RecordingTransport(cleanup_failure=cleanup_failure)

    with pytest.raises(ProvisioningError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert str(caught.value) == "could not verify removal of the guest bootstrap staging file"
    assert transport.guest_files
    assert all(not path.exists() for path in transport.local_paths)
    assert _AUTH_KEY not in repr(caught.value)


@pytest.mark.parametrize(
    "cleanup_failure",
    [
        KeyboardInterrupt("cleanup interrupted"),
        SystemExit("cleanup exited"),
        GeneratorExit("cleanup closed"),
    ],
    ids=("interrupt", "exit", "generator-exit"),
)
def test_standalone_guest_removal_control_flow_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
) -> None:
    transport = _RecordingTransport(cleanup_failure=cleanup_failure)

    with pytest.raises(type(cleanup_failure)) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is cleanup_failure
    assert transport.guest_files
    assert all(not path.exists() for path in transport.local_paths)


@pytest.mark.parametrize(
    "cleanup_failure",
    [SSHError("remove failed"), OSError("remove failed")],
    ids=("transport", "process"),
)
def test_guest_removal_failure_does_not_mask_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    cleanup_failure: BaseException,
) -> None:
    failure = SSHError("execution failed")
    transport = _RecordingTransport(
        execute_failure=failure,
        cleanup_failure=cleanup_failure,
    )

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    assert transport.guest_files
    assert warnings == [
        "could not verify removal of the guest bootstrap staging file; primary failure unchanged; plaintext may remain"
    ]
    assert _AUTH_KEY not in repr(warnings)


@pytest.mark.parametrize(
    "cleanup_failure",
    [
        KeyboardInterrupt("cleanup interrupted"),
        SystemExit("cleanup exited"),
        GeneratorExit("cleanup closed"),
    ],
    ids=("interrupt", "exit", "generator-exit"),
)
def test_guest_removal_control_flow_does_not_mask_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    cleanup_failure: BaseException,
) -> None:
    primary = SSHError("execution failed")
    transport = _RecordingTransport(
        execute_failure=primary,
        cleanup_failure=cleanup_failure,
    )

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is primary
    assert transport.guest_files
    assert warnings == [
        "could not verify removal of the guest bootstrap staging file; primary failure unchanged; plaintext may remain"
    ]


def test_warning_failure_does_not_mask_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = SSHError("execution failed")
    warning_failure = RuntimeError("warning sink failed")
    transport = _RecordingTransport(
        execute_failure=primary,
        cleanup_failure=SSHError("remove failed"),
    )

    def _fail_warning(message: str) -> None:
        del message
        raise warning_failure

    monkeypatch.setattr("agentworks.vms.initializer.driver.output.warn", _fail_warning)

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is primary
    assert transport.guest_files


def test_local_removal_failure_surfaces_after_guest_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_unlink = Path.unlink
    transport = _RecordingTransport()

    def _fail_bootstrap_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith("agentworks-bootstrap-"):
            raise OSError("remove failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _fail_bootstrap_unlink)
    try:
        with pytest.raises(ProvisioningError) as caught:
            _call(tmp_path, monkeypatch, transport)

        assert str(caught.value) == "could not verify removal of the local bootstrap staging file"
        assert transport.guest_files == {}
        assert len(transport.local_paths) == 1
        assert transport.local_paths[0].exists()
        assert _AUTH_KEY not in repr(caught.value)
    finally:
        for path in transport.local_paths:
            real_unlink(path, missing_ok=True)

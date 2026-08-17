"""Durable file lifecycle for the WSL2-owned bootstrap helper."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import wsl2_bootstrap
from agentworks.errors import ProvisioningError
from agentworks.ssh import SSHError, SSHLogger, SSHResult
from agentworks.transports.wsl2 import WSL2Transport

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import BootstrapProgress
    from tests.conftest import CapturedOutput

_AUTH_KEY = "tskey-bootstrap-file-sentinel"
_SUCCESS_OUTPUT = "\n".join(
    (
        "##STEP## Tailscale",
        "##SUCCESS## tailscale-ip=100.64.0.9",
        "",
    )
)


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


class _RecordingProgress:
    """Deliberately non-redacting sink that exposes helper output verbatim."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.outputs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def step(self, name: str) -> None:
        self.steps.append(name)

    def output(self, text: str) -> None:
        self.outputs.append(text)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def log_error(self, msg: str) -> None:
        self.errors.append(msg)


def _call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: _RecordingTransport,
    *,
    progress: BootstrapProgress | None = None,
) -> str:
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.wsl2_bootstrap.uuid.uuid4",
        lambda: SimpleNamespace(hex="a" * 32),
    )
    return wsl2_bootstrap.run_wsl2_bootstrap(
        transport,  # type: ignore[arg-type]
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        tailscale_auth_key=_AUTH_KEY,
        hostname="wsl2--vm1",
        swap_gib=0,
        progress=progress if progress is not None else MagicMock(),
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


def test_helper_does_not_close_manager_owned_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import ssh

    monkeypatch.setattr(ssh, "LOG_DIR", tmp_path / "logs")
    logger = SSHLogger("vm1", "vm-create", redactions=(_AUTH_KEY,))

    assert _call(tmp_path, monkeypatch, _RecordingTransport(), progress=logger) == "100.64.0.9"
    assert "# Finished:" not in logger.path.read_text()

    logger.close()
    assert "# Finished:" in logger.path.read_text()


def test_structured_failure_output_is_redacted_from_console_log_and_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    from agentworks import ssh

    monkeypatch.setattr(ssh, "LOG_DIR", tmp_path / "logs")
    logger = SSHLogger("vm1", "vm-create", redactions=(_AUTH_KEY,))
    transport = _RecordingTransport(
        execute_result=SSHResult(
            returncode=1,
            stdout="\n".join(
                (
                    f"##STEP## step {_AUTH_KEY}",
                    f"##SUCCESS## success {_AUTH_KEY}",
                    f"##WARN## warning {_AUTH_KEY}",
                    f"##ERROR## error {_AUTH_KEY}",
                    "",
                )
            ),
            stderr="",
        )
    )

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport, progress=logger)
    logger.close()

    assert str(caught.value) == "Bootstrap script failed (exit 1)"
    assert _AUTH_KEY not in repr(caught.value)
    durable_log = logger.path.read_text()
    console = repr(captured_output)
    assert _AUTH_KEY not in durable_log
    assert _AUTH_KEY not in console
    assert durable_log.count("[REDACTED]") >= 4
    assert console.count("[REDACTED]") >= 4
    _assert_no_residue(transport)


def test_helper_redacts_every_non_redacting_progress_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = "\n".join(
        (
            f"##STEP## step {_AUTH_KEY}",
            f"##SUCCESS## success {_AUTH_KEY}",
            f"##WARN## warning {_AUTH_KEY}",
            f"##ERROR## error {_AUTH_KEY}",
            f"raw transcript {_AUTH_KEY}",
            "",
        )
    )
    transport = _RecordingTransport(
        execute_result=SSHResult(returncode=1, stdout=transcript, stderr=""),
    )
    progress = _RecordingProgress()

    with pytest.raises(SSHError):
        _call(tmp_path, monkeypatch, transport, progress=progress)

    assert progress.steps == ["step [REDACTED]"]
    assert progress.outputs == [
        "success [REDACTED]",
        transcript.replace(_AUTH_KEY, "[REDACTED]"),
    ]
    assert progress.warnings == ["warning [REDACTED]"]
    assert progress.errors == ["error [REDACTED]"]
    for captured in (progress.steps, progress.outputs, progress.warnings, progress.errors):
        assert _AUTH_KEY not in repr(captured)


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
        "agentworks.capabilities.vm_platform.wsl2_bootstrap.tempfile.NamedTemporaryFile",
        lambda **kwargs: _FailingTempFile(local_path, failure),
    )
    transport = _RecordingTransport()

    with pytest.raises(OSError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    assert not local_path.exists()
    assert transport.guest_files == {}


def test_reflected_copy_failure_is_secret_free_across_every_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    from agentworks import ssh

    monkeypatch.setattr(ssh, "LOG_DIR", tmp_path / "logs")
    logger = SSHLogger("vm1", "vm-create", redactions=(_AUTH_KEY,))
    failure = SSHError(f"WSL2 copy failed: reflected {_AUTH_KEY}")
    transport = _RecordingTransport(copy_failure=failure)
    assert _AUTH_KEY in repr(failure)

    with pytest.raises(ProvisioningError) as caught:
        _call(tmp_path, monkeypatch, transport, progress=logger)
    logger.close()

    assert str(caught.value) == "could not copy the private guest bootstrap staging file"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_exception_graph_is_value_free(caught.value)
    assert _AUTH_KEY not in logger.path.read_text()
    assert _AUTH_KEY not in repr(captured_output)
    _assert_no_residue(transport)


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("stop"), SystemExit("exit"), GeneratorExit("close")],
    ids=("interrupt", "system-exit", "generator-exit"),
)
def test_copy_control_flow_removes_both_stages_and_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    transport = _RecordingTransport(copy_failure=failure)

    with pytest.raises(type(failure)) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert caught.value is failure
    _assert_no_residue(transport)


@pytest.mark.parametrize(
    "failure",
    [
        SSHError("execution failed"),
        SSHError("command timed out"),
        KeyboardInterrupt("stop"),
        SystemExit("exit"),
        GeneratorExit("close"),
    ],
    ids=("failure", "timeout", "interrupt", "system-exit", "generator-exit"),
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


def test_real_wsl_timeout_drops_reflected_output_and_removes_both_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest_path = "/tmp/agentworks-bootstrap-" + "a" * 32 + ".sh"
    guest_files: dict[str, bytes] = {}

    def _run(args: list[str], **kwargs: object) -> SimpleNamespace:
        command = str(args[-1])
        if "install -m 600" in command:
            guest_files[guest_path] = b""
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == f"cat > {guest_path}":
            content = kwargs["input"]
            assert isinstance(content, bytes)
            guest_files[guest_path] = content
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if "setsid sudo" in command:
            raise subprocess.TimeoutExpired(
                cmd=args,
                timeout=900,
                output=f"reflected {_AUTH_KEY}",
                stderr=f"rejected {_AUTH_KEY}",
            )
        if "rm -f --" in command:
            guest_files.pop(guest_path, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess command: {args}")

    monkeypatch.setattr("agentworks.transports.wsl2.subprocess.run", _run)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2_bootstrap.tempfile.tempdir", str(tmp_path))

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, WSL2Transport("vm1"))

    assert str(caught.value).startswith("WSL2 command timed out after 900s:")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_exception_graph_is_value_free(caught.value)
    assert guest_files == {}
    assert list(tmp_path.glob("agentworks-bootstrap-*.sh")) == []


def test_invalid_reflected_tailscale_ip_never_reaches_output_or_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    transport = _RecordingTransport(
        execute_result=SSHResult(
            returncode=0,
            stdout="\n".join(
                (
                    "##STEP## Tailscale",
                    f"##SUCCESS## tailscale-ip={_AUTH_KEY}",
                    "",
                )
            ),
            stderr="",
        )
    )

    with pytest.raises(SSHError) as caught:
        _call(tmp_path, monkeypatch, transport)

    assert str(caught.value) == "Bootstrap script failed (exit 0)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_exception_graph_is_value_free(caught.value)
    assert _AUTH_KEY not in repr(captured_output)
    assert not any(message.startswith("Tailscale IP:") for message in captured_output.detail)
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
    primary = KeyboardInterrupt("execution interrupted")
    transport = _RecordingTransport(
        execute_failure=primary,
        cleanup_failure=cleanup_failure,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
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
    primary = KeyboardInterrupt("execution interrupted")
    warning_failure = RuntimeError("warning sink failed")
    transport = _RecordingTransport(
        execute_failure=primary,
        cleanup_failure=SSHError("remove failed"),
    )

    def _fail_warning(message: str) -> None:
        del message
        raise warning_failure

    monkeypatch.setattr("agentworks.capabilities.vm_platform.wsl2_bootstrap.output.warn", _fail_warning)

    with pytest.raises(KeyboardInterrupt) as caught:
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


@pytest.mark.parametrize("operation", ["write", "copy"])
@pytest.mark.parametrize(
    "cleanup_failure_type",
    [OSError, SystemExit],
    ids=("ordinary-cleanup", "control-flow-cleanup"),
)
def test_local_removal_failure_does_not_mask_staging_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    operation: str,
    cleanup_failure_type: type[BaseException],
) -> None:
    real_unlink = Path.unlink
    local_path = tmp_path / "agentworks-bootstrap-write.sh"
    if operation == "write":
        primary: BaseException = OSError("write failed")
        monkeypatch.setattr(
            "agentworks.capabilities.vm_platform.wsl2_bootstrap.tempfile.NamedTemporaryFile",
            lambda **kwargs: _FailingTempFile(local_path, primary),
        )
        transport = _RecordingTransport()
    else:
        copy_failure = SSHError("copy failed")
        primary = ProvisioningError("could not copy the private guest bootstrap staging file")
        transport = _RecordingTransport(copy_failure=copy_failure)

    cleanup_failure = cleanup_failure_type("local cleanup failed")

    def _fail_bootstrap_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith("agentworks-bootstrap-"):
            raise cleanup_failure
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _fail_bootstrap_unlink)
    try:
        with pytest.raises(type(primary)) as caught:
            _call(tmp_path, monkeypatch, transport)

        if operation == "write":
            assert caught.value is primary
        else:
            assert str(caught.value) == str(primary)
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None
        assert transport.guest_files == {}
        staged_paths = [local_path] if operation == "write" else transport.local_paths
        assert len(staged_paths) == 1
        assert staged_paths[0].exists()
        assert warnings == [
            "could not verify removal of the local bootstrap staging file; "
            "primary failure unchanged; plaintext may remain"
        ]
        assert _AUTH_KEY not in repr(warnings)
    finally:
        for path in [local_path] if operation == "write" else transport.local_paths:
            real_unlink(path, missing_ok=True)

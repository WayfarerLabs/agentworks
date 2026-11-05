"""Remote Lima create staging, the detached create on the placement host, cleanup, and rollback contracts.

Every placement-host command goes through ``LimaHost._execute``, so the fake
here records each command and answers it the way the host would.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, RetainedProvisioningError
from agentworks.capabilities.vm_platform import lima as lima_mod
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.capabilities.vm_platform.lima_host import DetachedResult, LimaCommandFailed, LimaHost, _Completed
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError, StateError
from tests.conftest import provisioning_trust_for_tests

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


_REMOTE_TEMPLATE_DIR = "/tmp/agentworks-lima-template.A1b2C3d4E5"
_PROVIDER_YAML = "arch: default\nmounts: []\n"
_BASE = "/var/tmp/agentworks-lima-myvm"
_REMOTE = {"placement": {"mode": "ssh", "host": "user@host"}}


def _request(*, tailscale_auth_key: str = "tskey-test") -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="myvm",
        debian_release=DebianRelease.TRIXIE,
        hostname="lima--myvm",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("myvm"),
        tailscale_auth_key=tailscale_auth_key,
        progress=MagicMock(),
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


class _Host:
    """The placement host: records each command with its input and answers it.

    ``errors`` maps a command substring to the exception raised for it, and
    ``statuses`` maps a whole command to the exit status it ends with.
    ``detached_status`` is the status the detached create writes once it is
    launched; ``None`` keeps it running forever.
    """

    def __init__(self, *, errors: dict[str, BaseException] | None = None, detached_status: int | None = 0) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.errors = errors or {}
        self.detached_status = detached_status
        self.launched = False
        self.outputs: dict[str, str] = {}
        self.statuses: dict[str, int] = {}

    @property
    def commands(self) -> list[str]:
        return [command for command, _ in self.calls]

    def execute(self, command: str, input: str | None) -> _Completed:
        self.calls.append((command, input))
        for needle, error in self.errors.items():
            if needle in command:
                raise error
        if command in self.statuses:
            return _Completed(self.statuses[command], "", "the failure")
        if command in self.outputs:
            return _Completed(0, self.outputs[command], "")
        if "mktemp -d" in command:
            return _Completed(0, f"{_REMOTE_TEMPLATE_DIR}\n", "")
        if command.startswith("nohup "):
            self.launched = True
        if command.startswith("test -f") and command.endswith(".status"):
            return _Completed(0 if self.launched and self.detached_status is not None else 1, "", "")
        if command.startswith("cat") and ".status" in command:
            return _Completed(0, f"{self.detached_status}\n", "")
        if REBOOT_SENTINEL_PATH in command:
            return _Completed(0, f"{_REBOOT_CLEAR_MARKER}\n", "")
        if "tailscale ip" in command:
            return _Completed(0, "100.64.0.1\n", "")
        return _Completed(0, "", "")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    fake = _Host()
    monkeypatch.setattr(LimaHost, "_execute", lambda self, command, *, input, timeout: fake.execute(command, input))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", lambda seconds: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    return fake


def _index(commands: list[str], predicate: object) -> int:
    (found,) = [i for i, command in enumerate(commands) if predicate(command)]  # type: ignore[operator]
    return found


def _create_remote(platform: LimaPlatform | None = None, progress: object | None = None) -> None:
    (platform or LimaPlatform("lima", _REMOTE))._create_remote("myvm", _PROVIDER_YAML, progress=progress or MagicMock())


# -- Staging the template ------------------------------------------------------------------


def test_remote_template_staging_is_mode_0600_stdin_and_verified_cleanup(host: _Host) -> None:
    _create_remote()

    staged = [call for call in host.calls if "template.yaml" in call[0] and "cat >" in call[0]]
    assert host.calls[0] == ("umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX", None)
    assert staged == [(f"umask 077 && cat > {_REMOTE_TEMPLATE_DIR}/template.yaml", _PROVIDER_YAML)]
    assert host.calls[-1] == (f"rm -rf -- {_REMOTE_TEMPLATE_DIR} && test ! -e {_REMOTE_TEMPLATE_DIR}", None)
    assert not any(_PROVIDER_YAML in command for command in host.commands)


def test_remote_stdin_write_failure_removes_partial_template_and_preserves_error(host: _Host) -> None:
    write_failure = LimaCommandFailed("stdin write failed")
    host.errors = {"template.yaml": write_failure}

    with pytest.raises(LimaCommandFailed) as caught:
        _create_remote()

    assert caught.value is write_failure
    assert host.commands[-1].startswith(f"rm -rf -- {_REMOTE_TEMPLATE_DIR}")
    assert not any(command.startswith("nohup") for command in host.commands)


def test_remote_template_rejects_untrusted_allocation_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _run(self: LimaHost, command: str, **kwargs: object) -> str:
        calls.append(command)
        return "/tmp/agentworks-lima-template.not-safe\n/tmp/other\n"

    monkeypatch.setattr(LimaHost, "run", _run)

    with pytest.raises(ProvisioningError) as caught:
        _create_remote()

    assert calls == ["umask 077 && mktemp -d /tmp/agentworks-lima-template.XXXXXXXXXX"]
    assert "/tmp/other" not in repr(caught.value)


def test_remote_template_uses_private_random_directory_not_predictable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precreated legacy path is untouched by random private staging."""
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    predictable_path = remote_root / "agentworks-myvm.yaml"
    predictable_path.write_text("legacy provider data\n")
    staged_snapshots: list[tuple[int, int, str]] = []
    monkeypatch.setattr(lima_mod, "_REMOTE_TEMPLATE_ROOT", str(remote_root))
    monkeypatch.setattr(LimaHost, "run_detached", lambda self, command, **kwargs: DetachedResult(0, ""))

    def _run(self: LimaHost, command: str, *, input: str | None = None, **kwargs: object) -> str:
        if command.startswith("rm -rf"):
            (private_dir,) = remote_root.glob("agentworks-lima-template.*")
            template = private_dir / "template.yaml"
            staged_snapshots.append(
                (stat.S_IMODE(private_dir.stat().st_mode), stat.S_IMODE(template.stat().st_mode), template.read_text())
            )
        completed = subprocess.run(
            ["sh", "-c", command], input=input, capture_output=True, text=True, timeout=2, check=False
        )
        if completed.returncode != 0:
            raise LimaCommandFailed(f"semantic remote shell failed with {completed.returncode}")
        return completed.stdout

    monkeypatch.setattr(LimaHost, "run", _run)

    _create_remote()

    if sys.platform == "win32":
        # The local (git-bash) shell has no Unix mode bits for umask 077 to land on.
        assert [content for _, _, content in staged_snapshots] == [_PROVIDER_YAML]
    else:
        assert staged_snapshots == [(0o700, 0o600, _PROVIDER_YAML)]
    assert predictable_path.read_text() == "legacy provider data\n"
    assert list(remote_root.glob("agentworks-lima-template.*")) == []


def test_remote_template_cleanup_interrupt_preserves_active_failure_and_order(host: _Host) -> None:
    primary = LimaCommandFailed("detached create could not start")
    host.errors = {"nohup": primary, "rm -rf": KeyboardInterrupt("cleanup interrupted")}

    with pytest.raises(LimaCommandFailed) as caught:
        _create_remote()

    assert caught.value is primary
    commands = host.commands
    assert _index(commands, lambda c: "mktemp" in c) < _index(commands, lambda c: "template.yaml" in c and "cat >" in c)
    assert commands[-1].startswith("rm -rf")


def test_remote_standalone_template_cleanup_interrupt_remains_visible(host: _Host) -> None:
    cleanup_interrupt = KeyboardInterrupt("cleanup interrupted")
    host.errors = {"rm -rf": cleanup_interrupt}

    with pytest.raises(KeyboardInterrupt) as caught:
        _create_remote()

    assert caught.value is cleanup_interrupt


# -- The detached create ---------------------------------------------------------------------


def test_the_detached_create_runs_once_and_its_output_reaches_the_creates_log(host: _Host) -> None:
    progress = MagicMock()
    host.outputs[f"cat {_BASE}.out 2>/dev/null"] = "limactl says hello\n"

    _create_remote(progress=progress)

    launches = [command for command in host.commands if command.startswith("nohup ")]
    assert launches == [f"nohup /bin/bash {_BASE}.sh </dev/null >/dev/null 2>&1 &"]
    wrapper = [content for command, content in host.calls if command.endswith(f"cat > {_BASE}.sh")]
    assert len(wrapper) == 1
    assert "limactl create --name myvm --tty=false" in (wrapper[0] or "")
    progress.output.assert_called_once_with("limactl says hello\n")


def test_a_failed_detached_create_raises_and_logs_its_status(host: _Host) -> None:
    host.detached_status = 1
    progress = MagicMock()

    with pytest.raises(LimaCommandFailed):
        _create_remote(progress=progress)

    assert progress.log_error.called


def test_remote_interrupt_kills_the_detached_limactl_before_deleting(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A Ctrl-C during the poll stops nothing on the host, since nohup keeps the command alive, so the
    rollback kills it through its PID file, removes its files, and only then deletes the instance."""
    host.detached_status = None
    interrupt = KeyboardInterrupt("first")

    def _sleep(seconds: float) -> None:
        if seconds >= 1:
            raise interrupt

    monkeypatch.setattr("agentworks.capabilities.vm_platform.lima_host.time.sleep", _sleep)

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", _REMOTE).create(_request(), RunContext())

    assert caught.value is interrupt
    commands = host.commands
    kill = _index(commands, lambda c: f"kill $(cat {_BASE}.pid)" in c)
    artifacts = _index(commands, lambda c: c == f"rm -f {_BASE}.out {_BASE}.sh {_BASE}.pid {_BASE}.status")
    delete = _index(commands, lambda c: c == "limactl delete --force myvm")
    assert kill < artifacts < delete
    assert any(command.startswith(f"rm -rf -- {_REMOTE_TEMPLATE_DIR}") for command in commands)


def test_remote_interrupt_preserves_original_when_artifact_cleanup_fails(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """Detached artifact cleanup is best-effort on the interrupt unwind."""
    interrupt = KeyboardInterrupt("first")
    host.errors = {f"rm -f {_BASE}.out": LimaCommandFailed("artifact cleanup failed")}
    monkeypatch.setattr(
        LimaPlatform, "_create_remote", lambda self, name, yaml, *, progress: (_ for _ in ()).throw(interrupt)
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        LimaPlatform("lima", _REMOTE).create(_request(), RunContext())

    assert caught.value is interrupt
    assert "limactl delete --force myvm" in host.commands
    assert not any("artifact cleanup failed" in warning for warning in captured_output.warnings)


def test_remote_exception_kills_cleans_artifacts_then_deletes_without_masking(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """An ordinary provisioning failure gets the full remote rollback."""
    original = LimaCommandFailed("ordinary provisioning failure")
    host.errors = {f"rm -f {_BASE}.out": LimaCommandFailed("artifact cleanup failed")}
    monkeypatch.setattr(
        LimaPlatform, "_create_remote", lambda self, name, yaml, *, progress: (_ for _ in ()).throw(original)
    )

    with pytest.raises(LimaCommandFailed) as caught:
        LimaPlatform("lima", _REMOTE).create(_request(), RunContext())

    assert caught.value is original
    commands = host.commands
    kill = _index(commands, lambda c: f"kill $(cat {_BASE}.pid)" in c)
    artifacts = _index(commands, lambda c: c.startswith(f"rm -f {_BASE}.out"))
    delete = _index(commands, lambda c: c == "limactl delete --force myvm")
    assert kill < artifacts < delete
    surfaced = "\n".join([*captured_output.detail, *captured_output.warnings])
    assert "artifact cleanup failed" not in surfaced


def test_remote_abandon_warning_names_the_vm_host(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """On a remote-Lima site the manual removal runs on the vm_host, so the abandon warning says where."""
    interrupt = KeyboardInterrupt("first")
    host.errors = {"limactl delete": KeyboardInterrupt("second")}
    monkeypatch.setattr(
        LimaPlatform, "_create_remote", lambda self, name, yaml, *, progress: (_ for _ in ()).throw(interrupt)
    )

    with pytest.raises(RetainedProvisioningError):
        LimaPlatform("lima", _REMOTE).create(_request(), RunContext())

    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "on 'user@host'" in abandoned


# -- Local rollback, shared with the remote path -----------------------------------------------


def test_cleanup_failure_warns_and_retains_the_row_with_the_original_failure(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A teardown that cannot remove the instance warns with the manual
    removal command and hands the instance to core, so the row survives
    for ``vm delete``; the original failure stays in the error."""
    host.errors = {"limactl delete": LimaCommandFailed("cleanup broke")}
    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: (_ for _ in ()).throw(LimaCommandFailed("original failure")),
    )

    with pytest.raises(RetainedProvisioningError, match="original failure") as caught:
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert caught.value.platform_metadata == {"instance_name": "myvm"}
    assert host.commands.count("limactl delete --force myvm") == 1
    warned = "\n".join(captured_output.warnings)
    assert "could not clean up the partial Lima instance 'myvm'" in warned
    assert "'limactl delete --force myvm'" in warned


def test_pre_mutation_failure_makes_no_cleanup_calls(
    host: _Host, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A failure before anything is created (the name-collision
    pre-flight) must not fire any teardown: there is nothing of ours to
    delete, and the colliding instance is NOT ours to touch."""
    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: True)

    with pytest.raises(StateError, match="already exists"):
        LimaPlatform("lima", {"placement": {"mode": "local"}}).create(_request(), RunContext())

    assert host.commands == []
    assert not any("Cleaning up" in d for d in captured_output.detail)


def test_delete_op_issues_the_shared_teardown_command(host: _Host, captured_output: CapturedOutput) -> None:
    """delete() runs exactly the forced delete the rollback also uses."""
    vm = SimpleNamespace(name="myvm", platform_metadata={"instance_name": "myvm"})

    LimaPlatform("lima", {"placement": {"mode": "local"}}).delete(vm, RunContext())  # type: ignore[arg-type]

    assert host.commands == ["limactl delete --force myvm"]


@pytest.mark.parametrize(
    ("placement", "status"),
    [({"placement": {"mode": "local"}}, 1), (_REMOTE, 255)],
    ids=("limactl-failed", "host-unreachable"),
)
def test_a_delete_that_fails_raises_so_core_keeps_the_row(
    host: _Host, captured_output: CapturedOutput, placement: dict[str, object], status: int
) -> None:
    """``limactl delete --force`` exits 0 for an instance already gone, so any other status is a delete that failed."""
    host.statuses = {"limactl delete --force myvm": status}
    vm = SimpleNamespace(name="myvm", platform_metadata={"instance_name": "myvm"})

    with pytest.raises(LimaCommandFailed):
        LimaPlatform("lima", placement).delete(vm, RunContext())  # type: ignore[arg-type]

    assert host.commands == ["limactl delete --force myvm"]

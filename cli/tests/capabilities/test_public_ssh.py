"""The clouds' root channel: SSH to the public address under the VM policy, inside the platform's route."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import agentworks.execution as execution
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import public_ssh
from agentworks.capabilities.vm_platform.public_ssh import KeyRefused, PublicSSH
from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution import client, root_script, trust
from agentworks.execution.client import SSHClient
from agentworks.execution.errors import TrustMismatch, TrustMissing
from agentworks.execution.root_script import ChannelUnreachable, RawRun
from agentworks.execution.trust import TrustBinding, TrustMode
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp.platform import GCEPlatform
from tests.conftest import seed_vm_trust

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tests.conftest import CapturedOutput

SSH = shutil.which("ssh")
ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0"


@pytest.fixture(autouse=True)
def _resolved_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution, "resolve_ssh", lambda: SSHClient(SSH or "/usr/bin/ssh", (9, 2)))
    monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.sleep", lambda seconds: None)


def target(vm: str = "box") -> PublicSSH:
    seed_vm_trust(vm)
    return PublicSSH("203.0.113.7", "agw", Path("/home/op/.ssh/id_ed25519"), TrustBinding(TrustMode.VM, vm))


def answering(monkeypatch: pytest.MonkeyPatch, *, log_text: str = "", status: int | None = 0) -> list[list[str]]:
    """Replace the local process with one that answers ``status`` through the markers and logs ``log_text``."""
    argvs: list[list[str]] = []

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        argvs.append(list(argv))
        Path(argv[argv.index("-E") + 1]).write_text(log_text)
        if status is None:
            return RawRun(b"", b"")
        nonce = script.split(b"\\036", 1)[1][:32]
        prefix = b"\x1e" + nonce + b" "
        return RawRun(prefix + b"start\n" + prefix + b"end\n", prefix + b"start\n" + prefix + b"exit %d\n" % status)

    monkeypatch.setattr(root_script, "run_process", run_process)
    return argvs


# -- The argv --------------------------------------------------------------------------


@pytest.mark.skipif(SSH is None, reason="requires an installed OpenSSH client")
def test_the_argv_ignores_operator_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    argvs = answering(monkeypatch, status=None)
    target().run_as_root(b"true\n", 5)
    (argv,) = argvs
    operator_config = tmp_path / "config"
    operator_config.write_text("Host *\n  ForwardAgent yes\n  ControlMaster auto\n  RequestTTY force\n")

    # An operator file given first stands for their configuration; the argv's own -F none must still win.
    shown = subprocess.run(
        [argv[0], "-G", "-F", str(operator_config), *argv[1:]], capture_output=True, text=True, check=True
    ).stdout.splitlines()

    assert "forwardagent no" in shown
    assert "controlmaster false" in shown
    assert "requesttty false" in shown
    assert argv[-2:] == ["203.0.113.7", "sudo -n /bin/bash -s"]


def test_a_vm_with_no_record_raises_before_anything_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs = answering(monkeypatch)
    access = PublicSSH("203.0.113.7", "agw", Path("/k"), TrustBinding(TrustMode.VM, "never-enrolled"))

    with pytest.raises(TrustMissing):
        access.run_as_root(b"true\n", 5)

    assert argvs == []


@pytest.mark.parametrize("host", ["", "-oProxyCommand=x", "203.0.113.7 extra"])
def test_a_provider_address_that_is_not_one_literal_host_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        PublicSSH(host, "agw", Path("/k"), TrustBinding(TrustMode.VM, "box"))


def test_the_shell_asks_for_a_terminal_and_runs_the_admins_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs: list[list[str]] = []
    monkeypatch.setattr(
        "agentworks.capabilities.vm_platform.public_ssh.ssh_process.run_terminal",
        lambda argv: argvs.append(list(argv)) or 255,
    )

    assert target().shell() == 255

    (argv,) = argvs
    assert argv[1:4] == ["-F", "none", "-t"]
    # No remote command: the host is last, so sshd starts the admin's login shell.
    assert argv[-3:] == ["agw", "--", "203.0.113.7"]


# -- The client log ----------------------------------------------------------------------


def test_a_host_key_failure_in_the_client_log_is_a_trust_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    answering(monkeypatch, log_text="Host key verification failed.\n", status=None)

    with pytest.raises(TrustMismatch):
        target().run_as_root(b"true\n", 5)


@pytest.mark.parametrize("load_hint", [None, "ssh-add the key"], ids=["key-loaded-or-plain", "key-not-loaded"])
def test_a_refused_key_in_the_client_log_leads_with_the_agent_load_hint_when_there_is_one(
    monkeypatch: pytest.MonkeyPatch, load_hint: str | None
) -> None:
    answering(monkeypatch, log_text="agw@203.0.113.7: Permission denied (publickey).\n", status=None)
    monkeypatch.setattr(client, "agent_load_hint", lambda identity_file: load_hint)

    with pytest.raises(ConnectivityError) as caught:
        target().run_as_root(b"true\n", 5)

    hint = caught.value.hint
    assert hint  # the provider console, always
    assert hint.startswith("ssh-add the key") is (load_hint is not None)  # first: it fires only when likely


@pytest.mark.parametrize(
    "banner", ["Host key verification failed.\n", "Permission denied (publickey).\n"], ids=["host-key", "auth"]
)
def test_failure_text_in_the_log_of_a_script_that_started_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, banner: str
) -> None:
    """ssh logs the server's banner too; a script that started has already connected and logged in."""
    answering(monkeypatch, log_text=banner, status=0)

    assert root_script.run(target().run_as_root, "true", deadline=5).status == 0


# -- wait_until_answering ----------------------------------------------------------------


def test_the_wait_returns_at_the_first_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs = answering(monkeypatch)

    target().wait_until_answering()

    assert len(argvs) == 1


class _Clock:
    """A fake clock that only moves when the code under test sleeps; probes that are refused take no time."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.monotonic", lambda: self.now)
        monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _answering_after(monkeypatch: pytest.MonkeyPatch, clock: _Clock, seconds: float) -> list[list[str]]:
    """Refuse at once until ``seconds`` of the fake clock have passed, then answer."""
    argvs = answering(monkeypatch)
    answer = root_script.run_process

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        if clock.now < seconds:
            argvs.append(list(argv))
            Path(argv[argv.index("-E") + 1]).write_text("")
            return RawRun(b"", b"")
        return answer(argv, script, deadline)

    monkeypatch.setattr(root_script, "run_process", run_process)
    return argvs


def test_the_wait_outlasts_a_vm_that_refuses_fast_while_it_boots(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A cloud VM just started returns at the hypervisor's state; its sshd refuses for a while, quickly."""
    clock = _Clock(monkeypatch)
    _answering_after(monkeypatch, clock, 30)

    target().wait_until_answering()

    assert clock.now >= 30
    assert captured_output.warnings == []


def test_the_wait_warns_its_hint_and_raises_at_its_time_bound(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    clock = _Clock(monkeypatch)
    _answering_after(monkeypatch, clock, float("inf"))

    with pytest.raises(ChannelUnreachable):
        target().wait_until_answering()

    assert (
        public_ssh.PROBE_WAIT_SECONDS <= clock.now < public_ssh.PROBE_WAIT_SECONDS + public_ssh.PROBE_INTERVAL_SECONDS
    )
    assert captured_output.warnings == [public_ssh.PROBE_FAILURE_HINT]


@pytest.mark.parametrize(
    ("log_text", "error"),
    [
        ("Host key verification failed.\n", TrustMismatch),
        ("agw@203.0.113.7: Permission denied (publickey).\n", KeyRefused),
    ],
)
def test_the_wait_stops_at_once_where_waiting_cannot_help(
    monkeypatch: pytest.MonkeyPatch, log_text: str, error: type[Exception]
) -> None:
    argvs = answering(monkeypatch, log_text=log_text, status=None)

    with pytest.raises(error):
        target().wait_until_answering()

    assert len(argvs) == 1


def _refusing_the_key_until(monkeypatch: pytest.MonkeyPatch, clock: _Clock, seconds: float) -> None:
    """Refuse the configured key until ``seconds`` of the fake clock have passed, then answer."""
    answering(monkeypatch)
    real = root_script.run_process

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        if clock.now < seconds:
            Path(argv[argv.index("-E") + 1]).write_text("agw@203.0.113.7: Permission denied (publickey).\n")
            return RawRun(b"", b"")
        return real(argv, script, deadline)

    monkeypatch.setattr(root_script, "run_process", run_process)


def test_a_create_s_wait_retries_a_refused_key_while_the_guest_agent_installs_it(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    clock = _Clock(monkeypatch)
    _refusing_the_key_until(monkeypatch, clock, 30)

    target().wait_until_answering(seconds=600, retry_refused_key=True)

    assert clock.now >= 30
    assert captured_output.warnings == []


def test_a_refused_key_is_retried_at_the_longer_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast retries of a refused key would trip sshd's per-source penalties; other probes keep the short interval."""
    clock = _Clock(monkeypatch)
    sleeps: list[float] = []
    advance = clock.sleep

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        advance(seconds)

    monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.sleep", sleep)
    _refusing_the_key_until(monkeypatch, clock, 30)

    target().wait_until_answering(seconds=600, retry_refused_key=True)

    assert sleeps == [public_ssh.REFUSED_KEY_INTERVAL_SECONDS] * 3


def test_a_refusal_standing_at_the_bound_is_what_raises(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The VM answers, so the firewall advice would mislead; the refusal's own hint is the next step."""
    clock = _Clock(monkeypatch)
    _refusing_the_key_until(monkeypatch, clock, float("inf"))

    with pytest.raises(KeyRefused):
        target().wait_until_answering(seconds=600, retry_refused_key=True)

    assert 600 <= clock.now < 600 + public_ssh.PROBE_INTERVAL_SECONDS
    assert captured_output.warnings == []


def test_the_wait_stops_at_once_when_sudo_refuses_to_start_the_script(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The VM answered: an admin without passwordless sudo is not a network that needs more time."""
    probes: list[bytes] = []

    def run_process(argv: list[str], script: bytes, deadline: float) -> RawRun:
        probes.append(script)
        Path(argv[argv.index("-E") + 1]).write_text("")
        return RawRun(b"", b"sudo: a password is required\n")

    monkeypatch.setattr(root_script, "run_process", run_process)

    with pytest.raises(StateError) as caught:
        target().wait_until_answering()

    assert not isinstance(caught.value, ConnectivityError)
    assert caught.value.hint
    assert len(probes) == 1
    assert captured_output.warnings == []  # not the firewall hint


# -- Each cloud's run_as_root and read_host_keys ------------------------------------------


def _aws() -> EC2Platform:
    return EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}})


def _azure() -> AzureVMPlatform:
    config = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}}
    return AzureVMPlatform("az-site", config)


def _gcp() -> GCEPlatform:
    return GCEPlatform(
        "gcp-site",
        {"name": "gcp-gce", "project_id": "p", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )


CLOUDS: list[Callable[[], Any]] = [_aws, _azure, _gcp]


class _Cloud:
    """One cloud platform with its route and live address faked, recording what ran where."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any], address: str) -> None:
        self.platform: Any = factory()
        self.events: list[str] = []
        self.bindings: list[TrustBinding] = []
        cls = type(self.platform)

        @contextlib.contextmanager
        def route(platform: object, vm: object, ctx: object) -> Iterator[None]:
            self.events.append("route-open")
            try:
                yield
            finally:
                self.events.append("route-close")

        monkeypatch.setattr(cls, "_transient_route", route)
        monkeypatch.setattr(cls, "_public_address", lambda platform, vm, ctx: address)
        original = PublicSSH.__init__

        def recording_init(access: PublicSSH, host: str, user: str, identity: Path, binding: TrustBinding) -> None:
            self.bindings.append(binding)
            original(access, host, user, identity, binding)

        monkeypatch.setattr(PublicSSH, "__init__", recording_init)
        monkeypatch.setattr(PublicSSH, "wait_until_answering", lambda access: self._ran("probe"))

    def _ran(self, what: str) -> None:
        self.events.append(what)


def _vm() -> Any:
    return SimpleNamespace(
        name="box", admin_username="agw", platform_metadata={"instance_id": "i-1", "region": "us-east-1"}
    )


def _ctx() -> RunContext:
    return RunContext(config=SimpleNamespace(operator=SimpleNamespace(ssh_private_key=Path("/k"))))  # type: ignore[arg-type]


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_cloud_vm_with_no_record_opens_no_route(monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")

    with pytest.raises(TrustMissing):
        cloud.platform.run_as_root(_vm(), _ctx(), b"true\n", 5)

    assert cloud.events == []


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_cloud_runs_one_script_inside_its_own_route_under_vm_trust(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    seed_vm_trust("box")
    monkeypatch.setattr(
        PublicSSH, "run_as_root", lambda access, script, deadline: cloud._ran("script") or RawRun(b"", b"")
    )

    cloud.platform.run_as_root(_vm(), _ctx(), b"true\n", 5)

    assert cloud.events == ["route-open", "probe", "script", "route-close"]
    assert [binding.mode for binding in cloud.bindings] == [TrustMode.VM]


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_cloud_reads_host_keys_under_an_unverified_file_that_is_gone_afterwards(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    keys = (trust.HostKey("ssh-ed25519", ED25519),)
    monkeypatch.setattr(root_script, "read_host_keys", lambda run_as_root: cloud._ran("read") or keys)

    assert cloud.platform.read_host_keys(_vm(), _ctx()) == keys

    assert cloud.events == ["route-open", "probe", "read", "route-close"]
    (binding,) = cloud.bindings
    assert binding.mode is TrustMode.UNVERIFIED
    assert binding.provisioning_file is not None
    assert not os.path.exists(binding.provisioning_file)


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_cloud_vm_with_a_record_refuses_an_unverified_host_key_read(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    seed_vm_trust("box")
    monkeypatch.setattr(root_script, "read_host_keys", lambda run_as_root: cloud._ran("read") or ())

    with pytest.raises(StateError):
        cloud.platform.read_host_keys(_vm(), _ctx())

    assert cloud.events == []
    assert cloud.bindings == []


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_clouds_native_shell_runs_inside_its_own_route_after_the_probe(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    seed_vm_trust("box")
    monkeypatch.setattr(PublicSSH, "shell", lambda access: cloud._ran("shell") or 3)

    assert cloud.platform.native_shell(_vm(), _ctx()) == 3

    assert cloud.events == ["route-open", "probe", "shell", "route-close"]
    assert [binding.mode for binding in cloud.bindings] == [TrustMode.VM]


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_an_interrupted_native_shell_still_closes_its_route(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    seed_vm_trust("box")

    def interrupted(access: PublicSSH) -> int:
        cloud._ran("shell")
        raise KeyboardInterrupt

    monkeypatch.setattr(PublicSSH, "shell", interrupted)

    with pytest.raises(KeyboardInterrupt):
        cloud.platform.native_shell(_vm(), _ctx())

    assert cloud.events == ["route-open", "probe", "shell", "route-close"]


@pytest.mark.usefixtures("standard_streams")
@pytest.mark.skipif(sys.platform == "win32", reason="os.kill delivery is POSIX")
@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
def test_a_termination_during_a_root_script_still_closes_its_route(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any], signal_name: str
) -> None:
    """Recover, rekey and delete reach a cloud VM through run_as_root; a SIGTERM there must not leave the allow open."""
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")
    seed_vm_trust("box")

    def terminated(access: PublicSSH, script: bytes, deadline: float) -> RawRun:
        cloud._ran("script")
        os.kill(os.getpid(), getattr(signal, signal_name))
        raise AssertionError(f"{signal_name} did not interrupt the script")

    monkeypatch.setattr(PublicSSH, "run_as_root", terminated)

    with pytest.raises(KeyboardInterrupt):
        cloud.platform.run_as_root(_vm(), _ctx(), b"true\n", 5)

    assert cloud.events == ["route-open", "probe", "script", "route-close"]


@pytest.mark.parametrize("factory", CLOUDS, ids=["aws", "azure", "gcp"])
def test_a_clouds_native_shell_for_a_vm_with_no_record_opens_no_route(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "203.0.113.7")

    with pytest.raises(TrustMissing):
        cloud.platform.native_shell(_vm(), _ctx())

    assert cloud.events == []


# GCP's address read raises GCEError itself for an instance with no external address.
@pytest.mark.parametrize("factory", [_aws, _azure], ids=["aws", "azure"])
def test_a_cloud_vm_with_no_public_address_is_a_state_error(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    cloud = _Cloud(monkeypatch, factory, "")
    seed_vm_trust("box")

    with pytest.raises(StateError) as caught:
        cloud.platform.run_as_root(_vm(), _ctx(), b"true\n", 5)

    assert caught.value.hint
    assert cloud.events == ["route-open", "route-close"]

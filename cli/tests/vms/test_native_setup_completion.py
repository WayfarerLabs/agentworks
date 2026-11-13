"""VM command completion after independent native setup failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.config import load_config
from agentworks.db import InitStatus, ProvisioningStatus, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.output import Role
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import CapturedOutput, fake_installed_programs, pin_wsl2_unsupported, seed_vm_trust
from tests.ssh_fixtures import write_test_ssh_keypair
from tests.vm_access_support import AccessRecorder

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database


@pytest.mark.parametrize("operation", ["create", "reinit"])
@pytest.mark.parametrize("native_failure", [False, True])
def test_vm_reports_native_failures_only_after_command_completion(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    operation: str,
    native_failure: bool,
    vm_access_recorder: AccessRecorder,
) -> None:
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    settings = tmp_path / "config.toml"
    settings.write_text(
        f'[operator]\nssh_public_key = "{private.as_posix()}.pub"\nssh_private_key = "{private.as_posix()}"\n'
    )
    config = load_config(settings, warn_issues=False, warn_deprecations=False)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "fixture-ts-key")
    fake_installed_programs(monkeypatch, lambda name: f"/usr/bin/{name}")
    pin_wsl2_unsupported(monkeypatch)
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    monkeypatch.setattr(LimaPlatform, "status", lambda *a, **k: VMStatus.RUNNING)
    target = MagicMock()
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    # Reinit's base checks: the release, the check script, then the elevated sudo check.
    vm_access_recorder.access(vm_name="box").run_outcomes = [
        Result(Completed(0), b'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'),
        Result(Completed(0), b"AGW-CHECK-END\n"),
        Result(Completed(0), b"0\n"),
    ]
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *a, **k: ProvisionResult(tailnet_address="100.64.0.7"),
    )
    events: list[str] = []

    class Logger:
        display_path = "native-completion-fixture.log"

        def __init__(self, *args, **kwargs) -> None:
            pass

        def step(self, name: str) -> None:
            pass

        def record(self, request: object, result: object) -> None:
            pass

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("agentworks.ssh.SSHLogger", Logger)

    def bootstrap(*args: object, **kwargs: object) -> tuple[MagicMock, str, DebianRelease]:
        db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
        db.update_vm_tailscale("box", "100.64.0.7")
        return MagicMock(), "/home/agentworks", DebianRelease.TRIXIE

    monkeypatch.setattr(vm_manager, "bootstrap_vm", bootstrap)
    expected = [
        SetupFailure("vm", "box", "vm", "integration-alpha", "failure-alpha", "repair-alpha"),
        SetupFailure("vm", "box", "user", "integration-beta", "failure-beta", "repair-beta"),
    ]

    def initialize(*args: object, **kwargs: object) -> None:
        events.append("initialize")
        failures = cast("list[SetupFailure]", kwargs["setup_failures"])
        assert failures == []
        if native_failure:
            failures.extend(expected)
        # Ordinary core warnings retain the existing successful partial outcome.
        db.update_vm_init_status("box", InitStatus.PARTIAL)

    monkeypatch.setattr(vm_manager, "run_initialization", initialize)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: events.append("ssh"))
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", lambda *a: events.append("overlay"))

    def report(failures: list[SetupFailure]) -> None:
        events.append("report")
        assert failures == (expected if native_failure else [])
        row = db.get_vm("box")
        assert row is not None and row.init_status == InitStatus.PARTIAL.value
        raise_setup_failures(failures)

    monkeypatch.setattr("agentworks.vms.manager.lifecycle.raise_setup_failures", report)
    monkeypatch.setattr("agentworks.vms.manager.reinit.raise_setup_failures", report)
    if operation == "reinit":
        db.insert_vm("box", site="lima-local", hostname="box")
        seed_vm_trust("box")
        bootstrap()

    def run() -> None:
        if operation == "create":
            vm_manager.create_vm(db, config, name="box", spec='{"cpus":7}', interaction=TtyInteractionPolicy.REFUSE)
        else:
            vm_manager.reinit_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    if native_failure:
        with pytest.raises(StateError) as caught:
            run()
        assert caught.value.__cause__ is None
        for failure in expected:
            assert failure.integration in str(caught.value)
            assert failure.reason in str(caught.value)
            assert failure.hint is not None and failure.hint in str(caught.value)
    else:
        run()

    # Both sync the SSH config after initialization, since its aliases name the configured key, and report
    # their desired-state outcome before any collected setup failure.
    assert events == ["initialize", "close", "ssh", "overlay", "report"]
    row = db.get_vm("box")
    assert row is not None
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
    assert row.init_status == InitStatus.PARTIAL.value
    if operation == "create":
        assert db.instance_state.get_desired_overlay("vm", "box") is not None
    results = [line for line in captured_output.lines if line[0] is Role.RESULT]
    assert len(results) == (0 if native_failure else 1)

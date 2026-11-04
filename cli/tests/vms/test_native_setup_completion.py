"""VM command completion after independent native setup failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.config import load_config
from agentworks.db import InitStatus, ProvisioningStatus, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import StateError
from agentworks.output import Role
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import CapturedOutput, pin_wsl2_unsupported
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database


@pytest.mark.usefixtures("verified_debian_release")
@pytest.mark.parametrize("operation", ["create", "reinit"])
@pytest.mark.parametrize("native_failure", [False, True])
def test_vm_reports_native_failures_only_after_command_completion(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    operation: str,
    native_failure: bool,
) -> None:
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    settings = tmp_path / "config.toml"
    settings.write_text(
        f'[operator]\nssh_public_key = "{private.as_posix()}.pub"\nssh_private_key = "{private.as_posix()}"\n'
    )
    config = load_config(settings, warn_issues=False, warn_deprecations=False)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "fixture-ts-key")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    pin_wsl2_unsupported(monkeypatch)
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    monkeypatch.setattr(LimaPlatform, "status", lambda *a, **k: VMStatus.RUNNING)
    target = MagicMock()
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: target)
    monkeypatch.setattr(vm_manager, "verified_vm_release", lambda *a: DebianRelease.TRIXIE)
    monkeypatch.setattr(
        LimaPlatform,
        "create",
        lambda *a, **k: ProvisionResult(native_transport=target, tailscale_ip="100.64.0.7"),
    )
    events: list[str] = []

    class Logger:
        display_path = "native-completion-fixture.log"

        def __init__(self, *args, **kwargs) -> None:
            pass

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("agentworks.ssh.SSHLogger", Logger)

    def bootstrap(*args: object, **kwargs: object) -> tuple[MagicMock, str]:
        db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
        db.update_vm_tailscale("box", "100.64.0.7")
        return target, "/home/agentworks"

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
    if operation == "reinit":
        db.insert_vm("box", site="lima-local", hostname="box")
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

    assert events == ["initialize", "close"] + (["ssh", "overlay"] if operation == "create" else []) + ["report"]
    row = db.get_vm("box")
    assert row is not None
    assert row.provisioning_status == ProvisioningStatus.COMPLETE.value
    assert row.init_status == InitStatus.PARTIAL.value
    if operation == "create":
        assert db.instance_state.get_desired_overlay("vm", "box") is not None
    results = [line for line in captured_output.lines if line[0] is Role.RESULT]
    assert len(results) == (0 if native_failure else 1)

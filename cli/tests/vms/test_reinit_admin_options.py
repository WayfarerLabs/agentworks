"""``vm reinit --admin-template`` and ``--admin-spec``: retain, replace and clear the admin layer.

Real config, registry and desired state; the initializer and the VM's
transports are fakes, as in ``test_create_reinit_orchestrated``.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import ProvisioningStatus, VersionedPayload
from agentworks.errors import AgentworksError
from agentworks.execution.results import Completed, Result
from agentworks.instance_specs import (
    VM_ADMIN_REPLACEMENT_REMEDY,
    ActivationMapMigrationRequired,
    StoredSourceReferenceRefused,
    get_vm_instance_overlays,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.admin import AdminConfig
from tests.conftest import (
    ManifestDoc,
    fake_installed_programs,
    isolate_home,
    pin_wsl2_unsupported,
    seed_vm_trust,
    write_manifests,
)
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.db.instance_state import JsonObject
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import AccessRecorder

SECRET = "ghp-stored-admin-secret"
REFUSED_ADMIN: JsonObject = {"dotfiles_source": f"git::https://u:{SECRET}@example.com/dots.git", "shell": "bash"}
STORED_VM: JsonObject = {"cpus": 6}
WORK = ManifestDoc("admin-template", "work", {"shell": "zsh"})

TRIXIE = b'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    isolate_home(monkeypatch, tmp_path / "home")
    key = tmp_path / "id_ed25519"
    write_test_ssh_keypair(key)
    fake_installed_programs(monkeypatch, lambda name: f"/usr/bin/{name}")
    pin_wsl2_unsupported(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(f'[operator]\nssh_public_key = "{key.as_posix()}.pub"\nssh_private_key = "{key.as_posix()}"\n')
    write_manifests(tmp_path, WORK)
    return load_config(path, warn_issues=False, warn_deprecations=False)


@pytest.fixture
def initialized(db: Database, monkeypatch: pytest.MonkeyPatch, vm_access_recorder: AccessRecorder) -> list[AdminConfig]:
    """Seed a provisioned ``rvm`` whose base checks pass; each initialization records the admin it applied."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    db.insert_vm("rvm", site="lima-local", hostname="rvm")
    db.update_vm_tailscale("rvm", "100.64.0.9")
    db.update_vm_provisioning_status("rvm", ProvisioningStatus.COMPLETE)
    seed_vm_trust("rvm")
    global _recorder
    _recorder = vm_access_recorder
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    @contextlib.contextmanager
    def _hold(self: LimaPlatform, vm: object, *, config: object | None = None):  # noqa: ANN202
        yield

    monkeypatch.setattr(LimaPlatform, "vm_active", _hold)
    admins: list[AdminConfig] = []

    def _fake_init(*args: object, **kwargs: object) -> None:
        assert isinstance(args[4], AdminConfig)
        admins.append(args[4])

    monkeypatch.setattr(vm_manager, "run_initialization", _fake_init)
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kw: SimpleNamespace())
    return admins


def _store(db: Database, *, vm: JsonObject, admin: JsonObject) -> VersionedPayload:
    stored = VersionedPayload(2, {"vm": vm, "admin": admin})
    db.instance_state.put_desired_overlay("vm", "rvm", stored)
    return stored


_recorder: AccessRecorder | None = None


def _reinit(db: Database, config: object, **options: str) -> None:
    """Reinit ``rvm``, its base checks answering as a clean trixie VM."""
    assert _recorder is not None
    _recorder.access(vm_name="rvm").run_outcomes = [
        Result(Completed(0), TRIXIE),
        Result(Completed(0), b"AGW-CHECK-END\n"),
        Result(Completed(0), b"0\n"),
    ]
    vm_manager.reinit_vm(db, config, "rvm", interaction=TtyInteractionPolicy.REFUSE, **options)  # type: ignore[arg-type]


def _stored_payload(db: Database) -> VersionedPayload | None:
    record = db.instance_state.get_desired_overlay("vm", "rvm")
    return None if record is None else record.payload


def test_a_plain_reinit_retains_both_stored_layers(
    db: Database, config: object, initialized: list[AdminConfig]
) -> None:
    stored = _store(db, vm=STORED_VM, admin={"shell": "zsh"})

    _reinit(db, config)

    assert [admin.shell for admin in initialized] == ["zsh"]
    assert _stored_payload(db) == stored


def test_admin_spec_replaces_only_the_admin_layer(db: Database, config: object, initialized: list[AdminConfig]) -> None:
    _store(db, vm=STORED_VM, admin={"shell": "zsh", "mise_activate": True})

    _reinit(db, config, admin_spec='{"shell":"bash"}')

    assert [admin.shell for admin in initialized] == ["bash"]
    assert _stored_payload(db) == VersionedPayload(2, {"vm": STORED_VM, "admin": {"shell": "bash"}})
    # A later plain reinit reads the replacement back.
    _reinit(db, config)
    assert [admin.shell for admin in initialized] == ["bash", "bash"]


@pytest.mark.parametrize("cleared", ["", "{}"], ids=["empty", "empty-object"])
def test_admin_spec_clears_the_admin_layer_and_keeps_the_vm_layer(
    db: Database, config: object, initialized: list[AdminConfig], cleared: str
) -> None:
    _store(db, vm=STORED_VM, admin={"shell": "zsh"})

    _reinit(db, config, admin_spec=cleared)

    overlays = get_vm_instance_overlays(db, "rvm")
    assert overlays is not None and overlays.admin is None and overlays.vm is not None
    assert overlays.vm.payload.value == STORED_VM
    assert [admin.shell for admin in initialized] == [AdminConfig().shell]


def test_clearing_the_only_layer_removes_the_record(
    db: Database, config: object, initialized: list[AdminConfig]
) -> None:
    _store(db, vm={}, admin={"shell": "zsh"})

    _reinit(db, config, admin_spec="{}")

    assert _stored_payload(db) is None
    assert len(initialized) == 1


def test_admin_template_re_points_the_vm_and_keeps_the_stored_admin_layer(
    db: Database, config: object, initialized: list[AdminConfig]
) -> None:
    stored = _store(db, vm=STORED_VM, admin={"mise_activate": True})

    _reinit(db, config, admin_template="work")

    vm = db.get_vm("rvm")
    assert vm is not None and vm.admin_template == "work"
    (admin,) = initialized
    assert admin.shell == "zsh"
    assert admin.mise_activate is True
    assert _stored_payload(db) == stored


@pytest.mark.parametrize(
    "options",
    [{"admin_template": "missing"}, {"admin_template": "missing", "admin_spec": '{"shell":"zsh"}'}],
    ids=["template", "template-and-spec"],
)
def test_an_undeclared_admin_template_is_refused_before_anything_persists(
    db: Database, config: object, initialized: list[AdminConfig], options: dict[str, str]
) -> None:
    stored = _store(db, vm=STORED_VM, admin={"shell": "bash"})

    with pytest.raises(AgentworksError):
        _reinit(db, config, **options)

    vm = db.get_vm("rvm")
    assert vm is not None and vm.admin_template is None
    assert _stored_payload(db) == stored
    assert initialized == []


def test_an_invalid_admin_spec_is_refused_before_anything_persists(
    db: Database, config: object, initialized: list[AdminConfig]
) -> None:
    stored = _store(db, vm=STORED_VM, admin={"shell": "bash"})

    with pytest.raises(AgentworksError):
        _reinit(db, config, admin_template="work", admin_spec='{"no_such_field":1}')

    vm = db.get_vm("rvm")
    assert vm is not None and vm.admin_template is None
    assert _stored_payload(db) == stored
    assert initialized == []


def test_the_new_binding_and_layer_remain_when_initialization_fails(
    db: Database, config: object, initialized: list[AdminConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(db, vm=STORED_VM, admin={"shell": "bash"})

    def _fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(vm_manager, "run_initialization", _fail)

    with pytest.raises(RuntimeError):
        _reinit(db, config, admin_template="work", admin_spec='{"mise_activate":true}')

    vm = db.get_vm("rvm")
    assert vm is not None and vm.admin_template == "work"
    assert _stored_payload(db) == VersionedPayload(2, {"vm": STORED_VM, "admin": {"mise_activate": True}})


def test_a_plain_reinit_refuses_a_refused_stored_admin_source_and_names_the_repair(
    db: Database, config: object, initialized: list[AdminConfig], captured_output: CapturedOutput
) -> None:
    stored = _store(db, vm=STORED_VM, admin=REFUSED_ADMIN)

    for options in ({}, {"admin_template": "work"}):
        with pytest.raises(StoredSourceReferenceRefused) as refused:
            _reinit(db, config, **options)
        assert refused.value.hint == VM_ADMIN_REPLACEMENT_REMEDY
        assert SECRET not in f"{refused.value} {refused.value.hint}"

    assert _stored_payload(db) == stored
    assert initialized == []
    assert SECRET not in str(captured_output.lines)


@pytest.mark.parametrize(
    ("admin_spec", "stored_admin"),
    [
        (
            '{"dotfiles_source":"git::https://example.com/dots.git"}',
            {"dotfiles_source": "git::https://example.com/dots.git"},
        ),
        ("{}", {}),
    ],
    ids=["replace", "clear"],
)
def test_admin_spec_repairs_a_refused_stored_admin_source(
    db: Database,
    config: object,
    initialized: list[AdminConfig],
    captured_output: CapturedOutput,
    admin_spec: str,
    stored_admin: JsonObject,
) -> None:
    _store(db, vm=STORED_VM, admin=REFUSED_ADMIN)

    _reinit(db, config, admin_spec=admin_spec)

    assert _stored_payload(db) == VersionedPayload(2, {"vm": STORED_VM, "admin": stored_admin})
    assert len(initialized) == 1
    assert SECRET not in str(captured_output.lines)
    # The repaired record publishes again for every later command.
    _reinit(db, config)
    assert len(initialized) == 2


def test_admin_spec_repairs_a_stored_admin_activation_list_that_a_plain_reinit_refuses(
    db: Database, config: object, initialized: list[AdminConfig]
) -> None:
    stored = _store(db, vm=STORED_VM, admin={"harness_integrations": ["codex"]})

    with pytest.raises(ActivationMapMigrationRequired) as refused:
        _reinit(db, config)
    assert refused.value.hint is not None and VM_ADMIN_REPLACEMENT_REMEDY in refused.value.hint
    assert _stored_payload(db) == stored

    _reinit(db, config, admin_spec="{}")

    assert _stored_payload(db) == VersionedPayload(2, {"vm": STORED_VM, "admin": {}})
    assert len(initialized) == 1

"""Per-call rekey selection across the real orchestration/secret boundary."""

from __future__ import annotations

import sys

import pytest

from agentworks import output
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, ExternalError, NotFoundError, ValidationError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager
from tests.conftest import ManifestDoc
from tests.vms.test_remaining_commands_orchestrated import (
    _fake_rekey_transports,
    _fake_status,
    _reachable,
    _seed_vm,
)
from tests.vms.test_remaining_commands_orchestrated import (
    _stub_ssh_identity as _stub_ssh_identity,
)

pytestmark = pytest.mark.usefixtures("_stub_ssh_identity")


def test_replacement_secret_reaches_join_and_gate_repair_without_persisting(
    db, make_config, resolve_counter, monkeypatch, captured_output
) -> None:
    config = make_config(
        '[secret_config]\nsources = ["env-var"]\n',
        manifests=[ManifestDoc("secret", "replacement-key", description="replacement")],
    )
    monkeypatch.setenv("AW_SECRET_REPLACEMENT_KEY", "replacement-value")
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)
    _seed_vm(db)
    original_template = db.get_vm("box").template
    _reachable(monkeypatch, False)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    statuses = iter((VMStatus.RUNNING, VMStatus.STOPPED))
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: next(statuses))
    repairs = []

    def repair(*args, auth_keys, auth_key_name, **kwargs):
        repairs.append((auth_key_name, auth_keys.get(auth_key_name)))

    monkeypatch.setattr(manager, "_ensure_tailscale", repair)
    calls = _fake_rekey_transports(monkeypatch)
    pauses = []
    monkeypatch.setattr(output, "pause", lambda message: pauses.append(message))

    manager.rekey_vm(
        db,
        config,
        "box",
        secret="replacement-key",
        wait_for_share=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert resolve_counter == [["replacement-key", "proxmox-token"]]
    assert repairs == [("replacement-key", "replacement-value")]
    assert events == ["start"]
    assert [call.input_text for call in calls if call.input_text is not None] == ["replacement-value\n"]
    assert all("replacement-value" not in call.command for call in calls)
    assert len(pauses) == 1
    assert db.get_vm("box").template == original_template
    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    assert resolve_template(build_registry(config), original_template).tailscale_auth_key == "tailscale-auth-key"


@pytest.mark.parametrize("secret", ["", "bad name", "-invalid", "unknown-key"])
def test_explicit_secret_is_validated_before_operations(db, make_config, resolve_counter, monkeypatch, secret) -> None:
    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    expected = NotFoundError if secret == "unknown-key" else ValidationError
    with pytest.raises(expected):
        manager.rekey_vm(db, config, "box", secret=secret, interaction=TtyInteractionPolicy.REFUSE)
    assert resolve_counter == []
    assert events == []
    assert db.get_vm("box").tailscale_host == "100.64.0.9"


def test_secret_and_prompt_are_mutually_exclusive(db, make_config, resolve_counter) -> None:
    with pytest.raises(ValidationError):
        manager.rekey_vm(db, make_config(), "box", secret="", prompt=True, interaction=TtyInteractionPolicy.ALLOW)
    assert resolve_counter == []


def test_prompt_ignores_stale_key_but_resolves_site_normally(
    db, make_config, resolve_counter, monkeypatch, captured_output
) -> None:
    config = make_config('[secret_config]\nsources = ["env-var"]\n')
    _seed_vm(db)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "stale-key")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompted = []
    monkeypatch.setattr(output, "prompt_secret", lambda label, **kwargs: prompted.append(label) or "fresh-key")
    _reachable(monkeypatch, True)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    calls = _fake_rekey_transports(monkeypatch)

    manager.rekey_vm(db, config, "box", prompt=True, interaction=TtyInteractionPolicy.ALLOW)

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "tailscale-auth-key"]
    assert len(prompted) == 1 and "tailscale-auth-key" in prompted[0]
    assert [call.input_text for call in calls if call.input_text is not None] == ["fresh-key\n"]


@pytest.mark.parametrize(
    "interaction,usable", [(TtyInteractionPolicy.REFUSE, True), (TtyInteractionPolicy.ALLOW, False)]
)
def test_prompt_refuses_without_terminal_authority(
    db, make_config, resolve_counter, monkeypatch, interaction, usable
) -> None:
    config = make_config()
    _seed_vm(db)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "stale-key")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: usable)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    with pytest.raises(ConfigError):
        manager.rekey_vm(db, config, "box", prompt=True, interaction=interaction)
    assert resolve_counter == []
    assert events == []


@pytest.mark.parametrize("value", ["key\nsecond", "key\rsecond", "key\0second"])
def test_prompted_key_is_line_safe_before_operations(db, make_config, monkeypatch, value) -> None:
    config = make_config()
    _seed_vm(db)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(output, "prompt_secret", lambda label, **kwargs: value)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)
    with pytest.raises(ExternalError if "\0" in value else ValidationError) as caught:
        manager.rekey_vm(db, config, "box", prompt=True, interaction=TtyInteractionPolicy.ALLOW)
    assert value not in repr((caught.value.args, vars(caught.value)))
    assert events == []


@pytest.mark.parametrize("options", [["--secret", "replacement-key"], ["--secret", ""], ["--prompt"]])
def test_cli_forwards_key_selection(db, make_config, monkeypatch, options) -> None:
    from typer.testing import CliRunner

    from agentworks.cli.commands.vm import vm_app

    config = make_config()
    monkeypatch.setattr("agentworks.config.load_config", lambda: config)
    monkeypatch.setattr("agentworks.cli.commands.vm.get_db", lambda: db)
    calls = []
    monkeypatch.setattr(manager, "rekey_vm", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(vm_app, ["rekey", "box", *options])
    assert result.exit_code == 0, result.exception
    assert calls[0]["prompt"] == (options == ["--prompt"])
    assert calls[0]["secret"] == (None if options == ["--prompt"] else options[1])


def test_cli_rejects_removed_ignore_env_option() -> None:
    from typer.testing import CliRunner

    from agentworks.cli.commands.vm import vm_app

    assert CliRunner().invoke(vm_app, ["rekey", "box", "--ignore-env"]).exit_code == 2

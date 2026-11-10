"""Console use validation completes before any deferred tmux mutation."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

import agentworks.bootstrap as bootstrap
from agentworks.db import SessionMode
from agentworks.env.entry import EnvEntry
from agentworks.errors import ValidationError
from agentworks.secrets import SecretTarget
from agentworks.secrets.orchestration import validate_target_values
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console as mc
from agentworks.vms.admin import AdminConfig
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.mark.parametrize("admin_shell", [True, False])
def test_valid_line_secret_and_bad_pane_context_preserve_all_runtimes(
    db: Database,
    console_target_factory,
    monkeypatch: pytest.MonkeyPatch,
    admin_shell: bool,
):
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(
        db,
        name="con",
        vm_name="vm1",
        session_specs=[] if admin_shell else ["alpha+1"],
        add_admin_shell=admin_shell,
    )
    config = _StubConfig()
    config.admin = AdminConfig.model_validate(
        {
            "env": {"TOKEN": {"secret": "pane-key"}, "LOCATION": "${agw:session_name}"},
        }
    )
    monkeypatch.setattr("agentworks.vms.admin_templates.resolve_live_template", lambda *a: config.admin)
    model = TmuxModel()
    model.seed_session("aw-console-con", "old")
    model.seed_session("aw-console-build+con", "partial")
    target = console_target_factory(model)
    before = deepcopy(model._sessions)
    values = {"pane-key": "literal${agw:session_name}-secret"}
    secret_target = SecretTarget(vm={"TOKEN": EnvEntry.model_validate({"secret": "pane-key"})})
    # The line-use check cannot validate the later consumer's runtime context.
    validate_target_values([secret_target], values)
    monkeypatch.setattr(mc, "_console_build_secret_targets", lambda *a, **kw: [secret_target])
    monkeypatch.setattr(
        "agentworks.secrets.resolver.Resolver.resolve", lambda self: validate_target_values([secret_target], values)
    )
    monkeypatch.setattr("agentworks.secrets.resolver.Resolver.values", property(lambda self: dict(values)))
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda *a, **kw: pytest.fail("resolved a source"))

    with pytest.raises(ValidationError) as caught:
        mc.restart_console(db, config, name="con", interaction=TtyInteractionPolicy.REFUSE)

    assert model._sessions == before
    assert not any("kill-session" in command or "new-session" in command for command in target.commands)
    assert values["pane-key"] not in str(caught.value)


@pytest.mark.parametrize("staging", [False, True])
def test_healthy_or_cleanup_only_validation_skips_unused_environments(
    db: Database,
    console_target_factory,
    monkeypatch: pytest.MonkeyPatch,
    staging: bool,
):
    _seed_vm(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=[], add_admin_shell=True)
    model = TmuxModel()
    model.seed_session("aw-console-con", "old")
    if staging:
        model.seed_session("aw-console-build+con", "partial")
    target = console_target_factory(model)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    with mc.guarded_console_selection(db, "con") as (console, guard):
        prepared = mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=db.get_vm("vm1"),
            target=target,
            held_native_guard=guard,
        )
        before = list(target.commands)
        monkeypatch.setattr(mc, "_resolve_admin_env", lambda *a, **kw: pytest.fail("composed unused admin env"))
        monkeypatch.setattr(mc, "_resolve_pane_env", lambda *a, **kw: pytest.fail("composed unused sidecar env"))
        mc.validate_console_values(prepared, {})
        assert target.commands == before


def test_pure_build_validation_visits_each_new_shell_with_promoted_user(
    db: Database, console_target_factory, monkeypatch
):
    _seed_vm(db)
    _seed_sessions(db, ["admin-session", "attach-only"])
    db.insert_agent("bot", "vm1", linux_user="bot-user")
    db.insert_session(
        "agent-session",
        "ws-vm1",
        "default",
        SessionMode.AGENT,
        agent_name="bot",
        socket_path="/tmp/agent.sock",
    )
    create_console_record(
        db,
        name="con",
        vm_name="vm1",
        session_specs=["admin-session+1", "agent-session+2", "attach-only"],
        add_admin_shell=True,
    )
    db.update_console_shells("con", "agent-session", [{"cwd": None, "admin": False}, {"cwd": None, "admin": True}])
    model = TmuxModel()
    target = console_target_factory(model)
    registry = bootstrap.load_request_registry(_StubConfig(), live_database=db)
    calls = []
    with mc.guarded_console_selection(db, "con") as (console, guard):
        prepared = mc.prepare_console_reconcile(
            db,
            registry,
            console=console,
            vm=db.get_vm("vm1"),
            target=target,
            held_native_guard=guard,
        )
        before = list(target.commands)
        monkeypatch.setattr(mc, "_resolve_admin_env", lambda *a, **kw: calls.append(("standalone",)) or {})
        monkeypatch.setattr(
            mc,
            "_resolve_pane_env",
            lambda *a, **kw: (
                calls.append(
                    (
                        kw["session"].name,
                        kw["pane_user"],
                        kw["is_admin_pane"],
                    )
                )
                or {}
            ),
        )
        monkeypatch.setattr(
            "agentworks.secrets.resolve.active_sources", lambda *a, **kw: pytest.fail("resolved a source")
        )
        mc.validate_console_values(prepared, {})
        assert target.commands == before
    assert calls == [
        ("standalone",),
        ("admin-session", "admin", True),
        ("agent-session", "bot-user", False),
        ("agent-session", "admin", True),
    ]

"""``vm start`` / ``vm stop`` through the orchestrated model: the
lifecycle commands' shared derived graph, the boundary-burst parity
(these commands open NO activation gate: the power op IS the
operation), the operator-stopped flag semantics end to end, and the VM
scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops and the Tailscale verification are
the fakes. ``vm delete`` shares the same composition root
(``_live_vm_boundary``); its failure discipline and its no-gate /
boundary-burst pins live in ``test_delete_vm_gating.py``.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import VMStatus
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    if operator_stopped:
        db.set_operator_stopped("box", True)


def _fake_power(monkeypatch: pytest.MonkeyPatch, status: VMStatus) -> list[str]:
    """Fake the platform's backend power ops (recording the op order)
    and the Tailscale verification; everything upstream of them
    (registry, resolver, preflight, resolve) runs for real."""
    events: list[str] = []
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or status,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr(ProxmoxPlatform, "stop", lambda self, row, ctx: events.append("stop"))
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale"))
    return events


# -- the derived graph --------------------------------------------------------


def test_lifecycle_graph_derives_from_row(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """The lifecycle commands' shared graph (start / stop / delete all
    build it through ``_live_vm_boundary``): the live VM whose row's
    site field is the vm-site edge, nothing else, so the union is the
    site's config secret alone. Each command's boundary-burst test
    below pins that union per command."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)


# -- vm start: boundary burst, flag clear, short-circuits ---------------------


def test_start_stopped_vm_resolves_once_starts_and_clears_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The mirror of the tracer's gate-prompt parity, no-gate shape:
    exactly ONE boundary burst covering the union, nothing resolved
    twice, nothing after; the start drives through the node's held
    platform; the operator-stopped flag is cleared (an explicit start
    is operator intent)."""
    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db, operator_stopped=True)
    events = _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"], ["tailscale-auth-key"]]
    assert events == ["status", "start", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert any("VM 'box' is ready" in m for m in captured_output.info)
    assert not any("already running" in m for m in captured_output.info)


def test_start_running_vm_short_circuits_but_still_clears_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """An already-running VM: no start op, the 'already running' info
    (and no 'is ready' noise), Tailscale still verified, and the flag
    still cleared; the boundary still costs exactly one burst."""
    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db, operator_stopped=True)
    events = _fake_power(monkeypatch, VMStatus.RUNNING)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"], ["tailscale-auth-key"]]
    assert events == ["status", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert any("VM 'box' is already running" in m for m in captured_output.info)
    assert not any("is ready" in m for m in captured_output.info)


class _TrackedValues(dict[str, str]):
    def __init__(self, events: list[str]) -> None:
        super().__init__({"tailscale-auth-key": "ts-key"})
        self._events = events

    def clear(self) -> None:
        self._events.append("values-clear")
        super().clear()


def _patch_recording_start_hold(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)

    @contextlib.contextmanager
    def _hold(self: object, row: object, *, config: object) -> Iterator[None]:
        events.append("hold-enter")
        try:
            yield
        finally:
            events.append("hold-exit")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _hold)


def _patch_actual_ensure_sequence(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    failure_stage: str | None = None,
    failure: BaseException | None = None,
) -> dict[str, int]:
    import agentworks.ssh_config as ssh_config
    import agentworks.transports as transports
    from agentworks.vms.manager.tailscale import _ensure_tailscale as actual_ensure

    calls = {"verify": 0, "native": 0, "rejoin": 0, "final-wait": 0, "sync": 0}

    def _raise_at(stage: str) -> None:
        if failure_stage == stage:
            assert failure is not None
            raise failure

    class _Target:
        logger = None

    def _verify() -> None:
        calls["verify"] += 1

    def _native(*args: object, **kwargs: object) -> _Target:
        calls["native"] += 1
        return _Target()

    def _rejoin(*args: object, **kwargs: object) -> None:
        calls["rejoin"] += 1
        events.append("rejoin")
        _raise_at("rejoin")
        database = cast("Database", args[0])
        name = cast("str", args[1])
        database.update_vm_tailscale(name, "100.64.0.10")

    def _final_wait(*args: object, **kwargs: object) -> bool:
        calls["final-wait"] += 1
        events.append("final-wait")
        _raise_at("final-wait")
        return True

    def _sync(*args: object, **kwargs: object) -> None:
        calls["sync"] += 1

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", _verify)
    monkeypatch.setattr(vm_manager, "rejoin_tailscale", _rejoin)
    monkeypatch.setattr(transports, "native_transport", _native)
    monkeypatch.setattr(transports, "transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(transports, "wait_for_reconnect", _final_wait)
    monkeypatch.setattr(ssh_config, "sync_ssh_config", _sync)

    def _recording_ensure(*args: object, **kwargs: object) -> None:
        events.append("ensure-enter")
        cast("Callable[..., None]", actual_ensure)(*args, **kwargs)
        events.append("ensure-return")

    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _recording_ensure)
    return calls


def test_start_healthy_probe_stays_inside_hold_and_never_acquires_auth(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.secrets as secret_package

    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _patch_recording_start_hold(monkeypatch, events)
    monkeypatch.setattr(
        vm_manager,
        "_tailscale_rejoin_required",
        lambda *args, **kwargs: events.append("probe-false") or False,
    )
    monkeypatch.setattr(
        secret_package,
        "resolve_for_command",
        lambda *args, **kwargs: pytest.fail("healthy start acquired Tailscale auth"),
    )

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["hold-enter", "probe-false", "hold-exit"]


def test_start_rejoin_orders_acquisition_reader_ensure_cleanup_and_release(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.orchestration.secrets as orchestration_secrets
    import agentworks.secrets as secret_package
    import agentworks.vms.manager.power as power
    import agentworks.vms.templates as templates

    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _patch_recording_start_hold(monkeypatch, events)
    monkeypatch.setattr(
        vm_manager,
        "_tailscale_rejoin_required",
        lambda *args, **kwargs: events.append("probe-true") or True,
    )
    monkeypatch.setattr(
        templates,
        "resolve_template",
        lambda registry, name: SimpleNamespace(tailscale_auth_key="tailscale-auth-key"),
    )
    monkeypatch.setattr(power, "_lookup_or_synthesize_secret", lambda *args: object())
    values = _TrackedValues(events)
    monkeypatch.setattr(
        secret_package,
        "resolve_for_command",
        lambda *args, **kwargs: events.append("resolve") or values,
    )

    class _Reader:
        def __init__(self, mapping: dict[str, str], names: object) -> None:
            self._tracks_tailscale = tuple(cast("Iterable[str]", names)) == ("tailscale-auth-key",)
            if self._tracks_tailscale:
                events.append("reader-build")
            self._mapping = mapping

        def get(self, name: str) -> str:
            if self._tracks_tailscale:
                events.append("auth-read")
            return self._mapping[name]

    monkeypatch.setattr(orchestration_secrets, "ScopedSecrets", _Reader)

    calls = _patch_actual_ensure_sequence(monkeypatch, events)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == [
        "hold-enter",
        "probe-true",
        "resolve",
        "reader-build",
        "ensure-enter",
        "auth-read",
        "rejoin",
        "final-wait",
        "ensure-return",
        "values-clear",
        "hold-exit",
    ]
    assert values == {}
    assert calls == {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 1, "sync": 1}


def test_start_lazy_repair_validates_after_start_and_delivery_before_rejoin_work(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.secrets as secret_package
    import agentworks.transports as transports
    from agentworks.errors import ValidationError
    from agentworks.secrets.orchestration import resolve_for_command as actual_resolve
    from agentworks.vms.manager.tailscale import _ensure_tailscale as actual_ensure

    auth_key = "tskey-prefix\r\ntskey-suffix\r\n"
    config = make_config()
    _seed_vm(db)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", auth_key)
    events: list[str] = []
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform,
        "start",
        lambda self, row, ctx: events.append("start"),
    )
    monkeypatch.setattr(
        ProxmoxPlatform,
        "vm_active",
        lambda self, row, *, config: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        transports,
        "transport",
        lambda *args, **kwargs: events.append("probe-transport") or object(),
    )
    monkeypatch.setattr(
        transports,
        "wait_for_reconnect",
        lambda *args, **kwargs: events.append("repair-required") or False,
    )

    def _resolve(*args: object, **kwargs: object) -> dict[str, str]:
        events.append("late-resolve")
        return actual_resolve(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secret_package, "resolve_for_command", _resolve)

    def _ensure(*args: object, **kwargs: object) -> None:
        events.append("ensure")
        actual_ensure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _ensure)
    monkeypatch.setattr(
        vm_manager,
        "verify_tailscale_available",
        lambda: pytest.fail("line-unsafe late key reached Tailscale availability work"),
    )

    with pytest.raises(ValidationError) as caught:
        vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == [
        "status",
        "start",
        "probe-transport",
        "repair-required",
        "late-resolve",
        "ensure",
    ]
    refreshed = db.get_vm("box")
    assert refreshed is not None
    assert refreshed.tailscale_host == "100.64.0.9"
    assert "tskey-prefix" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("failure_stage", ["probe", "resolve", "reader-build", "auth-read", "rejoin", "final-wait"])
def test_start_tailscale_failure_matrix_cleans_before_one_release_without_retry(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    import agentworks.orchestration.secrets as orchestration_secrets
    import agentworks.secrets as secret_package
    import agentworks.vms.manager.power as power
    import agentworks.vms.templates as templates

    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _patch_recording_start_hold(monkeypatch, events)
    failure = failure_type(failure_stage)

    def _stage(name: str) -> None:
        events.append(name)
        if failure_stage == name:
            raise failure

    def _probe(*args: object, **kwargs: object) -> bool:
        _stage("probe")
        return True

    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", _probe)
    monkeypatch.setattr(
        templates,
        "resolve_template",
        lambda registry, name: SimpleNamespace(tailscale_auth_key="tailscale-auth-key"),
    )
    monkeypatch.setattr(power, "_lookup_or_synthesize_secret", lambda *args: object())
    values = _TrackedValues(events)

    def _acquire(*args: object, **kwargs: object) -> dict[str, str]:
        assert kwargs["interaction"] is TtyInteractionPolicy.REFUSE
        _stage("resolve")
        return values

    monkeypatch.setattr(secret_package, "resolve_for_command", _acquire)

    class _Reader:
        def __init__(self, mapping: dict[str, str], names: object) -> None:
            self._tracks_tailscale = tuple(cast("Iterable[str]", names)) == ("tailscale-auth-key",)
            if self._tracks_tailscale:
                _stage("reader-build")
            self._mapping = mapping

        def get(self, name: str) -> str:
            if self._tracks_tailscale:
                _stage("auth-read")
            return self._mapping[name]

    monkeypatch.setattr(orchestration_secrets, "ScopedSecrets", _Reader)

    calls = _patch_actual_ensure_sequence(
        monkeypatch,
        events,
        failure_stage=failure_stage,
        failure=failure,
    )

    with pytest.raises(failure_type) as caught:
        vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert caught.value is failure
    assert events.count(failure_stage) == 1
    assert events.count("hold-enter") == events.count("hold-exit") == 1
    assert events[-1] == "hold-exit"
    if failure_stage in {"reader-build", "auth-read", "rejoin", "final-wait"}:
        assert events[-2] == "values-clear"
        assert values == {}
    else:
        assert "values-clear" not in events

    expected_before_release = {
        "probe": ["hold-enter", "probe"],
        "resolve": ["hold-enter", "probe", "resolve"],
        "reader-build": ["hold-enter", "probe", "resolve", "reader-build", "values-clear"],
        "auth-read": [
            "hold-enter",
            "probe",
            "resolve",
            "reader-build",
            "ensure-enter",
            "auth-read",
            "values-clear",
        ],
        "rejoin": [
            "hold-enter",
            "probe",
            "resolve",
            "reader-build",
            "ensure-enter",
            "auth-read",
            "rejoin",
            "values-clear",
        ],
        "final-wait": [
            "hold-enter",
            "probe",
            "resolve",
            "reader-build",
            "ensure-enter",
            "auth-read",
            "rejoin",
            "final-wait",
            "values-clear",
        ],
    }
    assert events == [*expected_before_release[failure_stage], "hold-exit"]
    no_ensure_work = {"verify": 0, "native": 0, "rejoin": 0, "final-wait": 0, "sync": 0}
    expected_calls = {
        "probe": no_ensure_work,
        "resolve": no_ensure_work,
        "reader-build": no_ensure_work,
        "auth-read": no_ensure_work,
        "rejoin": {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 0, "sync": 0},
        "final-wait": {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 1, "sync": 0},
    }
    assert calls == expected_calls[failure_stage]


# -- vm stop: boundary burst, flag set, short-circuits ------------------------


def test_stop_running_vm_resolves_once_stops_and_sets_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    events = _fake_power(monkeypatch, VMStatus.RUNNING)

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "stop"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is True
    assert any(m == "VM 'box' stopped" for m in captured_output.info)


def test_stop_sets_flag_before_already_stopped_shortcut(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Stopping an idle-stopped VM still records the intent, and the
    message says so instead of the misleading bare 'already stopped'
    (the command DID change something: auto-start is now off)."""
    config = make_config()
    _seed_vm(db)
    events = _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is True
    assert events == ["status"]  # short-circuited, no stop op
    assert resolve_counter == [["proxmox-token"]]
    # The boundary resolve emits a "Resolved ..." info line of its own; the
    # stop message is the remaining info line.
    (message,) = [m for m in captured_output.info if not m.startswith("Resolved ")]
    assert "stopped on its own" in message
    assert "will not be auto-started" in message


def test_stop_of_a_manually_stopped_vm_is_a_true_noop(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Only when the intent was ALREADY recorded does 'already' apply,
    and it names the manual state."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (message,) = [m for m in captured_output.info if not m.startswith("Resolved ")]
    assert message == "VM 'box' is already manually stopped"


# -- the operation scope reaches readiness ------------------------------------


def test_vm_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db)
    _fake_power(monkeypatch, VMStatus.RUNNING)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None

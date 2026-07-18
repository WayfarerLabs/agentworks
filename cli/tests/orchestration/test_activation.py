"""The activation gate: fast path, auto-start vs operator-stopped
refusal, span open/close, and the just-in-time gate-secret resolve.

The fake target plays the live VM node's power-state surface
(``GateTarget``); the oracle semantics are today's
``vms.manager.ensure_active`` / ``keep_active``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError
from agentworks.orchestration.activation import activation_gate, ensure_active

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.base import SecretReader


class _Target:
    """Recording ``GateTarget`` double."""

    def __init__(
        self,
        *,
        active: bool = False,
        stopped: bool = False,
        operator_stopped: bool = False,
        refs: tuple[str, ...] = (),
    ) -> None:
        self._active = active
        self._stopped = stopped
        self._operator_stopped = operator_stopped
        self._refs = refs
        self.events: list[str] = []
        self.seen_secrets: dict[str, str] = {}

    def gate_secret_refs(self) -> tuple[str, ...]:
        return self._refs

    def confirmed_active(self) -> bool:
        self.events.append("probe")
        return self._active

    def observed_stopped(self, gate_secrets: SecretReader) -> bool:
        self.events.append("status")
        for name in self._refs:
            self.seen_secrets[name] = gate_secrets.get(name)
        return self._stopped

    def auto_start(self, gate_secrets: SecretReader) -> None:
        if self._operator_stopped:
            # The node's own refusal (the ``operator_stopped`` flag,
            # re-read for the race guard): auto-start never overrides
            # an operator's explicit stop.
            raise StateError(
                "VM 'box' was manually stopped so it will not be auto-started",
                entity_kind="vm",
                entity_name="box",
                hint="start it with: agw vm start box",
            )
        self.events.append("start")

    @contextlib.contextmanager
    def hold_active(self) -> Iterator[None]:
        self.events.append("hold-open")
        try:
            yield
        finally:
            self.events.append("hold-close")


def _resolver(values: dict[str, str], log: list[str]) -> Callable[[str], str]:
    def resolve(name: str) -> str:
        log.append(f"resolve:{name}")
        return values[name]

    return resolve


def test_fast_path_touches_no_secret_and_no_backend() -> None:
    """A confirmed-active target costs nothing: no resolution, no
    status probe, no start (env-backed setups see no interaction at
    all; today's Tailscale short-circuit)."""
    target = _Target(active=True, refs=("proxmox-token",))
    log: list[str] = []
    values = ensure_active(target, _resolver({"proxmox-token": "t"}, log))
    assert values == {}
    assert log == []
    assert target.events == ["probe"]


def test_gate_secrets_resolve_just_in_time_before_any_power_op() -> None:
    """The one sanctioned resolution outside the boundary pass: the
    narrow declared gate secrets, resolved before the authenticated
    status probe that needs them, and returned for the boundary seed."""
    target = _Target(stopped=True, refs=("proxmox-token",))
    log: list[str] = []
    values = ensure_active(target, _resolver({"proxmox-token": "tok"}, log))
    assert log == ["resolve:proxmox-token"]
    assert target.events == ["probe", "status", "start"]
    assert target.seen_secrets == {"proxmox-token": "tok"}
    assert values == {"proxmox-token": "tok"}


def test_running_target_is_not_started() -> None:
    """RUNNING or indeterminate proceeds: a transient status failure
    must not trigger a spurious start."""
    target = _Target(stopped=False, refs=("proxmox-token",))
    log: list[str] = []
    ensure_active(target, _resolver({"proxmox-token": "tok"}, log))
    assert target.events == ["probe", "status"]


def test_operator_stopped_refusal_propagates_from_the_node() -> None:
    """The node is the authority on auto-start: the gate surfaces its
    typed refusal (with the explicit-start hint) untouched."""
    target = _Target(stopped=True, operator_stopped=True)
    with pytest.raises(StateError, match="manually stopped") as exc_info:
        ensure_active(target, _resolver({}, []))
    assert exc_info.value.hint == "start it with: agw vm start box"
    assert "start" not in target.events


def test_gate_span_opens_after_convergence_and_closes_on_success() -> None:
    target = _Target(stopped=True, refs=())
    with activation_gate(target, _resolver({}, [])) as values:
        assert values == {}
        target.events.append("body")
    assert target.events == ["probe", "status", "start", "hold-open", "body", "hold-close"]


def test_gate_span_closes_on_failure_in_the_body() -> None:
    """The span closes on success and failure alike, AFTER the body
    (where any unwind runs), so teardown still reaches a held
    target."""
    target = _Target(active=True)
    with pytest.raises(RuntimeError, match="boom"), activation_gate(
        target, _resolver({}, [])
    ):
        raise RuntimeError("boom")
    assert target.events == ["probe", "hold-open", "hold-close"]


def test_refusal_precedes_the_span() -> None:
    target = _Target(stopped=True, operator_stopped=True)
    with pytest.raises(StateError, match="manually stopped"), activation_gate(target, _resolver({}, [])):
        pass  # pragma: no cover - never reached
    assert "hold-open" not in target.events


def test_gate_reader_is_scoped_to_declared_gate_secrets() -> None:
    """The gate's reader is a ``ScopedSecrets`` view: a power op cannot
    read past the target's declared gate needs."""

    class _Greedy(_Target):
        def observed_stopped(self, gate_secrets: SecretReader) -> bool:
            gate_secrets.get("git-token-gh")  # undeclared
            return False

    target = _Greedy(refs=("proxmox-token",))
    with pytest.raises(StateError, match="not declared"):
        ensure_active(target, _resolver({"proxmox-token": "t"}, []))

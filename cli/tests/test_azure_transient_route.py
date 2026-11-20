"""An Azure route's lifecycle: heal and converge on enter, poke its own allow, remove it on every exit.

The poke's own cleanup is covered here too: a create that failed, timed out
or was interrupted is removed by the poke, which alone knows its nonce name,
and a Ctrl-C during any removal waits for it. Shared setup lives in
``tests._azure_platform_support``.
"""

from __future__ import annotations

import signal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from azure.core.exceptions import ServiceResponseError

from agentworks.capabilities.base import RunContext
from agentworks.plugins.azure import network as azure_network
from agentworks.plugins.azure.errors import AzureError
from agentworks.plugins.azure.network import (
    ALLOW_PRIORITY_BAND_START,
    DENY_ALL_INBOUND_RULE_NAME,
    LEGACY_SSH_RULE_NAME,
    TRANSIENT_ALLOW_RULE_PREFIX,
)
from tests._azure_platform_support import (
    _DETECTED_PREFIX,
    _assert_allow_shape,
    _assert_deny_shape,
    _fake_vm,
    _FakeSecurityRules,
    _install_fakes,
    _operator_config,
    _platform,
    _Poller,
)
from tests._azure_platform_support import _stub_egress_detection as _stub_egress_detection

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput


class _InterruptedWait(_Poller):
    """A create whose first wait a Ctrl-C cuts short, after which every wait returns at once, as a
    join does on CPython 3.12 once an interrupt cut one short. Azure reports the create succeeded on
    the ``settles_after``-th read of whether it is final, or never when that is ``None``."""

    def __init__(self, value: object, *, settles_after: int | None = 1) -> None:
        super().__init__(value, finishes=False)
        self.settles_after = settles_after
        self.waits = 0
        self.reads = 0

    def result(self, timeout: float | None = None) -> object:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return None

    def done(self) -> bool:
        return self.waits > 0

    def finished(self) -> bool:
        self.reads += 1
        return self.settles_after is not None and self.reads >= self.settles_after

    def status(self) -> str:
        return "Succeeded" if self.settles_after is not None and self.reads >= self.settles_after else "InProgress"


class TestTransientRoute:
    def test_enter_heals_missing_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A NIC with no public IP (a VM created under the old detach
        scheme) converges on enter: the IP is created (idempotent, same
        name as create's) and attached to the NIC."""
        network = _install_fakes(monkeypatch, public_ip_attached=False).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform()._transient_route(vm, RunContext()):
            assert network.public_ip_addresses.created == [("rg1", "vm1-ip")]
            assert len(network.network_interfaces.updated) == 1
            nic = network.network_interfaces.updated[0][2]
            assert nic.ip_configurations[0].public_ip_address.id == "/pip/id"

    def test_enter_skips_heal_when_public_ip_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The steady state (permanent IP already on the NIC) is a
        single NIC read: no IP create, no NIC update."""
        network = _install_fakes(monkeypatch, public_ip_attached=True).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform()._transient_route(vm, RunContext()):
            assert network.public_ip_addresses.created == []
            assert network.network_interfaces.updated == []

    def test_enter_converges_then_pokes_and_exit_removes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enter re-pins the deny to 200 BEFORE any allow allocation
        (Azure requires unique priorities per direction, so a legacy deny
        still at 100 would occupy a band slot until it moves), deletes
        the legacy standing SSH rule, then pokes this operation's own
        nonce-named allow at the lowest free band slot. Exit deletes
        exactly that rule; the deny is never deleted. The legacy deny
        seeded at 100 here pins the freeing: after the re-pin, the poke
        lands on the just-vacated slot 100."""
        network = _install_fakes(monkeypatch).network
        # A legacy VM: its deny sits at the old priority 100, squatting
        # on the band's first slot until convergence re-pins it.
        network.security_rules.rules[DENY_ALL_INBOUND_RULE_NAME] = SimpleNamespace(
            name=DENY_ALL_INBOUND_RULE_NAME, priority=100, direction="Inbound"
        )
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with _platform()._transient_route(vm, RunContext(config=_operator_config(["198.51.100.0/24"]))):
            # Call order: deny re-pin (create), legacy SSH delete, allow poke.
            kinds = [(e[0], e[3]) for e in network.security_rules.events]
            transient = network.security_rules.transient_names()
            assert len(transient) == 1
            assert kinds == [
                ("create", DENY_ALL_INBOUND_RULE_NAME),
                ("delete", LEGACY_SSH_RULE_NAME),
                ("create", transient[0]),
            ]
            creates = network.security_rules.creates()
            _assert_deny_shape(creates[0][3])
            _assert_allow_shape(
                creates[1][3],
                [_DETECTED_PREFIX, "198.51.100.0/24"],
                name=transient[0],
                # The re-pin freed slot 100; the poke takes it.
                priority=ALLOW_PRIORITY_BAND_START,
            )

        # Exit removed this operation's rule, and only it: the deny (and
        # the legacy rule, already gone) saw no further ops.
        assert network.security_rules.deletes() == [
            ("rg1", "vm1-nsg", LEGACY_SSH_RULE_NAME),
            ("rg1", "vm1-nsg", transient[0]),
        ]
        assert [e[3] for e in network.security_rules.events if e[0] == "create"] == [
            DENY_ALL_INBOUND_RULE_NAME,
            transient[0],
        ]

    def test_an_interrupt_during_the_routes_delete_waits_for_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl-C the moment the script ends: the rule's delete still reaches Azure, then the interrupt raises."""
        network = _install_fakes(monkeypatch).network
        rules = network.security_rules
        delete = rules.begin_delete

        armed = False

        def interrupted_delete(rg: str, nsg: str, rule_name: str, **options: Any) -> Any:
            if armed:
                signal.raise_signal(signal.SIGINT)
            return delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_delete", interrupted_delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            armed = True

        (transient,) = rules.transient_names()
        assert ("rg1", "vm1-nsg", transient) in rules.deletes()

    def test_convergence_tolerates_absent_legacy_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 on the legacy SSH delete (an already-converged or
        new-scheme VM) is expected; the enter proceeds to the poke."""
        from azure.core.exceptions import ResourceNotFoundError

        network = _install_fakes(monkeypatch).network

        rules: _FakeSecurityRules = network.security_rules
        real_delete = rules.begin_delete

        def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
            if rule_name == LEGACY_SSH_RULE_NAME:
                raise ResourceNotFoundError("no legacy rule")
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(network.security_rules, "begin_delete", _delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        body_ran = False
        with _platform()._transient_route(vm, RunContext()):
            body_ran = True

        assert body_ran
        created = [e[3] for e in network.security_rules.events if e[0] == "create"]
        assert created[0] == DENY_ALL_INBOUND_RULE_NAME
        assert created[1].startswith(TRANSIENT_ALLOW_RULE_PREFIX)

    def test_poke_result_failure_cleans_its_own_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A client-side failure AFTER a server-side-successful allow
        create (the poller's ``.result()`` raising) must not leave any
        ephemeral allow standing: each failed attempt best-effort
        deletes its own nonce name (only the poke knows it), and the
        wrapped error propagates after the attempts are exhausted."""

        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update

        class _ExplodingPoller(_Poller):
            def result(self, timeout: float | None = None) -> object:
                raise RuntimeError("client-side poll failure")

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            # Record first (the server-side create succeeded), then hand
            # back a poller whose result() dies client-side.
            poller = real_create(rg, nsg, rule_name, rule)
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                return _ExplodingPoller(rule)
            return poller

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke fails")

        # Every attempted rule was created server-side and then removed
        # by the poke's own cleanup: nothing transient survives.
        attempted = rules.transient_names()
        assert attempted  # at least one attempt happened
        deleted = {d[2] for d in rules.deletes()}
        assert set(attempted) <= deleted
        assert not any(n.startswith(TRANSIENT_ALLOW_RULE_PREFIX) for n in rules.rules)

    def test_an_interrupt_during_the_poke_removes_its_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The route never learns the name of a rule whose create was interrupted, so the poke removes it."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                return _InterruptedWait(rule)
            return poller

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke is interrupted")

        (attempted,) = rules.transient_names()
        assert ("rg1", "vm1-nsg", attempted) in rules.deletes()

    def test_a_second_interrupt_during_the_pokes_cleanup_waits_for_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The route never learns the interrupted rule's name, so the poke's own delete must hold a second Ctrl-C."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update
        real_delete = rules.begin_delete

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                return _InterruptedWait(rule)
            return poller

        def interrupted_delete(rg: str, nsg: str, rule_name: str, **options: Any) -> Any:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                signal.raise_signal(signal.SIGINT)
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        monkeypatch.setattr(rules, "begin_delete", interrupted_delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke is interrupted")

        (attempted,) = rules.transient_names()
        assert ("rg1", "vm1-nsg", attempted) in rules.deletes()

    @pytest.mark.parametrize("settles_after", [3, None], ids=["settles", "never-settles"])
    def test_an_interrupted_poke_removes_its_rule_only_after_the_create_settles(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, settles_after: int | None
    ) -> None:
        """A delete racing the create can find nothing and let the rule land after it, so the create gets to settle.

        Every wait after the interrupt returns at once, so only reading Azure's state finds when it settles.
        """
        fakes = _install_fakes(monkeypatch)
        rules: _FakeSecurityRules = fakes.network.security_rules
        real_create, real_delete = rules.begin_create_or_update, rules.begin_delete
        created: list[_InterruptedWait] = []
        deleted_at: list[float] = []

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            if not rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                return poller
            created.append(_InterruptedWait(rule, settles_after=settles_after))
            return created[-1]

        def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                assert settles_after is None or created[-1].reads == settles_after, "the delete must follow the settle"
                deleted_at.append(fakes.clock.now)
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        monkeypatch.setattr(rules, "begin_delete", _delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke is interrupted")

        (attempted,) = rules.transient_names()
        assert ("rg1", "vm1-nsg", attempted) in rules.deletes()
        if settles_after is not None:
            assert captured_output.warnings == []
        else:
            assert deleted_at == [azure_network._UNFINISHED_CREATE_SETTLE_SECONDS]
            (warning,) = captured_output.warnings
            assert attempted in warning

    def test_a_poke_interrupted_before_its_create_returned_removes_and_names_its_rule(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """With no poller there is no create to wait for, and the request may still have reached Azure."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update
        attempted: list[str] = []

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                attempted.append(rule_name)
                raise KeyboardInterrupt
            return real_create(rg, nsg, rule_name, rule)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke is interrupted")

        (name,) = attempted
        assert ("rg1", "vm1-nsg", name) in rules.deletes()
        (warning,) = captured_output.warnings
        assert name in warning

    def test_a_held_interrupt_does_not_skip_the_unsettled_rule_warning(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A second Ctrl-C held through the removal is delivered when the hold ends, after the warning is out."""
        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create, real_delete = rules.begin_create_or_update, rules.begin_delete

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            return (
                _InterruptedWait(rule, settles_after=None)
                if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX)
                else poller
            )

        def interrupted_delete(rg: str, nsg: str, rule_name: str, **options: Any) -> Any:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                signal.raise_signal(signal.SIGINT)
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        monkeypatch.setattr(rules, "begin_delete", interrupted_delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(KeyboardInterrupt), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke is interrupted")

        (attempted,) = rules.transient_names()
        assert ("rg1", "vm1-nsg", attempted) in rules.deletes()
        (warning,) = captured_output.warnings
        assert attempted in warning

    def test_poke_attempt_cleanup_failure_warns(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """When an attempt's cleanup delete ALSO fails, the possibly
        server-side-created rule may be left standing, and that must not
        be silent: the removal path warns naming the rule, the NSG, and
        the prefixes that remain allowed."""

        network = _install_fakes(monkeypatch).network
        rules: _FakeSecurityRules = network.security_rules
        real_create = rules.begin_create_or_update

        class _ExplodingPoller(_Poller):
            def result(self, timeout: float | None = None) -> object:
                raise RuntimeError("client-side poll failure")

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            poller = real_create(rg, nsg, rule_name, rule)
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                # The create landed server-side; poison the cleanup too.
                rules.delete_error = RuntimeError("cleanup delete failed")
                return _ExplodingPoller(rule)
            return poller

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(AzureError), _platform()._transient_route(vm, RunContext()):
            pytest.fail("the body must not run when the poke fails")

        attempted = rules.transient_names()
        assert attempted
        # One warn per failed cleanup, naming the rule, NSG, and prefixes.
        for rule_name in attempted:
            warning = next(w for w in captured_output.warnings if rule_name in w)
            assert "vm1-nsg" in warning
            assert _DETECTED_PREFIX in warning

    @staticmethod
    def _first_poke_fails(
        monkeypatch: pytest.MonkeyPatch, first: Callable[[str, object], _Poller]
    ) -> tuple[_FakeSecurityRules, list[str], list[float]]:
        """Open a route whose first transient create goes through ``first`` and whose retry succeeds.

        Returns the rules, the first attempt's rule name, and the fake clock's reading at each
        transient delete.
        """
        fakes = _install_fakes(monkeypatch)
        rules: _FakeSecurityRules = fakes.network.security_rules
        real_create, real_delete = rules.begin_create_or_update, rules.begin_delete
        first_name: list[str] = []
        deleted_at: list[float] = []

        def _create(rg: str, nsg: str, rule_name: str, rule: object, **options: Any) -> _Poller:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX) and not first_name:
                first_name.append(rule_name)
                return first(rule_name, rule)
            return real_create(rg, nsg, rule_name, rule)

        def _delete(rg: str, nsg: str, rule_name: str, **options: Any) -> _Poller:
            if rule_name.startswith(TRANSIENT_ALLOW_RULE_PREFIX):
                deleted_at.append(fakes.clock.now)
            return real_delete(rg, nsg, rule_name)

        monkeypatch.setattr(rules, "begin_create_or_update", _create)
        monkeypatch.setattr(rules, "begin_delete", _delete)
        vm: VMRow = _fake_vm()  # type: ignore[assignment]
        with _platform()._transient_route(vm, RunContext()):
            assert rules.transient_names(), "the retry opens the route"
        return rules, first_name, deleted_at

    @pytest.mark.parametrize("settles_after", [3, None], ids=["settles", "never-settles"])
    def test_an_accepted_poke_whose_wait_fails_settles_before_its_rule_goes(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, settles_after: int | None
    ) -> None:
        """Polling can fail while the create still runs, so the rule is removed only once it settles, then retried."""
        reads: list[int] = []

        class _PollingFails(_Poller):
            def result(self, timeout: float | None = None) -> object:
                raise ServiceResponseError("polling connection reset")

            def finished(self) -> bool:
                reads.append(1)
                return settles_after is not None and len(reads) >= settles_after

            def status(self) -> str:
                return "Succeeded" if settles_after is not None and len(reads) >= settles_after else "InProgress"

        rules, (first,), deleted_at = self._first_poke_fails(
            monkeypatch, lambda _name, rule: _PollingFails(rule, finishes=False)
        )

        events = [(e[0], e[3]) for e in rules.events]
        (retry,) = rules.transient_names()
        assert events.index(("delete", first)) < events.index(("create", retry))
        if settles_after is not None:
            assert len(reads) == settles_after
            assert captured_output.warnings == []
        else:
            assert deleted_at[0] == azure_network._UNFINISHED_CREATE_SETTLE_SECONDS
            (warning,) = captured_output.warnings
            assert first in warning

    def test_exit_removes_allow_when_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The allow removal is a finally: it fires however the caller
        unwinds."""
        network = _install_fakes(monkeypatch).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="kaboom"), _platform()._transient_route(vm, RunContext()):
            raise RuntimeError("kaboom")

        rule_name = network.security_rules.transient_names()[0]
        assert network.security_rules.deletes()[-1] == ("rg1", "vm1-nsg", rule_name)

    def test_exit_removal_failure_warns_with_rule_and_prefixes(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A failed exit removal must not raise out of the finally, but
        the warning states the actual residual exposure: the rule, the
        NSG, and the exact prefixes that remain allowed (not the world)."""
        network = _install_fakes(monkeypatch).network
        vm: VMRow = _fake_vm()  # type: ignore[assignment]

        entered = False
        with _platform()._transient_route(vm, RunContext(config=_operator_config(["198.51.100.0/24"]))):
            entered = True
            # Poison ONLY the exit path's delete.
            network.security_rules.delete_error = RuntimeError("boom")

        assert entered
        rule_name = network.security_rules.transient_names()[0]
        warning = next(w for w in captured_output.warnings if rule_name in w)
        assert "vm1-nsg" in warning
        assert _DETECTED_PREFIX in warning
        assert "198.51.100.0/24" in warning

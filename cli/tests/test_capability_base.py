"""The ``Capability`` base: config-valid-by-construction, the
no-secret-machinery construction contract, and the per-op idempotency
markers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest

from agentworks.capabilities import Capability, idempotent_op, is_idempotent_op
from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference


class _SecretlessCap(Capability):
    name: ClassVar[str] = "plain"
    description: ClassVar[str] = "no config, no secrets"
    owner_kind: ClassVar[str] = "thing"


class _SecretCap(Capability):
    name: ClassVar[str] = "secretful"
    description: ClassVar[str] = "declares one secret"
    owner_kind: ClassVar[str] = "thing"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        from agentworks.resources.reference import ConfigReference

        return (
            ConfigReference(name="the-token", kind="secret", usage="the API token"),
        )


def test_construct_revalidates_config() -> None:
    """A shape error dies at construction, never later in preflight."""
    with pytest.raises(ConfigError, match="accepts no configuration"):
        _SecretlessCap("t1", {"stray": 1})


def test_construct_touches_no_secret_machinery() -> None:
    """Construction binds ``(name, config)`` and nothing else: no
    resolver, no reader, no registration (the boundary union comes
    from the plan's declared secret_refs). The never-again pin for
    the retired construct-time registration."""
    cap = _SecretCap("t1", {})
    assert not hasattr(cap, "resolver")


def test_base_preflight_is_a_no_op() -> None:
    """Resolvability prediction is CENTRAL (the holding node predicts
    over declarations via ``orchestration.secrets``), so the base's
    preflight has nothing to do, with or without declared secrets."""
    _SecretlessCap("t1", {}).preflight(RunContext())
    _SecretCap("t1", {}).preflight(RunContext())  # no resolver, no error


def test_idempotency_marker_reads_through_overrides() -> None:
    """The flag sits on the base's declaration; a subclass override
    inherits the contract without restating the marker."""

    class _Base(Capability):
        name: ClassVar[str] = "b"
        description: ClassVar[str] = ""
        owner_kind: ClassVar[str] = "thing"

        @idempotent_op
        def apply(self) -> None: ...

        def mint(self) -> None: ...

    class _Impl(_Base):
        def apply(self) -> None: ...

        def mint(self) -> None: ...

    assert is_idempotent_op(_Impl, "apply")
    assert not is_idempotent_op(_Impl, "mint")
    assert not is_idempotent_op(_Impl, "nonexistent")


def test_vm_platform_flags_start_stop_delete() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY, VMPlatform

    for op in ("start", "stop", "delete"):
        assert is_idempotent_op(VMPlatform, op), op
        for cls in VM_PLATFORM_REGISTRY.values():
            assert is_idempotent_op(cls, op), f"{cls.name}.{op}"
    # create is deliberately one-shot (collision check makes a re-run a
    # loud error, not a silent second resource).
    assert not is_idempotent_op(VMPlatform, "create")

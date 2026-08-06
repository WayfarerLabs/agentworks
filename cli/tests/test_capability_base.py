"""The ``Capability`` base: declare a model, receive an instance.

Construction validates the blob against the model the capability
DECLARES and binds the validated instance, so a shape error dies at
construction and everything downstream reads a typed field. Also here:
the no-secret-machinery construction contract and the per-op idempotency
markers.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

from agentworks.capabilities import Capability, idempotent_op, is_idempotent_op
from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef


class _NoConfig(AgwModel):
    """A capability that accepts no configuration says so with a model
    that has no fields, which is closed-world by construction."""


class _SecretfulConfig(AgwModel):
    """A config that names one secret, with a constant default."""

    token: Annotated[NonEmptyStr, SecretRef(usage="the API token", default_template="the-token")]


class _SecretlessCap(Capability):
    name: ClassVar[str] = "plain"
    description: ClassVar[str] = "no config, no secrets"
    owner_kind: ClassVar[str] = "thing"
    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = _NoConfig


class _SecretCap(Capability):
    name: ClassVar[str] = "secretful"
    description: ClassVar[str] = "declares one secret"
    owner_kind: ClassVar[str] = "thing"
    contract_version: ClassVar[int] = 1
    config_model: ClassVar[type[AgwModel]] = _SecretfulConfig


def test_construct_validates_config() -> None:
    """A shape error dies at construction, never later in preflight."""
    with pytest.raises(ConfigError, match="stray: unknown field"):
        _SecretlessCap("t1", {"stray": 1})


def test_construct_binds_the_validated_model_instance() -> None:
    """The result is KEPT, which is what lets an operation read a typed
    field instead of a dict key with a fallback beside it. The owner
    template resolves here too, so the bound name is the same one the
    graph carries."""
    cap = _SecretCap("t1", {})

    assert isinstance(cap.config, _SecretfulConfig)
    assert cap.config.token == "the-token"


def test_construct_extracts_the_declared_secret_refs() -> None:
    """Structurally, off the model: no capability code runs."""
    assert [ref.name for ref in _SecretCap("t1", {})._secret_refs] == ["the-token"]


def test_construct_touches_no_secret_machinery() -> None:
    """Construction binds the validated config and nothing else: no
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
        contract_version: ClassVar[int] = 1
        config_model: ClassVar[type[AgwModel]] = _NoConfig

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

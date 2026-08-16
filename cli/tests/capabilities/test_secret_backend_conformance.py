"""Phase 3 registration contract for nominal secret-backend classes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentworks.capabilities.conformance import conformance_error
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.secret_backend import (
    InteractionBroker,
    RemainingTime,
    SecretBackend,
    SecretSourceClient,
)
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, ResourceRef, SecretRef, model_is_complete
from tests.plugins._fixtures import ConformingSecretBackend

DESCRIPTOR = descriptor_for("secret-backend")


class GoodConfig(AgwModel):
    name: Literal["phase3-fixture"]


class GoodMapping(AgwRootModel[str]):
    pass


def _backend(**changes: object) -> type[ConformingSecretBackend]:
    attributes = {
        "name": "phase3-fixture",
        "description": "backend conformance fixture",
        "config_model": GoodConfig,
        "mapping_model": GoodMapping,
        **changes,
    }
    return type("Phase3FixtureBackend", (ConformingSecretBackend,), attributes)


def _reason(**changes: object) -> str:
    reason = conformance_error(DESCRIPTOR, _backend(**changes))
    assert reason is not None
    return reason


def test_a_nominal_concrete_version_two_backend_conforms() -> None:
    assert conformance_error(DESCRIPTOR, _backend()) is None


def test_a_structural_lookalike_is_rejected_before_its_members_are_considered() -> None:
    class Lookalike:
        name = "phase3-fixture"
        description = "looks complete"

    assert "does not derive from SecretBackend" in str(conformance_error(DESCRIPTOR, Lookalike))


def test_an_abstract_backend_is_rejected_as_not_constructible() -> None:
    class AbstractBackend(SecretBackend):
        name = "abstract"
        description = "still abstract"
        contract_version = 2
        config_model = GoodConfig
        mapping_model = GoodMapping
        interactive = False

    reason = conformance_error(DESCRIPTOR, AbstractBackend)
    assert reason is not None
    assert reason.startswith("it is abstract")
    assert "create_client" in reason


def test_a_non_callable_factory_hits_the_generic_operation_check_first() -> None:
    assert "required secret-backend operations: create_client" in _reason(create_client=None)


class MissingInteractiveBackend(SecretBackend):
    name = "phase3-fixture"
    description = "omits one required class fact"
    contract_version = 2
    config_model = GoodConfig
    mapping_model = GoodMapping

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return False

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        return None

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        raise NotImplementedError


def test_a_missing_required_class_attribute_is_rejected() -> None:
    assert conformance_error(DESCRIPTOR, MissingInteractiveBackend) == (
        "it is missing the required secret-backend attributes: interactive"
    )


@pytest.mark.parametrize("value", [0, 1, "yes", None], ids=("zero", "one", "string", "none"))
def test_interactive_must_be_exactly_bool(value: object) -> None:
    assert _reason(interactive=value) == f"its interactive class attribute is {value!r}, not a bool"


class UntaggedConfig(AgwModel):
    pass


class MistaggedConfig(AgwModel):
    name: Literal["somebody-else"]


class UnbuildableConfig(AgwModel):
    name: Literal["phase3-fixture"]
    unresolved: NeverDefined  # type: ignore[name-defined]  # noqa: F821


class UnbuildableMapping(AgwRootModel[Any]):
    root: NeverDefined  # type: ignore[name-defined]  # noqa: F821


class MisplacedMappingMarker(AgwModel):
    values: Annotated[list[str], SecretRef(usage="a misplaced lookup reference")]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"config_model": None}, "declares no config_model"),
        ({"config_model": object}, "config_model is"),
        ({"config_model": UnbuildableConfig}, "config_model UnbuildableConfig cannot be built"),
        ({"config_model": UntaggedConfig}, "config_model UntaggedConfig does not tag itself"),
        ({"config_model": MistaggedConfig}, "config_model MistaggedConfig does not tag itself"),
        ({"mapping_model": None}, "declares no mapping_model"),
        ({"mapping_model": AgwModel}, "mapping_model is"),
        ({"mapping_model": UnbuildableMapping}, "mapping_model UnbuildableMapping cannot be built"),
        (
            {"mapping_model": AgwRootModel[MisplacedMappingMarker]},
            "mapping_model declares a reference marker nothing can honor",
        ),
        ({"contract_version": 1}, "declares contract_version 1"),
    ],
    ids=(
        "missing-source-model",
        "wrong-source-model-base",
        "unbuildable-source-model",
        "untagged-source-model",
        "mistagged-source-model",
        "missing-mapping-model",
        "wrong-mapping-model-base",
        "unbuildable-mapping-model",
        "misplaced-mapping-marker",
        "old-contract-version",
    ),
)
def test_dual_model_and_version_rejection_matrix(changes: dict[str, object], expected: str) -> None:
    assert expected in _reason(**changes)


def test_mapping_models_do_not_need_a_discriminator_tag() -> None:
    class ScalarMapping(AgwRootModel[int]):
        pass

    assert conformance_error(DESCRIPTOR, _backend(mapping_model=ScalarMapping)) is None


class NestedSecretConfig(AgwModel):
    token: Annotated[str, SecretRef(usage="the nested credential")]


class SourceConfigWithNestedSecretRef(AgwModel):
    name: Literal["phase3-fixture"]
    nested: NestedSecretConfig


class SourceConfigWithDirectSecretRef(AgwModel):
    name: Literal["phase3-fixture"]
    token: Annotated[str, SecretRef(usage="the direct credential")]


class SourceConfigWithSecretRefList(AgwModel):
    name: Literal["phase3-fixture"]
    tokens: list[Annotated[str, SecretRef(usage="one listed credential")]]


class SourceConfigWithSecretRefMap(AgwModel):
    name: Literal["phase3-fixture"]
    tokens: dict[str, Annotated[str, SecretRef(usage="one mapped credential")]]


class SecretRefRoot(AgwRootModel[Annotated[str, SecretRef(usage="the rooted credential")]]):
    pass


class SourceConfigWithSecretRefRoot(AgwModel):
    name: Literal["phase3-fixture"]
    rooted: SecretRefRoot


class SecretRefArm(AgwModel):
    kind: Literal["secret"]
    token: Annotated[str, SecretRef(usage="the selected credential")]


class PlainArm(AgwModel):
    kind: Literal["plain"]
    value: str


class SourceConfigWithSecretRefUnionArm(AgwModel):
    name: Literal["phase3-fixture"]
    choice: Annotated[SecretRefArm | PlainArm, Field(discriminator="kind")]


@pytest.mark.parametrize(
    ("config_model", "path"),
    [
        (SourceConfigWithDirectSecretRef, "root.token"),
        (SourceConfigWithNestedSecretRef, "root.nested.token"),
        (SourceConfigWithSecretRefList, "root.tokens[]"),
        (SourceConfigWithSecretRefMap, "root.tokens{value}"),
        (SourceConfigWithSecretRefRoot, "root.rooted"),
        (SourceConfigWithSecretRefUnionArm, "root.choice.token"),
    ],
    ids=("direct", "nested", "list-element", "mapping-value", "root", "union-arm"),
)
def test_every_secret_reference_shape_in_source_config_is_rejected(config_model: type[AgwModel], path: str) -> None:
    reason = _reason(config_model=config_model)
    assert "references forbidden kind 'secret'" in reason
    assert path in reason


def test_a_non_secret_reference_in_source_config_is_allowed() -> None:
    class ConfigWithResourceRef(AgwModel):
        name: Literal["phase3-fixture"]
        site: Annotated[str, ResourceRef(kind="vm-site", usage="the selected site")]

    assert conformance_error(DESCRIPTOR, _backend(config_model=ConfigWithResourceRef)) is None


def test_a_secret_reference_in_the_mapping_model_is_allowed() -> None:
    class MappingWithSecretRef(AgwRootModel[Annotated[str, SecretRef(usage="a mapping-level secret")]]):
        pass

    assert conformance_error(DESCRIPTOR, _backend(mapping_model=MappingWithSecretRef)) is None


class JsonObject(AgwModel):
    arbitrary: Any
    wildcard: object
    scalar: str | bool | int | float | None
    choices: Literal["x", True, 7, 2.5, None]  # type: ignore[valid-type]
    rows: list[dict[str | Literal["alias"], object]]


@pytest.mark.parametrize(
    "mapping_model",
    [
        AgwRootModel[Any],
        AgwRootModel[object],
        AgwRootModel[str | bool | int | float | None],
        AgwRootModel[Annotated[list[dict[str, Any]], "fixture metadata"]],
        AgwRootModel[JsonObject],
    ],
    ids=("any", "object", "scalars-and-union", "annotated-list-map", "nested-model-and-literal"),
)
def test_every_json_native_annotation_family_conforms(mapping_model: type[BaseModel]) -> None:
    assert conformance_error(DESCRIPTOR, _backend(mapping_model=mapping_model)) is None


class StringEnum(StrEnum):
    VALUE = "value"


class PermissiveMapping[T](AgwRootModel[T]):
    """Lets conformance see Python types Pydantic does not support by default."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ArbitraryClass:
    pass


@pytest.mark.parametrize(
    "mapping_model",
    [
        AgwRootModel[date],
        AgwRootModel[bytes],
        PermissiveMapping[bytearray],
        AgwRootModel[set[str]],
        AgwRootModel[frozenset[str]],
        AgwRootModel[tuple[str, ...]],
        AgwRootModel[StringEnum],
        PermissiveMapping[ArbitraryClass],
    ],
    ids=("date", "bytes", "bytearray", "set", "frozenset", "tuple", "enum", "custom-class"),
)
def test_python_only_mapping_annotation_families_are_rejected(mapping_model: type[BaseModel]) -> None:
    reason = _reason(mapping_model=mapping_model)
    assert "accepts non-JSON-native type" in reason
    assert "at root" in reason


def test_a_non_string_mapping_key_is_rejected_with_its_nested_path() -> None:
    class Lookup(AgwModel):
        lookup: dict[int, list[str]]

    reason = _reason(mapping_model=AgwRootModel[Lookup])
    assert "accepts non-string mapping key type int at root.lookup{key}" in reason


def test_a_python_only_type_nested_in_a_container_is_rejected() -> None:
    class Expiring(AgwModel):
        rows: list[dict[str, date]]

    reason = _reason(mapping_model=AgwRootModel[Expiring])
    assert "datetime.date at root.rows[]{value}" in reason


def _valid_readiness(cls) -> Readiness:
    return Readiness.ready()


def _valid_would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
    return mapping_present


def _valid_describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
    return None


def _valid_external_timeout(cls, config: AgwModel) -> float | None:
    return None


@pytest.mark.parametrize(
    ("operation", "implementation"),
    [
        ("backend_readiness", _valid_readiness),
        ("would_attempt", _valid_would_attempt),
        ("describe_lookup", _valid_describe_lookup),
        ("external_operation_timeout", _valid_external_timeout),
    ],
)
def test_source_contract_operations_must_be_classmethods(operation: str, implementation: Callable[..., object]) -> None:
    assert _reason(**{operation: implementation}) == f"its {operation} must be declared as @classmethod"


def _readiness_with_extra(cls, extra: str) -> Readiness:
    return Readiness.ready()


def _would_attempt_with_positional_mapping(cls, secret_name: str, mapping_present: bool) -> bool:
    return mapping_present


def _would_attempt_with_default(cls, secret_name: str, *, mapping_present: bool = False) -> bool:
    return mapping_present


def _describe_with_wrong_name(cls, name: str, mapping: BaseModel | None) -> str | None:
    return None


def _timeout_with_wrong_name(cls, source_config: AgwModel) -> float | None:
    return None


def _timeout_with_keyword_only_config(cls, *, config: AgwModel) -> float | None:
    return None


def _timeout_with_default(cls, config: AgwModel | None = None) -> float | None:
    return None


@pytest.mark.parametrize(
    ("operation", "implementation", "expected"),
    [
        ("backend_readiness", _readiness_with_extra, "must declare 0 parameters after cls (got 1)"),
        ("would_attempt", _would_attempt_with_positional_mapping, "parameter 'mapping_present' must be keyword-only"),
        ("would_attempt", _would_attempt_with_default, "parameter 'mapping_present' must not have a default"),
        ("describe_lookup", _describe_with_wrong_name, "parameter 1 must be named 'secret_name'"),
        (
            "external_operation_timeout",
            _timeout_with_wrong_name,
            "parameter 1 must be named 'config'",
        ),
        (
            "external_operation_timeout",
            _timeout_with_keyword_only_config,
            "parameter 'config' must be positional-or-keyword",
        ),
        (
            "external_operation_timeout",
            _timeout_with_default,
            "parameter 'config' must not have a default",
        ),
    ],
    ids=(
        "readiness-count",
        "would-attempt-kind",
        "would-attempt-default",
        "describe-name",
        "timeout-name",
        "timeout-kind",
        "timeout-default",
    ),
)
def test_source_contract_operation_exact_signature_rejection_matrix(
    operation: str,
    implementation: Callable[..., object],
    expected: str,
) -> None:
    reason = _reason(**{operation: classmethod(implementation)})
    assert expected in reason


def test_external_timeout_staticmethod_is_rejected() -> None:
    assert _reason(external_operation_timeout=staticmethod(_valid_external_timeout)) == (
        "its external_operation_timeout must be declared as @classmethod"
    )


def test_external_timeout_is_never_invoked_during_conformance() -> None:
    calls = 0

    def timeout(cls, config: AgwModel) -> float | None:
        nonlocal calls
        calls += 1
        raise AssertionError("registration must not invoke the timeout declaration")

    assert conformance_error(DESCRIPTOR, _backend(external_operation_timeout=classmethod(timeout))) is None
    assert calls == 0


Factory = Callable[..., AbstractContextManager[SecretSourceClient]]


def _valid_factory(
    cls,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _alternate_binding_name(
    klass,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _positional_only_binding(
    cls,
    /,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _defaulted_binding(
    cls=None,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _too_few(
    cls,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _too_many(
    cls,
    *,
    source_name: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
    extra: object,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _wrong_parameter_name(
    cls,
    *,
    source: str,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _positional_parameter(
    cls,
    source_name: str,
    *,
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _defaulted_parameter(
    cls,
    *,
    source_name: str = "default",
    config: AgwModel,
    interaction_broker: InteractionBroker | None,
    remaining_time: RemainingTime,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _zero_parameter_factory() -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _bound(factory: Factory, binding: str) -> object:
    if binding == "class":
        return classmethod(factory)
    if binding == "static":
        return staticmethod(factory)
    return factory


@pytest.mark.parametrize(
    ("factory", "binding", "expected"),
    [
        (_valid_factory, "instance", "must be declared as @classmethod"),
        (_valid_factory, "static", "must be declared as @classmethod"),
        (_positional_only_binding, "class", "parameter 'cls' must be positional-or-keyword"),
        (_defaulted_binding, "class", "parameter 'cls' must not have a default"),
        (_zero_parameter_factory, "class", "must declare a 'cls' binding parameter"),
        (_too_few, "class", "must declare 4 parameters after cls (got 3)"),
        (_too_many, "class", "must declare 4 parameters after cls (got 5)"),
        (_wrong_parameter_name, "class", "parameter 1 must be named 'source_name'"),
        (_positional_parameter, "class", "parameter 'source_name' must be keyword-only"),
        (_defaulted_parameter, "class", "parameter 'source_name' must not have a default"),
    ],
    ids=(
        "instance-method",
        "staticmethod",
        "cls-kind",
        "cls-default",
        "no-binding-parameter",
        "missing-parameter",
        "extra-parameter",
        "parameter-name",
        "parameter-kind",
        "parameter-default",
    ),
)
def test_create_client_call_shape_rejection_matrix(factory: Factory, binding: str, expected: str) -> None:
    assert expected in _reason(create_client=_bound(factory, binding))


def test_the_class_binding_parameter_may_be_spelled_anything() -> None:
    """Python binds the class to the first parameter whatever it is called, so
    the framework can call this and the seam has no opinion on the spelling."""
    assert conformance_error(DESCRIPTOR, _backend(create_client=classmethod(_alternate_binding_name))) is None


def test_unbuildable_fixture_models_really_are_unbuildable() -> None:
    assert not model_is_complete(UnbuildableConfig)
    assert not model_is_complete(UnbuildableMapping)

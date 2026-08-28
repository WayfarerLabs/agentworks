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
    LookupDescription,
    LookupDisposition,
    SecretBackend,
    SecretClientIntent,
    SecretSourceClient,
    TtyInteractionAccess,
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


def test_a_nominal_concrete_version_one_backend_conforms() -> None:
    assert conformance_error(DESCRIPTOR, _backend()) is None


def test_capability_name_conformance_matches_the_exact_resolution_identity_boundary() -> None:
    class StringSubclass(str):
        pass

    assert conformance_error(DESCRIPTOR, _backend(name=StringSubclass("phase3-fixture"))) is not None
    assert conformance_error(DESCRIPTOR, _backend(name="surrogate\ud800backend")) is not None


def test_a_structural_lookalike_is_rejected_before_its_members_are_considered() -> None:
    class Lookalike:
        name = "phase3-fixture"
        description = "looks complete"

    assert "does not derive from SecretBackend" in str(conformance_error(DESCRIPTOR, Lookalike))


def test_an_abstract_backend_is_rejected_as_not_constructible() -> None:
    class AbstractBackend(SecretBackend):
        name = "abstract"
        description = "still abstract"
        contract_version = 1
        config_model = GoodConfig
        mapping_model = GoodMapping
        supports_tty_interaction = False

    reason = conformance_error(DESCRIPTOR, AbstractBackend)
    assert reason is not None
    assert reason.startswith("it is abstract")
    assert "create_client" in reason


def test_a_non_callable_factory_hits_the_generic_operation_check_first() -> None:
    assert "required secret-backend operations: create_client" in _reason(create_client=None)


class MissingContractVersionBackend(SecretBackend):
    name = "phase3-fixture"
    description = "omits the required contract version"
    config_model = GoodConfig
    mapping_model = GoodMapping
    supports_tty_interaction = False

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        raise NotImplementedError


def test_a_concrete_backend_without_a_declared_contract_version_is_rejected() -> None:
    assert conformance_error(DESCRIPTOR, MissingContractVersionBackend) is not None


class MissingTtySupportBackend(SecretBackend):
    name = "phase3-fixture"
    description = "omits one required class fact"
    contract_version = 1
    config_model = GoodConfig
    mapping_model = GoodMapping

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)

    @classmethod
    def create_client(
        cls,
        *,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        raise NotImplementedError


def test_a_missing_required_class_attribute_is_rejected() -> None:
    assert conformance_error(DESCRIPTOR, MissingTtySupportBackend) is not None


@pytest.mark.parametrize("value", [0, 1, "yes", None], ids=("zero", "one", "string", "none"))
def test_supports_tty_interaction_must_be_exactly_bool(value: object) -> None:
    assert conformance_error(DESCRIPTOR, _backend(supports_tty_interaction=value)) is not None


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
    "changes",
    [
        {"config_model": None},
        {"config_model": object},
        {"config_model": UnbuildableConfig},
        {"config_model": UntaggedConfig},
        {"config_model": MistaggedConfig},
        {"mapping_model": None},
        {"mapping_model": AgwModel},
        {"mapping_model": UnbuildableMapping},
        {"mapping_model": AgwRootModel[MisplacedMappingMarker]},
        {"contract_version": 2},
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
def test_dual_model_and_version_rejection_matrix(changes: dict[str, object]) -> None:
    assert conformance_error(DESCRIPTOR, _backend(**changes)) is not None


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


def test_forbidden_references_are_checked_on_the_offered_config_model() -> None:
    class OfferingForbiddenReference(ConformingSecretBackend):
        name = "phase3-fixture"
        description = "offers a forbidden source reference"
        config_model = GoodConfig
        mapping_model = GoodMapping

        @classmethod
        def config_for(cls) -> type[BaseModel]:
            return SourceConfigWithDirectSecretRef

    assert conformance_error(DESCRIPTOR, OfferingForbiddenReference) is not None


def test_an_unoffered_declared_model_does_not_drive_reference_conformance() -> None:
    class OfferingCleanConfig(ConformingSecretBackend):
        name = "phase3-fixture"
        description = "offers a clean model instead of its declaration"
        config_model = SourceConfigWithDirectSecretRef
        mapping_model = GoodMapping

        @classmethod
        def config_for(cls) -> type[BaseModel]:
            return GoodConfig

    assert conformance_error(DESCRIPTOR, OfferingCleanConfig) is None


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


def _valid_describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
    return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)


@pytest.mark.parametrize(
    ("operation", "implementation"),
    [
        ("backend_readiness", _valid_readiness),
        ("describe_lookup", _valid_describe_lookup),
    ],
)
def test_source_contract_operations_must_be_classmethods(operation: str, implementation: Callable[..., object]) -> None:
    assert conformance_error(DESCRIPTOR, _backend(**{operation: implementation})) is not None


def _readiness_with_extra(cls, extra: str) -> Readiness:
    return Readiness.ready()


def _describe_with_wrong_name(cls, name: str, mapping: BaseModel | None) -> LookupDescription:
    return LookupDescription(LookupDisposition.NOT_APPLICABLE, None)


@pytest.mark.parametrize(
    ("operation", "implementation"),
    [
        ("backend_readiness", _readiness_with_extra),
        ("describe_lookup", _describe_with_wrong_name),
    ],
    ids=(
        "readiness-count",
        "describe-name",
    ),
)
def test_source_contract_operation_exact_signature_rejection_matrix(
    operation: str,
    implementation: Callable[..., object],
) -> None:
    assert conformance_error(DESCRIPTOR, _backend(**{operation: classmethod(implementation)})) is not None


Factory = Callable[..., AbstractContextManager[SecretSourceClient]]


def _valid_factory(
    cls,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _alternate_binding_name(
    klass,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _positional_only_binding(
    cls,
    /,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _defaulted_binding(
    cls=None,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _too_few(
    cls,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _too_many(
    cls,
    *,
    config: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
    extra: object,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _wrong_parameter_name(
    cls,
    *,
    configuration: AgwModel,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _positional_parameter(
    cls,
    config: AgwModel,
    *,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
) -> AbstractContextManager[SecretSourceClient]:
    raise NotImplementedError


def _defaulted_parameter(
    cls,
    *,
    config: AgwModel | None = None,
    intent: SecretClientIntent,
    tty_access: TtyInteractionAccess,
    interaction_broker: InteractionBroker | None,
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
    ("factory", "binding"),
    [
        (_valid_factory, "instance"),
        (_valid_factory, "static"),
        (_positional_only_binding, "class"),
        (_defaulted_binding, "class"),
        (_zero_parameter_factory, "class"),
        (_too_few, "class"),
        (_too_many, "class"),
        (_wrong_parameter_name, "class"),
        (_positional_parameter, "class"),
        (_defaulted_parameter, "class"),
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
def test_create_client_call_shape_rejection_matrix(factory: Factory, binding: str) -> None:
    assert conformance_error(DESCRIPTOR, _backend(create_client=_bound(factory, binding))) is not None


def test_the_class_binding_parameter_may_be_spelled_anything() -> None:
    """Python binds the class to the first parameter whatever it is called, so
    the framework can call this and the seam has no opinion on the spelling."""
    assert conformance_error(DESCRIPTOR, _backend(create_client=classmethod(_alternate_binding_name))) is None


def test_unbuildable_fixture_models_really_are_unbuildable() -> None:
    assert not model_is_complete(UnbuildableConfig)
    assert not model_is_complete(UnbuildableMapping)

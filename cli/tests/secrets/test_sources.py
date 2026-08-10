"""Declarable secret sources and source-selected mapping operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from textwrap import dedent
from typing import Annotated, Any, Literal

import pytest
from pydantic import ValidationError

import agentworks.capabilities.config as capability_config
from agentworks.bootstrap import build_registry
from agentworks.capabilities.config import (
    capability_mapping_key_references,
    mapping_value_is_opt_out,
)
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.secret_backend import SecretBackend
from agentworks.config import load_config
from agentworks.errors import ConfigError, StateError
from agentworks.manifests import ManifestSet, load_manifests
from agentworks.origin import Origin
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.registry import Registry
from agentworks.schema import AgwRootModel, CapabilityBlock, NonEmptyStr, RefOwner, SecretRef
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.sources import (
    SecretSourceDecl,
    SourceProvenance,
    source_backend_class,
    source_mapping_references,
    source_provenance,
    validate_source_mapping,
)
from tests.plugins._fixtures import ConformingSecretBackend


def _config(tmp_path: Path) -> Any:
    public = tmp_path / "id.pub"
    private = tmp_path / "id"
    public.write_text("ssh-ed25519 AAAA test")
    private.write_text("key")
    config = tmp_path / "config.toml"
    config.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{public.as_posix()}"
            ssh_private_key = "{private.as_posix()}"
            """
        )
    )
    return load_config(config, warn_issues=False)


def _manifest(tmp_path: Path, *, name: str, backend: str) -> ManifestSet:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    (resources / "source.yaml").write_text(
        dedent(
            f"""\
            apiVersion: agentworks/v1
            kind: secret-source
            metadata:
              name: {name}
            spec:
              backend:
                name: {backend}
            """
        )
    )
    return load_manifests(resources)


def test_builtin_sources_publish_in_precedence_order_with_normal_origins(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path), ManifestSet.empty())

    sources = list(registry.iter_kind("secret-source"))
    assert [source.name for source in sources] == ["env-var", "prompt"]
    assert [source.backend.name for source in sources] == ["env-var", "prompt"]
    assert [source.origin for source in sources] == [
        Origin.built_in(source="agentworks.secrets.sources"),
        Origin.built_in(source="agentworks.secrets.sources"),
    ]
    assert [source_provenance(source) for source in sources] == [
        SourceProvenance.SYNTHESIZED_DEFAULT,
        SourceProvenance.SYNTHESIZED_DEFAULT,
    ]


def test_operator_manifest_overrides_a_same_name_builtin_with_provenance(tmp_path: Path) -> None:
    registry = build_registry(
        _config(tmp_path),
        _manifest(tmp_path, name="env-var", backend="prompt"),
    )

    overridden = registry.lookup("secret-source", "env-var")
    assert isinstance(overridden, SecretSourceDecl)
    assert overridden.backend.name == "prompt"
    assert overridden.origin is not None
    assert overridden.origin.variant == "operator-declared"
    assert overridden.origin.file is not None
    assert overridden.origin.file.name == "source.yaml"
    assert source_provenance(overridden) is SourceProvenance.OPERATOR_OVERRIDE

    prompt = registry.lookup("secret-source", "prompt")
    assert isinstance(prompt, SecretSourceDecl)
    assert source_provenance(prompt) is SourceProvenance.SYNTHESIZED_DEFAULT


def test_an_operator_can_declare_an_additional_source(tmp_path: Path) -> None:
    registry = build_registry(
        _config(tmp_path),
        _manifest(tmp_path, name="ci-env", backend="env-var"),
    )

    source = registry.lookup("secret-source", "ci-env")
    assert isinstance(source, SecretSourceDecl)
    assert source.backend == CapabilityBlock.of("env-var")
    assert source_provenance(source) is SourceProvenance.DECLARED


def test_source_backend_must_use_the_tagged_table_shape(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "source.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret-source
            metadata:
              name: ci-env
            spec:
              backend: env-var
            """
        )
    )

    with pytest.raises(ConfigError) as exc_info:
        load_manifests(resources)
    assert "secret-source/ci-env.backend: must be a table" in str(exc_info.value)
    assert "source.yaml" in str(exc_info.value)


def test_source_backend_config_errors_keep_manifest_location(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "source.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret-source
            metadata:
              name: ci-env
            spec:
              backend:
                name: env-var
                unknown: value
            """
        )
    )

    with pytest.raises(ConfigError) as exc_info:
        build_registry(_config(tmp_path), load_manifests(resources))
    assert "secret-source/ci-env.unknown: unknown field" in str(exc_info.value)
    assert "source.yaml" in str(exc_info.value)


class _Lookup:
    def __init__(
        self,
        sources: dict[str, object],
        backends: dict[str, type],
    ) -> None:
        self.sources = sources
        self.backends = backends
        self.backend_lookups: list[str] = []

    def source_row(self, name: str) -> object | None:
        return self.sources.get(name)

    def backend_class(self, name: str) -> type | None:
        self.backend_lookups.append(name)
        return self.backends.get(name)


def test_backend_selection_is_source_first_with_no_same_name_fallback() -> None:
    lookup = _Lookup({}, {"unknown-source": _StringBackend})
    assert source_backend_class(lookup, "unknown-source") is None
    assert lookup.backend_lookups == []


def test_backend_selection_rejects_framework_row_and_class_corruption() -> None:
    wrong_row = _Lookup({"broken": object()}, {})
    with pytest.raises(StateError, match="not SecretSourceDecl"):
        source_backend_class(wrong_row, "broken")

    source = SecretSourceDecl(name="broken", backend=CapabilityBlock.of("string-mapping"))
    wrong_class = _Lookup({"broken": source}, {"string-mapping": object})
    with pytest.raises(StateError, match="not a SecretBackend class"):
        source_backend_class(wrong_class, "broken")


class _StringBackend(ConformingSecretBackend):
    name = "string-mapping"
    description = "accepts one string mapping"
    mapping_model = AgwRootModel[str]


class _NumberBackend(ConformingSecretBackend):
    name = "number-mapping"
    description = "accepts one number mapping"
    mapping_model = AgwRootModel[int]


class _ObjectBackend(ConformingSecretBackend):
    name = "object-mapping"
    description = "accepts one object mapping"
    mapping_model = AgwRootModel[dict[str, int]]


class _ListBackend(ConformingSecretBackend):
    name = "list-mapping"
    description = "accepts one list mapping"
    mapping_model = AgwRootModel[list[str]]


class _TrueBackend(ConformingSecretBackend):
    name = "true-mapping"
    description = "accepts the true literal"
    mapping_model = AgwRootModel[Literal[True]]


class _NullBackend(ConformingSecretBackend):
    name = "null-mapping"
    description = "accepts null"
    mapping_model = AgwRootModel[None]


class _ReferenceBackend(ConformingSecretBackend):
    name = "reference-mapping"
    description = "accepts a mapping that names another secret"
    mapping_model = AgwRootModel[Annotated[NonEmptyStr, SecretRef(usage="a fixture backend credential")]]


class _FalseBackend(ConformingSecretBackend):
    name = "false-mapping"
    description = "accepts false when the host does not reserve it"
    mapping_model = AgwRootModel[Literal[False]]


_MAPPING_BACKENDS: tuple[type[SecretBackend], ...] = (
    _StringBackend,
    _NumberBackend,
    _ObjectBackend,
    _ListBackend,
    _TrueBackend,
    _NullBackend,
    _ReferenceBackend,
)


def _mapping_lookup(name: str) -> _Lookup:
    source = SecretSourceDecl(name="selected", backend=CapabilityBlock.of(name))
    return _Lookup({"selected": source}, {backend.name: backend for backend in _MAPPING_BACKENDS})


@pytest.mark.parametrize(
    ("backend_name", "value"),
    [
        pytest.param("string-mapping", "lookup", id="scalar"),
        pytest.param("number-mapping", 17, id="number"),
        pytest.param("object-mapping", {"item": 17}, id="object"),
        pytest.param("list-mapping", ["vault", "item"], id="list"),
        pytest.param("true-mapping", True, id="true"),
        pytest.param("null-mapping", None, id="null"),
    ],
)
def test_json_native_values_reach_exactly_the_source_selected_model(
    backend_name: str,
    value: Any,
) -> None:
    declaration = SecretDecl(
        name="fixture",
        description="fixture",
        backend_mappings={"selected": value},
    )
    carried = declaration.backend_mappings["selected"]
    with seated_plugin(Plugin(name="mapping-values", capabilities={"secret-backend": _MAPPING_BACKENDS})):
        validated = validate_source_mapping(
            lookup=_mapping_lookup(backend_name),
            source_name="selected",
            mapping=carried,
            owner=RefOwner(kind="secret", name="fixture"),
            location=None,
        )

    assert validated is not None
    assert validated.root == value  # type: ignore[attr-defined]
    assert type(validated.root) is type(value)  # type: ignore[attr-defined]


def _json_runtime_shape(value: object) -> object:
    """The exact recursive runtime types and leaves of a JSON value."""
    if type(value) is list:
        return (list, tuple(_json_runtime_shape(item) for item in value))  # type: ignore[union-attr]
    if type(value) is dict:
        return (
            dict,
            tuple((type(key), key, _json_runtime_shape(item)) for key, item in value.items()),  # type: ignore[union-attr]
        )
    return (type(value), value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("lookup", id="string"),
        pytest.param(17, id="integer"),
        pytest.param(17.5, id="float"),
        pytest.param(True, id="true"),
        pytest.param(None, id="null"),
        pytest.param(["vault", 17, True, None], id="list"),
        pytest.param({"vault": "Work", "nested": [17, True, None]}, id="mapping"),
    ],
)
def test_direct_secret_decl_preserves_json_native_values_recursively(value: Any) -> None:
    declaration = SecretDecl(
        name="fixture",
        description="fixture",
        backend_mappings={"selected": value},
    )

    assert _json_runtime_shape(declaration.backend_mappings["selected"]) == _json_runtime_shape(value)


class _PlainEnum(Enum):
    VALUE = "value"


class _StringEnum(StrEnum):
    VALUE = "value"


class _IntegerEnum(IntEnum):
    VALUE = 17


class _CustomString(str):
    pass


class _CustomInteger(int):
    pass


class _CustomFloat(float):
    pass


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_PlainEnum.VALUE, id="enum"),
        pytest.param(_StringEnum.VALUE, id="string-enum"),
        pytest.param(_IntegerEnum.VALUE, id="integer-enum"),
        pytest.param(_CustomString("value"), id="string-subclass"),
        pytest.param(_CustomInteger(17), id="integer-subclass"),
        pytest.param(_CustomFloat(17.5), id="float-subclass"),
    ],
)
def test_direct_secret_decl_rejects_coercible_python_lookalikes_at_the_nested_location(
    value: Any,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SecretDecl(
            name="fixture",
            description="fixture",
            backend_mappings={"selected": {"nested": [value]}},
        )

    exact_errors = [
        error
        for error in exc_info.value.errors(include_url=False)
        if "must use exact JSON-native runtime types" in error["msg"]
    ]
    assert len(exact_errors) == 1
    location = exact_errors[0]["loc"]
    assert location[0:2] == ("backend_mappings", "selected")
    assert "nested" in location
    assert location[-1] == 0


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(_StringEnum.VALUE, id="string-enum"),
        pytest.param(_CustomString("value"), id="string-subclass"),
    ],
)
def test_direct_secret_decl_requires_exact_string_keys(key: object) -> None:
    with pytest.raises(ValidationError, match="must use exact JSON string keys"):
        SecretDecl(
            name="fixture",
            description="fixture",
            backend_mappings={"selected": {key: "value"}},  # type: ignore[dict-item]
        )


def test_exact_false_alone_is_the_framework_mapping_opt_out() -> None:
    assert mapping_value_is_opt_out("secret-backend", False)
    assert not mapping_value_is_opt_out("secret-backend", True)
    assert not mapping_value_is_opt_out("secret-backend", 0)
    assert not mapping_value_is_opt_out("secret-backend", None)


def test_false_reaches_the_selected_model_when_the_descriptor_disables_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = descriptor_for("secret-backend")
    assert descriptor.mapping_host is not None
    no_opt_out = replace(
        descriptor,
        mapping_host=replace(descriptor.mapping_host, false_opt_out=False),
    )
    monkeypatch.setattr(
        capability_config,
        "descriptor_for",
        lambda kind: no_opt_out if kind == "secret-backend" else descriptor_for(kind),
    )
    source = SecretSourceDecl(
        name="selected",
        backend=CapabilityBlock.of("false-mapping"),
    )
    lookup = _Lookup({"selected": source}, {"false-mapping": _FalseBackend})

    with seated_plugin(Plugin(name="false-mapping", capabilities={"secret-backend": (_FalseBackend,)})):
        assert not capability_config.mapping_value_is_opt_out("secret-backend", False)
        validated = validate_source_mapping(
            lookup=lookup,
            source_name="selected",
            mapping=False,
            owner=RefOwner(kind="secret", name="fixture"),
            location=None,
        )

    assert validated is not None
    assert validated.root is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [
        pytest.param("17", 17, id="integer"),
        pytest.param("17.5", 17.5, id="float"),
        pytest.param("true", True, id="true"),
        pytest.param("null", None, id="null"),
        pytest.param("lookup", "lookup", id="string"),
        pytest.param("[vault, item]", ["vault", "item"], id="list"),
        pytest.param("{vault: Work, item: token}", {"vault": "Work", "item": "token"}, id="object"),
    ],
)
def test_manifest_carrier_accepts_json_native_values_unchanged(
    tmp_path: Path,
    yaml_value: str,
    expected: object,
) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            f"""\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                selected: {yaml_value}
            """
        )
    )

    manifests = load_manifests(resources)
    (entry,) = manifests.entries
    secret = entry.resource
    assert isinstance(secret, SecretDecl)
    carried = secret.backend_mappings["selected"]
    assert carried == expected
    assert type(carried) is type(expected)


@pytest.mark.parametrize(
    "yaml_value",
    [
        pytest.param(".nan", id="non-finite"),
        pytest.param("2026-08-08", id="timestamp"),
        pytest.param("!!binary SGVsbG8=", id="binary"),
        pytest.param("!!set {one: null}", id="set"),
    ],
)
def test_manifest_carrier_rejects_non_json_values_with_location(
    tmp_path: Path,
    yaml_value: str,
) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            f"""\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                selected: {yaml_value}
            """
        )
    )

    with pytest.raises(ConfigError) as exc_info:
        load_manifests(resources)
    assert "secret.yaml" in str(exc_info.value)


def test_manifest_carrier_rejects_non_string_mapping_keys_with_location(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                selected: {1: value}
            """
        )
    )

    with pytest.raises(ConfigError) as exc_info:
        load_manifests(resources)
    assert "secret.yaml" in str(exc_info.value)


def test_mapping_validation_does_not_accept_another_registered_models_shape() -> None:
    with (
        seated_plugin(Plugin(name="mapping-values", capabilities={"secret-backend": _MAPPING_BACKENDS})),
        pytest.raises(ConfigError, match="must be a string"),
    ):
        validate_source_mapping(
            lookup=_mapping_lookup("string-mapping"),
            source_name="selected",
            mapping=17,
            owner=RefOwner(kind="secret", name="fixture"),
            location=None,
        )


def test_mapping_reference_extraction_uses_the_same_source_selector() -> None:
    with seated_plugin(Plugin(name="mapping-values", capabilities={"secret-backend": _MAPPING_BACKENDS})):
        references = source_mapping_references(
            lookup=_mapping_lookup("reference-mapping"),
            source_name="selected",
            mapping="backend-token",
            owner=RefOwner(kind="secret", name="fixture"),
        )

    assert [(ref.kind, ref.name, ref.usage) for ref in references] == [
        ("secret", "backend-token", "a fixture backend credential")
    ]


def test_descriptor_derived_mapping_key_collector_is_ordered_and_value_blind() -> None:
    secret = SecretDecl(
        name="fixture",
        description="fixture",
        backend_mappings={
            "first-source": False,
            "second-source": True,
            "third-source": ["anything"],
        },
    )

    references = capability_mapping_key_references(
        descriptor=descriptor_for("secret-backend"),
        row=secret,
    )

    assert [(ref.kind, ref.name, ref.source, ref.usage) for ref in references] == [
        ("secret-source", "first-source", ("secret", "fixture"), "a source for resolving this secret"),
        ("secret-source", "second-source", ("secret", "fixture"), "a source for resolving this secret"),
        ("secret-source", "third-source", ("secret", "fixture"), "a source for resolving this secret"),
    ]


def test_mapping_key_collector_rejects_unknown_source_even_for_false(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                not-a-source: false
            """
        )
    )

    with pytest.raises(ConfigError, match="references unknown secret-source 'not-a-source'"):
        build_registry(_config(tmp_path), load_manifests(resources))


@pytest.mark.parametrize("mapping", [False, "address"])
def test_unknown_source_uses_identical_source_key_framing(tmp_path: Path, mapping: object) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    rendered = "false" if mapping is False else "address"
    (resources / "secret.yaml").write_text(
        dedent(
            f"""\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                not-a-source: {rendered}
            """
        )
    )
    with pytest.raises(ConfigError) as caught:
        build_registry(_config(tmp_path), load_manifests(resources))
    assert str(caught.value).count("references unknown secret-source 'not-a-source'") == 1


@dataclass(frozen=True)
class _OrdinaryReferenceEmitter:
    name: str
    target_kind: str
    target_name: str
    origin: object = None

    def dependencies(self, context: object) -> list[object]:
        del context
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name=self.target_name,
                kind=self.target_kind,
                usage="an ordinary fixture reference",
                source=("apt-package", self.name),
            )
        ]


@pytest.mark.parametrize("mapping_first", [False, True])
def test_source_key_miss_schedule_preserves_cross_row_first_target_order(
    tmp_path: Path,
    mapping_first: bool,
) -> None:
    registry = Registry.empty()
    origin = Origin.operator_declared(file=tmp_path / "resources.yaml", line=1)
    secret = SecretDecl(
        name="mapping-host",
        description="mapping host",
        backend_mappings={"mapping-missing": "address"},
    )
    emitter = _OrdinaryReferenceEmitter(
        name="ordinary-host",
        target_kind="secret-source",
        target_name="ordinary-missing",
    )
    rows = (
        (("secret", "mapping-host", secret), ("apt-package", "ordinary-host", emitter))
        if mapping_first
        else (("apt-package", "ordinary-host", emitter), ("secret", "mapping-host", secret))
    )
    for kind, name, row in rows:
        registry.add(kind, name, row, origin)

    expected = "mapping-missing" if mapping_first else "ordinary-missing"
    with pytest.raises(ConfigError, match=expected):
        registry.finalize()


def test_same_target_mapping_validation_and_candidate_emit_one_diagnostic(tmp_path: Path) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "mapping-host",
        SecretDecl(
            name="mapping-host",
            description="mapping host",
            backend_mappings={"same-missing": "address"},
        ),
        Origin.operator_declared(file=tmp_path / "resources.yaml", line=1),
    )

    with pytest.raises(ConfigError) as caught:
        registry.finalize()
    assert str(caught.value).count("references unknown secret-source 'same-missing'") == 1


def test_mapping_key_collection_runs_for_later_materialized_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.resources.kind import KIND_REGISTRY
    from agentworks.resources.reference import ResourceReference

    secret_kind = KIND_REGISTRY["secret"]

    def synthesize(self: object, references: list[ResourceReference]) -> SecretDecl:
        del self
        first = references[0]
        return SecretDecl(
            name=first.name,
            description="",
            backend_mappings={"string-mapping": "address"},
            origin=Origin.auto_declared(source=first.declarer),
        )

    monkeypatch.setattr(type(secret_kind), "synthesize", synthesize)
    registry = Registry.empty()
    origin = Origin.operator_declared(file=tmp_path / "resources.yaml", line=1)
    registry.add(
        "apt-package",
        "late-host-referrer",
        _OrdinaryReferenceEmitter(
            name="late-host-referrer",
            target_kind="secret",
            target_name="late-host",
        ),
        origin,
    )
    plugin = Plugin(name="materialized-map-host", capabilities={"secret-backend": (_StringBackend,)})
    with seated_plugin(plugin):
        publish_capability_rows(registry, descriptor_for("secret-backend"))
        with pytest.raises(ConfigError) as caught:
            registry.finalize()

    message = str(caught.value)
    assert "references unknown secret-source 'string-mapping'" in message
    assert "(a source for resolving this secret)" in message
    assert message.count("string-mapping") == 1


def test_known_false_source_validates_but_emits_no_candidate_edge(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                env-var: false
            """
        )
    )
    manifests = load_manifests(resources)
    registry = Registry.empty()
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.secrets.sources import publish_builtin_secret_sources

    publish_capability_rows(registry, descriptor_for("secret-backend"))
    publish_builtin_secret_sources(registry)
    entry = manifests.entries[0]
    registry.add("secret", "fixture", entry.resource, Origin.auto_declared(source=("test", "fixture")))
    registry.finalize()
    targets = {(ref.kind, ref.name) for ref in registry.graph.edges_of("secret", "fixture")}
    assert ("secret-source", "env-var") not in targets
    assert ("secret-source", "prompt") in targets


def test_direct_onepassword_table_mapping_gets_exact_source_rewrite(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secret.yaml").write_text(
        dedent(
            """\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: fixture
              description: fixture
            spec:
              backend_mappings:
                onepassword:
                  account: work.example.com
                  reference: op://Work/Fixture/password
            """
        )
    )

    with pytest.raises(ConfigError) as caught:
        build_registry(_config(tmp_path), load_manifests(resources))
    assert "secret/fixture.backend_mappings.onepassword references unknown secret-source 'onepassword'" in str(
        caught.value
    )
    assert 'account: "work.example.com"' in (caught.value.hint or "")
    assert '<source-name>: "op://Work/Fixture/password"' in (caught.value.hint or "")

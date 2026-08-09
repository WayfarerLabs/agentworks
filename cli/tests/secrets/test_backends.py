"""The secret-backend capability layer (post-collapse, ADR 0016).

Backends are code capabilities in ``SECRET_BACKEND_REGISTRY``, mirrored
into the resource Registry as read-only ``secret-backend`` descriptor
rows -- one per capability, no declarable instantiation layer. The
chain (``[secret_config].backends``) and per-secret ``backend_mappings``
name backends directly.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Annotated, Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.secret_backend import SECRET_BACKEND_REGISTRY
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel, AgwRootModel, NonEmptyStr, RefOwner, SecretRef
from agentworks.secrets import active_backends, resolve_secrets
from agentworks.secrets._backend_compat import mapping_references
from agentworks.secrets.base import SecretDecl
from tests.plugins._fixtures import ConformingSecretBackend


def _config(tmp_path: Path, body: str = "") -> Any:
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False)


def _manifest(tmp_path: Path, text: str, rel: str = "res.yaml") -> None:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    (resources / rel).write_text(dedent(text))


class MarkedMapping(AgwModel):
    """A hypothetical backend mapping that NAMES an agentworks secret,
    which no shipped backend does. It exists so the empty answers above
    are provably the walker's."""

    vault_secret: Annotated[NonEmptyStr, SecretRef(usage="the vault's own credential")]


class _TestOnlyBackend(ConformingSecretBackend):
    """A store-flavored capability registered only in tests: exercises
    the SecretBackend API end to end (structured mappings, soft-skip
    for unmapped secrets) without shipping artificial built-ins."""

    name = "test-only"
    description = "test-only store"
    mapping_model = AgwRootModel[dict[str, object]]
    batch_get_calls: list[list[str]] = []

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        # Store semantics: only attempts explicitly-mapped secrets
        # (soft-skip otherwise), unlike the always-attempt built-ins.
        return mapping_present

    @classmethod
    def _legacy_describe_lookup(cls, secret: Any, mapping: Any) -> str | None:
        if isinstance(mapping, dict):
            return f"store://{mapping.get('vault')}/{mapping.get('item')}"
        return str(mapping) if mapping is not None else None

    @classmethod
    def _legacy_batch_get(cls, wants: list[tuple[Any, Any]]) -> dict[str, str]:
        cls.batch_get_calls.append([s.name for s, _ in wants])
        return {s.name: f"value-of-{s.name}" for s, m in wants if m is not None}


@pytest.fixture
def test_only_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    _TestOnlyBackend.batch_get_calls = []
    monkeypatch.setitem(SECRET_BACKEND_REGISTRY, "test-only", _TestOnlyBackend)
    return _TestOnlyBackend


@pytest.mark.parametrize(
    ("backend", "mapping"),
    [
        pytest.param("env-var", "NPM_TOKEN", id="env-var"),
        pytest.param("onepassword", "op://Work/npm/token", id="onepassword-uri"),
    ],
)
def test_a_shipped_mapping_implies_no_agentworks_resource(backend: str, mapping: object) -> None:
    """Extraction over a backend mapping is core-driven like every other
    kind's, and every shipped mapping is an EXTERNAL identifier (an env
    var name, an ``op://`` reference into a vault), so none of them names
    an agentworks resource.

    Worth pinning rather than assuming: the wiring exists so
    secret-backend is not the one kind whose config references are
    structurally underivable, and an empty answer from a walker that was
    never called looks exactly like an empty answer from one that was.
    """
    assert (
        mapping_references(
            name=backend,
            mapping=mapping,
            owner=RefOwner(kind="secret", name="npm-token"),
        )
        == ()
    )


def test_extraction_over_a_backend_mapping_is_reached_at_all() -> None:
    """Non-vacuity for the pin above: the empty answers there are the
    walker's answer rather than a walk that never happened.

    Through ``capability_config_references`` with a SEATED backend, not
    through ``extract_references`` directly. Calling the walker straight
    proves only that the walker walks, which
    ``tests/schema/test_extract.py`` already owns; what is unproven
    without seating is the secret-backend WIRING, which is the whole
    reason this pin is here (``capabilities/config.py:214-218`` resolves
    the mapping model off the registered backend, and answers ``()`` for
    any name it cannot resolve). This test used to make the direct call
    and so could not fail for a broken lookup.
    """

    class _MarkedBackend(ConformingSecretBackend):
        name = "marked-backend"
        description = "a fixture backend whose mapping names a secret"
        mapping_model = AgwRootModel[MarkedMapping]

    with seated_plugin(Plugin(name="marked", capabilities={"secret-backend": (_MarkedBackend,)})):
        references = mapping_references(
            name="marked-backend",
            mapping={"vault_secret": "shared-key"},
            owner=RefOwner(kind="secret", name="npm-token"),
        )

    assert [ref.name for ref in references] == ["shared-key"]


def test_one_descriptor_row_per_capability(tmp_path: Path) -> None:
    """The collapse's registry shape: kind secret-backend holds exactly
    the capability descriptors (no declarable rows, no secret-provider
    kind at all)."""
    config = _config(tmp_path)
    registry = build_registry(config)
    names = sorted(e.name for e in registry.iter_kind("secret-backend"))
    assert names == sorted(SECRET_BACKEND_REGISTRY)
    row = registry.lookup("secret-backend", "env-var")
    assert row.origin.variant == "built-in"
    assert row.description  # capability-supplied, for inspection surfaces
    with pytest.raises(KeyError):
        registry.lookup("secret-provider", "env-var")


def test_secret_backend_is_not_declarable(tmp_path: Path) -> None:
    """A kind: secret-backend manifest gets the permanent R3
    capability-kind envelope error."""
    from agentworks.manifests import load_manifests

    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: secret-backend
        metadata:
          name: my-env
        spec:
          provider: env-var
        """,
    )
    with pytest.raises(ConfigError, match="provided by the app") as exc:
        load_manifests(tmp_path / "resources")
    assert "res.yaml" in str(exc.value)


def test_chain_resolves_against_descriptor_rows(tmp_path: Path, test_only_backend: Any) -> None:
    """[secret_config].backends names capabilities; the runtime chain
    wraps them with the loop-side orchestration."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only", "prompt"]
        """,
    )
    registry = build_registry(config)
    backends = active_backends(config, registry)
    assert [b.name for b in backends] == ["test-only", "prompt"]
    assert [b.interactive for b in backends] == [False, True]


def test_structured_mapping_reaches_the_backend(tmp_path: Path, test_only_backend: Any) -> None:
    """Per-secret store addressing lives in backend_mappings (the
    collapse's answer to the 1Password case): the structured dict rides
    through would_attempt / describe_lookup / batch_get."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only"]
        """,
    )
    registry = build_registry(config)
    (backend,) = active_backends(config, registry)
    mapped = SecretDecl(
        name="s1",
        description="s1",
        backend_mappings={"test-only": {"vault": "Work", "item": "npm"}},
    )
    unmapped = SecretDecl(name="s2", description="s2")
    assert backend.would_attempt(mapped)
    assert not backend.would_attempt(unmapped)  # store soft-skip
    assert backend.describe_lookup(mapped) == "store://Work/npm"
    values = resolve_secrets([mapped], [backend])
    assert values == {"s1": "value-of-s1"}


def test_opt_out_never_reaches_the_capability(tmp_path: Path, test_only_backend: Any) -> None:
    """The generic `false` opt-out is loop-side orchestration: an
    opted-out secret is excluded before the capability sees the
    batch."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["test-only"]
        """,
    )
    registry = build_registry(config)
    (backend,) = active_backends(config, registry)
    opted_out = SecretDecl(name="s1", description="s1", backend_mappings={"test-only": False})
    assert not backend.would_attempt(opted_out)
    assert backend.describe_lookup(opted_out) is None
    assert backend.resolve([opted_out]) == {}
    assert test_only_backend.batch_get_calls == []


def test_chain_naming_unknown_backend_errors_at_build_registry(
    tmp_path: Path,
) -> None:
    """A bad chain name fails the registry build.

    The message is the generic settings-reference one now (the chain is one
    of the settings that name resources, so it gets the same treatment and
    wording as ``defaults.site`` and as a dangling manifest reference); the
    per-setting behavior is pinned in tests/test_config_setting_references.py.
    What this asserts is the SECRETS-side contract that has to keep holding:
    a bad chain name never reaches resolution.
    """
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["nope", "prompt"]
        """,
    )
    with pytest.raises(ConfigError, match="references unknown secret-backend 'nope'"):
        build_registry(config)


def test_active_backends_still_guards_a_hand_built_registry(tmp_path: Path) -> None:
    """``active_backends``'s own unknown-name error is a backstop, and this
    is the path that still reaches it.

    On any registry from ``build_registry`` the settings-reference pass has
    already refused the name, so that branch is unreachable there. But
    ``active_backends`` is public and takes any registry: a caller that
    assembles one by hand skips that pass, and without the guard a typo would
    surface as a bare ``KeyError`` out of ``impl_of``. Pins that the lower
    layer still answers for itself.
    """
    from agentworks.resources import Registry

    config = _config(tmp_path, '[secret_config]\nbackends = ["nope"]\n')
    hand_built = Registry.empty()
    hand_built.finalize()
    with pytest.raises(ConfigError, match="unknown backend 'nope'") as exc:
        active_backends(config, hand_built)
    assert exc.value.hint is not None
    assert "registered backends" in exc.value.hint


def test_the_chain_serves_from_the_descriptor_row(tmp_path: Path) -> None:
    """The built-in backend rows come from the capability descriptor and
    serve the chain directly. Nothing in config.toml declares or overrides
    them: ``[secret_backends.*]`` is a retired resource section now (refused
    at load, covered in tests/test_config_deprecation_warnings.py), so this
    is the only way a built-in backend reaches the chain."""
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = build_registry(config)
    row = registry.lookup("secret-backend", "env-var")
    assert row.origin.variant == "built-in"
    backends = active_backends(config, registry)
    assert [b.name for b in backends] == ["env-var"]


def test_would_attempt_is_pure_of_secret_and_mapping() -> None:
    """``would_attempt`` must be a pure function of ``(secret, mapping)``
    with no host probing, so freezing it into edges at finalize is safe.
    env-var / prompt always attempt; onepassword attempts iff mapped."""
    from agentworks.capabilities.secret_backend.env_var import EnvVarBackend
    from agentworks.capabilities.secret_backend.prompt import PromptBackend
    from agentworks.plugins.onepassword.backend import OnePasswordBackend

    secret = SecretDecl(name="s1", description="s1")
    assert EnvVarBackend.would_attempt(secret.name, mapping_present=False) is True
    assert PromptBackend.would_attempt(secret.name, mapping_present=False) is True
    assert OnePasswordBackend.would_attempt(secret.name, mapping_present=False) is False
    assert OnePasswordBackend.would_attempt(secret.name, mapping_present=True) is True


def test_prompt_mapping_advertises_the_empty_input_vocabulary() -> None:
    from agentworks.capabilities.secret_backend.prompt import PromptMapping

    assert PromptMapping.model_json_schema() == {"not": {}}


def test_build_registry_is_pure(tmp_path: Path) -> None:
    """No memo: every call builds fresh (a command's composition root
    calls it once and threads the result)."""
    config = _config(tmp_path)
    first = build_registry(config)
    second = build_registry(config)
    assert first is not second

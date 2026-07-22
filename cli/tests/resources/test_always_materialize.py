"""Tests for the always-materialize pre-step in ``Registry.finalize``.

Phase 2a adds a pre-step: every kind whose ``auto_declare_names`` is a
non-None set gets its reserved names materialized at finalize even when
nothing references them. This closes the gap where an unreferenced
template default would otherwise crash at command time when the manager
looks it up.

The pre-step is no-op for resources already published by another
publisher (Config publishes admin / named_console singletons today,
so they keep their operator-declared origins). It only fires for
reserved names that *aren't* in the registry by the time finalize starts.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.sessions.template import NamedConsoleConfig
from agentworks.vms.admin import AdminConfig


def _write_minimal(path: Path) -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """),
    )
    return path


def test_admin_and_named_console_defaults_present_in_minimal_config(
    tmp_path: Path,
) -> None:
    """A config with no ``[admin.*]`` or ``[named_console]`` blocks still
    produces ``admin-template:default`` and ``named-console-template:default``
    in the registry. Today Config publishes synthesize-on-omit instances
    with operator-declared origins, so the always-materialize pre-step
    short-circuits; the rows are present either way.
    """
    cfg = load_config(_write_minimal(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)

    admin = registry.lookup("admin-template", "default")
    assert isinstance(admin, AdminConfig)
    assert admin.origin is not None

    nc = registry.lookup("named-console-template", "default")
    assert isinstance(nc, NamedConsoleConfig)
    assert nc.origin is not None


def test_unreferenced_default_lands_with_framework_source(tmp_path: Path) -> None:
    """Direct Registry.empty() + finalize, no publisher contributing
    admin-template:default. The always-materialize step lands the row
    with ``auto-declared`` origin and the synthetic
    ``("framework", "always-materialize")`` source.
    """
    from agentworks.resources import ALWAYS_MATERIALIZE_SOURCE, Registry

    registry = Registry.empty()
    registry.finalize()

    admin = registry.lookup("admin-template", "default")
    assert admin.origin.variant == "auto-declared"
    assert admin.origin.source == ALWAYS_MATERIALIZE_SOURCE


def test_always_materialized_row_gets_empty_usage_tuple_in_finalize(
    tmp_path: Path,
) -> None:
    """Unreferenced default goes through finalize's usage-attachment
    pass with ``usage=()``. AdminConfig has no ``description`` field so
    the polish is a no-op for it; the empty-usage description format is
    exercised against the helper directly in
    ``test_polish_empty_usage_format`` below.
    """
    from agentworks.resources import Registry

    registry = Registry.empty()
    registry.finalize()

    admin = registry.lookup("admin-template", "default")
    assert admin.references == ()


def test_polish_empty_usage_format() -> None:
    """The description-polish step's empty-usage branch reads as
    ``"(auto) auto-declared default <kind>"``. Exercised here directly
    against the helper because no current kind has both a ``description``
    field and a reserved auto-declare name; future kinds (plugin-published
    resources, etc.) inherit this behavior automatically because the
    polish is kind-agnostic.
    """
    from dataclasses import dataclass

    from agentworks.resources import ALWAYS_MATERIALIZE_SOURCE, Origin
    from agentworks.resources.registry import _polish_auto_declared_description

    @dataclass(frozen=True)
    class _Stub:
        description: str = ""
        origin: Origin | None = None
        references: tuple = ()

    stub = _Stub(
        description="",
        origin=Origin.auto_declared(source=ALWAYS_MATERIALIZE_SOURCE),
        references=(),
    )
    polished = _polish_auto_declared_description(stub, "vm-template")
    assert polished.description == "(auto) auto-declared default vm-template"


def test_polish_skips_operator_set_description() -> None:
    """Operator-set descriptions are honored verbatim regardless of
    origin variant; the polish only fires when the description is empty.
    """
    from dataclasses import dataclass

    from agentworks.resources import Origin
    from agentworks.resources.registry import _polish_auto_declared_description

    @dataclass(frozen=True)
    class _Stub:
        description: str = "operator's own text"
        origin: Origin | None = None
        references: tuple = ()

    stub = _Stub(
        description="operator's own text",
        origin=Origin.auto_declared(source=("vm-template", "default")),
        references=(),
    )
    polished = _polish_auto_declared_description(stub, "vm-template")
    assert polished.description == "operator's own text"


def test_polish_no_op_for_resources_without_description_field() -> None:
    """Kinds without a ``description`` field skip the polish entirely;
    the helper returns the resource unchanged.
    """
    from dataclasses import dataclass

    from agentworks.resources import Origin
    from agentworks.resources.registry import _polish_auto_declared_description

    @dataclass(frozen=True)
    class _NoDesc:
        origin: Origin | None = None
        references: tuple = ()

    stub = _NoDesc(
        origin=Origin.auto_declared(source=("vm-template", "default")),
        references=(),
    )
    polished = _polish_auto_declared_description(stub, "vm-template")
    assert polished is stub


def test_secret_kind_not_materialized_by_pre_step(tmp_path: Path) -> None:
    """``_SecretKind`` has ``auto_declare_names = None`` so the
    always-materialize step never synthesizes secrets directly. Any
    secret rows that DO appear in the registry came from the
    requirement-driven path (e.g., Phase 2a.1's always-materialized
    ``vm-template:default`` emits a ``SecretReference`` for
    ``tailscale-auth-key`` via its existing required_resources, which
    is the legitimate auto-declare path -- not always-materialize).

    The proof: every secret row in a minimal config has a non-empty
    usage list (showing a requirement triggered the auto-declare). If
    secrets WERE always-materialized, they'd have empty usage like
    template-kind defaults do.
    """
    from agentworks.resources import Registry

    registry = Registry.empty()
    registry.finalize()

    secrets = list(registry.iter_kind("secret"))
    # Positive assertion: Phase 2a.1's always-materialized
    # vm-template:default emits a SecretReference for
    # tailscale-auth-key via its required_resources, so the cascade
    # produces at least one secret row. Pinning this defends against a
    # future regression where the materialize-then-walk interaction
    # silently breaks (e.g. always-materialize lands rows but their
    # required_resources doesn't run).
    secret_names = {s.name for s in secrets}
    assert "tailscale-auth-key" in secret_names, (
        "expected vm-template:default's tailscale requirement to auto-declare 'tailscale-auth-key' via the cascade"
    )
    for secret in secrets:
        assert secret.references, (
            f"secret {secret.name!r} has empty references; suggests "
            f"always-materialize fired (which would be a contract "
            f"violation for the secret kind)"
        )


def test_operator_declared_admin_is_not_overwritten(tmp_path: Path) -> None:
    """When ``[admin.config]`` declares the admin template, Config
    publishes operator-declared. The always-materialize step must NOT
    overwrite the operator-declared row -- the short-circuit
    ``if name in self._resources.get(kind, {})`` handles it.
    """
    cfg_file = tmp_path / "config.toml"
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [admin.config]
        shell = "zsh"
        """),
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)

    admin = registry.lookup("admin-template", "default")
    assert admin.origin.variant == "operator-declared"
    assert admin.shell == "zsh"

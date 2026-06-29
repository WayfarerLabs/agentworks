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
from agentworks.config import AdminConfig, NamedConsoleConfig, load_config


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
    produces ``admin_template:default`` and ``named_console_template:default``
    in the registry. Today Config publishes synthesize-on-omit instances
    with operator-declared origins, so the always-materialize pre-step
    short-circuits; the rows are present either way.
    """
    cfg = load_config(_write_minimal(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)

    admin = registry.lookup("admin_template", "default")
    assert isinstance(admin, AdminConfig)
    assert admin.origin is not None

    nc = registry.lookup("named_console_template", "default")
    assert isinstance(nc, NamedConsoleConfig)
    assert nc.origin is not None


def test_unreferenced_default_lands_with_framework_source(tmp_path: Path) -> None:
    """Direct Registry.empty() + finalize, no publisher contributing
    admin_template:default. The always-materialize step lands the row
    with ``auto-declared`` origin and the synthetic
    ``("framework", "always-materialize")`` source.
    """
    from agentworks.resources import ALWAYS_MATERIALIZE_SOURCE, Registry

    registry = Registry.empty()
    registry.finalize()

    admin = registry.lookup("admin_template", "default")
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

    admin = registry.lookup("admin_template", "default")
    assert admin.usage == ()


def test_polish_empty_usage_format() -> None:
    """The description-polish step's empty-usage branch reads as
    ``"(auto) auto-declared default <kind>"``. Exercised here directly
    against the helper because no current kind has both a ``description``
    field and a reserved auto-declare name; future kinds (catalog
    entries, plugin-published resources) inherit this behavior
    automatically because the polish is kind-agnostic.
    """
    from dataclasses import dataclass

    from agentworks.resources import ALWAYS_MATERIALIZE_SOURCE, Origin
    from agentworks.resources.registry import _polish_auto_declared_description

    @dataclass(frozen=True)
    class _Stub:
        description: str = ""
        origin: Origin | None = None
        usage: tuple = ()

    stub = _Stub(
        description="",
        origin=Origin.auto_declared(source=ALWAYS_MATERIALIZE_SOURCE),
        usage=(),
    )
    polished = _polish_auto_declared_description(stub, "vm_template")
    assert polished.description == "(auto) auto-declared default vm_template"


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
        usage: tuple = ()

    stub = _Stub(
        description="operator's own text",
        origin=Origin.auto_declared(source=("vm_template", "default")),
        usage=(),
    )
    polished = _polish_auto_declared_description(stub, "vm_template")
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
        usage: tuple = ()

    stub = _NoDesc(
        origin=Origin.auto_declared(source=("vm_template", "default")),
        usage=(),
    )
    polished = _polish_auto_declared_description(stub, "vm_template")
    assert polished is stub


def test_secret_kind_not_materialized_by_pre_step(tmp_path: Path) -> None:
    """``_SecretKind`` has ``auto_declare_names = None`` so the
    always-materialize step skips it. No spurious ``secret:<anything>``
    rows appear in a minimal config.
    """
    from agentworks.resources import Registry

    registry = Registry.empty()
    registry.finalize()

    # iter_kind returns an empty iterator when the kind has no rows.
    secrets = list(registry.iter_kind("secret"))
    assert secrets == []


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

    admin = registry.lookup("admin_template", "default")
    assert admin.origin.variant == "operator-declared"
    assert admin.shell == "zsh"

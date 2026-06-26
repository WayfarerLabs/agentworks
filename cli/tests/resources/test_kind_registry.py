"""Tests for ``KIND_REGISTRY`` and each Phase 1a kind's ``synthesize`` shape."""

from __future__ import annotations

from agentworks.config import AdminConfig, NamedConsoleConfig
from agentworks.resources import KIND_REGISTRY, SecretRequirement
from agentworks.secrets.base import SecretDecl


def _secret_req(name: str, source: tuple[str, str], usage: str = "the X env var") -> SecretRequirement:
    return SecretRequirement(name=name, kind="secret", usage=usage, source=source)


def _admin_req() -> SecretRequirement:
    return SecretRequirement(
        name="default", kind="admin_template", usage="ignored", source=("test", "x")
    )


def _named_console_req() -> SecretRequirement:
    return SecretRequirement(
        name="default", kind="named_console_template", usage="ignored", source=("test", "x")
    )


def test_phase_1a_kinds_registered() -> None:
    assert set(KIND_REGISTRY) >= {"secret", "admin_template", "named_console_template"}


def test_secret_kind_attributes() -> None:
    k = KIND_REGISTRY["secret"]
    assert k.kind == "secret"
    assert k.miss_policy == "auto-declare"
    assert k.auto_declare_names is None  # any name


def test_admin_template_kind_attributes() -> None:
    k = KIND_REGISTRY["admin_template"]
    assert k.kind == "admin_template"
    assert k.miss_policy == "auto-declare"
    assert k.auto_declare_names == frozenset({"default"})


def test_named_console_template_kind_attributes() -> None:
    k = KIND_REGISTRY["named_console_template"]
    assert k.kind == "named_console_template"
    assert k.miss_policy == "auto-declare"
    assert k.auto_declare_names == frozenset({"default"})


def test_secret_kind_synthesize_builds_auto_declared_decl() -> None:
    reqs = [
        _secret_req("api-key", ("vm_template", "default"), "the auth key"),
        _secret_req("api-key", ("admin_template", "default"), "the admin env var"),
    ]
    decl = KIND_REGISTRY["secret"].synthesize(reqs)
    assert isinstance(decl, SecretDecl)
    assert decl.name == "api-key"
    assert decl.description == ""
    assert decl.origin is not None
    assert decl.origin.variant == "auto-declared"
    assert decl.origin.source == ("vm_template", "default")  # first-matching
    assert len(decl.usage) == 2
    assert decl.usage[0].source == ("vm_template", "default")
    assert decl.usage[0].text == "the auth key"
    assert decl.usage[1].source == ("admin_template", "default")
    assert decl.usage[1].text == "the admin env var"


def test_admin_template_kind_synthesize_builds_empty_admin() -> None:
    admin = KIND_REGISTRY["admin_template"].synthesize([_admin_req()])
    assert isinstance(admin, AdminConfig)
    assert admin.origin is not None
    assert admin.origin.variant == "auto-declared"
    # Default AdminConfig fields:
    assert admin.username == "agentworks"
    assert admin.shell == "zsh"


def test_named_console_template_kind_synthesize_builds_empty_named_console() -> None:
    nc = KIND_REGISTRY["named_console_template"].synthesize([_named_console_req()])
    assert isinstance(nc, NamedConsoleConfig)
    assert nc.origin is not None
    assert nc.origin.variant == "auto-declared"

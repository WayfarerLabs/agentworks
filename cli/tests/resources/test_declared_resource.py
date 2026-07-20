"""``DeclaredResource``: the shared metadata base every declared-resource
dataclass inherits.

Two guarantees are pinned here. First, the base itself carries the five
metadata fields with the right defaults and an empty ``referenced_resources``,
and a plain subclass inherits that override-free. Second, every one of the
nine full-shape resource dataclasses actually descends from the base, so the
"metadata (including ``description``) exists by construction" promise cannot
silently regress for any one kind.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.declared_resource import DeclaredResource
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.secrets.base import SecretDecl
from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate
from agentworks.source_location import synthesized
from agentworks.vms.admin import AdminConfig
from agentworks.vms.sites import VMSiteDecl
from agentworks.vms.template import VMTemplate
from agentworks.workspaces.template import WorkspaceTemplate


def test_base_carries_metadata_fields_with_defaults() -> None:
    resource = DeclaredResource(name="thing")
    assert resource.name == "thing"
    assert resource.description is None
    assert resource.declared_at == synthesized()
    assert resource.origin is None
    assert resource.references == ()
    assert resource.referenced_resources() == []


def test_plain_subclass_inherits_empty_referenced_resources() -> None:
    @dataclass(frozen=True, kw_only=True)
    class _NoOverride(DeclaredResource):
        pass

    assert _NoOverride(name="x").referenced_resources() == []


# The nine full-shape resource dataclasses (all carrying name + description +
# declared_at + origin + references). Pinning the subclass relationship is
# what keeps a kind from silently dropping a metadata field again.
_FULL_SHAPE_RESOURCES = [
    VMTemplate,
    AgentTemplate,
    WorkspaceTemplate,
    AdminConfig,
    NamedConsoleConfig,
    SessionTemplate,
    SecretDecl,
    GitCredentialConfig,
    VMSiteDecl,
]


@pytest.mark.parametrize("cls", _FULL_SHAPE_RESOURCES)
def test_concrete_resource_subclasses_declared_resource(
    cls: type[DeclaredResource],
) -> None:
    assert issubclass(cls, DeclaredResource)


def test_secret_decl_description_is_required() -> None:
    """``SecretDecl`` overrides the base's optional ``description`` back to
    required. This guards a real dataclass-inheritance trap: a bare
    ``description: str`` on the subclass would inherit the base's
    ``description = None`` default and silently stay optional, so the
    override uses ``field()`` to force MISSING. Without the guard, secrets
    could be declared with no description.
    """
    with pytest.raises(TypeError):
        SecretDecl(name="x")  # type: ignore[call-arg]
    assert SecretDecl(name="x", description="d").description == "d"


def test_optional_description_still_defaults_to_none() -> None:
    """The other full-shape resources keep the base's optional
    ``description`` (the SecretDecl override must not leak to siblings).
    """
    assert VMTemplate(name="dev").description is None


def test_admin_config_name_defaults_to_default() -> None:
    """``AdminConfig`` overrides the base's required ``name`` with the
    ``"default"`` singleton default; an omitted-name construction is valid.
    """
    assert AdminConfig().name == "default"

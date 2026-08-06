"""``DeclaredResource``: the shared metadata base every declared-resource
row inherits.

Two guarantees are pinned here. First, the base itself carries the four
metadata fields with the right defaults and an empty ``dependencies``,
and a plain subclass inherits that override-free. Second, every concrete
declared-resource row (the operator-declared templates plus the
apt / install-command entries) actually descends from the base, so the
"metadata (including ``description``) exists by construction" promise cannot
silently regress for any one kind.

A third is pinned as a consequence of the rows being MODELS: the two
framework fields are not operator surface, so neither the emitted schema
nor the field-reference stream carries them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.apt import AptPackageEntry, AptSourceEntry
from agentworks.declared_resource import DeclaredResource
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.install_commands import (
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
)
from agentworks.resources.graph import FinalizeContext
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
    assert resource.dependencies(FinalizeContext()) == []


def test_plain_subclass_inherits_empty_dependencies() -> None:
    class _NoOverride(DeclaredResource):
        """A row that adds nothing to the base."""

    assert _NoOverride(name="x").dependencies(FinalizeContext()) == []


def test_only_the_kinds_own_fields_are_spec_surface() -> None:
    """The row IS the kind's spec model, so neither emitted schema nor the
    field-reference stream may offer the envelope metadata or the
    framework's provenance as something an operator writes under
    ``spec``."""
    from agentworks.schema import iter_field_docs

    class _Spec(DeclaredResource):
        """A row with one spec field beside the base's metadata."""

        cpus: int | None = None

    assert set(_Spec.model_json_schema()["properties"]) == {"cpus"}
    assert [doc.path for doc in iter_field_docs(_Spec)] == [("cpus",)]


# Every concrete declared-resource row (all carrying name + description +
# declared_at + origin via the base). Pinning the subclass relationship is what
# keeps a kind from silently dropping a metadata field again. The last four are
# the apt / install-command entries.
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
    AptSourceEntry,
    AptPackageEntry,
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
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
    with pytest.raises(PydanticValidationError):
        SecretDecl(name="x")  # type: ignore[call-arg]
    assert SecretDecl(name="x", description="d").description == "d"


@pytest.mark.parametrize(
    ("cls", "kind_kwargs"),
    [
        (AptSourceEntry, {"key_url": "u", "key_path": "p", "source": "s", "source_file": "f"}),
        (AptPackageEntry, {"apt": ["pkg"]}),
        (SystemInstallCommandEntry, {"command": "c"}),
        (UserInstallCommandEntry, {"command": "c"}),
    ],
)
def test_apt_and_install_entry_description_is_optional(
    cls: type[DeclaredResource], kind_kwargs: dict[str, object]
) -> None:
    """The four apt / install-command entries inherit the base's OPTIONAL
    ``description``, unlike ``SecretDecl``.

    They used to require it on the class while their loaders defaulted it
    to ``""``, so a manifest that omitted ``metadata.description`` got an
    empty string and a direct construction got a ``TypeError``: two
    spellings of "no description", neither of them the base's. One value
    now, and it is the base's."""
    assert cls(name="x", **kind_kwargs).description is None
    entry = cls(name="x", description="d", **kind_kwargs)
    assert entry.description == "d"
    assert entry.declared_at == synthesized()


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

"""Phase 3a naming-consistency guard.

The Phase 3a rename swept ``ResourceRequirement`` -> ``ResourceReference``,
``UsageEntry`` -> ``ReferenceEntry``, the per-Resource collection field
``usage`` -> ``references``, and the producer method ``required_resources``
-> ``referenced_resources`` across the framework. This test pins the new
shape by asserting that the framework's public surface no longer exposes
any symbol whose name carries the old vocabulary -- a partial-rename
regression that compiled cleanly would otherwise slip through.

Scope: the ``agentworks.resources`` package and its kind modules plus the
producer touch-points (``EnvEntry``, ``SecretDecl``). The test walks
public attributes (``dir`` minus the underscore-prefixed names) and the
known producer module surface.
"""

from __future__ import annotations

import inspect
from types import ModuleType

import pytest

import agentworks.resources as resources_pkg
from agentworks import apt as apt_mod
from agentworks import install_commands as install_commands_mod
from agentworks.agents import kinds as agents_kinds_mod
from agentworks.capabilities.git_credential import kinds as git_credentials_kinds_mod
from agentworks.env.entry import EnvEntry
from agentworks.resources import inspect as inspect_mod
from agentworks.resources import reference as reference_mod
from agentworks.resources import registry as registry_mod
from agentworks.resources import walk as walk_mod
from agentworks.secrets import kinds as secrets_kinds_mod
from agentworks.sessions import kinds as sessions_kinds_mod
from agentworks.vms import kinds as vms_kinds_mod
from agentworks.workspaces import kinds as workspaces_kinds_mod

_BANNED_SUBSTRINGS = ("Requirement", "UsageEntry")


def _public_names(module: ModuleType) -> list[str]:
    return [name for name in dir(module) if not name.startswith("_")]


@pytest.mark.parametrize(
    "module",
    [
        resources_pkg,
        reference_mod,
        registry_mod,
        walk_mod,
        inspect_mod,
        agents_kinds_mod,
        apt_mod,
        install_commands_mod,
        git_credentials_kinds_mod,
        secrets_kinds_mod,
        sessions_kinds_mod,
        vms_kinds_mod,
        workspaces_kinds_mod,
    ],
)
def test_framework_module_has_no_old_vocabulary(module: ModuleType) -> None:
    """No public symbol on a framework module carries the old
    ``Requirement`` / ``UsageEntry`` vocabulary. Phase 3a's rename is
    a public-surface change; a future edit that re-introduces either
    word is a regression.
    """
    offenders = [name for name in _public_names(module) if any(banned in name for banned in _BANNED_SUBSTRINGS)]
    assert offenders == [], (
        f"{module.__name__} exposes legacy-named symbols: {offenders}. "
        f"Rename to the Phase 3a vocabulary (Reference / ReferenceEntry)."
    )


def test_resource_reference_carries_usage_field_not_text() -> None:
    """The `usage` prose field landed on both outbound (``ResourceReference``)
    and inbound (``ReferenceEntry``) types after the Phase 3a rename. The
    pre-rename ``UsageEntry.text`` is gone; the symmetry is part of the
    documented contract (see reference.py module docstring).
    """
    import dataclasses

    from agentworks.resources.reference import ReferenceEntry, ResourceReference

    ref_fields = {f.name for f in dataclasses.fields(ResourceReference)}
    entry_fields = {f.name for f in dataclasses.fields(ReferenceEntry)}
    assert "usage" in ref_fields
    assert "usage" in entry_fields
    assert "text" not in ref_fields
    assert "text" not in entry_fields


def test_producer_method_is_referenced_resources_not_required_resources() -> None:
    """Producers expose ``referenced_resources()``, not the old
    ``required_resources()``. Phase 3a renamed both the method and every
    call site (including the framework's ``getattr`` lookups in
    ``Registry._referenced_resources`` and ``walk._referenced_resources``).
    """
    entry = EnvEntry(key="K", secret="s")
    assert hasattr(entry, "referenced_resources")
    assert not hasattr(entry, "required_resources")
    # Inspect that the method exists as a real method, not via getattr
    # fallback. The kinds/* modules' Resource types all expose this name
    # (those with no references override to an empty list).
    sig = inspect.signature(entry.referenced_resources)
    assert "source" in sig.parameters


def test_resource_kinds_have_references_field_not_usage() -> None:
    """Every Resource type in the framework's kind set carries the
    collection field as ``references``, not the pre-rename ``usage``.
    """
    from agentworks.agents.template import AgentTemplate
    from agentworks.apt import AptPackageEntry, AptSourceEntry
    from agentworks.git_credentials.credential import GitCredentialConfig
    from agentworks.install_commands import (
        SystemInstallCommandEntry,
        UserInstallCommandEntry,
    )
    from agentworks.secrets.base import SecretDecl
    from agentworks.sessions.template import NamedConsoleConfig, SessionTemplate
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.template import VMTemplate
    from agentworks.workspaces.template import WorkspaceTemplate

    resource_types = [
        AptSourceEntry,
        AptPackageEntry,
        SystemInstallCommandEntry,
        UserInstallCommandEntry,
        AdminConfig,
        AgentTemplate,
        GitCredentialConfig,
        NamedConsoleConfig,
        SessionTemplate,
        VMTemplate,
        WorkspaceTemplate,
        SecretDecl,
    ]
    import dataclasses

    for cls in resource_types:
        fields = {f.name for f in dataclasses.fields(cls)}
        assert "references" in fields, f"{cls.__name__} missing `references` field after Phase 3a rename"
        assert "usage" not in fields, f"{cls.__name__} still carries pre-rename `usage` collection field"


def test_resources_package_has_no_old_vocabulary_in_source() -> None:
    """Complementary guard against Phase 3a stragglers in *prose* (comments
    and docstrings) -- the symbol-level test above catches type-name
    regressions but not prose. Scans every .py file under
    ``agentworks.resources`` for ``ResourceRequirement`` / ``SecretRequirement``
    / ``UsageEntry`` / ``required_resources``. The Resource framework is
    the area where vocabulary consistency matters most; broader scans live
    closer to where they're useful (e.g. CLI / SDD docs are scanned by
    lint-files.sh and reviewer passes).
    """
    import pathlib

    pkg_root = pathlib.Path(resources_pkg.__file__).resolve().parent
    banned = (
        "ResourceRequirement",
        "SecretRequirement",
        "TemplateRequirement",
        "UsageEntry",
        "required_resources",
        # Bare-word vocabulary that survived the type-name rename initially.
        # Now that the synthesize() parameter is `references` and internal
        # locals are `refs`/`all_refs`, `requirements` should not appear
        # anywhere in resources/ source.
        "requirements",
    )
    offenders: list[str] = []
    for py_file in pkg_root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for bad in banned:
            if bad in text:
                offenders.append(f"{py_file.relative_to(pkg_root)}: {bad}")
    assert offenders == [], (
        f"agentworks.resources still carries pre-rename vocabulary in comments/docstrings: {offenders}"
    )

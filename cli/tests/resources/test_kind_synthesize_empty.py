"""Tests for each kind's ``synthesize(requirements=())`` contract.

Phase 2a tightens the framework contract: every kind's ``synthesize``
must have defined behavior when called with no requirements. Kinds whose
``auto_declare_names`` is a non-None set build a code-defined default
(with the framework's reserved synthetic source); kinds with
``auto_declare_names = None`` raise ``NoUnreferencedDefaultError``.

The framework's always-materialize pre-step only calls synthesize empty
for the former category, so the latter's error is defensive (covers a
hypothetical future change to the kind's auto-declare configuration).
"""

from __future__ import annotations

import pytest

from agentworks.resources import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    NoUnreferencedDefaultError,
)
from agentworks.sessions.template import NamedConsoleConfig
from agentworks.vms.admin import AdminConfig


def test_secret_kind_raises_on_empty_requirements() -> None:
    """``_SecretKind`` has ``auto_declare_names = None`` -- the framework
    never calls its synthesize with no requirements. The kind's contract
    still has to be defined for that path; it raises a typed error so a
    future change that gives the secret kind a reserved default has an
    obvious landing pad.
    """
    secret_kind = KIND_REGISTRY["secret"]
    with pytest.raises(NoUnreferencedDefaultError, match="secret kind"):
        secret_kind.synthesize(())


def test_admin_template_kind_builds_default_on_empty_requirements() -> None:
    """``_AdminTemplateKind`` has ``auto_declare_names = {"default"}``.
    Called empty (the framework's always-materialize path), it builds an
    empty-defaults ``AdminConfig`` with the synthetic
    ``("framework", "always-materialize")`` source so the breadcrumb
    shows the row's provenance.
    """
    admin_kind = KIND_REGISTRY["admin-template"]
    result = admin_kind.synthesize(())
    assert isinstance(result, AdminConfig)
    assert result.origin is not None
    assert result.origin.variant == "auto-declared"
    assert result.origin.source == ALWAYS_MATERIALIZE_SOURCE


def test_named_console_template_kind_builds_default_on_empty_requirements() -> None:
    """Same shape as ``admin-template``: code-defined default + synthetic
    source.
    """
    nc_kind = KIND_REGISTRY["named-console-template"]
    result = nc_kind.synthesize(())
    assert isinstance(result, NamedConsoleConfig)
    assert result.origin is not None
    assert result.origin.variant == "auto-declared"
    assert result.origin.source == ALWAYS_MATERIALIZE_SOURCE


def test_always_materialize_source_is_reserved_constant() -> None:
    """The reserved sentinel is exported as a module constant so kinds
    don't hardcode the tuple literal in their own modules.
    """
    assert ALWAYS_MATERIALIZE_SOURCE == ("framework", "always-materialize")

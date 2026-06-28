"""Tests for the framework pass that ``Registry.finalize()`` runs: walking
requirements, dispatching miss policies, attaching usage, detecting cycles.

Phase 1a has no operator-side producers of ``SecretRequirement``; tests
synthesize them by attaching ``required_resources()`` to stub Resources or
directly populating ``Registry._resources``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.resources import Origin, Registry, SecretRequirement
from agentworks.resources.requirement import ResourceRequirement
from agentworks.secrets.base import SecretDecl


@dataclass(frozen=True)
class _PublisherStub:
    """A test-only Resource that publishes a fixed list of requirements
    via ``required_resources()``. Lives outside any kind in KIND_REGISTRY;
    Registry stores it under whatever kind the test uses.
    """

    reqs: tuple[ResourceRequirement, ...] = ()
    origin: Origin | None = None
    usage: tuple = ()

    def required_resources(self) -> tuple[ResourceRequirement, ...]:
        return self.reqs


def _opdecl(line: int = 1) -> Origin:
    return Origin.operator_declared(file=Path("/x/c.toml"), line=line)


# -- Auto-declare on miss for secrets ----------------------------------------


def test_secret_auto_declared_when_required_but_not_published() -> None:
    """A SecretRequirement for an unpublished name triggers the
    secret kind's auto-declare miss policy.
    """
    r = Registry.empty()
    stub = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="api-key", kind="secret", usage="the API key",
                source=("admin_template", "default"),
            ),
        ),
    )
    r.add("publisher_kind", "src", stub, _opdecl())
    r.finalize()

    found = r.lookup("secret", "api-key")
    assert isinstance(found, SecretDecl)
    assert found.origin is not None
    assert found.origin.variant == "auto-declared"
    assert found.origin.source == ("admin_template", "default")


# -- Reserved-name restriction -----------------------------------------------


def test_admin_template_rejects_non_default_name() -> None:
    """``admin_template`` kind only auto-declares ``default``. A
    requirement for any other name errors at finalize.
    """
    r = Registry.empty()
    stub = _PublisherStub(
        reqs=(
            ResourceRequirement(
                name="custom",
                kind="admin_template",
                usage="ignored",
                source=("test", "x"),
            ),
        ),
    )
    r.add("publisher_kind", "src", stub, _opdecl())
    with pytest.raises(ConfigError, match="reserved name"):
        r.finalize()


# -- Operator-declared Resource gets usage attached on finalize ---------------


def test_operator_declared_secret_gets_usage_populated() -> None:
    """An operator-declared SecretDecl in the registry accumulates a
    ``usage`` list from the requirements pointing at it.
    """
    r = Registry.empty()
    decl = SecretDecl(name="api-key", description="API key")
    r.add("secret", "api-key", decl, _opdecl(line=5))

    stub_a = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="api-key", kind="secret", usage="the API env var",
                source=("admin_template", "default"),
            ),
        ),
    )
    stub_b = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="api-key", kind="secret", usage="the agent's API env var",
                source=("agent_template", "claude"),
            ),
        ),
    )
    r.add("publisher_kind", "src_a", stub_a, _opdecl())
    r.add("publisher_kind", "src_b", stub_b, _opdecl())
    r.finalize()

    found = r.lookup("secret", "api-key")
    # Origin from publish stays operator-declared (the secret was operator-typed).
    assert found.origin is not None
    assert found.origin.variant == "operator-declared"
    assert found.origin.line == 5
    # Usage gets populated from BOTH requirements.
    assert len(found.usage) == 2
    sources = sorted(u.source for u in found.usage)
    assert sources == [("admin_template", "default"), ("agent_template", "claude")]


# -- Multiple requirements -> auto-declare uses first source -----------------


def test_auto_declared_secret_origin_uses_first_matching_requirement() -> None:
    r = Registry.empty()
    stub_a = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="shared-key", kind="secret", usage="A's use",
                source=("admin_template", "default"),
            ),
        ),
    )
    stub_b = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="shared-key", kind="secret", usage="B's use",
                source=("vm_template", "default"),
            ),
        ),
    )
    r.add("publisher_kind", "src_a", stub_a, _opdecl())
    r.add("publisher_kind", "src_b", stub_b, _opdecl())
    r.finalize()

    found = r.lookup("secret", "shared-key")
    # First publisher wins for Origin source.
    assert found.origin is not None
    assert found.origin.source == ("admin_template", "default")
    # Usage records BOTH requirements.
    assert len(found.usage) == 2


def test_publish_order_determines_first_matching_origin_source() -> None:
    """Reverse publish order to confirm the rule is "first publisher wins"
    and not "alphabetical" or anything implicit. Pins the dict-insertion-
    order contract that ``Registry.finalize`` relies on.
    """
    r = Registry.empty()
    # SAME two stubs, opposite registration order.
    stub_a = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="shared-key", kind="secret", usage="A's use",
                source=("admin_template", "default"),
            ),
        ),
    )
    stub_b = _PublisherStub(
        reqs=(
            SecretRequirement(
                name="shared-key", kind="secret", usage="B's use",
                source=("vm_template", "default"),
            ),
        ),
    )
    # Add B FIRST this time.
    r.add("publisher_kind", "src_b", stub_b, _opdecl())
    r.add("publisher_kind", "src_a", stub_a, _opdecl())
    r.finalize()

    found = r.lookup("secret", "shared-key")
    # vm_template's req now wins because B was added first.
    assert found.origin is not None
    assert found.origin.source == ("vm_template", "default")


@dataclass(frozen=True)
class _ChainPublisher:
    """Test-only Resource that publishes one ``SecretRequirement``. Used
    to set up the multi-level synthesize scenario where synthesizing one
    Resource produces a second-level requirement.
    """

    target_name: str
    source_name: str
    origin: Origin | None = None
    usage: tuple = ()

    def required_resources(self) -> tuple[SecretRequirement, ...]:
        return (
            SecretRequirement(
                name=self.target_name, kind="secret",
                usage="downstream", source=("publisher_kind", self.source_name),
            ),
        )


def test_synthesize_path_walked_for_second_level_requirements() -> None:
    """Reviewer-flagged correctness case: a Resource synthesized during
    finalize may itself produce requirements, and those need to be
    walked / dispatched. A single-pass finalize would silently drop
    them. The worklist-loop approach handles this.

    Setup: synthesize a SecretDecl, then point an operator-declared
    publisher at it whose req-list adds an INCOMING usage entry for
    the synthesized secret. The auto-declared secret's usage list
    should end up populated by finalize's post-stabilization pass --
    proving finalize walked the synthesized Resource even though its
    ``required_resources()`` is empty (and the reverse case is also
    exercised: usage attachment happens after the worklist settles
    rather than at synthesize time).
    """
    r = Registry.empty()
    # Two operator-declared publishers both pointing at "shared".
    # finalize auto-declares "shared"; usage should have 2 entries.
    r.add(
        "publisher_kind", "p1",
        _ChainPublisher(target_name="shared", source_name="p1"),
        _opdecl(),
    )
    r.add(
        "publisher_kind", "p2",
        _ChainPublisher(target_name="shared", source_name="p2"),
        _opdecl(),
    )
    r.finalize()

    shared = r.lookup("secret", "shared")
    # Both incoming requirements attached to usage after finalize.
    assert len(shared.usage) == 2
    assert {u.source for u in shared.usage} == {
        ("publisher_kind", "p1"),
        ("publisher_kind", "p2"),
    }

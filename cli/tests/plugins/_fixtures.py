"""Test-local fixture plugin and capability impls for the plugin framework.

Minimal impls: each carries the ``name`` / ``description`` class attributes
the adapters read, which is all seating and row-building need in isolation
(no ``build_registry``, no publish, no resolution runs in this phase). They
are deliberately NOT the real capability base classes: the framework treats
them as opaque impl classes, and keeping them minimal keeps the fixture
readable.
"""

from __future__ import annotations

from agentworks.plugins import Plugin


class FixtureVMPlatform:
    name = "fixture-vm"
    description = "Fixture VM platform"


class FixtureHarnessIntegration:
    name = "fixture-harness"
    description = "Fixture harness"


class FixtureProvider:
    name = "fixture-provider"
    description = "Fixture git credential provider"


class FixtureBackend:
    name = "fixture-backend"
    description = "Fixture secret backend"


def fixture_plugin(name: str = "fixture") -> Plugin:
    """A plugin seating one impl of every capability kind."""
    return Plugin(
        name=name,
        description="a test fixture plugin",
        capabilities={
            "vm-platform": (FixtureVMPlatform,),
            "harness-integration": (FixtureHarnessIntegration,),
            "git-credential-provider": (FixtureProvider,),
            "secret-backend": (FixtureBackend,),
        },
    )

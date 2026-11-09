"""A setup request's environment: the setup's own, with a plugin's additions, and core's identity kept."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

from agentworks.capabilities.harness_integration.setup import VMSetupInvocation
from agentworks.db import VMRow
from tests.vm_access_support import Admin, RecordingVMAccess


def invocation(environment: dict[str, str]) -> VMSetupInvocation:
    access = RecordingVMAccess([], Admin())
    return VMSetupInvocation(
        vm=cast("VMRow", object()),
        access=access,
        files=access.files(),
        runner=Mock(),
        prior=None,
        checkpoint=lambda claims: None,
        environment=environment,
    )


def test_a_plugin_overrides_and_adds_variables_but_never_an_agentworks_identity_value() -> None:
    setup = invocation({"TEAM_MODE": "careful", "API_TOKEN": "fixture-secret", "AGENTWORKS_VM": "vm"})

    environment = setup.request_environment(
        {"TEAM_MODE": "quick", "EXTRA": "1", "AGENTWORKS_VM": "forged", "AGENTWORKS_SESSION": "foreign"}
    )

    assert environment == {"TEAM_MODE": "quick", "API_TOKEN": "fixture-secret", "AGENTWORKS_VM": "vm", "EXTRA": "1"}

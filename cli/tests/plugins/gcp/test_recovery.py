"""Recovery commands reach the shell as the exact argv they name, whatever the names hold."""

from __future__ import annotations

import shlex

import pytest

from agentworks.plugins.gcp.compute import provider_operation_name
from agentworks.plugins.gcp.recovery import GcloudResource

_HOSTILE = "p'; touch /tmp/owned #"


def _argv(command: str) -> list[str]:
    return shlex.split(command.strip("`"))


@pytest.mark.parametrize(
    "resource",
    [
        GcloudResource("instance", _HOSTILE, f"vm {_HOSTILE}", f"zone {_HOSTILE}"),
        GcloudResource("firewall rule", _HOSTILE, f"rule {_HOSTILE}"),
    ],
    ids=("instance", "firewall-rule"),
)
def test_every_value_in_a_recovery_command_stays_one_argument(resource: GcloudResource) -> None:
    for command in (resource.describe(), resource.delete()):
        argv = _argv(command)
        assert argv[argv.index("--project") + 1] == resource.project_id
        assert resource.name in argv
        if resource.zone is not None:
            assert argv[argv.index("--zone") + 1] == resource.zone
    (wait,) = (part for part in resource.insert_wait("operation-1").split("`") if part.startswith("gcloud"))
    assert _argv(wait)[_argv(wait).index("--project") + 1] == resource.project_id


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("operation-1700000000000-60a1b2c3-1a2b3c4d", True),
        ("Operation-1", False),
        ("operation-1; rm -rf ~", False),
        ("", False),
        (None, False),
        (7, False),
    ],
)
def test_only_a_plain_resource_name_is_taken_as_an_operation_name(name: object, accepted: bool) -> None:
    operation = type("Operation", (), {"name": name})()

    assert provider_operation_name(operation) == (name if accepted else None)

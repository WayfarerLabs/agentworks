"""Recovery commands reach the shell as the exact argv they name, whatever the names hold, and a delete is
offered only for a resource whose owned provider ID is known."""

from __future__ import annotations

import shlex

import pytest

from agentworks.plugins.gcp.compute import provider_operation_name
from agentworks.plugins.gcp.recovery import GcloudResource, may_still_appear

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
    for operation_name in ("operation-1", None):
        (wait,) = (part for part in resource.insert_wait(operation_name).split("`") if part.startswith("gcloud"))
        argv = _argv(wait)
        assert argv[argv.index("--project") + 1] == resource.project_id
        if resource.zone is not None:
            assert resource.zone in argv
        if operation_name is None:
            (query,) = (arg.removeprefix("--filter=") for arg in argv if arg.startswith("--filter="))
            assert query.endswith(f"/{resource.name}$")


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


@pytest.mark.parametrize(
    "resource",
    [GcloudResource("instance", "proj", "vm-1", "us-central1-a"), GcloudResource("firewall rule", "proj", "rule-1")],
    ids=("instance", "firewall-rule"),
)
@pytest.mark.parametrize("operation_name", ["operation-1", None])
def test_an_insert_that_may_still_appear_offers_its_delete_only_with_an_owned_id(
    resource: GcloudResource, operation_name: str | None
) -> None:
    """Without the ID its own insert recorded, a resource of that name may be someone else's: no delete."""
    owned = may_still_appear(resource, operation_name=operation_name, expected_id="4242")
    unowned = may_still_appear(resource, operation_name=operation_name, expected_id=None)

    for steps in (owned, unowned):
        assert resource.insert_wait(operation_name) in steps
        assert resource.describe() in steps
    assert resource.delete() in owned and "4242" in owned
    assert resource.delete() not in unowned

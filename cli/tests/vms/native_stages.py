"""Focused access fixtures compose the production stages and preparation custody."""

from typing import Any

from agentworks.db import VMStatus
from agentworks.execution.carrier import Deadline
from agentworks.vms._native_operation import _prepare_target, _Workflow


def prepare_native(fixture: Any, power: VMStatus, deadline: Deadline) -> None:
    access = fixture.access
    if not hasattr(fixture, "workflow"):
        fixture.workflow = _Workflow(fixture.owner, deadline, access=access)
    if access.native_configuration_check is not None:
        access.native_configuration_check()
    locator = access.ensure_available(power, deadline)
    binding = access.prepare_native_route(deadline)
    _prepare_target(fixture.workflow, access._vm, access._platform, locator, binding)

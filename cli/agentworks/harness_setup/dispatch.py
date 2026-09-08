"""Ordered setup dispatch with synchronous instance-state acknowledgements."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled, harness_integration_for
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.errors import ConfigError, StateError
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
from agentworks.harness_setup.state import read_native_setup, replace_setup_record, write_native_setup

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources.registry import Registry


def destination_id(invocation: SetupInvocation) -> str:
    """Fingerprint actual native placement and its owning VM generation.

    Native filesystem identity distinguishes a recreated user home or workspace.
    This contains no env or resolved secret values and performs only probes.
    """
    location: str | None = None
    user: str | None = None
    if isinstance(invocation, UserSetupInvocation):
        location = invocation.home
        user = invocation.username
    elif isinstance(invocation, WorkspaceSetupInvocation):
        location = invocation.root
    machine = invocation.runner.run("cat /etc/machine-id", timeout=15).stdout.strip()
    if not machine:
        raise StateError("native setup could not establish the VM identity")
    directory = None
    if location is not None:
        directory = invocation.runner.run(f"stat -Lc '%d:%i:%u' -- {shlex.quote(location)}", timeout=15).stdout.strip()
        if not directory:
            raise StateError("native setup could not establish the destination identity")
    vm = invocation.vm
    payload = (vm.name, vm.site, vm.created_at, vm.platform_metadata, machine, user, location, directory)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_setup(
    db: Database,
    registry: Registry,
    inputs: SetupInputs,
    invocation: SetupInvocation,
    *,
    operation: str,
    buffered: bool = False,
    held: NativeMutationGuard | None = None,
) -> NativeSetupState:
    """Run desired attachments, then retire removed attachments in prior order.

    A fresh owner's caller buffers and commits this result with its owner row.
    It must already have refused native residue and own failed-create rollback.
    Existing owners checkpoint each confirmed mutation before another can run.
    """
    expected = {"vm": VMSetupInvocation, "user": UserSetupInvocation, "workspace": WorkspaceSetupInvocation}
    if not isinstance(invocation, expected[inputs.facet]):
        raise StateError("native setup invocation does not match its facet")
    with native_mutation_guard(db.path, invocation.vm.name, held=held):
        state = read_native_setup(db, inputs.kind, inputs.name)
        prior = {record.integration: record for record in state.records if record.component == inputs.component}
        if not inputs.attachments and not prior:
            return state
        destination = destination_id(invocation)
        desired = {block.name: block for block in inputs.attachments}
        # Validate and bind the complete active list before its first mutation.
        bound = {}
        for selected in inputs.attachments:
            ensure_harness_integration_enabled(registry, selected.name)
            implementation = harness_integration_for(selected.name)
            bound[selected.name] = implementation.for_setup(
                owner_kind=inputs.kind, owner_name=inputs.name, facet=inputs.facet, config=selected.config
            )

        def persist(value: NativeSetupState) -> None:
            if not buffered:
                write_native_setup(db, inputs.kind, inputs.name, value, operation=operation)

        for name in (*desired, *(name for name in prior if name not in desired)):
            previous = prior.get(name)
            block = desired.get(name)
            if previous is not None and previous.destination_id != destination:
                raise StateError("native setup evidence belongs to a different destination; ownership was retained")
            if block is None and previous is not None and not previous.claims:
                state = NativeSetupState(records=tuple(record for record in state.records if record is not previous))
                persist(state)
                continue
            if block is None:
                assert previous is not None
                try:
                    ensure_harness_integration_enabled(registry, name)
                    integration = harness_integration_for(name).for_setup(
                        owner_kind=inputs.kind, owner_name=inputs.name, facet=inputs.facet, config=None
                    )
                except (ConfigError, StateError):
                    pending = previous.model_copy(update={"complete": False, "pending_cleanup": True})
                    state = replace_setup_record(state, pending)
                    persist(state)
                    output.warn(f"Native cleanup for {name} remains pending; enable its integration and retry setup.")
                    continue
                declaration = previous.declaration
            else:
                integration = bound[name]
                declaration = inputs.declaration(block)

            current = SetupRecord(
                component=inputs.component,
                integration=name,
                destination_id=destination,
                declaration=declaration,
                claims=() if previous is None else previous.claims,
            )
            state = replace_setup_record(state, current)
            persist(state)

            def checkpoint(claims: tuple[NativeClaim, ...]) -> None:
                nonlocal current, state
                # Plugin output crosses the registered capability boundary.
                current = SetupRecord.model_validate({**current.model_dump(), "claims": claims})
                state = replace_setup_record(state, current)
                persist(state)

            call = replace(invocation, prior=previous, checkpoint=checkpoint)
            if isinstance(call, VMSetupInvocation):
                integration.vm_init(call)
            elif isinstance(call, UserSetupInvocation):
                integration.user_init(call)
            elif isinstance(call, WorkspaceSetupInvocation):
                integration.workspace_init(call)
            if block is None:
                if current.claims:
                    current = current.model_copy(update={"pending_cleanup": True})
                    state = replace_setup_record(state, current)
                    output.warn(f"Native cleanup for {name} retains outstanding ownership; retry its owning setup.")
                else:
                    state = NativeSetupState(
                        records=tuple(
                            record
                            for record in state.records
                            if (record.component, record.integration) != (inputs.component, name)
                        )
                    )
            else:
                current = current.model_copy(update={"complete": True})
                state = replace_setup_record(state, current)
            persist(state)
        return state

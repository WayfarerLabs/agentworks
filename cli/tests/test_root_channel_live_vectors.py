"""The V lane of the root channel's vectors: a live VM's platform ``run_as_root``, run by the integration tester.

Set ``AGW_CONFORMANCE_VM`` to the name of a VM in the operator's own state and
run ``uv run pytest tests/test_root_channel_live_vectors.py -m integration``
from each workstation. The lane opens the same gated platform boundary
``vm shell --platform`` does (it starts a stopped VM and resolves the site's
secrets, prompting if it must), then hands each vector
``platform.run_as_root`` bound to the VM. It creates and removes only
``/tmp/agw-conformance-*`` on the guest. It lives outside
``tests/execution/`` because the platform boundary imports legacy modules.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

import agentworks.db as db_module
from agentworks.execution import trust
from tests.execution.conformance.lanes import live_vm_name
from tests.execution.conformance.root_vectors import RootLane, RootVector, root_vectors_for

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("live-root-vectors")]

# The suite isolates the database and the host-key store for every test; this lane
# needs the operator's real ones, captured before any fixture replaces them.
REAL_DB_PATH = db_module.DB_PATH
REAL_STORE_DIR = trust.STORE_DIR


@pytest.fixture
def live_root_lane(monkeypatch: pytest.MonkeyPatch) -> Iterator[RootLane]:
    name = live_vm_name()
    if name is None:
        pytest.skip("set AGW_CONFORMANCE_VM to run the live lane")
    monkeypatch.setattr(db_module, "DB_PATH", REAL_DB_PATH)
    monkeypatch.setattr(trust, "STORE_DIR", REAL_STORE_DIR)
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.manager import gated_vm_platform_recovery_boundary

    config = load_config()
    database = db_module.Database()
    try:
        vm = database.get_vm(name)
        if vm is None:
            pytest.fail(f"AGW_CONFORMANCE_VM names '{name}', which is not a VM in the operator's state")
        registry = load_request_registry(config, live_database=database)
        with gated_vm_platform_recovery_boundary(
            database, config, registry, vm, interaction=TtyInteractionPolicy.ALLOW
        ) as (vm_node, _resolver, ops_ctx):
            platform = vm_node.site.platform
            yield RootLane("V", partial(platform.run_as_root, vm, ops_ctx), platform=platform.name)
    finally:
        database.close()


@pytest.mark.parametrize("vector", root_vectors_for("V"), ids=lambda vector: vector.name)
def test_root_live_lane(vector: RootVector, live_root_lane: RootLane) -> None:
    vector.check(live_root_lane)

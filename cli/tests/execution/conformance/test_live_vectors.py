"""The V lane: the conformance vectors against a live VM, run by the integration tester.

Set ``AGW_CONFORMANCE_VM`` to the name of a VM in the operator's own state and
run ``uv run pytest tests/execution/conformance/test_live_vectors.py -m integration``
from each workstation. The lane reaches the VM as the operator's configured
admin, over the operator's real host-key store, and creates and removes only
files and directories under ``/tmp`` named ``agw-conformance-*`` and
``agw-handover-*``. Deadlines on every request bound a hung guest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import agentworks.db as db_module
from agentworks.execution import trust
from tests.execution.conformance.lanes import LiveLane, live_vm_name
from tests.execution.conformance.vectors import Vector, vectors_for

if TYPE_CHECKING:
    from collections.abc import Iterator

# One worker: the guest's sshd drops unauthenticated connections past Debian's MaxStartups 10:30:100.
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("live-vectors")]

# The suite isolates the database and the host-key store for every test; this lane
# needs the operator's real ones, captured before any fixture replaces them.
REAL_DB_PATH = db_module.DB_PATH
REAL_STORE_DIR = trust.STORE_DIR


@pytest.fixture
def live_lane(monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveLane]:
    name = live_vm_name()
    if name is None:
        pytest.skip("set AGW_CONFORMANCE_VM to run the live lane")
    monkeypatch.setattr(db_module, "DB_PATH", REAL_DB_PATH)
    monkeypatch.setattr(trust, "STORE_DIR", REAL_STORE_DIR)
    from agentworks.config import load_config

    database = db_module.Database()
    try:
        vm = database.get_vm(name)
        if vm is None:
            pytest.fail(f"AGW_CONFORMANCE_VM names '{name}', which is not a VM in the operator's state")
        lane = LiveLane(monkeypatch, vm, load_config())
        yield lane
        lane.cleanup()
    finally:
        database.close()


@pytest.mark.parametrize("vector", vectors_for("V"), ids=lambda vector: vector.name)
def test_live_lane(vector: Vector, live_lane: LiveLane) -> None:
    vector.check(live_lane)

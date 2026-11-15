"""Run the conformance vectors on the lanes that need no live VM: L and S.

The V lane is ``test_live_vectors.py``.
"""

from __future__ import annotations

import pytest

from tests.execution.conformance.lanes import Lane, require_linux_guest
from tests.execution.conformance.vectors import Vector, vectors_for


@pytest.mark.parametrize("vector", vectors_for("L"), ids=lambda vector: vector.name)
def test_local_lane(vector: Vector, local_lane: Lane) -> None:
    vector.check(local_lane)


@pytest.mark.parametrize("vector", vectors_for("S"), ids=lambda vector: vector.name)
def test_sshd_lane(vector: Vector, sshd_lane: Lane) -> None:
    # sshd starts agw-launch on this machine; the lane's forward tests need no launcher.
    require_linux_guest()
    vector.check(sshd_lane)

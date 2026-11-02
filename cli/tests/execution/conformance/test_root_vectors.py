"""Run the root channel's vectors on the local lane: ``/bin/bash -s``, and under ``sudo -n`` where granted.

The V lane is ``tests/test_root_channel_live_vectors.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import partial

import pytest

from agentworks.execution import root_script
from tests.execution.conformance.root_vectors import RootLane, RootVector, root_vectors_for

pytestmark = pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="the root channel runs /bin/bash")


def _sudo_granted() -> bool:
    sudo = shutil.which("sudo")
    if sudo is None:
        return False
    return subprocess.run([sudo, "-n", "true"], capture_output=True, check=False).returncode == 0


@pytest.fixture(params=["L", "L-sudo"])
def root_lane(request: pytest.FixtureRequest) -> RootLane:
    if request.param == "L-sudo":
        if not _sudo_granted():
            pytest.skip("the runner does not grant sudo -n")
        argv = ["sudo", "-n", "/bin/bash", "-s"]
    else:
        argv = ["/bin/bash", "-s"]
    return RootLane(request.param, partial(root_script.run_process, argv))


@pytest.mark.parametrize("vector", root_vectors_for("L"), ids=lambda vector: vector.name)
def test_root_local_lane(vector: RootVector, root_lane: RootLane) -> None:
    vector.check(root_lane)

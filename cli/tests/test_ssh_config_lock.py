"""The SSH config rebuild lock: a rebuild waits for another, then reads the newer state; a held lock times out."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks import ssh_config_lock
from agentworks.errors import StateError
from agentworks.ssh_config import _MANAGED_CONF, sync_ssh_config
from tests.test_ssh_config import _mock_config, _mock_vm

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows


def test_a_second_rebuild_waits_for_the_first_then_reads_the_newer_state(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    vms = [_mock_vm("early", "100.64.0.1")]
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.side_effect = lambda: list(vms)
    db.list_agents.return_value = []
    held = threading.Event()

    def first_rebuild_commits_late() -> None:
        with ssh_config_lock.ssh_config_rebuild_lock():
            held.set()
            # The second rebuild is waiting by now; the state it must read lands only after.
            time.sleep(0.3)
            vms.append(_mock_vm("late", "100.64.0.2"))

    first = threading.Thread(target=first_rebuild_commits_late)
    first.start()
    assert held.wait(5)
    sync_ssh_config(config, db, announce=False)
    first.join()

    assert "late" in (ssh_dir / "config.d" / _MANAGED_CONF).read_text()


def test_a_lock_held_past_the_bound_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_config_lock, "WAIT_SECONDS", 0.1)
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()

    with ssh_config_lock.ssh_config_rebuild_lock(), pytest.raises(StateError) as raised:
        sync_ssh_config(config, db)

    assert raised.value.hint is not None
    db.list_vms.assert_not_called()
    assert not (ssh_dir / "config.d" / _MANAGED_CONF).exists()

"""The per-OS-user lock that makes each SSH config rebuild one critical section.

A rebuild reads the database and then writes the operator's SSH config, so
two rebuilds that interleave could leave the file built from the older
reads. :func:`ssh_config_rebuild_lock` serializes them for one OS user,
whatever database each uses, since the SSH config is that user's file. It
waits for the lock, unlike the native mutation guard, and names no holder:
it guards a workstation file, not native state, and nothing taken under it
acquires a native lock.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agentworks.config.validation import CONFIG_DIR
from agentworks.deadline import Deadline
from agentworks.errors import StateError
from agentworks.harness_setup.locking import try_lock_exclusive, unlock

if TYPE_CHECKING:
    from collections.abc import Iterator

LOCK_PATH = CONFIG_DIR / ".locks" / "ssh-config"
"""The lock file. It is kept after release, so every rebuild locks the same file."""

WAIT_SECONDS = 10.0
"""Seconds a rebuild waits for another to finish before it gives up."""

POLL_SECONDS = 0.05
"""Seconds between attempts while a rebuild waits."""


@contextmanager
def ssh_config_rebuild_lock() -> Iterator[None]:
    """Hold the SSH config rebuild lock, waiting up to :data:`WAIT_SECONDS` for it.

    Raises ``StateError`` when another rebuild still holds it at the bound;
    the hint is the command that rebuilds the file once nothing else does.
    """
    LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = Deadline.after(WAIT_SECONDS)
    with LOCK_PATH.open("a+b") as lock_file:
        while not try_lock_exclusive(lock_file):
            if deadline.expired:
                raise StateError(
                    f"another SSH config rebuild did not finish within {WAIT_SECONDS:g} seconds",
                    hint="Run 'agw config sync-ssh-config' once other agw commands finish.",
                )
            time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            unlock(lock_file)

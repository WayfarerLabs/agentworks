"""Read-only runtime observation for named consoles."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from agentworks.errors import AgentworksError, NotFoundError, UserAbort
from agentworks.sessions.tmux import ProbeStatus

from ._helpers import tmux_session_name, tmux_staging_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.db import ConsoleRow, Database
    from agentworks.transports import Transport

_OBSERVATION_TIMEOUT_SECONDS = 10
_OBSERVATION_ATTEMPTS = 1


class ConsoleStatus(Enum):
    """Live state of a console's canonical and staging tmux sessions."""

    RUNNING = "running"
    STOPPED = "stopped"
    RESIDUAL = "residual"
    UNKNOWN = "unknown"


def classify_console_status(
    *,
    canonical_present: bool,
    staging_present: bool,
) -> ConsoleStatus:
    """Project conclusive canonical and staging membership facts."""
    if staging_present:
        return ConsoleStatus.RESIDUAL
    if canonical_present:
        return ConsoleStatus.RUNNING
    return ConsoleStatus.STOPPED


def _enumerate_tmux_sessions(target: Transport) -> set[str] | None:
    """Return an authoritative default-server session-name snapshot."""
    from agentworks.sessions.tmux import _normalized_probe_streams, _tmux_presence_from_result

    result = target.run(
        "tmux list-sessions -F '#{session_name}'",
        check=False,
        tty=False,
        timeout=_OBSERVATION_TIMEOUT_SECONDS,
        retries=_OBSERVATION_ATTEMPTS,
    )
    stdout, stderr = _normalized_probe_streams(result)
    if result.returncode == 0:
        if stderr:
            return None
        names = stdout.splitlines() if stdout else []
        if any(not name or "\x00" in name or "\r" in name for name in names):
            return None
        return set(names)
    presence = _tmux_presence_from_result(result, missing_target_is_absent=False)
    return set() if presence is ProbeStatus.ABSENT else None


def observe_console_statuses(
    db: Database,
    config: Config,
    consoles: Sequence[ConsoleRow],
) -> dict[str, ConsoleStatus]:
    """Observe every selected console with one bounded guest call per VM."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context

    from agentworks.transports import transport
    from agentworks.vms.manager import require_vm_ssh_boundary

    statuses = {console.name: ConsoleStatus.UNKNOWN for console in consoles}
    by_vm: dict[str, list[ConsoleRow]] = {}
    targets: dict[str, Transport] = {}
    for console in consoles:
        by_vm.setdefault(console.vm_name, []).append(console)

    for vm_name in tuple(by_vm):
        vm = db.get_vm(vm_name)
        if vm is None:
            raise NotFoundError(
                f"VM '{vm_name}' not found",
                entity_kind="vm",
                entity_name=vm_name,
            )
        try:
            require_vm_ssh_boundary(db, config, vm)
            targets[vm_name] = transport(vm, config, default_timeout=_OBSERVATION_TIMEOUT_SECONDS)
        except UserAbort:
            raise
        except AgentworksError:
            continue

    def observe_vm(vm_name: str) -> set[str] | None:
        return _enumerate_tmux_sessions(targets[vm_name])

    if not targets:
        return statuses
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        futures = {executor.submit(copy_context().run, observe_vm, vm_name): vm_name for vm_name in targets}
        for future in as_completed(futures):
            vm_name = futures[future]
            try:
                tmux_names = future.result()
            except UserAbort:
                raise
            except AgentworksError:
                continue
            if tmux_names is None:
                continue
            for console in by_vm[vm_name]:
                statuses[console.name] = classify_console_status(
                    canonical_present=tmux_session_name(console.name) in tmux_names,
                    staging_present=tmux_staging_name(console.name) in tmux_names,
                )
    return statuses


def observe_console_status(
    db: Database,
    config: Config,
    console: ConsoleRow,
) -> ConsoleStatus:
    """Observe one console through the same bounded batch authority."""
    return observe_console_statuses(db, config, [console])[console.name]

"""Typed failures of the independent execution package.

Each subclasses the ``agentworks.errors`` kind the failure belongs to. No
message, ``repr`` or hint holds a stream, an environment value or script text:
the only transcript an error may show is the last lines of ssh's own
diagnostics, which the client keeps apart from the payload's output.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.ssh_identity import identity_is_passphrase_protected

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.results import NotDeliveredReason, Result


class TrustMissing(StateError):
    """A VM has no host-key record in its store, so nothing can be verified.

    Raised instead of falling back to first use: a missing record must never
    make an existing VM look new. It is a state error, not a connectivity one,
    so handlers that treat an unreachable peer as best-effort do not absorb it.
    Takes only the VM name because the message and the remediation are fixed by
    the type.
    """

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name
        super().__init__(
            f"VM '{vm_name}' has no recorded host key",
            entity_kind="vm",
            entity_name=vm_name,
            hint=(
                f"Run 'agw vm import-host-keys {vm_name}' (see its --help for --known-hosts), "
                "or delete and recreate a VM whose create never enrolled."
            ),
        )


class ClientUnsupported(ConfigError):
    """No acceptable OpenSSH client, or the resolved one failed to start.

    The message lists each path tried with its reason and says how to install a
    supported client, so the caller supplies the whole text.
    """


def _with_tail(message: str, client_log_tail: str) -> str:
    return f"{message}\n{client_log_tail}" if client_log_tail else message


class VMNotReady(StateError):
    """The VM cannot run agw-launch: no python3, or one without its standard library.

    PR A's hint names ``agw vm reinit``, whose release probe and Phase A still
    run on the legacy stack and so reach a VM without python3.
    """

    # One constant so the slice that adds `agw vm recover` changes one place.
    HINT_TEMPLATE = "Run 'agw vm reinit {vm}' to install python3."

    def __init__(self, vm_name: str, reason: NotDeliveredReason) -> None:
        # results.py imports this module for the errors it raises.
        from agentworks.execution.results import NotDeliveredReason

        self.vm_name = vm_name
        what = (
            "its python3 lacks the standard library"
            if reason is NotDeliveredReason.STDLIB_MISSING
            else "python3 is not installed on it"
        )
        super().__init__(
            f"VM '{vm_name}' cannot run Agentworks requests: {what}",
            entity_kind="vm",
            entity_name=vm_name,
            hint=self.HINT_TEMPLATE.format(vm=vm_name),
        )


class TrustMismatch(StateError):
    """The VM presented a host key that is not the recorded one.

    Typed from ssh's own diagnostics, never from the guest's output. The hint
    names the manual path, because import never rewrites an existing record.
    """

    # One constant so the slice that adds `agw vm recover` changes one place.
    HINT_TEMPLATE = (
        "If the VM was rebuilt or rekeyed, verify its new host key through the platform console or "
        "provider, remove {store} and run 'agw vm import-host-keys {vm} --known-hosts FILE' with a "
        "verified file. If you did not expect a change, do not connect."
    )

    def __init__(self, vm_name: str, client_log_tail: str = "") -> None:
        from agentworks.execution.trust import store_file
        from agentworks.path_rendering import format_host_path

        self.vm_name = vm_name
        super().__init__(
            _with_tail(f"host key verification failed for VM '{vm_name}'", client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint=self.HINT_TEMPLATE.format(store=f"'{format_host_path(store_file(vm_name))}'", vm=vm_name),
        )


class RequestRefused(StateError):
    """The guest could not start the payload: a command, interpreter or directory it cannot use.

    Raised for a ``refused`` trailer under ``run`` and for a refused terminal
    handover. ``code`` is the guest's refusal, already checked against the
    refusal grammar by the classifier.
    """

    def __init__(self, vm_name: str, code: str, result: Result) -> None:
        self.vm_name = vm_name
        self.code = code
        self.result = result
        super().__init__(
            _with_tail(f"VM '{vm_name}' refused the request: {describe_refusal(code)}", result.client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint="Fix the working directory or the command.",
        )


def describe_refusal(code: str) -> str:
    """A short phrase for a refusal code, for the one line an operator sees."""
    import errno
    import os

    if code == "cwd":
        return "cannot enter the working directory"
    if code == "handover":
        return "the terminal handover could not be set up"
    name = code.removeprefix("exec ")
    number = getattr(errno, name, None)
    if code.startswith("exec ") and isinstance(number, int):
        return f"cannot execute the command: {os.strerror(number)} ({name})"
    if code.startswith("exec "):
        return f"cannot execute the command ({name})"
    return "the request could not start"


class ExecutionIncomplete(ConnectivityError):
    """A request did not complete: it never started, or its outcome is unknown.

    Carries the whole ``Result`` and names which case it is, so that fact is
    held once. A ``NotDelivered`` command did not start; an ``Unknown`` one may
    have run. ``attaching`` is for the parking connection of a terminal attach,
    where an unknown outcome still means no shell started: the message says so
    instead of that the command may have run.

    ``identity_file`` is the operator's SSH identity. ssh runs in batch mode and
    cannot ask for a passphrase, so when a connection failed and that file is a
    passphrase-protected key, the hint also says to load it into the SSH agent.
    The file is read only here, on the failure path.
    """

    PASSPHRASE_HINT = (
        "The SSH identity {key} is passphrase-protected, and Agentworks runs ssh in batch mode, "
        "so it cannot prompt for the passphrase. Load the key into your SSH agent with 'ssh-add {key}'."
    )

    def __init__(
        self, vm_name: str, result: Result, *, attaching: bool = False, identity_file: Path | None = None
    ) -> None:
        from agentworks.execution.results import NotDelivered, UnknownReason
        from agentworks.path_rendering import format_host_path

        self.vm_name = vm_name
        self.result = result
        completion = result.completion
        if attaching:
            reason = getattr(completion, "reason", None)
            why = getattr(reason, "value", reason)
            summary = f"no shell started on VM '{vm_name}': the connection did not become ready ({why})"
            hint = "Nothing was started on the VM, so connecting again is safe."
            if reason is UnknownReason.DEADLINE:
                hint += (
                    " A startup file of the login account that reads standard input"
                    " (a bare 'cat' in ~/.bashrc or ~/.zshenv, say) is the usual cause."
                )
        elif isinstance(completion, NotDelivered):
            summary = f"the command did not start on VM '{vm_name}' ({completion.reason.value})"
            hint = "The command did not start, so running it again is safe."
        else:
            reason = getattr(completion, "reason", None)
            summary = f"the outcome of the command on VM '{vm_name}' is unknown ({getattr(reason, 'value', reason)})"
            hint = "The command may have run; check its effects before running it again."
        if (
            identity_file is not None
            and getattr(completion, "reason", None) is UnknownReason.CONNECTION
            and identity_is_passphrase_protected(identity_file)
        ):
            hint += " " + self.PASSPHRASE_HINT.format(key=f"'{format_host_path(identity_file)}'")
        super().__init__(
            _with_tail(summary, result.client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
        )


class CommandFailed(ExternalError):
    """``check=True`` and the command exited non-zero. Names the status, never stream text."""

    def __init__(self, vm_name: str, status: int, result: Result) -> None:
        self.vm_name = vm_name
        self.status = status
        self.result = result
        super().__init__(
            f"command on VM '{vm_name}' failed with exit status {status}",
            entity_kind="vm",
            entity_name=vm_name,
        )

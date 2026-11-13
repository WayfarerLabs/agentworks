"""Typed failures of the independent execution package.

Each subclasses the ``agentworks.errors`` kind the failure belongs to. No
message, ``repr`` or hint holds a stream, an environment value or script text:
the only transcript an error may show is the last lines of ssh's own
diagnostics, which the client keeps apart from the payload's output.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import AuthorizationError, ConfigError, ConnectivityError, ExternalError, StateError

if TYPE_CHECKING:
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
                f"Run 'agw vm import-host-keys {vm_name}' (see its --help for --known-hosts), or "
                f"'agw vm recover {vm_name}' to read the keys through the platform and confirm them; "
                "delete and recreate a VM whose create never enrolled."
            ),
        )


class FeatureUnavailable(StateError):
    """A VM's platform does not offer what the command needs, such as a native shell.

    Raised before the operation boundary resolves anything. ``hint`` is the
    platform's own guidance, when it gives any.
    """

    def __init__(self, vm_name: str, platform_name: str, feature: str, hint: str | None) -> None:
        super().__init__(
            f"VM platform '{platform_name}' does not offer {feature}",
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
        )


class PermissionDenied(AuthorizationError):
    """An agent-bound access was asked for an elevated request, which an agent never gets.

    Raised before anything is resolved or spawned. Names the VM and the
    account, never the request. The guest's ``sudo -n`` is the second layer,
    so an agent placed in a sudo group by hand still gains nothing through
    Agentworks.
    """

    def __init__(self, vm_name: str, account: str) -> None:
        self.vm_name = vm_name
        self.account = account
        super().__init__(
            f"an elevated request was refused for agent account '{account}' on VM '{vm_name}': "
            "agents never run as root through Agentworks",
            entity_kind="vm",
            entity_name=vm_name,
        )


class ClientUnsupported(ConfigError):
    """No acceptable OpenSSH client, the resolved one failed to start, or a program it ships with is missing.

    The message names what is missing and how to install a supported client,
    so the caller supplies the whole text.
    """


def _with_tail(message: str, client_log_tail: str) -> str:
    return f"{message}\n{client_log_tail}" if client_log_tail else message


class VMNotReady(StateError):
    """The VM cannot run agw-launch: no python3, or one without its standard library.

    The hint names ``agw vm recover``, whose repair script installs python3
    through the platform's own channel, which needs no python3 on the VM.
    """

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
            hint=f"Run 'agw vm recover {vm_name}' to install python3.",
        )


class TrustMismatch(StateError):
    """The VM presented a host key that is not the recorded one.

    Typed from ssh's own diagnostics, never from the guest's output. The hint
    names the manual path, because import never rewrites an existing record.
    """

    def __init__(self, vm_name: str, client_log_tail: str = "") -> None:
        from agentworks.execution.trust import store_file
        from agentworks.path_rendering import format_host_path

        self.vm_name = vm_name
        store = f"'{format_host_path(store_file(vm_name))}'"
        super().__init__(
            _with_tail(f"host key verification failed for VM '{vm_name}'", client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint=(
                "If the VM was rebuilt or rekeyed, verify its new host key through the platform console or "
                f"provider, remove {store} and run 'agw vm import-host-keys {vm_name} --known-hosts FILE' with a "
                "verified file. If you did not expect a change, do not connect."
            ),
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
        hint = (
            "No shell started, so running the shell command again is safe."
            if code == "handover"
            else "Fix the working directory or the command."
        )
        super().__init__(
            _with_tail(f"VM '{vm_name}' refused the request: {describe_refusal(code)}", result.client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
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
    have run, except ``AUTH_FAILED``: the VM refused the key, so nothing ran.
    ``attaching`` is for the parking connection of a terminal attach,
    where an unknown outcome still means no shell started: the message says so
    instead of that the command may have run.
    """

    def __init__(self, vm_name: str, result: Result, *, attaching: bool = False) -> None:
        from agentworks.execution.results import NotDelivered, UnknownReason

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
        elif getattr(completion, "reason", None) is UnknownReason.AUTH_FAILED:
            summary = f"VM '{vm_name}' did not accept the configured SSH key, so nothing ran"
            hint = (
                "Check that operator.ssh_private_key is the key authorized for this account on the VM. "
                f"If the admin account refused it, 'agw vm recover {vm_name}' restores the admin's key through "
                "the platform's own channel; on a cloud that channel uses the same key and sshd, so use the "
                "provider's console there."
            )
        else:
            reason = getattr(completion, "reason", None)
            summary = f"the outcome of the command on VM '{vm_name}' is unknown ({getattr(reason, 'value', reason)})"
            hint = "The command may have run; check its effects before running it again."
            if reason is UnknownReason.CONNECTION:
                # No start marker says nothing ran: one written after the payload started can be lost
                # with the connection, so only the repair is named.
                hint += (
                    f" If the VM does not answer, 'agw vm recover {vm_name}' repairs its SSH server and"
                    " Tailscale node through the platform's own channel; on a cloud that channel uses the"
                    " same key and sshd, so if they are what fails, use the provider's console."
                )
        super().__init__(
            _with_tail(summary, result.client_log_tail),
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint,
        )


class CommandFailed(ExternalError):
    """A command that had to succeed exited non-zero. Names the status, never stream text.

    ``streams_logged`` is the failed request's ``log_streams``: a caller that
    shows any of ``result``'s streams (an initialization warning's stderr
    tail) shows them only when it is true, so a request that kept its streams
    out of the log keeps them out of a warning too.
    """

    def __init__(self, vm_name: str, status: int, result: Result, *, streams_logged: bool) -> None:
        self.vm_name = vm_name
        self.status = status
        self.result = result
        self.streams_logged = streams_logged
        super().__init__(
            f"command on VM '{vm_name}' failed with exit status {status}",
            entity_kind="vm",
            entity_name=vm_name,
        )


class FileContentMismatch(ExternalError):
    """``write_if``'s content reached the VM with a size or SHA-256 other than what was sent. Nothing changed."""

    def __init__(self, vm_name: str, path: str) -> None:
        self.vm_name = vm_name
        self.path = path
        super().__init__(
            f"the content written to '{path}' on VM '{vm_name}' did not arrive as sent, so nothing was written",
            entity_kind="vm",
            entity_name=vm_name,
            hint="Retry the operation; if it recurs, check the connection to the VM.",
        )


class FileConflict(StateError):
    """A conditional file operation found the destination changed since its caller observed it.

    Nothing was written or removed. Names the VM, the operation and the path,
    never content. A caller that knows what rerunning does passes its own ``hint``.
    """

    def __init__(self, vm_name: str, operation: str, path: str, *, hint: str | None = None) -> None:
        self.vm_name = vm_name
        self.operation = operation
        self.path = path
        super().__init__(
            f"'{path}' on VM '{vm_name}' changed since it was read, so {operation} left it as it is",
            entity_kind="vm",
            entity_name=vm_name,
            hint=hint or "Inspect the file, then retry the operation against its current content.",
        )


class PathRefusal(StrEnum):
    """Why the file program would not use a path."""

    UNSUITABLE = "unsuitable"
    """A symbolic link, or the wrong type of file, at the path or one of its parents."""
    DENIED = "denied"
    """The account may not use the path: ``EACCES``, ``EPERM`` or ``EROFS``."""


class PathRefused(StateError):
    """The file program refused a path: a link or wrong type at it or a parent, or a permission denial."""

    def __init__(self, vm_name: str, operation: str, path: str, reason: PathRefusal) -> None:
        self.vm_name = vm_name
        self.operation = operation
        self.path = path
        self.reason = reason
        if reason is PathRefusal.UNSUITABLE:
            message = (
                f"{operation} refused '{path}' on VM '{vm_name}': a link or another file type is at it or a parent"
            )
            hint = (
                "Agentworks never reads or writes through a link. Inspect the path and whatever created it "
                "(a dotfile installer is the usual cause), then put a regular file or directory there."
            )
        else:
            message = f"{operation} was denied access to '{path}' on VM '{vm_name}'"
            hint = "Check the ownership and mode of the path and each of its parent directories, then retry."
        super().__init__(message, entity_kind="vm", entity_name=vm_name, hint=hint)


class FileLockTimeout(StateError):
    """A mutating file operation could not take the VM's file lock within its wait. Nothing changed."""

    def __init__(self, vm_name: str, operation: str, path: str) -> None:
        self.vm_name = vm_name
        self.operation = operation
        self.path = path
        super().__init__(
            f"{operation} of '{path}' on VM '{vm_name}' timed out waiting for the VM's file lock",
            entity_kind="vm",
            entity_name=vm_name,
            hint="Another Agentworks operation is writing files on this VM; retry once it has finished.",
        )


class DetachedRefused(StateError):
    """The detached program refused a launch: its id already exists at ``launch``, or it is running at ``discard``.

    Nothing was changed on the VM.
    """

    def __init__(self, vm_name: str, launch_id: str, *, running: bool) -> None:
        self.vm_name = vm_name
        self.launch_id = launch_id
        self.running = running
        if running:
            message = f"detached launch {launch_id} on VM '{vm_name}' is still running, so it was not discarded"
            hint = "Wait for it to end, or stop it, then run the command again."
        else:
            message = f"detached launch {launch_id} already exists on VM '{vm_name}', so nothing was started"
            hint = "Run the command again: it starts its work under a new id."
        super().__init__(message, entity_kind="vm", entity_name=vm_name, hint=hint)

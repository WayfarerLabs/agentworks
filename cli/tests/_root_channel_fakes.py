"""A fake guest behind a platform's root channel, for tests that drive provisioning over ``run_as_root``.

:class:`FakeGuest` reads each script the way the real wrapper builds it
(``root_script.wrap``), answers by its payload with the markers the real
script would print, and records every script, deadline and argv. Install it
at the lowest seam a platform uses: as ``run_as_root`` itself, at
``root_script.run_process`` (Lima, WSL2 and the clouds) or behind the QGA
client (Proxmox).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.execution import root_script
from agentworks.execution.client import SSHClient
from agentworks.execution.root_script import RawRun

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pytest

ADDRESS = "100.64.0.9"


@dataclass(frozen=True)
class Answer:
    """How the guest answers one payload: a status (``None`` for no markers at all), stdout, or a raise."""

    status: int | None = 0
    stdout: bytes = b""
    raises: BaseException | None = None


def nonce_of(script: bytes) -> str:
    match = re.search(rb"\\036([0-9a-f]{32}) start", script)
    assert match is not None, "a root script names its nonce in its start marker"
    return match[1].decode()


def payload_of(script: bytes) -> str:
    """The payload between the wrapper's subshell parentheses."""
    text = script.decode("utf-8")
    return text[text.index("\n(\n") + 3 : text.rindex("\n)\ns=$?")]


@dataclass
class FakeGuest:
    """Answers by payload; ``respond`` sees each payload and the count of earlier ones like it.

    The default answers every payload with status 0 and the address for
    ``tailscale ip -4``.
    """

    respond: Callable[[str, int], Answer] | None = None
    events: list[str] | None = None
    """When given, each payload is appended as ``root:<payload>``, to order it among other events."""
    scripts: list[bytes] = field(default_factory=list)
    argvs: list[list[str]] = field(default_factory=list)
    deadlines: list[float] = field(default_factory=list)

    @property
    def payloads(self) -> list[str]:
        return [payload_of(script) for script in self.scripts]

    def count(self, payload: str) -> int:
        return self.payloads.count(payload)

    @property
    def joins(self) -> int:
        return self.count(JOIN_FRAGMENT)

    def run_as_root(self, script: bytes, deadline: float) -> RawRun:
        payload = payload_of(script)
        seen = self.payloads.count(payload)
        self.scripts.append(script)
        self.deadlines.append(deadline)
        if self.events is not None:
            self.events.append(f"root:{payload}")
        if self.respond is not None:
            answer = self.respond(payload, seen)
        elif payload == "tailscale ip -4":
            answer = Answer(stdout=f"{ADDRESS}\n".encode())
        else:
            answer = Answer()
        if answer.raises is not None:
            raise answer.raises
        if answer.status is None:
            return RawRun(b"", b"")
        prefix = b"\x1e" + nonce_of(script).encode() + b" "
        return RawRun(
            prefix + b"start\n" + answer.stdout + prefix + b"end\n",
            prefix + b"start\n" + prefix + b"exit %d\n" % answer.status,
        )

    def run_process(self, argv: Sequence[str], script: bytes, deadline: float) -> RawRun:
        """Stands in for ``root_script.run_process``; records the argv too."""
        self.argvs.append(list(argv))
        return self.run_as_root(script, deadline)


def answer_by_payload(
    *,
    failures: dict[str, list[BaseException]] | None = None,
    answers: dict[str, Answer] | None = None,
) -> Callable[[str, int], Answer]:
    """A ``respond`` that raises each queued failure for a payload first, then gives its fixed answer.

    A payload with neither completes with status 0, and ``tailscale ip -4``
    reports :data:`ADDRESS`.
    """
    queued = failures if failures is not None else {}
    fixed = {"tailscale ip -4": Answer(stdout=f"{ADDRESS}\n".encode()), **(answers or {})}

    def respond(payload: str, seen: int) -> Answer:
        if queued.get(payload):
            return Answer(raises=queued[payload].pop(0))
        return fixed.get(payload, Answer())

    return respond


def install_at_run_process(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> FakeGuest:
    """Answer every local root channel (``root_script.run_process``) from ``guest``, with an OpenSSH client resolved."""
    monkeypatch.setattr(execution, "resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))
    monkeypatch.setattr(root_script, "run_process", guest.run_process)
    return guest

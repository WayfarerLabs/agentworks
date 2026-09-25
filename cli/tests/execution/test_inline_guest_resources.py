"""Resource ownership checks for the private inline guest."""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from agentworks.execution import _inline_guest as guest
from agentworks.execution._evidence_wire import FrameKind
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_control import FailureCode, FailurePhase, parse_failure
from agentworks.execution._inline_request import InlineManifest, InvocationKind, OutputMode, ScriptShell
from agentworks.execution._process import ProcessResult, StreamResult

pytestmark = pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="source ownership requires Linux memfd")
NONCE = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def manifest() -> InlineManifest:
    return InlineManifest(
        nonce=NONCE,
        kind=InvocationKind.SCRIPT,
        argv=(),
        source=b"exit 0\n",
        shell=ScriptShell.SH,
        env=(),
        cwd=None,
        stdin=b"",
        output_mode=OutputMode.CAPTURE,
        capture_limit=4096,
        identity=IdentityExpectation(0, 0, (0,)),
    )


def _install_manifest(monkeypatch: pytest.MonkeyPatch, manifest: InlineManifest) -> None:
    monkeypatch.setattr(os, "read", lambda descriptor, limit: b"")
    monkeypatch.setattr(guest, "decode_manifest", lambda data: manifest)
    monkeypatch.setattr(guest, "matches_current_identity", lambda identity: True)
    monkeypatch.setattr(guest._Emitter, "_write", staticmethod(lambda data: None))


def _prepared_source(monkeypatch: pytest.MonkeyPatch) -> int:
    descriptor = os.memfd_create("agentworks-inline-resource-test")
    monkeypatch.setattr(guest, "_prepare_launch", lambda manifest: guest._Launch(["/bin/true"], descriptor))
    return descriptor


def _assert_closed(descriptor: int) -> None:
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_prepared_source_closes_when_launching_emission_fails(
    manifest: InlineManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_manifest(monkeypatch, manifest)
    descriptor = _prepared_source(monkeypatch)
    monkeypatch.setattr(guest._Emitter, "emit", lambda self, kind, body: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError):
        guest.main(NONCE)

    _assert_closed(descriptor)


def test_prepared_source_closes_after_dispatch_failure(
    manifest: InlineManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_manifest(monkeypatch, manifest)
    descriptor = _prepared_source(monkeypatch)
    emitted: list[tuple[FrameKind, bytes]] = []
    monkeypatch.setattr(guest._Emitter, "emit", lambda self, kind, body: emitted.append((kind, body)))
    monkeypatch.setattr(
        guest,
        "run_owned_process",
        lambda *args, **kwargs: ProcessResult(
            False,
            None,
            None,
            StreamResult(b"", False),
            StreamResult(b"", False),
            None,
        ),
    )

    assert guest.main(NONCE) == 0

    _assert_closed(descriptor)
    failures = [parse_failure(body) for kind, body in emitted if kind is FrameKind.FAILED]
    assert len(failures) == 1
    assert failures[0].phase is FailurePhase.LAUNCH
    assert failures[0].code is FailureCode.DISPATCH


def test_source_close_failure_is_reported_after_successful_wait(
    manifest: InlineManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_manifest(monkeypatch, manifest)
    descriptor = _prepared_source(monkeypatch)
    emitted: list[tuple[FrameKind, bytes]] = []
    monkeypatch.setattr(guest._Emitter, "emit", lambda self, kind, body: emitted.append((kind, body)))
    monkeypatch.setattr(
        guest,
        "run_owned_process",
        lambda *args, **kwargs: ProcessResult(
            True,
            0,
            0,
            StreamResult(b"", True),
            StreamResult(b"", True),
            None,
        ),
    )
    original_close: Callable[[int], None] = os.close

    def fail_source_close(selected: int) -> None:
        if selected == descriptor:
            raise OSError
        original_close(selected)

    monkeypatch.setattr(os, "close", fail_source_close)
    try:
        assert guest.main(NONCE) == 0
    finally:
        original_close(descriptor)

    failures = [parse_failure(body) for kind, body in emitted if kind is FrameKind.FAILED]
    assert len(failures) == 1
    assert failures[0].phase is FailurePhase.CLEANUP
    assert failures[0].code is FailureCode.RESOURCE

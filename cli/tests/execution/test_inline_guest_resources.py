"""Resource ownership checks for the private inline guest."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

import pytest

from agentworks.execution import _inline_guest as guest
from agentworks.execution._evidence_wire import FrameKind
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_bundle import _MODULE_NAMES, ROOT_PROGRAM
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


@pytest.mark.parametrize("loader", ["fixed", "root"])
@pytest.mark.parametrize("pending_constructor", [False, True])
def test_fixed_module_retains_one_owner_and_waits_for_script_construction(
    loader: str, pending_constructor: bool
) -> None:
    bootstrap = (
        build_helper_modules("_agw_inline", _MODULE_NAMES)
        if loader == "fixed"
        else ROOT_PROGRAM.loader_source + "_agw_load_identity(); _agw_load_remaining()\n"
    )
    probe = r"""
import gc, os, subprocess, sys, threading, weakref
g = sys.modules['_agw_inline._inline_guest']
c = sys.modules['_agw_inline._process']
request = sys.modules['_agw_inline._inline_request']
identity = sys.modules['_agw_inline._helper_identity']
owner = g._PROCESS_OWNER
assert owner.snapshot().terminal is None and not owner._start_called
manifest = request.InlineManifest(
    nonce='0'*32, kind=request.InvocationKind.SCRIPT, argv=(), source=b'exit 7\n',
    shell=request.ScriptShell.SH, env=(), cwd=None, stdin=b'',
    output_mode=request.OutputMode.CAPTURE, capture_limit=100,
    identity=identity.IdentityExpectation(0,0,(0,)))
g.decode_manifest = lambda data: manifest
g.matches_current_identity = lambda value: True
g._Emitter._write = staticmethod(lambda data: None)
emissions=[]
g._Emitter.emit = lambda self, kind, body: emissions.append(kind.value)
spawn = subprocess.Popen
entered, release, finished = threading.Event(), threading.Event(), threading.Event()
sources, closes, originals = [], [], []
entry = c._local_process_owner_entry
def complete_entry(target):
    try: entry(target)
    finally: finished.set()
c._local_process_owner_entry = complete_entry
def create(*args, **kwargs):
    sources.append(kwargs['pass_fds'][0])
    entered.set()
    if sys.argv[1]=='True': assert release.wait(3)
    child=spawn(*args, **kwargs)
    if sys.argv[1]=='False':
        original=child.stdout.close
        originals.append(original)
        def close():
            closes.append(1)
            raise OSError()
        child.stdout.close=close
    return child
subprocess.Popen=create
results=[]
body=threading.Thread(target=lambda:results.append(g.main('0'*32)))
body.start()
assert entered.wait(2)
source=sources[0]
if sys.argv[1]=='True':
    assert body.is_alive() and 'FINISHED' not in emissions
    os.fstat(source)
    assert owner.snapshot().terminal is None
release.set()
body.join(3)
assert not body.is_alive() and results==[0] and finished.wait(2)
try: os.fstat(source)
except OSError: pass
else: raise AssertionError('script source remains open after waiting construction')
assert emissions[-1]=='FINISHED'
terminal=owner.close()
assert terminal.local_status==terminal.exit_status==7
assert terminal.cleaned is (sys.argv[1]=='True')
if sys.argv[1]=='False':
    assert len(closes)==1 and not terminal.cleanup_retryable
    assert owner._retained_status is not None and owner._retained_status.pipes is not None
    assert owner._retained_status.pipes.stdout is owner._retained_process.stdout
    assert not owner._retained_status.pipes.stdout.closed
reference=weakref.ref(owner)
del owner, g, body
gc.collect()
held=sys.modules['_agw_inline._inline_guest']._PROCESS_OWNER
assert reference() is held
assert held.close() is terminal
try:
    held.start(c.LocalProcessRequest(('/bin/true',),c.LocalProcessInput.EOF))
except RuntimeError: pass
else: raise AssertionError('module owner reused')
# Foreign fixture closes do not settle managed close uncertainty.
for original in originals: original()
assert held.close() is terminal and terminal.cleaned is (sys.argv[1]=='True')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", bootstrap + probe, str(pending_constructor)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == completed.stderr == b""

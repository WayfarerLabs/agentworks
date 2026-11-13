"""Exercise target-side Claude detection without Claude or operator state."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from agentworks.errors import StateError
from agentworks.plugins.claude.conversation_probe import ConversationProbe, probe_conversation
from agentworks.transports import Transport
from tests.conftest import _FakeResult, _FakeTarget

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="The launch target uses a POSIX login shell")

_SID = "939b1597-7c61-5ace-80f4-14617b7b4257"
_NEXT = "4d7ca145-7abd-4ed9-9f53-0dff15487e57"
_PARKED = "cf9fde6f-3ad3-4c32-aac9-29a92460f1f8"


class _LocalTarget:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []
        self.env = {**os.environ, "SHELL": "/bin/sh", "CLAUDE_CONFIG_DIR": str(root)}

    def run(self, command: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        environment = {**self.env, **cast(dict[str, str], kwargs.get("env") or {})}
        return subprocess.run(command, shell=True, env=environment, capture_output=True, text=True, timeout=20)


def _probe(target: _LocalTarget, *, detect: bool = True, sid: str = _SID) -> ConversationProbe:
    return probe_conversation(cast(Transport, target), sid, detect=detect)


def _transcript(root: Path, records: list[dict[str, object]], sid: str = _SID) -> Path:
    path = root / "projects" / "arbitrary-slug" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _link(source: str = _SID, destination: str = _NEXT) -> dict[str, object]:
    return {"type": "continued-in", "sessionId": source, "continuedInSessionId": destination}


def _session(root: Path, filename: str, record: dict[str, object]) -> None:
    path = root / "sessions" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


@pytest.fixture
def target(tmp_path: Path) -> _LocalTarget:
    return _LocalTarget(tmp_path / "Claude's config $(not-a-command)")


def test_missing_original_is_clean_absence(target: _LocalTarget) -> None:
    assert _probe(target) == ConversationProbe(False)
    assert len(target.commands) == 1


def test_tail_pointer_reports_candidate_without_requiring_successor(target: _LocalTarget) -> None:
    _transcript(target.root, [_link()])
    assert _probe(target) == ConversationProbe(True, (_NEXT,))
    assert len(target.commands) == 1


def test_pointer_buried_before_large_history_is_found(target: _LocalTarget) -> None:
    path = _transcript(target.root, [_link()])
    with path.open("a") as stream:
        for _ in range(5000):
            stream.write(json.dumps({"type": "assistant", "message": "x" * 80}) + "\n")
    assert _probe(target).candidates == (_NEXT,)


def test_huge_malformed_line_does_not_hide_buried_pointer(target: _LocalTarget) -> None:
    path = _transcript(target.root, [_link()])
    with path.open("ab") as stream:
        stream.write(b"x" * (4 * 1024 * 1024) + b"\n")
    assert _probe(target).candidates == (_NEXT,)


def test_copied_pointer_for_other_session_is_ignored(target: _LocalTarget) -> None:
    _transcript(target.root, [_link(_NEXT, _PARKED)])
    assert _probe(target) == ConversationProbe(True)


def test_unrelated_transcript_is_not_inspected(target: _LocalTarget) -> None:
    _transcript(target.root, [_link()], sid=_PARKED)
    assert _probe(target) == ConversationProbe(False)


def test_malformed_records_preserve_transcript_existence(target: _LocalTarget) -> None:
    path = _transcript(target.root, [])
    path.write_bytes(b"not json\n\xff\n[]\n{}\n")
    assert _probe(target) == ConversationProbe(True)


def test_invalid_target_uuid_is_ignored(target: _LocalTarget) -> None:
    _transcript(target.root, [_link(destination="../../unrelated")])
    assert _probe(target) == ConversationProbe(True)


def test_parked_link_joins_exact_background_job(target: _LocalTarget) -> None:
    _transcript(target.root, [])
    _session(target.root, "101.json", {"sessionId": _SID, "parkedJobId": "12ab3456"})
    _session(target.root, "102.json", {"kind": "bg", "jobId": "12ab3456", "sessionId": _PARKED})
    assert _probe(target) == ConversationProbe(True, (_PARKED,), False, ("12ab3456",))


@pytest.mark.parametrize("buried", [False, True])
def test_parked_and_continuation_are_independent(target: _LocalTarget, buried: bool) -> None:
    transcript = _transcript(target.root, [_link()])
    if buried:
        with transcript.open("a") as stream:
            for _ in range(5000):
                stream.write(json.dumps({"type": "assistant", "message": "x" * 80}) + "\n")
    _session(target.root, "101.json", {"sessionId": _SID, "parkedJobId": "12ab3456"})
    _session(target.root, "102.json", {"kind": "bg", "jobId": "12ab3456", "sessionId": _PARKED})
    result = _probe(target)
    assert set(result.candidates) == {_NEXT, _PARKED}
    assert result.parked_job_ids == ("12ab3456",)


def test_unresolved_short_job_id_remains_separate_from_uuid_candidates(target: _LocalTarget) -> None:
    _session(target.root, "101.json", {"sessionId": _SID, "parkedJobId": "12ab3456"})
    assert _probe(target) == ConversationProbe(False, (), False, ("12ab3456",))


def test_unrelated_and_prewarmed_background_records_are_excluded(target: _LocalTarget) -> None:
    _session(target.root, "103.json", {"sessionId": _NEXT, "parkedJobId": "12ab3456"})
    _session(target.root, "102.json", {"kind": "bg", "jobId": "12ab3456", "sessionId": _PARKED})
    _session(target.root, "104.json", {"kind": "bg", "jobId": "aabbccdd", "sessionId": _NEXT})
    assert _probe(target) == ConversationProbe(False)


def test_force_can_verify_live_background_without_transcript(target: _LocalTarget) -> None:
    _session(target.root, f"{os.getpid()}.json", _live_record())
    assert _probe(target, detect=False) == ConversationProbe(False, (), True)


def test_force_ignores_links_and_parked_metadata(target: _LocalTarget) -> None:
    _transcript(target.root, [_link()])
    _session(target.root, "101.json", {"sessionId": _SID, "parkedJobId": "12ab3456"})
    assert _probe(target, detect=False) == ConversationProbe(True)


@pytest.mark.parametrize("pid", [None, -1, 0, "123", True, 2147483647])
def test_force_cannot_verify_dead_or_malformed_pid(target: _LocalTarget, pid: object) -> None:
    _session(target.root, "102.json", {"kind": "bg", "sessionId": _SID, "pid": pid})
    assert _probe(target, detect=False) == ConversationProbe(False)


def test_foreground_pid_cannot_verify_background(target: _LocalTarget) -> None:
    _session(target.root, "101.json", {"sessionId": _SID, "pid": os.getpid()})
    assert _probe(target, detect=False) == ConversationProbe(False)


def test_malformed_session_file_does_not_hide_valid_link(target: _LocalTarget) -> None:
    _transcript(target.root, [_link()])
    _session(target.root, "105.json", {})
    (target.root / "sessions" / "105.json").write_text("{")
    assert _probe(target) == ConversationProbe(True, (_NEXT,))


def test_unreadable_transcript_still_exists(target: _LocalTarget) -> None:
    path = _transcript(target.root, [_link()])
    path.chmod(0)
    try:
        assert _probe(target).transcript_exists
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("sid", ["not-a-uuid", "../../other", _NEXT.upper()])
def test_invalid_pin_never_reaches_transport(target: _LocalTarget, sid: str) -> None:
    with pytest.raises(StateError):
        _probe(target, sid=sid)
    assert target.commands == []


@pytest.mark.parametrize("status", [6, 127, 255])
def test_probe_execution_failure_never_becomes_fresh(status: int) -> None:
    with pytest.raises(StateError):
        probe_conversation(cast(Transport, _FakeTarget({"": _FakeResult(status)})), _SID)


@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize(
    "invalid",
    [
        {"candidates": ["../../invalid"]},
        {"candidates": [1]},
        {"parked_job_ids": [""]},
        {"parked_job_ids": ["a" * 129]},
        {"parked_job_ids": "abc"},
        {"background_exists": 1},
    ],
)
def test_malformed_probe_output_preserves_only_transcript_existence(status: int, invalid: dict[str, object]) -> None:
    metadata = {"candidates": [_NEXT], "parked_job_ids": ["af0980f9"], "background_exists": True, **invalid}
    result = _FakeResult(status, stdout="AGW_CLAUDE_CONVERSATION=" + json.dumps(metadata))
    observed = probe_conversation(cast(Transport, _FakeTarget({"": result})), _SID)
    assert observed == ConversationProbe(status == 0)


def test_missing_python_keeps_existence_boundary(target: _LocalTarget, tmp_path: Path) -> None:
    _transcript(target.root, [_link()])
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "find").symlink_to(subprocess.check_output(["which", "find"], text=True).strip())
    shell = tmp_path / "shell"
    shell.write_text(f'#!/bin/sh\nPATH={shlex.quote(str(tools))}; export PATH\nexec /bin/sh -c "$2"\n')
    shell.chmod(0o700)
    target.env["SHELL"] = str(shell)
    assert _probe(target) == ConversationProbe(True)


def test_no_claude_invocation_occurs(target: _LocalTarget, tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    sentinel = tmp_path / "claude-called"
    cli = tools / "claude"
    cli.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\nexit 99\n")
    cli.chmod(0o700)
    target.env["PATH"] = str(tools) + os.pathsep + target.env["PATH"]
    _transcript(target.root, [_link()])
    assert _probe(target).candidates == (_NEXT,)
    assert not sentinel.exists()


def test_bad_job_id_in_other_record_does_not_hide_parked_link(target: _LocalTarget) -> None:
    _session(target.root, "101.json", {"sessionId": _SID, "parkedJobId": "12ab3456"})
    _session(target.root, "103.json", {"kind": "bg", "jobId": [], "sessionId": _NEXT})
    _session(target.root, "102.json", {"kind": "bg", "jobId": "12ab3456", "sessionId": _PARKED})
    assert _probe(target).candidates == (_PARKED,)


def test_failed_find_is_distinct_from_absence(target: _LocalTarget, tmp_path: Path) -> None:
    _transcript(target.root, [])
    tools = tmp_path / "bin"
    tools.mkdir()
    finder = tools / "find"
    finder.write_text("#!/bin/sh\nexit 2\n")
    finder.chmod(0o700)
    shell = tmp_path / "shell"
    shell.write_text(f'#!/bin/sh\nPATH={shlex.quote(str(tools))}; export PATH\nexec /bin/sh -c "$2"\n')
    shell.chmod(0o700)
    target.env["SHELL"] = str(shell)
    with pytest.raises(StateError):
        _probe(target)


def test_existence_survives_detection_program_failure(target: _LocalTarget, tmp_path: Path) -> None:
    _transcript(target.root, [_link()])
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "python3"
    python.write_text("#!/bin/sh\nexit 99\n")
    python.chmod(0o700)
    (tools / "find").symlink_to(subprocess.check_output(["which", "find"], text=True).strip())
    shell = tmp_path / "shell"
    shell.write_text(f'#!/bin/sh\nPATH={shlex.quote(str(tools))}; export PATH\nexec /bin/sh -c "$2"\n')
    shell.chmod(0o700)
    target.env["SHELL"] = str(shell)
    assert _probe(target) == ConversationProbe(True)


def test_configured_launch_environment_selects_probe_root(target: _LocalTarget, tmp_path: Path) -> None:
    alternate = tmp_path / "configured-root"
    _transcript(alternate, [_link()])
    observed = probe_conversation(cast(Transport, target), _SID, environment={"CLAUDE_CONFIG_DIR": str(alternate)})
    assert observed == ConversationProbe(True, (_NEXT,))


def _live_record() -> dict[str, object]:
    if not sys.platform.startswith("linux"):
        pytest.skip("Native process identity verification targets Linux guests")
    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = stat[stat.rfind(")") + 2 :].split(" ")[19]
    domain = "linux:" + Path("/etc/machine-id").read_text().strip() + ":" + os.readlink("/proc/self/ns/pid")
    return {"kind": "bg", "sessionId": _SID, "pid": -1, "procStart": start, "pidDomain": domain}


@pytest.mark.parametrize(
    "field,value",
    [("procStart", "0"), ("procStart", None), ("pidDomain", "different-host-or-namespace"), ("pidDomain", None)],
)
def test_force_rejects_stale_process_identity(target: _LocalTarget, field: str, value: object) -> None:
    record = _live_record()
    record[field] = value
    _session(target.root, f"{os.getpid()}.json", record)
    assert _probe(target, detect=False) == ConversationProbe(False)


def test_non_pid_registry_filename_cannot_verify_background(target: _LocalTarget) -> None:
    _session(target.root, "background.json", _live_record())
    assert _probe(target, detect=False) == ConversationProbe(False)

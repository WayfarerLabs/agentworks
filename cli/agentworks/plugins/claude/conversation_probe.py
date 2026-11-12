"""Observe explicit Claude conversation links without choosing a conversation."""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.transports import Transport


@dataclass(frozen=True)
class ConversationProbe:
    """Addressing evidence returned from the launch target, without prompt content."""

    transcript_exists: bool
    candidates: tuple[str, ...] = ()
    background_exists: bool = False
    parked_job_ids: tuple[str, ...] = ()


# Read the pid records before any Claude invocation can sweep parked links.
# Metadata records are small; bounded reads skip oversized conversation content.
_PROGRAM = r"""
import json, os, pathlib, sys, uuid
path, sid, detect = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
root = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or pathlib.Path.home() / '.claude')
limit = 131072

def identity(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value)) if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None

def load_record(path):
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        return json.loads(data) if len(data) <= limit else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None

def live(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False

records = []
try:
    with os.scandir(root / 'sessions') as entries:
        for entry in entries:
            if entry.name.endswith('.json'):
                record = load_record(pathlib.Path(entry.path))
                if isinstance(record, dict):
                    records.append(record)
except OSError:
    pass
background = any(record.get('kind') == 'bg' and record.get('sessionId') == sid
                 and live(record.get('pid')) for record in records)
parked = set()
candidates = set()
if detect:
    for record in records:
        job = record.get('parkedJobId')
        if record.get('sessionId') == sid and isinstance(job, str) and job and len(job) <= 128:
            parked.add(job)
    for record in records:
        candidate = identity(record.get('sessionId'))
        if (record.get('kind') == 'bg' and isinstance(record.get('jobId'), str)
                and record['jobId'] in parked and candidate and candidate != sid):
            candidates.add(candidate)

def continuation(line):
    try:
        record = json.loads(line)
        if isinstance(record, dict) and record.get('type') == 'continued-in' and record.get('sessionId') == sid:
            candidate = identity(record.get('continuedInSessionId'))
            if candidate and candidate != sid:
                candidates.add(candidate)
                return True
    except (ValueError, UnicodeError, RecursionError):
        pass
    return False

if detect and path:
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - limit)
            stream.seek(start)
            tail = stream.read(limit)
            lines = tail.splitlines()
            if start:
                lines = lines[1:]
            found = False
            for line in lines:
                found = continuation(line) or found
            if not found and start and not candidates:
                stream.seek(0)
                while True:
                    line = stream.readline(limit + 1)
                    if not line:
                        break
                    if len(line) > limit:
                        while line and not line.endswith(b'\n'):
                            line = stream.readline(limit + 1)
                        continue
                    continuation(line)
    except OSError:
        pass
print('\nAGW_CLAUDE_CONVERSATION=' + json.dumps({
    'transcript_exists': bool(path), 'candidates': sorted(candidates),
    'background_exists': background, 'parked_job_ids': sorted(parked)
}, separators=(',', ':')))
"""


def probe_conversation(transport: Transport, sid: str, *, detect: bool = True) -> ConversationProbe:
    """Probe exact stored UUID addressing in one login-shell round trip.

    Transcript existence failures raise. Optional metadata detection fails soft,
    including a target without Python 3, preserving the existing resume boundary.
    No Claude process runs here and no transcript content leaves the target.
    """
    try:
        if str(uuid.UUID(sid)) != sid:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise StateError("Claude Code conversation ID must be a canonical UUID") from error
    program = shlex.join(("python3", "-c", _PROGRAM))
    inner = (
        'root="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"; out=""; '
        'if [ -d "$root" ]; then '
        f'out=$(find "$root" -name {shlex.quote(sid + ".jsonl")} -print -quit 2>/dev/null); rc=$?; '
        '[ -n "$out" ] || [ "$rc" -eq 0 ] || exit 6; fi; '
        'exists=0; [ -z "$out" ] || exists=1; '
        'printf "\\nAGW_CLAUDE_TRANSCRIPT=%s\\n" "$exists"; '
        f'if command -v python3 >/dev/null 2>&1; then {program} "$out" {shlex.quote(sid)} '
        f'{"1" if detect else "0"}; fi; [ "$exists" -eq 1 ]'
    )
    result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
    if result.returncode not in (0, 1):
        raise StateError(
            f"could not probe for the Claude transcript (exit {result.returncode}); "
            "refusing to guess resume-vs-launch.",
            hint="Retry once the launch target is reachable.",
        )
    lines = result.stdout.splitlines()
    exists = next((line.endswith("=1") for line in lines if line.startswith("AGW_CLAUDE_TRANSCRIPT=")), None)
    if exists is None:
        exists = result.returncode == 0
    fallback = ConversationProbe(exists)
    try:
        payload = next(
            line.removeprefix("AGW_CLAUDE_CONVERSATION=")
            for line in reversed(lines)
            if line.startswith("AGW_CLAUDE_CONVERSATION=")
        )
        metadata = json.loads(payload)
        candidates = metadata["candidates"]
        parked = metadata["parked_job_ids"]
        if not isinstance(candidates, list) or not isinstance(parked, list):
            return fallback
        if any(not isinstance(value, str) or str(uuid.UUID(value)) != value for value in candidates):
            return fallback
        if any(not isinstance(value, str) or not value or len(value) > 128 for value in parked):
            return fallback
        if type(metadata["background_exists"]) is not bool:
            return fallback
        return ConversationProbe(exists, tuple(candidates), metadata["background_exists"], tuple(parked))
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return fallback

"""Fixed, no-staging guest query and strict reduction for one WSL2 anchor.

This module provides source for ``python -c`` and a pure response reducer. It
does not dispatch a WSL client or claim custody of one.
"""

from __future__ import annotations

import re

from agentworks.execution._wsl2_lifecycle import GuestAnchorIdentity, GuestAnchorPresence

MAX_GUEST_QUERY_RESPONSE_BYTES = 160
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_RESPONSE = re.compile(
    rb"AGW_GQ1 ([0-9a-f]{32}) ([1-9][0-9]*) "
    rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|-) "
    rb"(found|missing|unknown) (-|0|[1-9][0-9]*)\n\Z"
)

# This literal source runs on the WSL guest's Python 3.11. Only a validated
# nonce and decimal PID travel as arguments; no paths or code come from input.
FIXED_GUEST_QUERY_SOURCE = """import errno
import os
import re
import sys

UUID = re.compile(rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\n\\Z')
NONCE = re.compile(r'[0-9a-f]{32}\\Z')

def boot():
    with open('/proc/sys/kernel/random/boot_id', 'rb') as source:
        value = source.read(38)
    if UUID.fullmatch(value) is None:
        raise ValueError('invalid boot')
    return value[:-1].decode('ascii')

def process(pid):
    try:
        with open('/proc/{}/stat'.format(pid), 'rb') as source:
            value = source.read(4097)
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as error:
            if error.errno == errno.ESRCH:
                return 'missing', '-'
        return 'unknown', '-'
    if len(value) > 4096 or not value.endswith(b'\\n') or b'\\n' in value[:-1]:
        return 'unknown', '-'
    prefix = str(pid).encode('ascii') + b' ('
    closing = value.rfind(b') ')
    if not value.startswith(prefix) or closing < len(prefix):
        return 'unknown', '-'
    fields = value[closing + 2:-1].split()
    if len(fields) < 20 or len(fields[19]) > 20 or not fields[19].isdigit():
        return 'unknown', '-'
    ticks = int(fields[19])
    if ticks > 2**64 - 1:
        return 'unknown', '-'
    return 'found', str(ticks)

if (
    len(sys.argv) != 3
    or NONCE.fullmatch(sys.argv[1]) is None
    or not sys.argv[2].isascii()
    or not sys.argv[2].isdecimal()
    or sys.argv[2][0] == '0'
):
    raise SystemExit(2)
nonce, pid_text = sys.argv[1:]
pid = int(pid_text)
if pid <= 0:
    raise SystemExit(2)
observed_boot, kind, ticks = '-', 'unknown', '-'
try:
    before = boot()
    kind, ticks = process(pid)
    after = boot()
    if before == after:
        observed_boot = before
    else:
        kind, ticks = 'unknown', '-'
except (OSError, ValueError):
    kind, ticks = 'unknown', '-'
sys.stdout.buffer.write('AGW_GQ1 {} {} {} {} {}\\n'.format(nonce, pid, observed_boot, kind, ticks).encode('ascii'))
sys.stdout.buffer.flush()
"""


def reduce_guest_query_response(
    response: bytes,
    identity: GuestAnchorIdentity,
    nonce: str,
    *,
    exit_status: int | None,
    complete: bool,
    deadline_expired: bool,
) -> GuestAnchorPresence:
    """Reduce untrusted output only after complete, timely client observation."""
    if (
        type(identity) is not GuestAnchorIdentity
        or type(nonce) is not str
        or _NONCE.fullmatch(nonce) is None
        or type(response) is not bytes
        or len(response) > MAX_GUEST_QUERY_RESPONSE_BYTES
        or type(exit_status) is not int
        or exit_status != 0
        or complete is not True
        or deadline_expired is not False
    ):
        return GuestAnchorPresence.UNKNOWN
    match = _RESPONSE.fullmatch(response)
    if match is None or match.group(1) != nonce.encode("ascii") or match.group(2) != str(identity.pid).encode("ascii"):
        return GuestAnchorPresence.UNKNOWN
    boot = match.group(3).decode("ascii")
    kind = match.group(4)
    ticks = match.group(5)
    if kind == b"unknown":
        return GuestAnchorPresence.UNKNOWN
    if boot == "-":
        return GuestAnchorPresence.UNKNOWN
    if kind == b"missing":
        if ticks != b"-":
            return GuestAnchorPresence.UNKNOWN
        return GuestAnchorPresence.ABSENT_CONFIRMED
    if ticks == b"-" or int(ticks) > 2**64 - 1:
        return GuestAnchorPresence.UNKNOWN
    if boot != identity.boot_id or int(ticks) != identity.start_time:
        return GuestAnchorPresence.ABSENT_CONFIRMED
    return GuestAnchorPresence.PRESENT

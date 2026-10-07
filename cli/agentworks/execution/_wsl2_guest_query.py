"""Fixed, no-staging guest query and strict reduction for one WSL2 anchor.

This module provides a fixed callable body and a pure response reducer. It
does not dispatch a WSL client or claim custody of one.
"""

from __future__ import annotations

import re

from agentworks.execution._wsl2_lifecycle import GuestAnchorIdentity, GuestAnchorPresence

MAX_GUEST_QUERY_RESPONSE_BYTES = 160
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_RESPONSE = re.compile(
    rb"AGW_GQ2 ([0-9a-f]{32}) ([1-9][0-9]*) "
    rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|-) "
    rb"(-|0|[1-9][0-9]*) (found|missing|unknown) (-|0|[1-9][0-9]*)\n\Z"
)

# This literal source runs on the WSL guest's Python 3.11. Only a validated
# nonce and bound PID select the query; no paths or code come from input.
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

def stat_ticks(pid):
    if pid == 1:
        value = _agw_read_init()
    else:
        with open('/proc/{}/stat'.format(pid), 'rb') as source:
            value = source.read(4097)
    if len(value) > 4096 or not value.endswith(b'\\n') or b'\\n' in value[:-1]:
        raise ValueError('invalid stat')
    prefix = str(pid).encode('ascii') + b' ('
    closing = value.rfind(b') ')
    if not value.startswith(prefix) or closing < len(prefix):
        raise ValueError('invalid stat')
    fields = value[closing + 2:-1].split()
    if len(fields) < 20 or len(fields[19]) > 20 or not fields[19].isdigit():
        raise ValueError('invalid stat')
    ticks = int(fields[19])
    if ticks > 2**64 - 1:
        raise ValueError('invalid stat')
    return ticks

def process(pid):
    try:
        ticks = stat_ticks(pid)
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as error:
            if error.errno == errno.ESRCH:
                return 'missing', '-'
        return 'unknown', '-'
    return 'found', str(ticks)

def main(nonce):
    pid = _agw_query_pid
    if NONCE.fullmatch(nonce) is None or type(pid) is not int or pid <= 0:
        return 2
    observed_boot, observed_init, kind, ticks = '-', '-', 'unknown', '-'
    try:
        before_boot, before_init = boot(), stat_ticks(1)
        kind, ticks = process(pid)
        after_init, after_boot = stat_ticks(1), boot()
        if before_boot == after_boot and before_init == after_init:
            observed_boot, observed_init = before_boot, str(before_init)
        else:
            kind, ticks = 'unknown', '-'
    except (OSError, ValueError):
        kind, ticks = 'unknown', '-'
    line = 'AGW_GQ2 {} {} {} {} {} {}\\n'.format(nonce, pid, observed_boot, observed_init, kind, ticks)
    sys.stdout.buffer.write(line.encode('ascii'))
    sys.stdout.buffer.flush()
    return 0
"""

# Only persisted v3 recovery uses this former same-user init observation.
LEGACY_GUEST_QUERY_SOURCE = (
    FIXED_GUEST_QUERY_SOURCE
    + "\ndef _agw_read_init():\n"
    + "    with open('/proc/1/stat', 'rb') as source:\n"
    + "        return source.read(4097)\n"
    + "_agw_query_pid = int(sys.argv[2])\n"
    + "raise SystemExit(main(sys.argv[1]))\n"
)


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
    init_ticks = match.group(4)
    kind = match.group(5)
    ticks = match.group(6)
    if kind == b"unknown" or init_ticks == b"-":
        return GuestAnchorPresence.UNKNOWN
    if boot == "-" or int(init_ticks) > 2**64 - 1:
        return GuestAnchorPresence.UNKNOWN
    if kind == b"missing":
        if ticks != b"-":
            return GuestAnchorPresence.UNKNOWN
        return GuestAnchorPresence.ABSENT_CONFIRMED
    if ticks == b"-" or int(ticks) > 2**64 - 1:
        return GuestAnchorPresence.UNKNOWN
    if boot != identity.boot_id or int(init_ticks) != identity.init_start_ticks or int(ticks) != identity.start_time:
        return GuestAnchorPresence.ABSENT_CONFIRMED
    return GuestAnchorPresence.PRESENT

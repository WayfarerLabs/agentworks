"""Bind byte-exact machine output to the CLI process streams."""

from __future__ import annotations

import sys
from typing import BinaryIO, cast

from agentworks.machine_output import JsonObject, MachineOutputCommand, write_json_envelope


def _binary_writer(stream: object) -> BinaryIO | None:
    """Return ``stream`` when it accepts bytes."""
    writer = cast("BinaryIO", stream)
    try:
        writer.write(b"")
    except Exception:
        return None
    return writer


def write_json_stdout(command: MachineOutputCommand, data: JsonObject) -> None:
    """Write one JSON v1 document to the current process stdout."""
    stream = _binary_writer(sys.stdout)
    if stream is None:
        stream = _binary_writer(getattr(sys.stdout, "buffer", None))
    if stream is None:
        raise RuntimeError("Was not able to determine binary stream for sys.stdout.")
    write_json_envelope(command, data, stream)

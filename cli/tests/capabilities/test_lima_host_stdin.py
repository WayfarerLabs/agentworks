r"""Byte-exact stdin to a local ``limactl`` command, and its output decoded as text mode did.

Text mode rewrites ``\n`` to ``os.linesep`` on the way into a child, which on
Windows appends a CR to every line. CI runs Linux, where that rewriting does
not happen, so the round trips below cannot fail there; the Windows lane is
what proves them. Each command runs through :class:`LimaHost` in local mode,
the one caller, with this interpreter standing in for ``limactl``.
"""

from __future__ import annotations

import shlex
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.lima_host import LimaHost
from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows


def python(source: str, *args: str) -> str:
    return shlex.join([sys.executable, "-c", source, *args])


def received(tmp_path: Path, payload: str) -> bytes:
    """The bytes a local command read from its stdin when handed ``payload``."""
    sink = tmp_path / "stdin"
    LimaHost(None).run(
        python("import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())", str(sink)), input=payload
    )
    return sink.read_bytes()


def test_a_trailing_newline_reaches_the_child_as_one_byte(tmp_path: Path) -> None:
    assert received(tmp_path, "tskey-auth-sentinel\n") == b"tskey-auth-sentinel\n"


def test_embedded_newlines_are_not_rewritten(tmp_path: Path) -> None:
    """The Lima provider YAML crosses this same pipe, multi-line."""
    assert received(tmp_path, "images:\n- location: file\ncpus: 4\n") == b"images:\n- location: file\ncpus: 4\n"


def test_an_os_sourced_value_goes_back_out_as_its_original_byte(tmp_path: Path) -> None:
    assert received(tmp_path, "tskey-" + chr(0xDCFF)) == b"tskey-\xff"


def test_an_unencodable_payload_starts_nothing_and_stays_out_of_the_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The payload here is a secret, and ``UnicodeEncodeError`` carries it verbatim."""
    from agentworks.execution import ssh_process

    monkeypatch.setattr(ssh_process, "spawn", lambda *args, **kwargs: pytest.fail("a process was started"))
    with pytest.raises(ValidationError) as caught:
        LimaHost(None).run(python("pass"), input="tskey-auth-swordfish-" + chr(0xD800))

    assert "swordfish" not in str(caught.value)
    assert "swordfish" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_output_folds_newlines_and_replaces_undecodable_bytes() -> None:
    printed = LimaHost(None).run(python(r"import sys; sys.stdout.buffer.write(b'X\r\nY\rZ\xff\n')"))

    assert printed == "X\nY\nZ�\n"

"""The request envelope: what survives the wire exactly and what is refused up front."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import envelope
from agentworks.execution.envelope import MAX_STRING_BYTES, encode
from agentworks.execution.requests import Command, LoginShell, Script, Shell

NONCE = "0" * 32


def decode_text(value: str) -> bytes:
    """What the guest does with a string before exec."""
    return value.encode("utf-8", "surrogateescape")


def wire(request: Command | Script | LoginShell, **extra: str) -> dict[str, object]:
    body = encode(request, nonce=NONCE, **extra)
    assert body.isascii()
    return json.loads(body)  # type: ignore[no-any-return]


def as_text(data: bytes) -> str:
    """How a workstation produces a ``str`` for bytes it did not choose (``os.fsdecode``)."""
    return data.decode("utf-8", "surrogateescape")


def test_every_byte_but_nul_survives_in_argv_and_environment() -> None:
    values = [as_text(b"<" + bytes([byte]) + b">") for byte in range(1, 256)]
    request = Command(tuple(["tool", *values]), env={f"V{n}": value for n, value in enumerate(values)})

    body = wire(request)

    assert [decode_text(item) for item in body["argv"][1:]] == [  # type: ignore[index]
        b"<" + bytes([byte]) + b">" for byte in range(1, 256)
    ]
    assert [decode_text(value) for value in body["env"].values()] == [  # type: ignore[attr-defined]
        b"<" + bytes([byte]) + b">" for byte in range(1, 256)
    ]


@pytest.mark.parametrize("text", ["100%", "%s %d %%", "'single' \"double\"", "${HOME} $(id) `id`", "a\r\nb", "tab\t"])
def test_shell_significant_text_travels_unchanged(text: str) -> None:
    body = wire(Command(("tool", text), env={"X": text}))

    assert body["argv"] == ["tool", text]
    assert body["env"] == {"X": text}


def test_script_source_cwd_and_flags_are_carried() -> None:
    body = wire(Script("echo hi\n", Shell.BASH, login=True, cwd="/srv/ws", env={"A": "b"}))

    assert (body["kind"], body["mode"], body["shell"], body["login"]) == ("script", "run", "bash", True)
    assert (body["source"], body["cwd"], body["env"]) == ("echo hi\n", "/srv/ws", {"A": "b"})


def test_a_login_shell_travels_as_a_handover_with_its_terminal_type() -> None:
    body = wire(LoginShell(cwd="/srv/ws"), term="xterm", handover="ab" * 8)

    assert (body["mode"], body["kind"], body["term"], body["handover"]) == ("park", "shell", "xterm", "ab" * 8)


@pytest.mark.parametrize(
    "request_",
    [
        LoginShell(),
        Command(("true",)),
    ],
)
def test_only_a_login_shell_takes_a_handover(request_: Command | LoginShell) -> None:
    extra = {"term": "xterm", "handover": "ab" * 8} if isinstance(request_, Command) else {}
    with pytest.raises(ValueError, match=r"login shell"):
        encode(request_, nonce=NONCE, **extra)


@pytest.mark.parametrize(
    "request_",
    [
        Command(("tool", "a\0b")),
        Command(("tool",), env={"X": "a\0b"}),
        Command(("tool",), cwd="/a\0b"),
        Script("echo \0", Shell.SH),
        Command(("tool", "\ud800")),
        Command(("tool",), env={"X": "\udc7f"}),
        Command(("tool",), env={"1X": "v"}),
        Command(("tool",), env={"A-B": "v"}),
        Command(("tool",), env={"": "v"}),
        Command(("tool",), cwd="relative/dir"),
        Command(("tool",), cwd=""),
        Command(()),
        Command(("",)),
    ],
)
def test_input_the_guest_cannot_represent_is_refused_up_front(request_: Command | Script) -> None:
    with pytest.raises(ValidationError):
        encode(request_, nonce=NONCE)


def test_a_surrogate_escape_is_accepted_and_a_lone_high_surrogate_is_not() -> None:
    encode(Command(("tool", "\udc80\udcff")), nonce=NONCE)

    with pytest.raises(ValidationError):
        encode(Command(("tool", "\udc7f")), nonce=NONCE)


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(MAX_STRING_BYTES, True), (MAX_STRING_BYTES + 1, False)],
)
def test_the_kernel_limit_on_one_argument_is_enforced_in_bytes(length: int, accepted: bool) -> None:
    request = Command(("tool", "a" * length))
    if accepted:
        encode(request, nonce=NONCE)
    else:
        with pytest.raises(ValidationError, match="argument 1"):
            encode(request, nonce=NONCE)


@pytest.mark.parametrize(("extra", "accepted"), [(0, True), (1, False)])
def test_the_kernel_limit_on_one_environment_string_counts_name_equals_and_value(extra: int, accepted: bool) -> None:
    value = "v" * (MAX_STRING_BYTES - len("NAME=") + extra)
    request = Command(("tool",), env={"NAME": value})
    if accepted:
        encode(request, nonce=NONCE)
    else:
        with pytest.raises(ValidationError, match="NAME"):
            encode(request, nonce=NONCE)


def test_the_limit_counts_encoded_bytes_not_characters() -> None:
    # Two bytes per character: half the limit in characters is the limit in bytes.
    encode(Command(("tool", "é" * (MAX_STRING_BYTES // 2))), nonce=NONCE)
    with pytest.raises(ValidationError):
        encode(Command(("tool", "é" * (MAX_STRING_BYTES // 2 + 1))), nonce=NONCE)


def test_refusals_never_quote_what_they_refused() -> None:
    secret = "sekrit-value-8f3a"
    cases = [
        Command(("tool", f"{secret}\0")),
        Command(("tool",), env={"X": secret + "\0"}),
        Command(("tool", secret + "\ud800")),
        Command(("tool", secret * 20_000)),
        Command(("tool",), env={"X": secret * 20_000}),
        Command(("tool",), cwd=secret),
        Script(secret + "\0", Shell.SH),
    ]
    for request in cases:
        with pytest.raises(ValidationError) as raised:
            encode(request, nonce=NONCE)
        assert secret not in str(raised.value)
        assert secret not in repr(raised.value)
        assert raised.value.__cause__ is None


def test_requests_do_not_repr_their_payload() -> None:
    secret = "sekrit-value-8f3a"
    for request in (
        Command((secret,), env={"X": secret}),
        Script(secret, Shell.SH, env={"X": secret}),
        LoginShell(env={"X": secret}),
    ):
        assert secret not in repr(request)


def test_frames_are_ten_digits_then_the_bytes() -> None:
    assert envelope.frame(b"abc") == b"0000000003abc"
    assert envelope.frame(b"") == b"0000000000"


def test_the_launcher_source_is_the_installed_file() -> None:
    from agentworks.execution import agw_launch

    source = envelope.launcher_source()

    assert source == Path(agw_launch.__file__).read_bytes()
    assert source.isascii()

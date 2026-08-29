"""Behavioral tests for public-only SSH identity derivation."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from agentworks.ssh_identity import (
    SSHIdentityReadError,
    UnverifiableSSHIdentity,
    VerifiedSSHIdentity,
    fingerprint_public_blob,
    parse_public_ssh_identity,
    read_private_ssh_identity,
)

_OPENSSH_BEGIN = b"-----BEGIN OPENSSH PRIVATE KEY-----"
_OPENSSH_END = b"-----END OPENSSH PRIVATE KEY-----"
_OPENSSH_MAGIC = b"openssh-key-v1\x00"
_SSH_KEYGEN = shutil.which("ssh-keygen")


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _public_blob(algorithm: bytes = b"ssh-ed25519") -> bytes:
    return _ssh_string(algorithm) + _ssh_string(bytes(range(32)))


def _native_envelope(
    *,
    public_blob: bytes | None = None,
    private_section: bytes = b"encrypted-or-plain-private-section",
    cipher: bytes = b"none",
    kdf: bytes = b"none",
    kdf_options: bytes = b"",
    key_count: int = 1,
    trailing_envelope: bytes = b"",
) -> bytes:
    blob = _public_blob() if public_blob is None else public_blob
    return (
        _OPENSSH_MAGIC
        + _ssh_string(cipher)
        + _ssh_string(kdf)
        + _ssh_string(kdf_options)
        + struct.pack(">I", key_count)
        + _ssh_string(blob)
        + _ssh_string(private_section)
        + trailing_envelope
    )


def _native_armor(envelope: bytes, *, trailing_armor: bytes = b"\n") -> bytes:
    encoded = base64.b64encode(envelope)
    lines = [encoded[offset : offset + 31] for offset in range(0, len(encoded), 31)]
    return _OPENSSH_BEGIN + b"\n" + b"\n".join(lines) + b"\n" + _OPENSSH_END + trailing_armor


def _write(path: Path, contents: bytes) -> Path:
    path.write_bytes(contents)
    return path


def _captured_error(callable_: Callable[[], object]) -> SSHIdentityReadError:
    with pytest.raises(SSHIdentityReadError) as caught:
        callable_()
    return caught.value


def test_fingerprint_public_blob_matches_openssh_sha256_shape() -> None:
    blob = _public_blob()
    expected = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")

    assert fingerprint_public_blob(blob) == f"SHA256:{expected}"


def test_native_private_identity_extracts_public_blob_without_decrypting(tmp_path: Path) -> None:
    blob = _public_blob()
    key = _write(
        tmp_path / "id_ed25519",
        _native_armor(
            _native_envelope(
                public_blob=blob,
                private_section=b"opaque-protected-material",
                cipher=b"future-cipher",
                kdf=b"future-kdf",
                kdf_options=b"future-options",
            ),
            trailing_armor=b" \t\r\n",
        ),
    )

    assert read_private_ssh_identity(key) == VerifiedSSHIdentity(fingerprint_public_blob(blob))


def test_private_identity_does_not_consult_stale_sibling_public_file(tmp_path: Path) -> None:
    blob = _public_blob()
    key = _write(tmp_path / "id", _native_armor(_native_envelope(public_blob=blob)))
    (tmp_path / "id.pub").write_text("ssh-ed25519 definitely-not-base64 stale\n")

    assert read_private_ssh_identity(key) == VerifiedSSHIdentity(fingerprint_public_blob(blob))


@pytest.mark.parametrize(
    "label",
    [
        "RSA PRIVATE KEY",
        "DSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
    ],
)
def test_recognized_legacy_private_armor_is_permissively_unverifiable(tmp_path: Path, label: str) -> None:
    sentinel = "legacy-sensitive-sentinel"
    contents = (
        f"-----BEGIN {label}-----\n"
        f"Proc-Type: 4,ENCRYPTED\nDEK-Info: future,{sentinel}\n"
        "not base64 and not parsed\n"
        f"-----END {label}-----\n \t\r\n"
    )
    key = tmp_path / "legacy-key"
    key.write_text(contents)

    assert read_private_ssh_identity(key) == UnverifiableSSHIdentity()


def test_recognized_legacy_private_armor_requires_matching_end(tmp_path: Path) -> None:
    key = tmp_path / "legacy-key"
    key.write_text("-----BEGIN ENCRYPTED PRIVATE KEY-----\nopaque")

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


def test_recognized_legacy_private_armor_rejects_trailing_non_whitespace(tmp_path: Path) -> None:
    key = tmp_path / "legacy-key"
    key.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        "opaque transport-owned body\n"
        "-----END ENCRYPTED PRIVATE KEY-----\n"
        "trailing-garbage"
    )

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


@pytest.mark.parametrize(
    "envelope",
    [
        _OPENSSH_MAGIC + struct.pack(">I", 0xFFFFFFFF),
        _native_envelope(key_count=0),
        _native_envelope(key_count=2),
        _native_envelope(public_blob=_ssh_string(b"")),
        _native_envelope(public_blob=_ssh_string(b"ssh-ed25519\xff")),
        _native_envelope(private_section=b""),
        _native_envelope(trailing_envelope=b"extra"),
    ],
)
def test_native_private_identity_rejects_invalid_envelope_shapes(tmp_path: Path, envelope: bytes) -> None:
    key = _write(tmp_path / "id", _native_armor(envelope))

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


def test_native_private_identity_rejects_truncation_after_public_blob(tmp_path: Path) -> None:
    blob = _public_blob()
    envelope = (
        _OPENSSH_MAGIC
        + _ssh_string(b"none")
        + _ssh_string(b"none")
        + _ssh_string(b"")
        + struct.pack(">I", 1)
        + _ssh_string(blob)
    )
    key = _write(tmp_path / "id", _native_armor(envelope))

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


@pytest.mark.parametrize(
    "contents",
    [
        _OPENSSH_BEGIN + b"\n%%%\n" + _OPENSSH_END + b"\n",
        _native_armor(b"wrong-magic"),
        _native_armor(_native_envelope(), trailing_armor=b"\ntrailing-garbage"),
        _OPENSSH_BEGIN + b"\n" + base64.b64encode(_native_envelope()),
        b"not a private key",
    ],
)
def test_private_identity_rejects_malformed_or_unrecognized_input(tmp_path: Path, contents: bytes) -> None:
    key = _write(tmp_path / "id", contents)

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


def test_private_identity_rejects_oversized_file_before_parsing(tmp_path: Path) -> None:
    key = _write(tmp_path / "id", _OPENSSH_BEGIN + b"A" * (1024 * 1024))

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "invalid"


@pytest.mark.parametrize("target", ["missing", "directory"])
def test_private_identity_classifies_unavailable_paths(tmp_path: Path, target: str) -> None:
    path = tmp_path / target
    if target == "directory":
        path.mkdir()

    error = _captured_error(lambda: read_private_ssh_identity(path))

    assert error.kind == "unavailable"


def test_private_identity_classifies_short_filesystem_read_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write(tmp_path / "id", _native_armor(_native_envelope()))
    monkeypatch.setattr(os, "read", lambda _descriptor, _size: b"")

    error = _captured_error(lambda: read_private_ssh_identity(key))

    assert error.kind == "unavailable"


def test_public_identity_parses_one_key_with_comments_and_blank_lines(tmp_path: Path) -> None:
    blob = _public_blob()
    encoded = base64.b64encode(blob).decode("ascii")
    text = f"\n# configured identity\nssh-ed25519 {encoded} operator comment\n"
    expected = VerifiedSSHIdentity(fingerprint_public_blob(blob))
    assert parse_public_ssh_identity(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# comment only\n",
        "ssh-ed25519",
        "ssh-ed25519 invalid!base64",
        "ssh-rsa AAAA",
    ],
)
def test_public_identity_rejects_invalid_key_lines(text: str) -> None:
    error = _captured_error(lambda: parse_public_ssh_identity(text))

    assert error.kind == "invalid"


def test_public_identity_rejects_algorithm_mismatch() -> None:
    blob = _public_blob()
    encoded = base64.b64encode(blob).decode("ascii")

    error = _captured_error(lambda: parse_public_ssh_identity(f"ssh-rsa {encoded}"))

    assert error.kind == "invalid"


def test_public_identity_rejects_multiple_key_lines() -> None:
    blob = base64.b64encode(_public_blob()).decode("ascii")

    error = _captured_error(lambda: parse_public_ssh_identity(f"ssh-ed25519 {blob}\nssh-ed25519 {blob}\n"))

    assert error.kind == "invalid"


@pytest.mark.parametrize("source", ["private", "public", "filesystem"])
def test_diagnostic_errors_do_not_echo_sensitive_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    sentinel = "sensitive-diagnostic-sentinel"
    if source == "private":
        path = _write(
            tmp_path / "id",
            _OPENSSH_BEGIN + b"\n" + base64.b64encode(sentinel.encode()) + b"\n" + _OPENSSH_END,
        )

        def operation() -> object:
            return read_private_ssh_identity(path)

    elif source == "public":

        def operation() -> object:
            return parse_public_ssh_identity(f"ssh-ed25519 {sentinel}!")

    else:
        path = tmp_path / "id"

        def fail_open(_path: object, _flags: int) -> int:
            raise OSError(sentinel)

        monkeypatch.setattr(os, "open", fail_open)

        def operation() -> object:
            return read_private_ssh_identity(path)

    error = _captured_error(operation)

    assert error.kind == ("unavailable" if source == "filesystem" else "invalid")
    assert sentinel not in str(error)
    assert sentinel not in repr((error.args, vars(error)))
    assert len(error.detail) <= 256


@pytest.mark.skipif(_SSH_KEYGEN is None, reason="ssh-keygen is not installed")
@pytest.mark.parametrize("passphrase", ["", "test-passphrase"])
def test_real_native_openssh_key_matches_ssh_keygen_fingerprint(tmp_path: Path, passphrase: str) -> None:
    private_key = tmp_path / "id_ed25519"
    subprocess.run(
        [_SSH_KEYGEN or "ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(private_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    fingerprint = subprocess.run(
        [_SSH_KEYGEN or "ssh-keygen", "-l", "-E", "sha256", "-f", str(private_key.with_suffix(".pub"))],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]

    assert read_private_ssh_identity(private_key) == VerifiedSSHIdentity(fingerprint)

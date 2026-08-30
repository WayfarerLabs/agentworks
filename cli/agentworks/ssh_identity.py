"""Bounded, public-only SSH identity parsing.

This leaf derives the fingerprint OpenSSH exposes for a public key. It never
decrypts private material, consults a sibling public-key file, or invokes an
external program.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

_MAX_IDENTITY_FILE_BYTES = 1024 * 1024
_MAX_ERROR_DETAIL_CHARACTERS = 256
_OPENSSH_MAGIC = b"openssh-key-v1\x00"
_OPENSSH_BEGIN = b"-----BEGIN OPENSSH PRIVATE KEY-----"
_OPENSSH_END = b"-----END OPENSSH PRIVATE KEY-----"
_ASCII_WHITESPACE = b" \t\r\n\v\f"
_LEGACY_ARMOR_LABELS = (
    b"RSA PRIVATE KEY",
    b"DSA PRIVATE KEY",
    b"EC PRIVATE KEY",
    b"PRIVATE KEY",
    b"ENCRYPTED PRIVATE KEY",
)

type SSHIdentityErrorKind = Literal["invalid", "unavailable"]


@dataclass(frozen=True, slots=True)
class VerifiedSSHIdentity:
    """A private or public identity with an authoritative fingerprint."""

    fingerprint: str


@dataclass(frozen=True, slots=True)
class UnverifiableSSHIdentity:
    """A recognized private envelope without exposed public identity."""


type SSHIdentity = VerifiedSSHIdentity | UnverifiableSSHIdentity


class SSHIdentityReadError(Exception):
    """A value-safe identity read or parse failure."""

    kind: SSHIdentityErrorKind
    detail: str

    def __init__(self, kind: SSHIdentityErrorKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail[:_MAX_ERROR_DETAIL_CHARACTERS]
        super().__init__(self.detail)


def fingerprint_public_blob(blob: bytes) -> str:
    """Return the OpenSSH SHA-256 fingerprint for one public-key blob."""
    digest = hashlib.sha256(blob).digest()
    encoded = base64.standard_b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def parse_public_ssh_identity(text: str) -> VerifiedSSHIdentity:
    """Parse one configured OpenSSH public-key line.

    Blank lines and comment-only lines are ignored. Authorization options are
    not accepted because this input is a public-key file, not an
    ``authorized_keys`` policy entry.
    """
    if len(text) > _MAX_IDENTITY_FILE_BYTES:
        raise _invalid("public identity exceeds the supported size limit")
    try:
        encoded_text = text.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid("public identity is not valid UTF-8 text") from None
    if len(encoded_text) > _MAX_IDENTITY_FILE_BYTES:
        raise _invalid("public identity exceeds the supported size limit")

    key_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key_lines.append(stripped)
    if len(key_lines) != 1:
        raise _invalid("public identity must contain exactly one key line")

    fields = key_lines[0].split(maxsplit=2)
    if len(fields) < 2:
        raise _invalid("public identity key line is incomplete")
    key_type, encoded_blob = fields[0], fields[1]
    try:
        blob = base64.b64decode(encoded_blob.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise _invalid("public identity contains invalid base64") from None
    algorithm = _public_blob_algorithm(blob, stage="public identity")
    if key_type != algorithm:
        raise _invalid("public identity key type does not match its key blob")
    return VerifiedSSHIdentity(fingerprint=fingerprint_public_blob(blob))


def read_private_ssh_identity(path: Path) -> SSHIdentity:
    """Derive identity from a private-key envelope without decrypting it."""
    contents = _read_bounded_file(path)
    if contents.startswith(_OPENSSH_BEGIN):
        return _parse_openssh_private_identity(contents)
    for label in _LEGACY_ARMOR_LABELS:
        begin = b"-----BEGIN " + label + b"-----"
        if not contents.startswith(begin):
            continue
        end = b"-----END " + label + b"-----"
        end_offset = contents.find(end, len(begin))
        if end_offset < 0:
            raise _invalid("recognized private identity armor is incomplete")
        armor_suffix = contents[end_offset + len(end) :]
        if any(byte not in _ASCII_WHITESPACE for byte in armor_suffix):
            raise _invalid("recognized private identity has trailing armor data")
        return UnverifiableSSHIdentity()
    raise _invalid("private identity uses an unrecognized envelope")


def _read_bounded_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _unavailable("identity path is not a regular file")
        expected_size = file_stat.st_size
        if expected_size > _MAX_IDENTITY_FILE_BYTES:
            raise _invalid("identity file exceeds the supported size limit")

        remaining = expected_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk or len(chunk) > remaining:
                raise _unavailable("identity file could not be read completely")
            chunks.append(chunk)
            remaining -= len(chunk)

        if os.fstat(descriptor).st_size != expected_size:
            raise _unavailable("identity file changed while it was being read")
        return b"".join(chunks)
    except SSHIdentityReadError:
        raise
    except OSError:
        raise _unavailable("identity file is unavailable") from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _parse_openssh_private_identity(contents: bytes) -> VerifiedSSHIdentity:
    end_offset = contents.find(_OPENSSH_END, len(_OPENSSH_BEGIN))
    if end_offset < 0:
        raise _invalid("OpenSSH private identity armor is incomplete")
    armor_suffix = contents[end_offset + len(_OPENSSH_END) :]
    if any(byte not in _ASCII_WHITESPACE for byte in armor_suffix):
        raise _invalid("OpenSSH private identity has trailing armor data")

    encoded_envelope = contents[len(_OPENSSH_BEGIN) : end_offset].translate(None, _ASCII_WHITESPACE)
    try:
        envelope = base64.b64decode(encoded_envelope, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("OpenSSH private identity contains invalid base64") from None
    if len(envelope) > _MAX_IDENTITY_FILE_BYTES:
        raise _invalid("OpenSSH private identity envelope exceeds the supported size limit")
    if not envelope.startswith(_OPENSSH_MAGIC):
        raise _invalid("OpenSSH private identity has invalid envelope magic")

    reader = _SSHReader(envelope, offset=len(_OPENSSH_MAGIC))
    reader.skip_string("cipher name")
    reader.skip_string("KDF name")
    reader.skip_string("KDF options")
    key_count = reader.read_uint32("public-key count")
    if key_count != 1:
        raise _invalid("OpenSSH private identity must contain exactly one public key")
    public_blob = reader.read_string("public key")
    _public_blob_algorithm(public_blob, stage="OpenSSH private identity public key")
    encrypted_private = reader.read_string("encrypted private section")
    if not encrypted_private:
        raise _invalid("OpenSSH private identity has an empty private section")
    if not reader.exhausted:
        raise _invalid("OpenSSH private identity envelope has trailing data")
    return VerifiedSSHIdentity(fingerprint=fingerprint_public_blob(public_blob))


def _public_blob_algorithm(blob: bytes, *, stage: str) -> str:
    reader = _SSHReader(blob)
    algorithm = reader.read_string(f"{stage} algorithm")
    if not algorithm:
        raise _invalid(f"{stage} has an empty algorithm")
    try:
        return algorithm.decode("ascii")
    except UnicodeDecodeError:
        raise _invalid(f"{stage} algorithm is not ASCII") from None


class _SSHReader:
    def __init__(self, contents: bytes, *, offset: int = 0) -> None:
        self._contents = contents
        self._offset = offset

    @property
    def exhausted(self) -> bool:
        return self._offset == len(self._contents)

    def read_uint32(self, stage: str) -> int:
        end = self._offset + 4
        if end > len(self._contents):
            raise _invalid(f"{stage} is truncated at offset {self._offset}")
        value = int(struct.unpack_from(">I", self._contents, self._offset)[0])
        self._offset = end
        return value

    def read_string(self, stage: str) -> bytes:
        declared_length = self.read_uint32(f"{stage} length")
        remaining = len(self._contents) - self._offset
        if declared_length > remaining or declared_length > _MAX_IDENTITY_FILE_BYTES:
            raise _invalid(
                f"{stage} length {declared_length} exceeds {remaining} available bytes at offset {self._offset}"
            )
        end = self._offset + declared_length
        value = self._contents[self._offset : end]
        self._offset = end
        return value

    def skip_string(self, stage: str) -> None:
        self.read_string(stage)


def _invalid(detail: str) -> SSHIdentityReadError:
    return SSHIdentityReadError("invalid", detail)


def _unavailable(detail: str) -> SSHIdentityReadError:
    return SSHIdentityReadError("unavailable", detail)

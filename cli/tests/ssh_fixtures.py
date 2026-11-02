"""Non-secret SSH identity fixtures shared by tests that cross key boundaries."""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.trust import HostKey

TEST_SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOU/3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv test@agentworks"


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def write_test_ssh_keypair(private_path: Path, *, cipher: bytes = b"none") -> None:
    """Write a matching public key and non-secret structural private carrier.

    A ``cipher`` other than ``none`` makes the envelope look passphrase-protected.
    """
    public_blob = base64.b64decode(TEST_SSH_PUBLIC_KEY.split()[1], validate=True)
    envelope = b"openssh-key-v1\x00"
    envelope += _ssh_string(cipher) + _ssh_string(b"none") + _ssh_string(b"")
    envelope += struct.pack(">I", 1) + _ssh_string(public_blob) + _ssh_string(b"test-only")
    encoded = base64.b64encode(envelope).decode("ascii")
    private_path.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{encoded}\n-----END OPENSSH PRIVATE KEY-----\n")
    private_path.with_name(f"{private_path.name}.pub").write_text(f"{TEST_SSH_PUBLIC_KEY}\n")


def fake_host_key(key_type: str = "ssh-ed25519", *, seed: str = "test") -> HostKey:
    """A well-formed host key (its blob is the type string and 32 bytes) that no real host holds."""
    from agentworks.execution.trust import HostKey

    blob = _ssh_string(key_type.encode("ascii")) + _ssh_string(hashlib.sha256(seed.encode()).digest())
    return HostKey(key_type, base64.b64encode(blob).decode("ascii"))

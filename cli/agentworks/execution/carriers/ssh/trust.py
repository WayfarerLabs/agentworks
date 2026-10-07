"""Explicit SSH trust sources and maintenance of byte-preserving owned copies.

OpenSSH interprets host keys and revocations. This module owns only local file
publication and admission; it never discovers policy, enrolls targets, or resets
trust. Source snapshots must be stable for the duration of import or refresh.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh._trust_files import TrustBusyError as TrustBusyError


def _path(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise ValidationError("SSH trust paths must be absolute native paths without parent traversal")


@dataclass(frozen=True)
class SSHTrustFiles:
    """Explicit read-only policy; construction performs no filesystem access."""

    known_hosts: tuple[Path, ...]
    revoked_host_keys: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.known_hosts, tuple) or not self.known_hosts:
            raise ValidationError("SSH trust requires a nonempty tuple of known-host files")
        for path in self.known_hosts:
            _path(path)
        if self.revoked_host_keys is not None:
            _path(self.revoked_host_keys)


@dataclass(frozen=True)
class ManagedSSHTrust:
    """An owned bundle resolved again whenever an operation is admitted."""

    directory: Path

    def __post_init__(self) -> None:
        _path(self.directory)


@dataclass(frozen=True)
class SSHTrustStatus:
    """Maintenance facts, not permission to use the described generation."""

    generation: str | None
    blocked: bool
    authority: str
    sources: SSHTrustFiles


class TrustBlockedError(StateError):
    """Policy cannot admit another SSH operation."""


class TrustBlockUnprovenError(StateError):
    """Storage failed to establish durable blocking; external quiescence is required."""


@dataclass(frozen=True)
class _Manifest:
    status: SSHTrustStatus
    hashes: tuple[str, ...] = ()
    revoked_hash: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "version": 1,
            "generation": self.status.generation,
            "blocked": self.status.blocked,
            "authority": self.status.authority,
            "known_hosts": [str(path) for path in self.status.sources.known_hosts],
            "revoked_host_keys": (
                str(self.status.sources.revoked_host_keys)
                if self.status.sources.revoked_host_keys is not None
                else None
            ),
            "hashes": list(self.hashes),
            "revoked_hash": self.revoked_hash,
        }


def _authority(authority: str) -> None:
    if not isinstance(authority, str) or not authority.strip() or not authority.isprintable():
        raise ValidationError("SSH trust maintenance requires an explicit authority")


def _load(directory: Path) -> _Manifest:
    """Validate persisted state, which can be partial, corrupt, or from another version."""
    try:
        with files.read_file(directory / "state.json", owned=True) as source:
            raw = source.read(files.MANIFEST_LIMIT + 1)
        if len(raw) > files.MANIFEST_LIMIT:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "version",
            "generation",
            "blocked",
            "authority",
            "known_hosts",
            "revoked_host_keys",
            "hashes",
            "revoked_hash",
        }:
            raise ValueError
        generation = value["generation"]
        blocked = value["blocked"]
        authority = value["authority"]
        known = value["known_hosts"]
        revoked = value["revoked_host_keys"]
        hashes = value["hashes"]
        revoked_hash = value["revoked_hash"]
        if type(value["version"]) is not int or value["version"] != 1 or type(blocked) is not bool:
            raise ValueError
        if generation is not None and (
            not isinstance(generation, str) or re.fullmatch(r"[a-f0-9]{32}", generation) is None
        ):
            raise ValueError
        if not isinstance(known, list) or not all(isinstance(path, str) for path in known):
            raise ValueError
        if revoked is not None and not isinstance(revoked, str):
            raise ValueError
        _authority(authority)
        sources = SSHTrustFiles(tuple(Path(path) for path in known), Path(revoked) if revoked is not None else None)
        if not isinstance(hashes, list) or not all(
            isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes
        ):
            raise ValueError
        if generation is None:
            if not blocked or hashes or revoked_hash is not None:
                raise ValueError
        elif (
            len(hashes) != len(known)
            or (
                revoked_hash is not None
                and (not isinstance(revoked_hash, str) or re.fullmatch(r"[a-f0-9]{64}", revoked_hash) is None)
            )
            or (revoked is None) != (revoked_hash is None)
        ):
            raise ValueError
        return _Manifest(SSHTrustStatus(generation, blocked, authority, sources), tuple(hashes), revoked_hash)
    except (ValueError, TypeError, RecursionError, ValidationError) as error:
        raise TrustBlockedError("SSH trust manifest is malformed; repair complete policy before use") from error


def _blocked(directory: Path, manifest: _Manifest) -> _Manifest:
    blocked = replace(manifest, status=replace(manifest.status, blocked=True))
    try:
        files.write_state(directory, blocked.document())
    except (KeyboardInterrupt, SystemExit) as error:
        error.add_note("SSH trust blocking was interrupted; quiesce new use until maintenance state is verified")
        raise
    except Exception as error:
        raise TrustBlockUnprovenError(
            "SSH trust could not durably record blocked policy; quiesce new use and repair storage"
        ) from error
    return blocked


def _publish(directory: Path, blocked: _Manifest, sources: SSHTrustFiles, authority: str) -> SSHTrustStatus:
    generation = uuid.uuid4().hex
    target = directory / generation
    try:
        target.mkdir(mode=0o700)
        hashes = tuple(
            files.copy_file(path, target / f"known-hosts-{index}") for index, path in enumerate(sources.known_hosts)
        )
        revoked_hash = (
            files.copy_file(sources.revoked_host_keys, target / "revoked-host-keys")
            if sources.revoked_host_keys is not None
            else None
        )
        files.sync_directory(target)
        status = SSHTrustStatus(generation, False, authority, sources)
        files.write_state(directory, _Manifest(status, hashes, revoked_hash).document())
        return status
    except BaseException as error:
        # Publication can replace the manifest and then fail its directory
        # flush. Re-establish blocking even in that uncertain case. Keep every
        # generation and partial file as evidence, including interrupted input.
        try:
            _blocked(directory, blocked)
        except BaseException as blocking_error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                error.add_note("SSH trust blocking could not be established; quiesce new use and repair storage")
                raise error from blocking_error
            raise
        raise


def _expected(manifest: _Manifest, generation: str | None) -> None:
    if manifest.status.generation != generation:
        raise StateError("SSH trust generation changed; inspect current maintenance state before retrying")


def import_trust(directory: Path, *, sources: SSHTrustFiles, authority: str) -> ManagedSSHTrust:
    """Create a new owned destination; failure preserves a blocked partial import."""
    bundle = ManagedSSHTrust(directory)
    _authority(authority)
    try:
        files.create_bundle(directory)
        with files.bundle_lock(directory):
            blocked = _blocked(directory, _Manifest(SSHTrustStatus(None, True, authority, sources)))
            _publish(directory, blocked, sources, authority)
    except OSError as error:
        raise StateError("SSH trust import could not publish owned files; existing evidence was retained") from error
    return bundle


def trust_status(bundle: ManagedSSHTrust) -> SSHTrustStatus:
    """Read maintenance state even while blocked, without admitting policy for use."""
    try:
        with files.bundle_lock(bundle.directory):
            return _load(bundle.directory).status
    except OSError as error:
        raise TrustBlockedError("SSH trust maintenance state is unavailable") from error


def block_trust(bundle: ManagedSSHTrust, *, expected_generation: str | None) -> SSHTrustStatus:
    """Persist refusal when policy is superseded, without deleting the old evidence."""
    try:
        with files.bundle_lock(bundle.directory):
            manifest = _load(bundle.directory)
            _expected(manifest, expected_generation)
            return _blocked(bundle.directory, manifest).status
    except OSError as error:
        raise TrustBlockUnprovenError(
            "SSH trust could not record blocking; quiesce new use and repair storage"
        ) from error


def refresh_trust(
    bundle: ManagedSSHTrust, *, sources: SSHTrustFiles, authority: str, expected_generation: str | None
) -> SSHTrustStatus:
    """Replace complete policy explicitly; no failure reactivates an older generation."""
    _authority(authority)
    try:
        with files.bundle_lock(bundle.directory):
            manifest = _load(bundle.directory)
            _expected(manifest, expected_generation)
            blocked = _blocked(bundle.directory, manifest)
            return _publish(bundle.directory, blocked, sources, authority)
    except OSError as error:
        raise StateError(
            "SSH trust refresh could not complete; inspect maintenance state before further use"
        ) from error


def resolve_trust(trust: SSHTrustFiles | ManagedSSHTrust) -> SSHTrustFiles:
    """Admit one operation; a later refresh never changes its selected generation."""
    try:
        if isinstance(trust, SSHTrustFiles):
            for path in (
                *trust.known_hosts,
                *((trust.revoked_host_keys,) if trust.revoked_host_keys is not None else ()),
            ):
                with files.read_file(path):
                    pass
            return trust
        with files.bundle_lock(trust.directory):
            manifest = _load(trust.directory)
            if manifest.status.blocked or manifest.status.generation is None:
                raise TrustBlockedError("SSH trust policy is blocked pending complete maintenance")
            directory = trust.directory / manifest.status.generation
            files.check_path(directory, directory=True, owned=True)
            known = tuple(directory / f"known-hosts-{index}" for index in range(len(manifest.hashes)))
            for path, expected in zip(known, manifest.hashes, strict=True):
                if files.file_hash(path) != expected:
                    raise TrustBlockedError("SSH known-host policy failed its integrity check")
            revoked = None
            if manifest.revoked_hash is not None:
                revoked = directory / "revoked-host-keys"
                if files.file_hash(revoked) != manifest.revoked_hash:
                    raise TrustBlockedError("SSH revocation policy failed its integrity check")
            return SSHTrustFiles(known, revoked)
    except OSError as error:
        raise TrustBlockedError("SSH trust files are unavailable; refusing new use") from error

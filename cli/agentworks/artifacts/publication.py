"""Guarded whole-file publication of integration-selected artifact destinations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ArtifactFacet, ArtifactInput
from agentworks.errors import StateError
from agentworks.native_files import NativeFiles, native_path
from agentworks.transports import Transport


def validate_application(
    application: object, inputs: tuple[ArtifactInput, ...], facet: ArtifactFacet
) -> ArtifactApplication:
    """Validate the registered plugin's output without accepting new artifact inputs."""
    if not isinstance(application, ArtifactApplication):
        raise StateError("integration did not return an artifact application")
    identities = {item.identity for item in inputs}
    origins = {item.origin.identity for item in inputs}
    routes = {"vm": {"user", "workspace", "session"}, "user": {"session"}, "workspace": {"session"}, "session": set()}
    deferred: set[str] = set()
    paths: set[str] = set()
    if not all(isinstance(value, tuple) for value in (application.files, application.deferred, application.environment)):
        raise StateError("integration returned malformed artifact application sequences")
    for item in application.deferred:
        if not isinstance(item, ArtifactDeferral) or item.input_id not in identities or item.input_id in deferred:
            raise StateError("integration deferred an unknown or duplicate artifact input")
        if item.destination not in routes[facet]:
            if facet == "session":
                raise StateError(
                    "integration cannot handle every artifact at the session facet",
                    hint="Use a supported artifact type or apply it at a supported ancestor facet.",
                )
            raise StateError("integration returned an invalid artifact route")
        deferred.add(item.input_id)
    for item in application.files:
        if not isinstance(item, ArtifactFile) or not isinstance(item.data, bytes) or type(item.executable) is not bool:
            raise StateError("integration returned malformed artifact file content")
        if not isinstance(item.origins, tuple) or not item.origins or not set(item.origins) <= origins:
            raise StateError("integration returned a file with unknown artifact origins")
        path = native_path(item.path)
        if path.casefold() in paths:
            raise StateError("integration returned conflicting artifact destinations")
        paths.add(path.casefold())
    names: set[str] = set()
    for pair in application.environment:
        if not isinstance(pair, tuple) or len(pair) != 2 or not all(isinstance(value, str) for value in pair):
            raise StateError("integration returned malformed artifact environment")
        name, value = pair
        if name != "AGENTWORKS_ARTIFACTS_DIR" or name in names or "\x00" in value or facet != "session":
            raise StateError("integration returned an unsupported artifact environment field")
        names.add(name)
    return application


def publish_artifacts(
    runner: Transport,
    desired: Sequence[ArtifactFile],
    previous: tuple[OwnedArtifactFile, ...],
    checkpoint: Callable[[tuple[OwnedArtifactFile, ...]], None],
    *,
    roots: tuple[str, ...],
    group: str = "",
) -> tuple[OwnedArtifactFile, ...]:
    """Preflight all destinations, then checkpoint each confirmed file change.

    Modified obsolete files remain owned and visible for later cleanup. Neither
    matching bytes nor a familiar filename authorize adopting an unowned file.
    """
    if not desired and not previous:
        return ()
    for item in (*desired, *previous):
        path = native_path(item.path)
        if not any(path.startswith(native_path(root).rstrip("/") + "/") for root in roots):
            raise StateError("artifact destination is outside its owning scope")
    current = {item.path: item for item in previous}
    planned = {item.path: item for item in desired}
    if len(planned) != len(desired) or len(current) != len(previous):
        raise StateError("artifact publication contains duplicate destinations")
    with NativeFiles(runner) as files:
        observed = {path: files.read(path) for path in dict.fromkeys((*planned, *current))}
        for path in planned:
            data = observed[path]
            prior = current.get(path)
            if data is not None and (prior is None or hashlib.sha256(data).hexdigest() != prior.sha256):
                raise StateError("artifact destination is unowned or has been modified; existing content was retained")
        for path, prior in tuple(current.items()):
            if path in planned:
                continue
            data = observed[path]
            if data is not None and hashlib.sha256(data).hexdigest() != prior.sha256:
                output.warn("An obsolete owned artifact was modified; its file and cleanup evidence were retained.")
                continue
            if data is not None:
                files.remove(path, expected=prior.sha256)
            del current[path]
            checkpoint(tuple(current.values()))
        for path, item in planned.items():
            digest = hashlib.sha256(item.data).hexdigest()
            record = OwnedArtifactFile(
                path=path, sha256=digest, origins=item.origins, executable=item.executable, native_identity=item.native_identity
            )
            prior = current.get(path)
            if observed[path] != item.data or prior is None or prior.executable != item.executable:
                files.publish(
                    path,
                    item.data,
                    expected=None if observed[path] is None else hashlib.sha256(observed[path]).hexdigest(),
                    group=group,
                    executable=item.executable,
                )
            if prior != record:
                current[path] = record
                checkpoint(tuple(current.values()))
    return tuple(current.values())

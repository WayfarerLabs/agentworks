"""Guarded whole-file publication of integration-selected artifact destinations."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile, OwnedArtifactFile
from agentworks.artifacts.model import ALLOWED_DEFERRALS
from agentworks.errors import StateError
from agentworks.native_files import NativeFiles, native_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.artifacts.model import ArtifactFacet, ArtifactInputs
    from agentworks.transports import Transport


def validate_application(
    application: object, inputs: ArtifactInputs, facet: ArtifactFacet, *, integration: str
) -> ArtifactApplication:
    """Validate the registered plugin's output without accepting new artifact inputs."""
    if not isinstance(application, ArtifactApplication):
        raise StateError("integration did not return an artifact application")
    identities = {item.identity for item in inputs.items()}
    origins = {item.origin_identity for item in inputs.items()}
    deferred: set[str] = set()
    paths: set[str] = set()
    if not all(isinstance(value, tuple) for value in (application.files, application.deferred)):
        raise StateError("integration returned malformed artifact application sequences")
    for item in application.deferred:
        if not isinstance(item, ArtifactDeferral) or item.input_id not in identities or item.input_id in deferred:
            raise StateError("integration deferred an unknown or duplicate artifact input")
        if item.destination not in ALLOWED_DEFERRALS[facet]:
            if facet == "session":
                artifact = next(value for value in inputs.items() if value.identity == item.input_id)
                origin = artifact.origin
                raise StateError(
                    f"integration '{integration}' cannot apply {artifact.content.type.value} "
                    f"'{origin.bundle}/{origin.entry}' from {origin.component} "
                    f"{origin.resource_kind}/{origin.resource_name} at the session facet",
                    entity_kind=origin.resource_kind,
                    entity_name=origin.resource_name,
                    hint=item.reason,
                )
            raise StateError("integration returned an invalid artifact route")
        deferred.add(item.input_id)
    for file in application.files:
        if not isinstance(file, ArtifactFile) or not isinstance(file.data, bytes) or type(file.executable) is not bool:
            raise StateError("integration returned malformed artifact file content")
        if not isinstance(file.origins, tuple) or not file.origins or not set(file.origins) <= origins:
            raise StateError("integration returned a file with unknown artifact origins")
        path = native_path(file.path)
        if path.casefold() in paths:
            raise StateError("integration returned conflicting artifact destinations")
        paths.add(path.casefold())
    if application.artifacts_dir is not None:
        if not isinstance(application.artifacts_dir, str) or facet != "session":
            raise StateError("integration returned an unsupported artifact directory")
        native_path(application.artifacts_dir)
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
    for destination in [entry.path for entry in desired] + [entry.path for entry in previous]:
        path = native_path(destination)
        if not any(path.startswith(native_path(root).rstrip("/") + "/") for root in roots):
            raise StateError("artifact destination is outside its owning scope")
    current = {item.path: item for item in previous}
    planned = {item.path: item for item in desired}
    if len(planned) != len(desired) or len(current) != len(previous):
        raise StateError("artifact publication contains duplicate destinations")
    with NativeFiles(runner) as files:
        observed = {path: files.fingerprint(path) for path in dict.fromkeys((*planned, *current))}
        for path in planned:
            data = observed[path]
            prior = current.get(path)
            if data is not None and (prior is None or data != (prior.sha256, _mode(prior.executable, group))):
                raise StateError("artifact destination is unowned or has been modified; existing content was retained")
        for path, prior in tuple(current.items()):
            if path in planned:
                continue
            data = observed[path]
            if data is not None and data != (prior.sha256, _mode(prior.executable, group)):
                output.warn("An obsolete owned artifact was modified; its file and cleanup evidence were retained.")
                continue
            if data is not None:
                files.remove(path, expected=prior.sha256)
            del current[path]
            checkpoint(tuple(current.values()))
        for path, item in planned.items():
            digest = hashlib.sha256(item.data).hexdigest()
            record = OwnedArtifactFile(
                path=path,
                sha256=digest,
                origins=item.origins,
                executable=item.executable,
                native_identity=item.native_identity,
            )
            prior = current.get(path)
            observed_file = observed[path]
            if observed_file != (digest, _mode(item.executable, group)):
                files.publish(
                    path,
                    item.data,
                    expected=None if observed_file is None else observed_file[0],
                    group=group,
                    executable=item.executable,
                )
            if prior != record:
                current[path] = record
                checkpoint(tuple(current.values()))
    return tuple(current.values())


def _mode(executable: bool, group: str) -> int:
    return (0o770 if executable else 0o660) if group else (0o700 if executable else 0o600)

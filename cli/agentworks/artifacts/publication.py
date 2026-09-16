"""Guarded publication of integration-selected files and generated sections."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.application import (
    ArtifactApplication,
    ArtifactDeferral,
    ArtifactFile,
    ArtifactPublication,
    ArtifactSkip,
    OwnedArtifactFile,
)
from agentworks.artifacts.model import ALLOWED_DEFERRALS
from agentworks.artifacts.sections import MalformedSectionError, replace_section
from agentworks.errors import StateError
from agentworks.native_files import NativeFiles, native_path, root_native_path

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
            raise StateError(
                f"integration '{integration}' returned artifact route '{item.destination}' at the '{facet}' facet"
            )
        deferred.add(item.input_id)
    for file in application.files:
        if (
            not isinstance(file, ArtifactFile)
            or not isinstance(file.data, bytes)
            or type(file.executable) is not bool
            or type(file.generated_section) is not bool
        ):
            raise StateError("integration returned malformed artifact file content")
        if not isinstance(file.origins, tuple) or not file.origins or not set(file.origins) <= origins:
            raise StateError("integration returned a file with unknown artifact origins")
        path = native_path(file.path)
        _validate_package_root(file)
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
    root: bool = False,
) -> ArtifactPublication:
    """Preflight all destinations, then checkpoint each confirmed file change.

    Modified obsolete whole files remain owned for later cleanup. Whole-file
    adoption requires prior ownership; generated sections use their delimiters
    as the boundary and preserve all surrounding content.
    """
    if not desired and not previous:
        return ArtifactPublication()
    boundaries = tuple(native_path(boundary) + "/" for boundary in roots)
    entries: tuple[ArtifactFile | OwnedArtifactFile, ...] = (*desired, *previous)
    for entry in entries:
        path = root_native_path(entry.path) if root else native_path(entry.path)
        if not path.startswith(boundaries):
            raise StateError("artifact destination is outside its owning scope")
        package_root = _validate_package_root(entry)
        if package_root is not None:
            if root:
                root_native_path(package_root)
            if not package_root.startswith(boundaries):
                raise StateError("artifact package root is outside its owning scope")
    current = {item.path: item for item in previous}
    planned = {item.path: item for item in desired}
    if len(planned) != len(desired) or len(current) != len(previous):
        raise StateError("artifact publication contains duplicate destinations")
    skipped: list[ArtifactSkip] = []
    with NativeFiles(runner, root=root) as files:
        observed = {path: files.fingerprint(path) for path in dict.fromkeys((*planned, *current))}
        for path, item in planned.items():
            prior = current.get(path)
            if observed[path] is not None and prior is not None and prior.generated_section != item.generated_section:
                raise StateError(
                    f"Artifact destination '{path}' cannot change between whole-file and generated-section ownership",
                    hint="Retire the old placement and use a separate destination for the new artifact.",
                )
        sections: dict[str, tuple[bytes | None, bytes]] = {}
        skipped_paths: set[str] = set()
        for path in observed:
            entry = planned.get(path) or current[path]
            if not entry.generated_section:
                continue
            existing = files.read(path)
            body = planned[path].data if path in planned else None
            try:
                replacement = replace_section(existing or b"", body)
            except MalformedSectionError as error:
                reason = f"{error}; repair the delimiters and retry setup"
                skipped.append(ArtifactSkip(path=path, origins=entry.origins, reason=reason))
                skipped_paths.add(path)
                output.warn(f"Skipping artifact destination '{path}': {reason}.")
                continue
            sections[path] = (existing, replacement)
        for path in planned:
            if path in sections or path in skipped_paths:
                continue
            data = observed[path]
            prior = current.get(path)
            if data is not None and (prior is None or data != (prior.sha256, _mode(prior.executable, group, root))):
                raise StateError("artifact destination is unowned or has been modified; existing content was retained")
        retirement = sorted(
            current.items(),
            key=lambda item: (
                _skill_entrypoint_directory(item[1]) is not None,
                -item[0].count("/") if _skill_entrypoint_directory(item[1]) is not None else 0,
            ),
        )
        for path, prior in retirement:
            if path in planned or path in skipped_paths:
                continue
            entrypoint_directory = _skill_entrypoint_directory(prior)
            if entrypoint_directory is not None and any(
                item.path != path and item.path.startswith(entrypoint_directory + "/") for item in current.values()
            ):
                continue  # Keep native discovery valid until every owned supporting member retires.
            if path in sections:
                existing, replacement = sections[path]
                if existing is not None and existing != replacement:
                    files.publish(
                        path, replacement, expected=hashlib.sha256(existing).hexdigest(), preserve_metadata=True
                    )
                del current[path]
                checkpoint(tuple(current.values()))
                continue
            data = observed[path]
            if data is not None and data != (prior.sha256, _mode(prior.executable, group, root)):
                output.warn("An obsolete owned artifact was modified; its file and cleanup evidence were retained.")
                continue
            if data is not None:
                files.remove(path, expected=prior.sha256)
            if prior.package_root is not None:
                files.prune_empty_parents(path, root=prior.package_root)
            del current[path]
            checkpoint(tuple(current.values()))
        publication = sorted(
            planned.items(),
            key=lambda item: (
                _skill_entrypoint_directory(item[1]) is None,
                item[0].count("/") if _skill_entrypoint_directory(item[1]) is not None else 0,
            ),
        )
        for path, item in publication:
            if path in skipped_paths:
                continue
            content = sections[path][1] if path in sections else item.data
            digest = hashlib.sha256(content).hexdigest()
            record = OwnedArtifactFile(
                path=path,
                sha256=digest,
                origins=item.origins,
                executable=item.executable,
                native_identity=item.native_identity,
                package_root=item.package_root,
                generated_section=item.generated_section,
            )
            prior = current.get(path)
            observed_file = observed[path]
            if path in sections:
                existing, replacement = sections[path]
                if existing != replacement:
                    files.publish(
                        path,
                        replacement,
                        expected=None if existing is None else hashlib.sha256(existing).hexdigest(),
                        group=group,
                        executable=item.executable,
                        preserve_metadata=True,
                    )
            elif observed_file != (digest, _mode(item.executable, group, root)):
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
    return ArtifactPublication(tuple(current.values()), tuple(skipped))


def _mode(executable: bool, group: str, root: bool) -> int:
    if root:
        return 0o755 if executable else 0o644
    return (0o770 if executable else 0o660) if group else (0o700 if executable else 0o600)


def _validate_package_root(file: ArtifactFile | OwnedArtifactFile) -> str | None:
    """Validate an integration-supplied cleanup boundary without inferring one from a path."""
    root = file.package_root
    if root is not None:
        if not isinstance(root, str):
            raise StateError("artifact package root must be an absolute normalized path")
        root = native_path(root)
        if not file.path.startswith(root + "/"):
            raise StateError("artifact file is outside its package root")
    return root


def _skill_entrypoint_directory(file: ArtifactFile | OwnedArtifactFile) -> str | None:
    """Find an entrypoint directory for publication and retirement ordering, never pruning."""
    if file.package_root is not None:
        return file.package_root if file.path == file.package_root + "/SKILL.md" else None
    if file.native_identity and file.native_identity.startswith("skill:") and file.path.endswith("/SKILL.md"):
        return file.path.rsplit("/", 1)[0]
    return None

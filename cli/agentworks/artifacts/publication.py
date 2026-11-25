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
from agentworks.artifacts.publication_plan import Placement, held_placements, plan_files
from agentworks.artifacts.sections import MalformedSectionError, replace_section
from agentworks.errors import StateError
from agentworks.execution.errors import PathRefusal, PathRefused
from agentworks.execution.files import Directory, Pruned, RegularFile, guest_path
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.artifacts.model import ArtifactFacet, ArtifactInputs
    from agentworks.execution.files import Files

ROOT_FILE_DIRECTORIES = ("/etc/claude-code", "/etc/codex", "/opt/agentworks/artifacts")
"""Where VM-scope publication may place files as root, shared by every integration.

These are directory boundaries, never file targets: an elevated file and a
removable package root must lie strictly beneath one of them.
"""


def root_path(path: str) -> str:
    """``path`` if an elevated publication may use it; recorded paths are checked as well as plugin output."""
    path = guest_path(path)
    if not any(path.startswith(root + "/") for root in ROOT_FILE_DIRECTORIES):
        raise StateError(f"Elevated native file access is outside the allowed directories: '{path}'")
    return path


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
    if not all(
        isinstance(value, tuple) for value in (application.files, application.deferred, application.shared_paths)
    ):
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
        path = guest_path(file.path)
        _validate_package_root(file)
        if path.casefold() in paths:
            raise StateError("integration returned conflicting artifact destinations")
        paths.add(path.casefold())
    for shared_path in application.shared_paths:
        if facet != "workspace" or not isinstance(shared_path, str):
            raise StateError("integration returned an unsupported shared artifact path")
        path = guest_path(shared_path)
        if path.casefold() in paths:
            raise StateError("integration returned conflicting artifact destinations")
        paths.add(path.casefold())
    if application.artifacts_dir is not None:
        if not isinstance(application.artifacts_dir, str) or facet != "session":
            raise StateError("integration returned an unsupported artifact directory")
        guest_path(application.artifacts_dir)
    return application


def publication_placements(
    files: Sequence[ArtifactFile | OwnedArtifactFile], *, roots: tuple[str, ...], group: str, root: bool
) -> dict[str, Placement]:
    """Validate destinations before either the publisher or the probe observes them."""
    boundaries = tuple(guest_path(boundary) + "/" for boundary in roots)
    result = {}
    for entry in files:
        path = root_path(entry.path) if root else guest_path(entry.path)
        if not path.startswith(boundaries):
            raise StateError("artifact destination is outside its owning scope")
        package_root = _validate_package_root(entry)
        if package_root is not None:
            if root:
                root_path(package_root)
            if not package_root.startswith(boundaries):
                raise StateError("artifact package root is outside its owning scope")
        if path in result:
            raise StateError("artifact publication contains duplicate destinations")
        digest = hashlib.sha256(entry.data).hexdigest() if isinstance(entry, ArtifactFile) else entry.sha256
        result[path] = Placement(
            digest,
            artifact_file_mode(entry.executable, group, root),
            entry.origins,
            entry.native_identity,
            package_root,
            _skill_entrypoint_directory(entry),
            entry.generated_section,
        )
    return result


def publish_artifacts(
    files: Files,
    desired: Sequence[ArtifactFile],
    previous: tuple[OwnedArtifactFile, ...],
    checkpoint: Callable[[tuple[OwnedArtifactFile, ...]], None],
    *,
    roots: tuple[str, ...],
    group: str = "",
    root: bool = False,
    retain_origins: frozenset[str] = frozenset(),
) -> ArtifactPublication:
    """Preflight all destinations, then checkpoint each confirmed file change.

    Modified obsolete whole files remain owned for later cleanup. Matching files
    can be adopted; differing unowned files skip their artifact without blocking
    independent entries. Generated sections preserve all surrounding content.
    Unresolved inherited origins hold their prior placement and related package
    against retirement or replacement. Independent local origins still converge.
    """
    if not desired and not previous:
        return ArtifactPublication()
    wanted = publication_placements(desired, roots=roots, group=group, root=root)
    owned = publication_placements(previous, roots=roots, group=group, root=root)
    current = {item.path: item for item in previous}
    planned = {item.path: item for item in desired}
    held = held_placements(owned, wanted, retain_origins)
    paths = (path for path in dict.fromkeys((*planned, *current)) if path not in held)
    observed = {path: _observe(files, path, elevated=root) for path in paths}
    for path in observed:
        if path not in planned or path not in current:
            continue
        if observed[path] is not None and current[path].generated_section != planned[path].generated_section:
            raise StateError(
                f"Artifact destination '{path}' cannot change between whole-file and generated-section ownership",
                hint="Retire the old placement and use a separate destination for the new artifact.",
            )
    sections: dict[str, tuple[bytes | None, bytes]] = {}
    invalid_sections: dict[str, str] = {}
    for path in observed:
        entry = planned.get(path) or current[path]
        if not entry.generated_section:
            continue
        existing = files.read(path, limit=DEFAULT_CAPTURE_LIMITS.total_bytes, elevated=root)
        body = planned[path].data if path in planned else None
        try:
            replacement = replace_section(existing or b"", body)
        except MalformedSectionError as error:
            invalid_sections[path] = f"{error}; repair the delimiters and retry setup"
            continue
        sections[path] = (existing, replacement)
    decisions = plan_files(owned, wanted, observed, held, invalid_sections)
    for warning in decisions.warnings:
        output.warn(warning)
    skipped = tuple(
        ArtifactSkip(path=path, origins=origins, reason=reason) for path, origins, reason in decisions.skipped
    )
    removed: list[str] = []
    removed_sections: list[str] = []
    for path in decisions.retire:
        retired = current[path]
        if path in sections:
            existing, replacement = sections[path]
            if existing is not None and existing != replacement:
                files.write_if(
                    path,
                    replacement,
                    expected=hashlib.sha256(existing).hexdigest(),
                    mode=artifact_file_mode(False, "", root),
                    parent_mode=_directory_mode("", root),
                    preserve_metadata=True,
                    elevated=root,
                )
                removed_sections.append(path)
            del current[path]
            checkpoint(tuple(current.values()))
            continue
        data = observed[path]
        if data is not None:
            files.remove_if(path, expected=retired.sha256, elevated=root)
            removed.append(path)
        if (
            retired.package_root is not None
            and files.prune_empty_parents(path, root=retired.package_root, elevated=root) is Pruned.ROOT_RETAINED
        ):
            output.warn(
                f"Empty retired package root '{retired.package_root}' was retained "
                "because its parent denies directory removal."
            )
        del current[path]
        checkpoint(tuple(current.values()))
    _report_file_changes("Removed", removed)
    _report_file_changes("Removed", removed_sections, noun="managed artifact section body")
    adopted: list[str] = []
    updated: list[str] = []
    updated_sections: list[str] = []
    for path in decisions.publish:
        item = planned[path]
        content = sections[path][1] if path in sections else item.data
        digest = hashlib.sha256(content).hexdigest() if path in sections else wanted[path].digest
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
                files.write_if(
                    path,
                    replacement,
                    expected=None if existing is None else hashlib.sha256(existing).hexdigest(),
                    mode=artifact_file_mode(item.executable, group, root),
                    parent_mode=_directory_mode(group, root),
                    group=group or None,
                    preserve_metadata=True,
                    elevated=root,
                )
                if prior is not None:
                    updated_sections.append(path)
        elif observed_file != (digest, artifact_file_mode(item.executable, group, root)):
            files.write_if(
                path,
                item.data,
                expected=None if observed_file is None else observed_file[0],
                mode=artifact_file_mode(item.executable, group, root),
                parent_mode=_directory_mode(group, root),
                group=group or None,
                elevated=root,
            )
            if prior is not None:
                updated.append(path)
        if prior != record:
            current[path] = record
            checkpoint(tuple(current.values()))
            if path not in sections and observed_file is not None and (prior is None or path in decisions.modified):
                adopted.append(path)
    _report_file_changes("Updated", updated)
    _report_file_changes("Updated", updated_sections, noun="managed artifact section")
    _report_file_changes("Adopting", adopted, noun="matching artifact file")
    return ArtifactPublication(tuple(current.values()), skipped)


def _report_file_changes(action: str, paths: Sequence[str], *, noun: str = "artifact file") -> None:
    if paths:
        noun += "s" if len(paths) != 1 else ""
        shown = paths if len(paths) <= 3 else (*paths[:2], "...")
        output.info(f"{action} {len(paths)} {noun} ({', '.join(shown)}).")


def _observe(files: Files, path: str, *, elevated: bool) -> tuple[str, int] | None:
    """A destination's digest and mode, ``None`` when absent; a directory where a file belongs is refused."""
    state = files.stat(path, elevated=elevated)
    if isinstance(state, Directory):
        raise PathRefused(files.access.vm_name, "stat", path, PathRefusal.UNSUITABLE)
    return (state.digest, state.mode) if isinstance(state, RegularFile) else None


def artifact_file_mode(executable: bool, group: str, root: bool) -> int:
    """The exact mode used to publish and recognize an unchanged owned file."""
    if root:
        return 0o755 if executable else 0o644
    return (0o770 if executable else 0o660) if group else (0o700 if executable else 0o600)


def _directory_mode(group: str, root: bool) -> int:
    """The mode of a parent directory publication creates: shared with the workspace group, or private."""
    if root:
        return 0o755
    return 0o2770 if group else 0o700


def _validate_package_root(file: ArtifactFile | OwnedArtifactFile) -> str | None:
    """Validate an integration-supplied cleanup boundary without inferring one from a path."""
    root = file.package_root
    if root is not None:
        if not isinstance(root, str):
            raise StateError("artifact package root must be an absolute normalized path")
        root = guest_path(root)
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

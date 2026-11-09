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
        path = guest_path(file.path)
        _validate_package_root(file)
        if path.casefold() in paths:
            raise StateError("integration returned conflicting artifact destinations")
        paths.add(path.casefold())
    if application.artifacts_dir is not None:
        if not isinstance(application.artifacts_dir, str) or facet != "session":
            raise StateError("integration returned an unsupported artifact directory")
        guest_path(application.artifacts_dir)
    return application


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
    boundaries = tuple(guest_path(boundary) + "/" for boundary in roots)
    entries: tuple[ArtifactFile | OwnedArtifactFile, ...] = (*desired, *previous)
    for entry in entries:
        path = root_path(entry.path) if root else guest_path(entry.path)
        if not path.startswith(boundaries):
            raise StateError("artifact destination is outside its owning scope")
        package_root = _validate_package_root(entry)
        if package_root is not None:
            if root:
                root_path(package_root)
            if not package_root.startswith(boundaries):
                raise StateError("artifact package root is outside its owning scope")
    current = {item.path: item for item in previous}
    planned = {item.path: item for item in desired}
    if len(planned) != len(desired) or len(current) != len(previous):
        raise StateError("artifact publication contains duplicate destinations")
    skipped: list[ArtifactSkip] = []
    held_paths = {path for path, item in current.items() if retain_origins.intersection(item.origins)}
    if held_paths:
        retained = tuple(current[path] for path in held_paths)
        prior_origins = {origin for prior in retained for origin in prior.origins}
        prior_identities = {prior.native_identity for prior in retained if prior.native_identity is not None}
        package_roots = {
            package
            for prior in retained
            if (package := prior.package_root or _skill_entrypoint_directory(prior)) is not None
        }
        for item in desired:
            if (
                prior_origins.intersection(item.origins)
                or item.native_identity in prior_identities
                or any(item.path.startswith(package + "/") for package in package_roots)
            ):
                held_paths.add(item.path)
        for path in dict.fromkeys((*current, *planned)):
            if path not in held_paths:
                continue
            origins = (current.get(path) or planned[path]).origins
            if path in planned:
                origins = tuple(dict.fromkeys((*origins, *planned[path].origins)))
            reason = (
                "inherited artifact routes are unresolved; "
                "existing placement is retained until ancestor setup is repaired"
            )
            skipped.append(ArtifactSkip(path=path, origins=origins, reason=reason))
        output.warn("Retaining existing artifact placement while inherited routes are unresolved.")
    paths = tuple(path for path in dict.fromkeys((*planned, *current)) if path not in held_paths)
    if not paths:
        return ArtifactPublication(tuple(current.values()), tuple(skipped))
    observed = {path: _observe(files, path, elevated=root) for path in paths}
    modified = {
        path
        for path, prior in current.items()
        if path not in held_paths
        and observed[path] is not None
        and observed[path] != (prior.sha256, _mode(prior.executable, group, root))
    }
    for path, item in planned.items():
        if path in held_paths:
            continue
        prior = current.get(path)
        if observed[path] is not None and prior is not None and prior.generated_section != item.generated_section:
            raise StateError(
                f"Artifact destination '{path}' cannot change between whole-file and generated-section ownership",
                hint="Retire the old placement and use a separate destination for the new artifact.",
            )
    sections: dict[str, tuple[bytes | None, bytes]] = {}
    skipped_paths = set(held_paths)
    for path in observed:
        entry = planned.get(path) or current[path]
        if not entry.generated_section:
            continue
        section_file = files.read(path, limit=DEFAULT_CAPTURE_LIMITS.total_bytes, elevated=root)
        existing = None if section_file is None else section_file.data
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
    conflicts: dict[str, str] = {}
    for path, item in planned.items():
        if path in sections or path in skipped_paths:
            continue
        data = observed[path]
        prior = current.get(path)
        differences = []
        if data is not None:
            if data[0] != hashlib.sha256(item.data).hexdigest():
                differences.append("content")
            if bool(data[1] & 0o111) != item.executable:
                differences.append("executable intent")
        if differences and (prior is None or path in modified):
            reason = (
                f"existing artifact at '{path}' has different {' and '.join(differences)} from the declaration; "
                "resolve that difference before retrying owning setup"
            )
            for origin in (*item.origins, *(prior.origins if prior is not None else ())):
                conflicts.setdefault(origin, reason)
            output.warn(f"Skipping artifact destination '{path}': {reason}; existing content was retained.")
    requested_origins = {origin for item in planned.values() for origin in item.origins}
    for path, prior in current.items():
        if path in planned or path in sections or path in skipped_paths:
            continue
        if path in modified and requested_origins.intersection(prior.origins):
            reason = (
                f"obsolete member at '{path}' was modified and must be retained; "
                "resolve that difference before retrying owning setup"
            )
            for origin in prior.origins:
                if origin in requested_origins:
                    conflicts.setdefault(origin, reason)
            output.warn(f"Skipping artifact containing '{path}': {reason}.")
    # A skill's supporting files and entrypoint are one artifact. Never
    # publish part of a package when another member must be preserved.
    for path in observed:
        entry = planned.get(path) or current[path]
        conflict_reason = next((conflicts[origin] for origin in entry.origins if origin in conflicts), None)
        if conflict_reason is not None and path not in skipped_paths:
            skipped.append(ArtifactSkip(path=path, origins=entry.origins, reason=conflict_reason))
            skipped_paths.add(path)
    retirement = sorted(
        current.items(),
        key=lambda item: (
            _skill_entrypoint_directory(item[1]) is not None,
            -item[0].count("/") if _skill_entrypoint_directory(item[1]) is not None else 0,
        ),
    )
    removed: list[str] = []
    removed_sections: list[str] = []
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
                files.write_if(
                    path,
                    replacement,
                    expected=hashlib.sha256(existing).hexdigest(),
                    mode=_mode(False, "", root),
                    parent_mode=_directory_mode("", root),
                    preserve_metadata=True,
                    elevated=root,
                )
                removed_sections.append(path)
            del current[path]
            checkpoint(tuple(current.values()))
            continue
        data = observed[path]
        if path in modified:
            output.warn(f"Obsolete owned artifact '{path}' was modified; its file and cleanup evidence were retained.")
            continue
        if data is not None:
            files.remove_if(path, expected=prior.sha256, elevated=root)
            removed.append(path)
        if (
            prior.package_root is not None
            and files.prune_empty_parents(path, root=prior.package_root, elevated=root) is Pruned.ROOT_RETAINED
        ):
            output.warn(
                f"Empty retired package root '{prior.package_root}' was retained "
                "because its parent denies directory removal."
            )
        del current[path]
        checkpoint(tuple(current.values()))
    _report_file_changes("Removed", removed)
    _report_file_changes("Removed", removed_sections, noun="managed artifact section body")
    publication = sorted(
        planned.items(),
        key=lambda item: (
            _skill_entrypoint_directory(item[1]) is None,
            item[0].count("/") if _skill_entrypoint_directory(item[1]) is not None else 0,
        ),
    )
    adopted: list[str] = []
    updated: list[str] = []
    updated_sections: list[str] = []
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
                files.write_if(
                    path,
                    replacement,
                    expected=None if existing is None else hashlib.sha256(existing).hexdigest(),
                    mode=_mode(item.executable, group, root),
                    parent_mode=_directory_mode(group, root),
                    group=group or None,
                    preserve_metadata=True,
                    elevated=root,
                )
                if prior is not None:
                    updated_sections.append(path)
        elif observed_file != (digest, _mode(item.executable, group, root)):
            files.write_if(
                path,
                item.data,
                expected=None if observed_file is None else observed_file[0],
                mode=_mode(item.executable, group, root),
                parent_mode=_directory_mode(group, root),
                group=group or None,
                elevated=root,
            )
            if prior is not None:
                updated.append(path)
        if prior != record:
            current[path] = record
            checkpoint(tuple(current.values()))
            if path not in sections and observed_file is not None and (prior is None or path in modified):
                adopted.append(path)
    _report_file_changes("Updated", updated)
    _report_file_changes("Updated", updated_sections, noun="managed artifact section")
    _report_file_changes("Adopting", adopted, noun="matching artifact file")
    return ArtifactPublication(tuple(current.values()), tuple(skipped))


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


def _mode(executable: bool, group: str, root: bool) -> int:
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

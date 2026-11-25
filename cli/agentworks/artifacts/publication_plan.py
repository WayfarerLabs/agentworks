"""Read-only file decisions shared by the host publisher and the guest catalog probe.

The guest executes this module's source using its system Python and standard library.
Content stays with the caller. Decisions use the same file digests and permission bits
on both sides.
"""

from __future__ import annotations

from typing import NamedTuple


class Placement(NamedTuple):
    """Comparable declaration or ownership evidence for one file."""

    digest: str
    mode: int
    origins: tuple[str, ...]
    native_identity: str | None
    package_root: str | None
    marker_directory: str | None
    section: bool


class Skip(NamedTuple):
    path: str
    origins: tuple[str, ...]
    reason: str


class FilePlan(NamedTuple):
    retire: list[str]
    publish: list[str]
    modified: set[str]
    skipped: list[Skip]
    warnings: list[str]


def held_placements(
    previous: dict[str, Placement], desired: dict[str, Placement], retain_origins: frozenset[str]
) -> dict[str, Skip]:
    held = {path for path, item in previous.items() if retain_origins.intersection(item.origins)}
    if not held:
        return {}
    retained = [previous[path] for path in held]
    origins = {origin for item in retained for origin in item.origins}
    identities = {item.native_identity for item in retained if item.native_identity is not None}
    packages = {root for item in retained if (root := item.package_root or item.marker_directory) is not None}
    held.update(
        path
        for path, item in desired.items()
        if origins.intersection(item.origins)
        or item.native_identity in identities
        or any(path.startswith(package + "/") for package in packages)
    )
    reason = "inherited artifact routes are unresolved; existing placement is retained until ancestor setup is repaired"
    skips = {}
    for path in dict.fromkeys((*previous, *desired)):
        if path in held:
            related = (previous.get(path) or desired[path]).origins
            if path in desired:
                related = tuple(dict.fromkeys((*related, *desired[path].origins)))
            skips[path] = Skip(path, related, reason)
    return skips


def plan_files(
    previous: dict[str, Placement],
    desired: dict[str, Placement],
    observed: dict[str, tuple[str, int] | None],
    held: dict[str, Skip],
    invalid_sections: dict[str, str],
) -> FilePlan:
    """Select the exact guarded retirement and publication order, before any writes."""
    skips = held.copy()
    warnings = ["Retaining existing artifact placement while inherited routes are unresolved."] if held else []
    modified = {
        path
        for path, prior in previous.items()
        if path not in held and observed[path] is not None and observed[path] != (prior.digest, prior.mode)
    }
    for path, reason in invalid_sections.items():
        skips[path] = Skip(path, (desired.get(path) or previous[path]).origins, reason)
        warnings.append(f"Skipping artifact destination '{path}': {reason}.")
    conflicts: dict[str, str] = {}
    for path, item in desired.items():
        if path in skips or item.section:
            continue
        data = observed[path]
        prior = previous.get(path)
        differences = []
        if data is not None:
            if data[0] != item.digest:
                differences.append("content")
            if bool(data[1] & 0o111) != bool(item.mode & 0o111):
                differences.append("executable intent")
        if differences and (prior is None or path in modified):
            reason = (
                f"existing artifact at '{path}' has different {' and '.join(differences)} from the declaration; "
                "resolve that difference before retrying owning setup"
            )
            for origin in (*item.origins, *(prior.origins if prior is not None else ())):
                conflicts.setdefault(origin, reason)
            warnings.append(f"Skipping artifact destination '{path}': {reason}; existing content was retained.")
    requested = {origin for item in desired.values() for origin in item.origins}
    for path, prior in previous.items():
        if path in desired or prior.section or path in skips:
            continue
        if path in modified and requested.intersection(prior.origins):
            reason = (
                f"obsolete member at '{path}' was modified and must be retained; "
                "resolve that difference before retrying owning setup"
            )
            for origin in prior.origins:
                if origin in requested:
                    conflicts.setdefault(origin, reason)
            warnings.append(f"Skipping artifact containing '{path}': {reason}.")
    for path in observed:
        item = desired.get(path) or previous[path]
        conflict = next((conflicts[origin] for origin in item.origins if origin in conflicts), None)
        if conflict is not None and path not in skips:
            skips[path] = Skip(path, item.origins, conflict)

    remaining = set(previous)
    retire = []
    for path in sorted(
        previous,
        key=lambda path: (
            previous[path].marker_directory is not None,
            -path.count("/") if previous[path].marker_directory is not None else 0,
        ),
    ):
        if path in desired or path in skips:
            continue
        directory = previous[path].marker_directory
        if directory is not None and any(other != path and other.startswith(directory + "/") for other in remaining):
            continue
        if path in modified and not previous[path].section:
            warnings.append(
                f"Obsolete owned artifact '{path}' was modified; its file and cleanup evidence were retained."
            )
            continue
        retire.append(path)
        remaining.remove(path)
    publish = sorted(
        (path for path in desired if path not in skips),
        key=lambda path: (
            desired[path].marker_directory is None,
            path.count("/") if desired[path].marker_directory is not None else 0,
        ),
    )
    return FilePlan(retire, publish, modified, list(skips.values()), warnings)

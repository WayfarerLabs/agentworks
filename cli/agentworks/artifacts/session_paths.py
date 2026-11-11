"""Bound session-owned native packages without widening publication to HOME."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.artifacts.names import is_artifact_name
from agentworks.errors import StateError
from agentworks.native_files import native_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.artifacts.application import ArtifactFile, OwnedArtifactFile


def session_package_prefix(session_uuid: str, run_id: str | None = None) -> str:
    """Keep a bounded ownership namespace independent of a reusable display name."""
    prefix = "agw-s-" + hashlib.sha256(session_uuid.encode()).hexdigest()[:32] + "-"
    return prefix if run_id is None else prefix + hashlib.sha256(run_id.encode()).hexdigest()[:8] + "-"


def session_publication_roots(
    files: Iterable[ArtifactFile | OwnedArtifactFile],
    *,
    home: str,
    session_uuid: str,
    directory: str,
    run_id: str | None = None,
) -> tuple[str, ...]:
    """Validate integration plans or persisted ownership before filesystem access.

    Outside the private directory, only a standalone skill package whose native
    identity and directory carry this session's namespace is authorized.
    """
    native_path(home)
    roots = [native_path(directory)]
    prefix = session_package_prefix(session_uuid, run_id)
    entry = session_package_prefix(session_uuid) + "entry"
    for file in files:
        path = native_path(file.path)
        if path.startswith(directory + "/"):
            continue
        package = file.package_root
        if package is not None:
            package = native_path(package)
            name = PurePosixPath(package).name
            parent = str(PurePosixPath(package).parent)
            if (
                package.startswith(home + "/")
                and parent.startswith(home + "/")
                and (name.startswith(prefix) or name == entry)
                and is_artifact_name(name)
                and path.startswith(package + "/")
                and file.native_identity == f"skill:{name}"
                and not file.generated_section
            ):
                if parent not in roots:
                    roots.append(parent)
                continue
        raise StateError(
            "session artifact publication requires its private directory or an owned session skill package"
        )
    return tuple(roots)

"""Locally exclude confirmed workspace outputs with ordinary association ownership."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ExternalError, StateError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles, native_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.transports import Transport

_ROLE = "artifact-git-exclude"
_METADATA = """
import json, pathlib, subprocess, sys
r = json.load(sys.stdin)
p = subprocess.run(['git', '-C', r['workspace'], 'rev-parse', '--path-format=absolute',
                    '--show-toplevel', '--git-path', 'info/exclude'], capture_output=True, text=True)
if p.returncode:
    print(json.dumps({'repository': None, 'exclude': None, 'tracked': []}))
    raise SystemExit(0)
repository, exclude = p.stdout.splitlines()
tracked = set()
for start in range(0, len(r['paths']), 64):
    p = subprocess.run(['git', '-C', r['workspace'], 'ls-files', '-z', '--',
                        *r['paths'][start:start + 64]], capture_output=True)
    if p.returncode:
        raise SystemExit(p.returncode)
    tracked.update(str(pathlib.Path(repository) / path.decode()) for path in p.stdout.split(b'\\0') if path)
print(json.dumps({'repository': repository, 'exclude': str(pathlib.Path(exclude)),
                  'tracked': sorted(tracked)}))
"""


def _pattern(path: str, repository: str) -> str:
    relative = path.removeprefix(repository + "/")
    return "/" + "".join("\\" + char if char in "\\*?[]#! " else char for char in relative)


def _pair(claim: NativeClaim, integration: str) -> bytes:
    identity = json.dumps([claim.source, integration, claim.identifier], separators=(",", ":"))
    marker = hashlib.sha256(identity.encode()).hexdigest()
    return f"# agentworks artifact {marker}\n{claim.identifier}\n".encode()


def reconcile_workspace_excludes(
    runner: Transport,
    workspace: str,
    integration: str,
    files: Sequence[OwnedArtifactFile],
    claims: tuple[NativeClaim, ...],
    checkpoint: Callable[[tuple[NativeClaim, ...]], None],
    *,
    group: str,
    report_incomplete: Callable[[str], None],
) -> None:
    """Own only our exact marker/pattern pairs, preserving all unrelated lines.

    Claims cross persisted state, Git metadata crosses the guest boundary, and
    native file writes use a compare-and-publish guard against concurrent edits.
    """
    prior = tuple(claim for claim in claims if claim.role == _ROLE)
    paths = tuple(file.path for file in files if not file.generated_section)
    if not paths and not prior:
        return
    request = json.dumps({"workspace": workspace, "paths": paths})
    result = runner.run(shlex.join(["python3", "-c", _METADATA]), input_data=request, check=False, tty=False)
    try:
        observed = json.loads(result.stdout)
        repository, destination, tracked = observed["repository"], observed["exclude"], observed["tracked"]
        if result.returncode or not isinstance(tracked, list) or any(not isinstance(path, str) for path in tracked):
            raise ValueError()
        if repository is None and destination is None:
            if prior:
                output.warn("Workspace no longer resolves Git metadata; prior artifact exclusions were retained.")
            return
        repository, destination = native_path(repository), native_path(destination)
        if not (workspace == repository or workspace.startswith(repository + "/")):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ExternalError("Git metadata for workspace artifact exclusions is unavailable") from None
    for claim in prior:
        # Decode only our canonical literal pattern spelling, never arbitrary glob input.
        path = repository + "/" + re.sub(r"\\(.)", r"\1", claim.identifier.removeprefix("/"))
        if (
            claim.source != workspace
            or claim.destination != destination
            or not native_path(path).startswith(workspace + "/")
            or claim.identifier != _pattern(path, repository)
        ):
            raise StateError("stored workspace artifact exclusion ownership does not match its Git metadata")
    selected = tuple(path for path in paths if path not in tracked)
    for path in paths:
        if path in tracked:
            output.warn(f"Artifact '{path}' is tracked by Git; local exclusions cannot hide its changes.")
    desired = tuple(
        NativeClaim(role=_ROLE, identifier=_pattern(path, repository), destination=destination, source=workspace)
        for path in dict.fromkeys(selected)
    )
    remaining = list(claims)
    with NativeFiles(runner) as native:
        current = native.read(destination)
        for claim in (*prior, *(claim for claim in desired if claim not in prior)):
            pair = _pair(claim, integration)
            existing = current or b""
            if existing.count(pair) > 1:
                raise StateError("workspace artifact exclusion ownership markers are duplicated")
            if pair not in existing and pair.split(b"\n", 1)[0] in existing:
                report_incomplete("An owned artifact exclusion was modified; its bytes and ownership were retained.")
                continue
            if claim in desired:
                replacement = (
                    existing
                    if pair in existing
                    else existing + (b"" if not existing or existing.endswith(b"\n") else b"\n") + pair
                )
            else:
                replacement = existing.replace(pair, b"", 1)
            if replacement != existing:
                native.publish(
                    destination,
                    replacement,
                    expected=None if current is None else hashlib.sha256(current).hexdigest(),
                    group=group,
                    preserve_metadata=current is not None,
                )
                current = replacement
            updated = [item for item in remaining if item != claim]
            if claim in desired:
                updated.append(claim)
            if updated != remaining:
                checkpoint(tuple(updated))
                remaining = updated

"""Locally exclude confirmed workspace outputs with ordinary association ownership."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.config_files import publish_config
from agentworks.errors import ExternalError, StateError
from agentworks.execution.files import guest_path
from agentworks.execution.requests import Bytes, PythonProgram
from agentworks.harness_setup.model import NativeClaim
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.execution.access import VMAccess

GIT_EXCLUDE_ROLE = "artifact-git-exclude"
METADATA_DEADLINE_SECONDS = 30
"""How long the Git metadata read may take.

The read is bounded so an unresponsive guest cannot stall the operation
around it. A timeout is ``ExecutionIncomplete``: setup records the
integration's setup as failed, and workspace deletion and rehome record the
exclusion as incomplete and carry on.
"""
_METADATA = """
import json, pathlib, subprocess, sys
r = json.load(sys.stdin)
p = subprocess.run(['git', '-C', r['workspace'], 'rev-parse', '--path-format=absolute',
                    '--show-prefix', '--git-path', 'info/exclude'], capture_output=True, text=True)
if p.returncode:
    print(json.dumps({'prefix': None, 'exclude': None, 'tracked': [], 'error': p.stderr.strip()}))
    raise SystemExit(0)
prefix, exclude = p.stdout.splitlines()
tracked = set()
for start in range(0, len(r['paths']), 64):
    p = subprocess.run(['git', '-c', 'core.fsmonitor=false', '--literal-pathspecs',
                        '-C', r['workspace'], 'ls-files', '-z', '--',
                        *r['paths'][start:start + 64]], capture_output=True)
    if p.returncode:
        raise SystemExit(p.returncode)
    tracked.update(path.decode() for path in p.stdout.split(b'\\0') if path)
print(json.dumps({'prefix': prefix, 'exclude': str(pathlib.Path(exclude)), 'tracked': sorted(tracked)}))
"""


def _pattern(relative: str, prefix: str) -> str:
    return "/" + "".join("\\" + char if char in "\\*?[]#! " else char for char in prefix + relative)


def _pair(claim: NativeClaim, integration: str) -> bytes:
    identity = json.dumps([claim.source, integration, claim.identifier], separators=(",", ":"))
    marker = hashlib.sha256(identity.encode()).hexdigest()
    return f"# agentworks artifact {marker}\n{claim.identifier}\n".encode()


def _relative(claim: NativeClaim, workspace: str, prefix: str) -> str | None:
    """Decode only a persisted current-owner claim's canonical literal path."""
    decoded = re.sub(r"\\(.)", r"\1", claim.identifier.removeprefix("/"))
    relative = decoded.removeprefix(prefix)
    try:
        if (
            claim.source == workspace
            and decoded.startswith(prefix)
            and claim.identifier == _pattern(relative, prefix)
            and guest_path(workspace + "/" + relative).startswith(workspace + "/")
        ):
            return relative
    except StateError:
        pass
    return None


def _metadata(
    access: VMAccess,
    workspace: str,
    paths: Sequence[str],
    unavailable: Callable[[str], None],
    redactions: Iterable[str],
) -> tuple[str, str, list[str]] | None:
    request = json.dumps({"workspace": workspace, "paths": paths}).encode()
    result = access.run(PythonProgram(_METADATA, stdin=Bytes(request), deadline=METADATA_DEADLINE_SECONDS))
    status = result.require_completed(access.vm_name)
    unavailable_reason = ""
    try:
        observed = json.loads(result.stdout)
        prefix, destination, tracked = observed["prefix"], observed["exclude"], observed["tracked"]
        if status or not isinstance(tracked, list) or any(not isinstance(path, str) for path in tracked):
            raise ValueError()
        if prefix is None and destination is None:
            reason = observed.get("error")
            if not isinstance(reason, str) or not reason:
                raise ValueError()
            unavailable_reason = reason
        else:
            if not isinstance(destination, str) or not isinstance(prefix, str) or (prefix and not prefix.endswith("/")):
                raise ValueError()
            if prefix:
                guest_path("/" + prefix.removesuffix("/"))
            for path in tracked:
                guest_path("/" + path)
            return prefix, guest_path(destination), tracked
    except (ValueError, KeyError, TypeError, StateError):
        raise ExternalError(f"Git metadata for workspace artifact exclusions is unavailable at '{workspace}'") from None
    unavailable(
        sanitize_setup_diagnostic(
            f"Git could not resolve artifact exclusion metadata for '{workspace}': {unavailable_reason}",
            redactions=redactions,
        )
    )
    return None


def reconcile_workspace_excludes(
    access: VMAccess,
    workspace: str,
    integration: str,
    files: Sequence[OwnedArtifactFile],
    claims: tuple[NativeClaim, ...],
    checkpoint: Callable[[tuple[NativeClaim, ...]], None],
    *,
    group: str,
    report_incomplete: Callable[[str], None],
    redactions: Iterable[str],
    prior_workspace: str | None = None,
    shared_paths: Sequence[str] = (),
) -> None:
    """Own only our exact marker/pattern pairs, preserving all unrelated lines.

    Claims cross persisted state, Git metadata crosses the guest boundary, and
    native file writes use a compare-and-publish guard against concurrent edits.
    Only Git's current destination authorizes I/O, never a stored destination.
    ``redactions`` are resolved values to keep out of Git's diagnostics.
    Rehome alone supplies its verified old row path as prior_workspace; saved
    claims never select another source or authorize a migration by themselves.
    """
    prior = tuple(claim for claim in claims if claim.role == GIT_EXCLUDE_ROLE)
    workspace = guest_path(workspace)
    paths = tuple(file.path for file in files if not file.generated_section) + tuple(shared_paths)
    if not paths and not prior:
        return
    if any(not path.startswith(workspace + "/") for path in paths):
        raise StateError("workspace artifact exclusion is outside its workspace")
    relative_paths = tuple(path.removeprefix(workspace + "/") for path in paths)
    source_workspace = workspace
    source_prefix = None
    if prior_workspace is not None and prior:
        source_workspace = guest_path(prior_workspace)
        source_metadata = _metadata(access, source_workspace, (), report_incomplete, redactions)
        if source_metadata is None:
            return
        source_prefix = source_metadata[0]
        # Include verified claim paths for shared files and incomplete inventories.
        relative_paths = tuple(
            dict.fromkeys(
                (
                    *relative_paths,
                    *(
                        relative
                        for claim in prior
                        if (relative := _relative(claim, source_workspace, source_prefix)) is not None
                    ),
                )
            )
        )
    metadata = _metadata(access, workspace, relative_paths, report_incomplete if prior else output.warn, redactions)
    if metadata is None:
        return
    prefix, destination, tracked = metadata
    if source_prefix is None:
        source_prefix = prefix

    remaining = list(claims)
    rebound = []
    for claim in prior:
        index = remaining.index(claim)
        remaining.pop(index)
        if _relative(claim, source_workspace, source_prefix) is None:
            output.warn(
                sanitize_setup_diagnostic(
                    f"Stored artifact exclusion '{claim.identifier}' at recorded path '{claim.destination}' "
                    "no longer matches this workspace; its association was released.",
                    redactions=redactions,
                )
            )
            continue
        current_claim = NativeClaim(
            role=GIT_EXCLUDE_ROLE, identifier=claim.identifier, destination=destination, source=source_workspace
        )
        if claim.destination != destination:
            output.warn("Git metadata moved; artifact exclusion ownership now uses Git's current destination.")
        if current_claim not in remaining:
            remaining.insert(index, current_claim)
        if current_claim not in rebound:
            rebound.append(current_claim)
    if remaining != list(claims):
        checkpoint(tuple(remaining))
    prior = tuple(rebound)
    selected = tuple(path for path in relative_paths if path not in tracked)
    for path in relative_paths:
        if path in tracked:
            output.warn(f"Artifact '{workspace}/{path}' is tracked by Git; local exclusions cannot hide its changes.")
    desired = tuple(
        NativeClaim(role=GIT_EXCLUDE_ROLE, identifier=_pattern(path, prefix), destination=destination, source=workspace)
        for path in dict.fromkeys(selected)
    )
    if not prior and not desired:
        return
    guest = access.files()
    # The read keeps Files' default capture limit (1 MiB). A larger exclude file
    # is ExecutionIncomplete naming that limit, handled as a timeout is (see
    # METADATA_DEADLINE_SECONDS), and nothing rewrites a file not read whole.
    current = guest.read(destination)
    for claim in (*prior, *(claim for claim in desired if claim not in prior)):
        if claim in desired and any(
            previous != claim
            and (previous.role, previous.identifier, previous.destination)
            == (claim.role, claim.identifier, claim.destination)
            for previous in remaining
        ):
            report_incomplete(
                f"Artifact exclusion '{claim.identifier}' in '{destination}' retains unresolved prior ownership; "
                "the replacement was skipped."
            )
            continue
        pair = _pair(claim, integration)
        existing = current or b""
        marker = pair.split(b"\n", 1)[0]
        positions = tuple(match.start() for match in re.finditer(re.escape(marker), existing))
        if any(
            (start != 0 and existing[start - 1 : start] != b"\n") or not existing.startswith(pair, start)
            for start in positions
        ):
            report_incomplete(
                f"An owned artifact exclusion in '{destination}' was modified; its bytes and ownership were retained."
            )
            continue
        if len(positions) > 1:
            raise StateError(f"workspace artifact exclusion ownership markers are duplicated in '{destination}'")
        if claim in desired:
            replacement = (
                existing
                if positions
                else existing + (b"" if not existing or existing.endswith(b"\n") else b"\n") + pair
            )
        elif positions:
            start = positions[0]
            replacement = existing[:start] + existing[start + len(pair) :]
        else:
            replacement = existing
        if replacement != existing:
            publish_config(
                guest,
                destination,
                replacement,
                observed=current,
                group=group,
                operation="artifact exclusion",
                preserve_metadata=current is not None,
            )
            current = replacement
        if claim in desired:
            updated = remaining if claim in remaining else [*remaining, claim]
        else:
            updated = [item for item in remaining if item != claim]
        if updated != remaining:
            checkpoint(tuple(updated))
            remaining = updated

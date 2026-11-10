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
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.native_files import NativeFiles, native_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.transports import Transport

GIT_EXCLUDE_ROLE = "artifact-git-exclude"
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


def _metadata(
    runner: Transport,
    workspace: str,
    paths: Sequence[str],
    unavailable: Callable[[str], None],
) -> tuple[str, str, list[str]] | None:
    request = json.dumps({"workspace": workspace, "paths": paths})
    result = runner.run(shlex.join(["python3", "-c", _METADATA]), input_data=request, check=False, tty=False)
    unavailable_reason = ""
    try:
        observed = json.loads(result.stdout)
        prefix, destination, tracked = observed["prefix"], observed["exclude"], observed["tracked"]
        if result.returncode or not isinstance(tracked, list) or any(not isinstance(path, str) for path in tracked):
            raise ValueError()
        if prefix is None and destination is None:
            reason = observed.get("error")
            if not isinstance(reason, str) or not reason:
                raise ValueError()
            unavailable_reason = reason
        else:
            if not isinstance(prefix, str) or (prefix and not prefix.endswith("/")):
                raise ValueError()
            if prefix:
                native_path("/" + prefix.removesuffix("/"))
            for path in tracked:
                native_path("/" + path)
            return prefix, native_path(destination), tracked
    except (ValueError, KeyError, TypeError, StateError):
        raise ExternalError(f"Git metadata for workspace artifact exclusions is unavailable at '{workspace}'") from None
    unavailable(
        sanitize_setup_diagnostic(
            f"Git could not resolve artifact exclusion metadata for '{workspace}': {unavailable_reason}",
            logger=runner.logger,
        )
    )
    return None


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
    prior_workspace: str | None = None,
) -> None:
    """Own only our exact marker/pattern pairs, preserving all unrelated lines.

    Claims cross persisted state, Git metadata crosses the guest boundary, and
    native file writes use a compare-and-publish guard against concurrent edits.
    Only Git's current destination authorizes I/O, never a stored destination.
    Rehome alone supplies its verified old row path as prior_workspace; saved
    claims never select another source or authorize a migration by themselves.
    """
    prior = tuple(claim for claim in claims if claim.role == GIT_EXCLUDE_ROLE)
    workspace = native_path(workspace)
    paths = tuple(file.path for file in files if not file.generated_section)
    if not paths and not prior:
        return
    if any(not path.startswith(workspace + "/") for path in paths):
        raise StateError("workspace artifact exclusion is outside its workspace")
    relative_paths = tuple(path.removeprefix(workspace + "/") for path in paths)
    metadata = _metadata(runner, workspace, relative_paths, report_incomplete if prior else output.warn)
    if metadata is None:
        return
    prefix, destination, tracked = metadata
    source_workspace, source_prefix = workspace, prefix
    if prior_workspace is not None and prior:
        source_workspace = native_path(prior_workspace)
        source_metadata = _metadata(runner, source_workspace, (), report_incomplete)
        if source_metadata is None:
            return
        source_prefix = source_metadata[0]

    remaining = list(claims)
    rebound = []
    for claim in prior:
        # Decode only our canonical literal spelling under the source's current Git prefix.
        decoded = re.sub(r"\\(.)", r"\1", claim.identifier.removeprefix("/"))
        relative = decoded.removeprefix(source_prefix)
        try:
            valid = (
                claim.source == source_workspace
                and decoded.startswith(source_prefix)
                and claim.identifier == _pattern(relative, source_prefix)
                and native_path(source_workspace + "/" + relative).startswith(source_workspace + "/")
            )
        except StateError:
            valid = False
        index = remaining.index(claim)
        remaining.pop(index)
        if not valid:
            output.warn("Stored artifact exclusion no longer matches this workspace; its association was released.")
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
    with NativeFiles(runner) as native:
        current = native.read(destination)
        for claim in (*prior, *(claim for claim in desired if claim not in prior)):
            pair = _pair(claim, integration)
            existing = current or b""
            marker = pair.split(b"\n", 1)[0]
            positions = tuple(match.start() for match in re.finditer(re.escape(marker), existing))
            if any(
                (start != 0 and existing[start - 1 : start] != b"\n") or not existing.startswith(pair, start)
                for start in positions
            ):
                report_incomplete(
                    f"An owned artifact exclusion in '{destination}' was modified; "
                    "its bytes and ownership were retained."
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
                native.publish(
                    destination,
                    replacement,
                    expected=None if current is None else hashlib.sha256(current).hexdigest(),
                    group=group,
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

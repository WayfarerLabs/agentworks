"""Source reference resolution for fetching files or directories from local or remote sources.

Supports Terraform-style source references:
  - Local path:  ~/.config/agentworks/mise.lock (or file::~/.config/...)
  - Git repo:    git::https://github.com/user/repo.git
  - Git + path:  git::https://github.com/user/repo.git//path/to/file
  - Git + ref:   git::https://github.com/user/repo.git//path/to/file?ref=main

Two fetch modes:
  - fetch_file: fetches a single file (e.g., mise lockfile)
  - fetch_dir:  fetches a directory (e.g., dotfiles repo); for git sources,
    clones the repo to the destination with pull-on-reinit support
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import ExternalError

if TYPE_CHECKING:
    from agentworks.ssh import ExecTarget, SSHLogger


class SourceRefError(ExternalError):
    """Raised when a source reference is invalid."""


# Ref must be safe for shell and git: alphanumeric, hyphens, dots, underscores, slashes
_SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")


@dataclass(frozen=True)
class SourceRef:
    """Parsed source reference."""

    kind: str  # "file" or "git"
    path: str  # local path (for file) or repo URL (for git)
    subpath: str  # file within repo (git only), empty for file sources
    ref: str  # git ref (branch/tag/commit), empty string = default branch


def parse_source_ref(source: str, *, default_filename: str = "") -> SourceRef:
    """Parse a source reference string into a SourceRef.

    Args:
        source: The source reference string.
        default_filename: Default filename when git source has no subpath.
            If empty and no subpath is provided, the subpath will be empty.

    Raises:
        SourceRefError: If the source reference is malformed.
    """
    if not source:
        raise SourceRefError("source reference cannot be empty")

    # Strip file:: prefix
    if source.startswith("file::"):
        return SourceRef(kind="file", path=source[6:], subpath="", ref="")

    # Git source
    if source.startswith("git::"):
        return _parse_git_source(source[5:], default_filename)

    # Default: local file
    return SourceRef(kind="file", path=source, subpath="", ref="")


def _parse_git_source(url_str: str, default_filename: str) -> SourceRef:
    """Parse the URL portion of a git:: source reference."""
    if not url_str:
        raise SourceRefError("git source URL cannot be empty")

    # Split on // to separate URL from subpath
    subpath = ""
    if "//" in url_str:
        # Find the first // that is NOT part of https://
        # Strategy: split on //, skip the protocol portion
        parts = url_str.split("//")
        if len(parts) >= 3:
            # e.g. ["https:", "github.com/user/repo.git", "path/to/file?ref=main"]
            # Rejoin protocol + host, take the rest as subpath
            url_str = parts[0] + "//" + parts[1]
            subpath = "/".join(parts[2:])
        # If only 2 parts, it's just a URL with protocol (https://...) and no subpath

    # Extract query params from either the URL or the subpath
    ref = ""
    # Check subpath first for ?ref=
    if subpath and "?" in subpath:
        subpath, query = subpath.rsplit("?", 1)
        ref = _extract_ref(query)
    elif "?" in url_str:
        url_str, query = url_str.rsplit("?", 1)
        ref = _extract_ref(query)

    # Validate
    if not (url_str.startswith("https://") or url_str.startswith("git@")):
        raise SourceRefError(f"git source URL must start with https:// or git@, got: {url_str}")

    if ".." in subpath:
        raise SourceRefError(f"git source subpath must not contain '..': {subpath}")

    if ref and not _SAFE_REF_RE.match(ref):
        raise SourceRefError(f"git source ref contains unsafe characters: {ref}")

    # Apply default filename if no subpath
    if not subpath and default_filename:
        subpath = default_filename

    return SourceRef(kind="git", path=url_str, subpath=subpath, ref=ref)


def _extract_ref(query: str) -> str:
    """Extract ref= value from a query string."""
    for param in query.split("&"):
        if param.startswith("ref="):
            return param[4:]
    return ""


def fetch_file(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Fetch a file from a source reference to a destination on the target.

    Args:
        source: Parsed source reference.
        target: SSH execution target.
        dest: Destination file path on the target.
        logger: Optional SSH logger.
    """
    if source.kind == "file":
        _fetch_local(source, target, dest, logger=logger)
    elif source.kind == "git":
        _fetch_git(source, target, dest, logger=logger)
    else:
        raise SourceRefError(f"unknown source kind: {source.kind}")


def _fetch_local(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Copy a local file to the target."""
    local_path = Path(source.path).expanduser()
    if not local_path.exists():
        raise SourceRefError(f"local source file does not exist: {local_path}")

    target.copy_to(local_path, dest)
    if logger:
        logger.output(f"Copied {local_path} to {dest}")


def _fetch_git(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Clone a git repo on the target and copy a file from it."""
    from agentworks.ssh import SSHError

    tmp_dir = "/tmp/agentworks-source-ref"

    try:
        # Clean up any previous clone
        target.run(f"rm -rf {tmp_dir}", check=False)

        # Shallow clone
        clone_cmd = "git clone --depth 1"
        if source.ref:
            clone_cmd += f" --branch {shlex.quote(source.ref)}"
        clone_cmd += f" {shlex.quote(source.path)} {tmp_dir}"

        target.run(clone_cmd, timeout=60)
        if logger:
            logger.output(f"Cloned {source.path} (ref: {source.ref or 'default'})")

        # Copy the file
        src_file = f"{tmp_dir}/{source.subpath}" if source.subpath else tmp_dir
        target.run(f"test -f {shlex.quote(src_file)}", timeout=10)

        # Use cp on the target (file is already there from clone)
        inner = f"cp {shlex.quote(src_file)} {shlex.quote(dest)}"
        target.run(f"sh -c {shlex.quote(inner)}", timeout=10)
        if logger:
            logger.output(f"Copied {source.subpath or '(root)'} to {dest}")

    except SSHError as e:
        raise SourceRefError(f"failed to fetch from git source: {e}") from e
    finally:
        target.run(f"rm -rf {tmp_dir}", check=False)


def fetch_dir(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Fetch a directory from a source reference to a destination on the target.

    For file sources, copies a local directory to the target.
    For git sources, clones the repo to the destination (or pulls if the
    destination is already a matching clone). The subpath is ignored for
    directory fetches (the whole repo is cloned).

    Args:
        source: Parsed source reference.
        target: SSH execution target.
        dest: Destination directory path on the target.
        logger: Optional SSH logger.
    """
    if source.kind == "file":
        _fetch_local_dir(source, target, dest, logger=logger)
    elif source.kind == "git":
        _fetch_git_dir(source, target, dest, logger=logger)
    else:
        raise SourceRefError(f"unknown source kind: {source.kind}")


def _fetch_local_dir(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Copy a local directory to the target."""
    local_path = Path(source.path).expanduser()
    if not local_path.exists():
        raise SourceRefError(f"local source directory does not exist: {local_path}")
    if not local_path.is_dir():
        raise SourceRefError(f"local source is not a directory: {local_path}")

    # Check if destination exists and warn
    if target.run(f"test -d {shlex.quote(dest)}", check=False).ok:
        if logger:
            logger.warning(f"overwriting existing {dest}")
        target.run(f"rm -rf {shlex.quote(dest)}", check=False)

    target.copy_dir_to(local_path, dest)
    if logger:
        logger.output(f"Copied {local_path} to {dest}")


def _fetch_git_dir(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Clone a git repo to the destination, or pull if already cloned."""
    from agentworks.ssh import SSHError

    # Check if destination is already a matching clone
    is_git = target.run(f"test -d {shlex.quote(dest)}/.git", check=False)
    if is_git.ok:
        remote = target.run(
            f"git -C {shlex.quote(dest)} remote get-url origin",
            check=False,
        )
        if remote.ok and remote.stdout.strip() == source.path:
            if logger:
                logger.output(f"Pulling {source.path} in {dest}")
            try:
                target.run(f"git -C {shlex.quote(dest)} pull", timeout=120)
            except SSHError as e:
                raise SourceRefError(f"git pull failed in {dest}: {e}") from e
            return
        # Different repo at the destination
        raise SourceRefError(f"{dest} exists but is a clone of {remote.stdout.strip()}, not {source.path}")

    # Check if destination exists but is not a git repo
    if target.run(f"test -d {shlex.quote(dest)}", check=False).ok:
        raise SourceRefError(f"{dest} exists but is not a git repo")

    # Fresh clone
    clone_cmd = "git clone"
    if source.ref:
        clone_cmd += f" --branch {shlex.quote(source.ref)}"
    clone_cmd += f" {shlex.quote(source.path)} {shlex.quote(dest)}"

    try:
        target.run(clone_cmd, timeout=120)
    except SSHError as e:
        raise SourceRefError(f"git clone failed: {e}") from e

    if logger:
        logger.output(f"Cloned {source.path} to {dest}")

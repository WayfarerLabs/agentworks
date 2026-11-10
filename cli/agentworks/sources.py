"""Source reference resolution for fetching files or directories from local or remote sources.

Supports Terraform-style source references:
  - Local path:  ~/.config/agentworks/mise.lock (or file::~/.config/...)
  - Git repo:    git::https://github.com/user/repo.git
  - Git + path:  git::https://github.com/user/repo.git//path/to/file
  - Git + ref:   git::https://github.com/user/repo.git//path/to/file?ref=main

Two fetch modes, each run on a managed VM as one identity through its ``Files``:
  - fetch_file: fetches a single file (e.g., mise lockfile)
  - fetch_dir:  fetches a directory (e.g., dotfiles repo); a git source is
    cloned to the destination and brought to its ref on reinit, and a local
    directory replaces the destination whole (``upload_tree``)
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.errors import ExternalError, StateError, ValueFreeError
from agentworks.execution.errors import CommandFailed, PathRefusal, PathRefused
from agentworks.execution.files import guest_path
from agentworks.execution.requests import Bytes, Command, Script, Shell
from agentworks.path_rendering import format_host_path
from agentworks.vms.home_layout import MANAGED

if TYPE_CHECKING:
    from agentworks.execution.files import Files


class _Output(Protocol):
    """Where a fetch notes what it did: an operation log's ``output``."""

    def output(self, text: str) -> None: ...


class SourceRefError(ExternalError):
    """Raised when a source reference or a destination is invalid, or a fetch from it fails."""


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
        path = source[6:]
        if not path:
            raise SourceRefError("file source path cannot be empty")
        return SourceRef(kind="file", path=path, subpath="", ref="")

    # Git source
    if source.startswith("git::"):
        return _parse_git_source(source[5:], default_filename)

    if "::" in source:
        raise SourceRefError("source reference scheme must be file:: or git::")

    # Default: local file
    return SourceRef(kind="file", path=source, subpath="", ref="")


def _parse_git_source(url_str: str, default_filename: str) -> SourceRef:
    """Parse the URL portion of a git:: source reference."""
    if not url_str:
        raise SourceRefError("git source URL cannot be empty")

    # Split on // to separate URL from subpath. For HTTPS, skip the
    # protocol separator; SCP-style git@ URLs have no protocol separator.
    subpath = ""
    separator_start = len("https://") if url_str.startswith("https://") else 0
    separator = url_str.find("//", separator_start)
    if separator >= 0:
        url_str, subpath = url_str[:separator], url_str[separator + 2 :]

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
    refs = []
    for param in query.split("&"):
        key, _, value = param.partition("=")
        if key == "ref":
            refs.append(value)
    if len(refs) != 1 or not refs[0]:
        raise SourceRefError("git source query must contain exactly one non-empty ref parameter")
    return refs[0]


class DestinationError(ValueFreeError):
    """A ``dotfiles_destination`` that is not one; the message names the accepted forms, never the value."""


_DESTINATION_FORMS = (
    "dotfiles_destination must be a directory below the home (written 'dir' or '~/dir') or an absolute path, "
    "with no empty, '.' or '..' component and no other user's '~'"
)


def normalize_destination(value: str) -> str:
    """``value`` as a dotfiles destination: ``~/`` and a path in the home, or an absolute path.

    Boundary: the templates' ``dotfiles_destination``, operator
    configuration, validated here at load, stored overlays included. A path
    with no leading ``~/`` or ``/`` is in the home, as it always meant, so
    ``dots`` becomes ``~/dots``. No component may be empty, ``.`` or ``..``
    and ``~user`` is refused; a trailing ``/`` is dropped. The home
    itself (``~`` or ``~/``), and a path in it that is or holds one Agentworks
    manages there (:data:`agentworks.vms.home_layout.MANAGED`), are refused,
    since replacing the checkout would replace them. Refusals are
    :class:`DestinationError`, whose message holds no part of ``value``.
    """
    if value.startswith("/"):
        prefix, rest = "/", value[1:]
    else:
        prefix, rest = "~/", value.removeprefix("~/") if value != "~" else ""
    rest = rest.removesuffix("/")
    if (
        not value.isprintable()
        or not rest
        or rest.startswith("~")
        or any(part in ("", ".", "..") for part in rest.split("/"))
    ):
        raise DestinationError(_DESTINATION_FORMS)
    if prefix == "~/":
        _refuse_managed(rest)
    return prefix + rest


def _refuse_managed(relative: str) -> None:
    """Refuse the path ``relative`` to a home when it is, or is a parent of, a path Agentworks manages there."""
    if any(managed == relative or managed.startswith(relative + "/") for managed in MANAGED):
        raise DestinationError(
            "dotfiles_destination may not be, or hold, a path Agentworks manages in the home: "
            + ", ".join(f"~/{managed}" for managed in MANAGED)
        )


def expand_destination(value: str, home: str) -> str:
    """The guest path of the normalized destination ``value`` for an account whose home is ``home``.

    Only a leading ``~`` is replaced. An absolute destination that is the
    home or one of its ancestors, or a path in the home that
    :func:`normalize_destination` would refuse, is refused before anything
    is sent.
    """
    path = home + value[1:] if value.startswith("~/") else value
    if path == home or home.startswith(path.rstrip("/") + "/"):
        raise SourceRefError(f"dotfiles destination {path} is the home {home} or one of its parents")
    if path.startswith(home + "/"):
        try:
            _refuse_managed(path[len(home) + 1 :])
        except DestinationError as refused:
            raise SourceRefError(str(refused)) from None
    return path


def snapshot_workstation_file(source: str) -> bytes:
    """Capture a local source once, using the invoking process's home and cwd.

    This boundary accepts operator-authored references and filesystem input.
    Errors omit source values and OS exception details, which can contain paths
    or credentials. No transport or persistent staging file is involved.
    """
    try:
        reference = parse_source_ref(source)
    except SourceRefError:
        raise SourceRefError("invalid workstation file source reference") from None
    if reference.kind != "file":
        raise SourceRefError("workstation snapshots require a local file source")
    try:
        path = Path(reference.path).expanduser()
        if not stat.S_ISREG(path.stat().st_mode):
            raise SourceRefError("workstation source must be a regular file")
        return path.read_bytes()
    except (OSError, ValueError, RuntimeError):
        raise SourceRefError(f"could not read workstation source file {reference.path!r}") from None


def fetch_file(source: SourceRef, files: Files, dest: str, *, log: _Output | None = None) -> None:
    """Publish the file ``source`` names at ``dest`` on the guest, 0644, as ``files``' identity.

    A local source is read on the workstation; a git source is cloned shallow
    into a temporary directory on the guest, and its file read back. Either
    way the file program writes it, so a link at ``dest`` is refused.
    """
    if source.kind == "file":
        data = _read_local_file(source)
    elif source.kind == "git":
        data = _read_git_file(source, files, log=log)
    else:
        raise SourceRefError(f"unknown source kind: {source.kind}")
    files.write(dest, data, mode=0o644, parent_mode=0o755)
    if log:
        log.output(f"Wrote {source.subpath or source.path} to {dest}")


def _read_local_file(source: SourceRef) -> bytes:
    """The bytes of a local file source; a file that cannot be read is ``SourceRefError``."""
    local_path = Path(source.path).expanduser()
    try:
        return local_path.read_bytes()
    except FileNotFoundError:
        raise SourceRefError(f"local source file does not exist: {format_host_path(local_path)}") from None
    except OSError as error:
        raise SourceRefError(f"could not read local source file {format_host_path(local_path)}: {error}") from None


GIT_FILE_LIMIT = 16 * 1024 * 1024
"""The largest file a git source may supply (a ``mise.lock``, far smaller)."""

DOTFILES_DEADLINE = 120
"""Seconds each git command that syncs a dotfiles checkout, and the dotfiles install, may take."""

SHALLOW_CLONE_DEADLINE = 60
"""Seconds the shallow clone a git file source is read from may take."""

_STAGING_PREFIX = "/var/tmp/agentworks-source-ref-"


def _read_git_file(source: SourceRef, files: Files, *, log: _Output | None) -> bytes:
    """Clone ``source`` shallow into a temporary directory on the guest and read its file back."""
    if not source.subpath or any(part in ("", ".", "..") for part in source.subpath.split("/")):
        raise SourceRefError(f"git source must name a file in the repository: {source.path!r}")
    access = files.access
    created = access.run(Command(("mktemp", "-d", f"{_STAGING_PREFIX}XXXXXX"), deadline=files.deadline), check=True)
    try:
        tmp_dir = guest_path(created.stdout.decode("utf-8", "replace").strip())
    except StateError:
        raise SourceRefError(f"mktemp on VM '{access.vm_name}' did not name a staging directory") from None
    try:
        branch = ("--branch", source.ref) if source.ref else ()
        clone = ("git", "clone", "--depth", "1", *branch, source.path, tmp_dir)
        access.run(Command(clone, deadline=SHALLOW_CLONE_DEADLINE), check=True)
        if log:
            log.output(f"Cloned {source.path} (ref: {source.ref or 'default'})")
        data = files.read(f"{tmp_dir}/{source.subpath}", limit=GIT_FILE_LIMIT)
        if data is None:
            raise SourceRefError(f"{source.subpath} is not in {source.path}")
        return data
    finally:
        try:
            access.run(Command(("rm", "-rf", "--", tmp_dir), deadline=files.deadline))
        except Exception as error:
            output.warn(f"Could not remove remote source staging directory: {error}")


def fetch_dir(source: SourceRef, files: Files, dest: str, *, log: _Output | None = None) -> None:
    """Fetch a directory from ``source`` to ``dest`` on the guest, as ``files``' identity.

    A local source's tree replaces ``dest`` whole (:func:`upload_tree`). A git
    source is cloned to ``dest``, or brought up to date when ``dest`` is
    already a clone of the same repository (:func:`_fetch_git_dir`); its
    subpath is ignored. Git follows a link at ``dest``, as it always has.
    """
    if source.kind == "file":
        local_path = Path(source.path).expanduser()
        if not local_path.is_dir():
            raise SourceRefError(f"local source is not a directory: {format_host_path(local_path)}")
        upload_tree(files, local_path, dest)
        if log:
            log.output(f"Replaced {dest} with {local_path}")
    elif source.kind == "git":
        _fetch_git_dir(source, files, dest, log=log)
    else:
        raise SourceRefError(f"unknown source kind: {source.kind}")


def _fetch_git_dir(source: SourceRef, files: Files, dest: str, *, log: _Output | None) -> None:
    """Clone ``source`` to ``dest``, or bring an existing clone of the same repository to its ref.

    An existing clone with a ref fetches that ref and checks it out detached,
    so a changed ref takes effect, a branch advances and a tag works alike;
    one with no ref fast-forwards its branch. Every git command must succeed:
    a failure is ``CommandFailed``, so the caller can tell a sync that did not
    happen from one that did.
    """
    access = files.access

    def git(*argv: str) -> None:
        access.run(Command(("git", "-C", dest, *argv), deadline=DOTFILES_DEADLINE), check=True)

    def succeeds(argv: tuple[str, ...]) -> bool:
        return access.run(Command(argv, deadline=files.deadline)).require_completed(access.vm_name) == 0

    if succeeds(("test", "-d", f"{dest}/.git")):
        remote = access.run(Command(("git", "-C", dest, "remote", "get-url", "origin"), deadline=files.deadline))
        origin = remote.stdout.decode("utf-8", "replace").strip()
        if remote.require_completed(access.vm_name) != 0 or origin != source.path:
            raise SourceRefError(f"{dest} exists but is a clone of {origin}, not {source.path}")
        if log:
            log.output(f"Updating {source.path} in {dest} (ref: {source.ref or 'default'})")
        if source.ref:
            git("fetch", "origin", source.ref)
            git("checkout", "--detach", "FETCH_HEAD")
        else:
            git("pull", "--ff-only")
        return
    if succeeds(("test", "-d", dest)):
        raise SourceRefError(f"{dest} exists but is not a git repo")
    branch = ("--branch", source.ref) if source.ref else ()
    access.run(Command(("git", "clone", *branch, "--", source.path, dest), deadline=DOTFILES_DEADLINE), check=True)
    if log:
        log.output(f"Cloned {source.path} to {dest}")


UPLOAD_TREE_SCRIPT = r"""set -eu
dest=$AGW_TREE_DEST
parent=${dest%/*}
new="$parent/.agentworks-new-$AGW_TREE_TAG"
old="$parent/.agentworks-old-$AGW_TREE_TAG"
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -d "$dest" ]; }; then
    exit 4
fi
mkdir -p -- "${parent:-/}"
# A run killed between its two renames left the previous tree at old and nothing at dest.
if [ -d "$old" ] && [ ! -L "$old" ] && [ ! -e "$dest" ] && [ ! -L "$dest" ]; then
    mv -T -- "$old" "$dest"
fi
rm -rf -- "$old" "$new"
restore() {
    rm -rf -- "$new"
    if [ -e "$old" ] || [ -L "$old" ]; then
        if [ -e "$dest" ] || [ -L "$dest" ]; then
            rm -rf -- "$old"
        else
            mv -T -- "$old" "$dest"
        fi
    fi
}
trap restore EXIT
trap 'restore; exit 129' HUP
trap 'restore; exit 130' INT
trap 'restore; exit 143' TERM
mkdir -m 0700 -- "$new"
/usr/bin/tar -x -z -f - -C "$new" --no-same-owner --no-same-permissions
if [ -e "$dest" ]; then
    mv -T -- "$dest" "$old"
fi
mv -T -- "$new" "$dest"
trap - EXIT HUP INT TERM
rm -rf -- "$old" || :
"""
"""Replace the directory ``$AGW_TREE_DEST`` with the gzip tar archive on stdin, as one ``sh`` script.

The archive is extracted into a sibling, ``.agentworks-new-$AGW_TREE_TAG``,
and only then is the previous tree moved aside to ``.agentworks-old-<tag>``
and the new one moved in. Any failure or signal from the extraction on puts
the previous tree back; a run killed outright between the two renames leaves
it at the ``old`` sibling, which the next upload to the same destination
moves back before it does anything else. A link or anything but a directory
at the destination is status 4.
"""

UNSUITABLE_TREE = 4
""":data:`UPLOAD_TREE_SCRIPT`'s status for a link or a non-directory at the destination."""


def upload_tree(files: Files, local_dir: Path, dest: str) -> None:
    """Replace the directory ``dest`` on the guest with ``local_dir``'s tree, as ``files``' identity, unelevated.

    One :data:`UPLOAD_TREE_SCRIPT` request carries the tree as a gzip tar
    archive on stdin. It takes no file lock: no Agentworks file operation
    writes beneath a dotfiles checkout, and every caller holds the VM's
    native mutation guard. Files are created with the identity's ownership
    and umask. A link or another file type at ``dest`` is ``PathRefused``;
    any other failure is ``CommandFailed``, whose stderr names ``tar``'s
    complaint, and leaves ``dest`` as it was.
    """
    dest = guest_path(dest)
    archive = io.BytesIO()
    try:
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            tar.add(local_dir, arcname=".")
    except OSError as error:
        raise SourceRefError(f"could not read local source directory {format_host_path(local_dir)}: {error}") from None
    tag = hashlib.sha256(dest.rsplit("/", 1)[1].encode("utf-8", "surrogateescape")).hexdigest()[:16]
    request = Script(
        UPLOAD_TREE_SCRIPT,
        Shell.SH,
        env={"AGW_TREE_DEST": dest, "AGW_TREE_TAG": tag},
        stdin=Bytes(archive.getvalue()),
        deadline=files.deadline,
    )
    access = files.access
    result = access.run(request)
    status = result.require_completed(access.vm_name)
    if status == UNSUITABLE_TREE:
        raise PathRefused(access.vm_name, "upload_tree", dest, PathRefusal.UNSUITABLE)
    if status != 0:
        raise CommandFailed(access.vm_name, status, result, streams_logged=request.log_streams)

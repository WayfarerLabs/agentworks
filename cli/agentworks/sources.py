"""Source reference resolution for fetching files or directories from local or remote sources.

Supports Terraform-style source references:
  - Local path:  ~/.config/agentworks/mise.lock (or file::~/.config/...)
  - Git repo:    git::https://github.com/user/repo.git
  - Git + path:  git::https://github.com/user/repo.git//path/to/file
  - Git + ref:   git::https://github.com/user/repo.git//path/to/file?ref=main

Two fetch modes, each run on a managed VM as one identity through its ``Files``:
  - fetch_file: fetches a single file (e.g., mise lockfile)
  - fetch_dir:  fetches a directory (e.g., dotfiles repo); a git source is
    cloned to the destination if absent, then its ref fetched and checked
    out detached every time, and a local directory replaces the destination
    whole (``upload_tree``)

A git file source is fetched the same way, at depth 1, into a temporary
repository on the guest.
"""

from __future__ import annotations

import io
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from agentworks import output
from agentworks.errors import ExternalError, StateError

# SourceRefFormError is defined in agentworks.errors, beside the other value-free errors, and re-exported
# here with check_source_ref, which raises it; the `X as X` shape marks it as explicit for mypy.
from agentworks.errors import SourceRefFormError as SourceRefFormError
from agentworks.execution.errors import CommandFailed, PathRefusal, PathRefused
from agentworks.execution.files import guest_path
from agentworks.execution.requests import Bytes, Command, Script, Shell
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from agentworks.execution.files import Files


class _Output(Protocol):
    """Where a fetch notes what it did: an operation log's ``output``."""

    def output(self, text: str) -> None: ...


class SourceRefError(ExternalError):
    """Raised when a source reference or a destination is invalid, or a fetch from it fails."""


_DRIVE_RE = re.compile(r"^[A-Za-z]:([/\\]|$)")
"""A Windows drive letter and its separator (or nothing more), which ``git`` exempts from its scp-style rule.

A single letter and a colon followed by anything else, such as ``u:token@host:path``, is a remote.
"""

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

    A local path, plain or after ``file::``, may not hold ``::`` or ``://``,
    nor a colon in its first ``/``-delimited component unless that is a
    Windows drive letter: git's own rule for an scp-style remote
    (``user@host:path``). So a remote written without ``git::`` is refused
    instead of read as a local file.

    Raises:
        SourceRefError: If the source reference is malformed.
    """
    if not source:
        raise SourceRefError("source reference cannot be empty")

    # Git source
    if source.startswith("git::"):
        return _parse_git_source(source[5:], default_filename)

    path = source.removeprefix("file::")
    if not path:
        raise SourceRefError("file source path cannot be empty")
    if "::" in path:
        raise SourceRefError("source reference scheme must be file:: or git::")
    # A URL, or what git reads as an scp-style remote (a colon in the first path component,
    # a drive letter aside), is not a local path and may hold a credential, so it is refused
    # without being echoed.
    if "://" in path or (":" in path.split("/", 1)[0] and not _DRIVE_RE.match(path)):
        raise SourceRefError(
            "a remote source reference must start with git::; a local path holds no '://' and no ':' before "
            "its first '/', a drive letter aside"
        )
    return SourceRef(kind="file", path=path, subpath="", ref="")


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

    # Validate. A URL that may carry a credential is never echoed.
    if not (url_str.startswith("https://") or url_str.startswith("git@")):
        raise SourceRefError("git source URL must start with https:// or git@")
    if url_str.startswith("https://") and "@" in url_str[len("https://") :].split("/", 1)[0]:
        raise SourceRefError(
            "git source URL must not carry a user name or token; configure git credentials for the host instead"
        )

    if subpath and any(part in ("", ".", "..") for part in subpath.split("/")):
        raise SourceRefError(f"git source subpath must not have an empty, '.' or '..' component: {subpath}")

    if ref and not _SAFE_REF_RE.match(ref):
        raise SourceRefError(f"git source ref contains unsafe characters: {ref}")

    # Apply default filename if no subpath
    if not subpath and default_filename:
        subpath = default_filename

    return SourceRef(kind="git", path=url_str, subpath=subpath, ref=ref)


def check_source_ref(value: str, *, field: str) -> str:
    """``value`` when it parses as a source reference, else :class:`SourceRefFormError` naming ``field``.

    Boundary: a template's source fields, operator configuration and stored
    overlays alike, validated at load. The message holds no part of
    ``value``, since a refused URL may carry a credential.
    """
    try:
        parse_source_ref(value)
    except SourceRefError:
        raise SourceRefFormError(
            f"{field} must be a local path, file::<path>, or git:: and an https:// or git@ URL holding no user "
            "name or token, with an optional //<subpath> and ?ref=<branch, tag or full commit hash>",
            field=field,
        ) from None
    return value


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


class DestinationError(SourceRefError):
    """A ``dotfiles_destination`` that is not one; the message names the accepted forms, never the value."""


_DESTINATION_FORMS = (
    "dotfiles_destination must be a directory below the home (written 'dir' or '~/dir') or an absolute path, "
    "with no empty, '.' or '..' component and no other user's '~'"
)


def normalize_destination(value: str, *, managed: tuple[str, ...]) -> str:
    """``value`` as a dotfiles destination: ``~/`` and a path in the home, or an absolute path.

    Boundary: the templates' ``dotfiles_destination``, operator
    configuration, checked here as rendered for one account, just before its
    dotfiles sync (``vms.initializer.dotfiles``). A path with no leading
    ``~/`` or ``/`` is in the home, as it always meant, so ``dots`` becomes
    ``~/dots``. No component may be empty, ``.`` or ``..``
    and ``~user`` is refused; a trailing ``/`` is dropped. The home
    itself (``~`` or ``~/``), and a path in it that is, holds or lies inside
    one of ``managed``, the account's set in :mod:`agentworks.vms.home_layout`
    (``MANAGED`` for an agent, ``ADMIN_MANAGED`` for the admin), are refused,
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
        _refuse_managed(rest, managed)
    return prefix + rest


def _refuse_managed(relative: str, managed: tuple[str, ...]) -> None:
    """Refuse the path ``relative`` to a home when it is, holds, or lies inside a path in ``managed``."""
    if any(path == relative or path.startswith(relative + "/") or relative.startswith(path + "/") for path in managed):
        raise DestinationError(
            "dotfiles_destination may not be, hold, or lie inside a path Agentworks manages in the home: "
            + ", ".join(f"~/{path}" for path in managed)
        )


def expand_destination(value: str, home: str, *, managed: tuple[str, ...]) -> str:
    """The guest path of the normalized destination ``value`` for an account whose home is ``home``.

    Only a leading ``~`` is replaced. An absolute destination that is the
    home or one of its ancestors, or a path in the home that
    :func:`normalize_destination` would refuse for the same ``managed``, is
    refused before anything is sent, with a :class:`DestinationError` that
    names no part of ``value`` or ``home``.
    """
    path = home + value[1:] if value.startswith("~/") else value
    if path == home or home.startswith(path.rstrip("/") + "/"):
        raise DestinationError("dotfiles_destination may not be the home or one of its parents")
    if path.startswith(home + "/"):
        _refuse_managed(path[len(home) + 1 :], managed)
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

    A local source is read on the workstation; a git source's ref is fetched
    at depth 1 into a temporary repository on the guest, and its file read
    back. Either
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

SHALLOW_FETCH_DEADLINE = 60
"""Seconds the depth-1 fetch a git file source is read from may take, and so may its checkout."""

_STAGING_PREFIX = "/var/tmp/agentworks-source-ref-"


def _read_git_file(source: SourceRef, files: Files, *, log: _Output | None) -> bytes:
    """Fetch ``source``'s ref at depth 1 into a temporary repository on the guest and read its file back.

    The directory is removed afterwards whatever happened; a removal that
    fails or does not complete warns and leaves the fetch's own outcome.
    """
    if not source.subpath:
        raise SourceRefError(f"git source must name a file in the repository: {source.path!r}")
    access = files.access
    created = access.run(Command(("mktemp", "-d", f"{_STAGING_PREFIX}XXXXXX"), deadline=files.deadline), check=True)
    try:
        tmp_dir = guest_path(created.stdout.decode("utf-8", "replace").strip())
    except StateError:
        tmp_dir = ""
    if not tmp_dir.startswith(_STAGING_PREFIX):
        raise SourceRefError(f"mktemp on VM '{access.vm_name}' did not name a staging directory")
    try:
        access.run(Command(("git", "init", "-q", "--", tmp_dir), deadline=files.deadline), check=True)
        access.run(
            Command(("git", "-C", tmp_dir, "remote", "add", "origin", "--", source.path), deadline=files.deadline),
            check=True,
        )
        _fetch_and_detach(files, tmp_dir, source.ref, deadline=SHALLOW_FETCH_DEADLINE, depth=1)
        if log:
            log.output(f"Fetched {source.path} (ref: {source.ref or 'default'})")
        data = files.read(f"{tmp_dir}/{source.subpath}", limit=GIT_FILE_LIMIT)
        if data is None:
            raise SourceRefError(f"{source.subpath} is not in {source.path}")
        return data
    finally:
        _remove_staging(files, tmp_dir)


def _remove_staging(files: Files, tmp_dir: str) -> None:
    """Remove the staging directory ``tmp_dir``; a removal that fails or does not complete only warns."""
    from agentworks.vms.initializer.steps import STEP_FAILURES

    access = files.access
    try:
        removed = access.run(Command(("rm", "-rf", "--", tmp_dir), deadline=files.deadline))
        if removed.require_completed(access.vm_name) != 0:
            output.warn(f"Could not remove remote source staging directory {tmp_dir} on VM '{access.vm_name}'")
    except STEP_FAILURES as error:
        output.warn(f"Could not remove remote source staging directory {tmp_dir}: {error}")


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

    Either way the ref, or the remote's ``HEAD`` without one, is fetched and
    checked out detached (:func:`_fetch_and_detach`), so a changed or dropped ref
    takes effect, a branch advances, and a tag or a full commit hash works
    alike. A fresh clone is made beside ``dest`` and moved there only once its
    ref is checked out (:func:`_clone_into_place`). Every git command must
    succeed: a failure is ``CommandFailed``, so the caller can tell a sync
    that did not happen from one that did. A directory that is not a clone,
    or a link that does not lead to one, is refused.

    The existing clone's origin is its configured URL, read as written
    (``insteadOf`` rewrites apply to what git fetches, not to what the
    clone is of), with its streams kept out of the log; a refusal names
    neither URL, since a clone made from a URL with credentials in it holds
    them there.
    """
    access = files.access

    def succeeds(argv: tuple[str, ...]) -> bool:
        return access.run(Command(argv, deadline=files.deadline)).require_completed(access.vm_name) == 0

    if succeeds(("test", "-d", f"{dest}/.git")):
        url = ("git", "-C", dest, "config", "--get", "remote.origin.url")
        probe = Command(url, deadline=files.deadline, log_streams=False)
        remote = access.run(probe)
        if (
            remote.require_completed(access.vm_name) != 0
            or remote.stdout.decode("utf-8", "replace").strip() != source.path
        ):
            raise SourceRefError(f"{dest} exists but is a clone of a different repository")
        if log:
            log.output(f"Updating {source.path} in {dest} (ref: {source.ref or 'default'})")
    elif succeeds(("test", "-d", dest)) or succeeds(("test", "-L", dest)):
        raise SourceRefError(f"{dest} exists but is not a git repo")
    else:
        _clone_into_place(source, files, dest)
        if log:
            log.output(f"Cloned {source.path} to {dest}")
        return
    _fetch_and_detach(files, dest, source.ref, deadline=DOTFILES_DEADLINE)


_CLONE_STAGING_NAME = ".agentworks-clone-"


def _clone_into_place(source: SourceRef, files: Files, dest: str) -> None:
    """Clone ``source`` and check out its ref beside ``dest``, then move the checkout to ``dest``.

    ``dest`` only ever appears as a complete checkout, so a clone or a ref
    that fails leaves nothing a later sync would take for a clone of that
    origin. The checkout is made in a ``mktemp`` directory in ``dest``'s
    parent, so the move is a rename; that directory is removed whatever
    happened, and a removal that fails or does not complete only warns.
    """
    access = files.access
    parent = str(PurePosixPath(guest_path(dest)).parent)
    prefix = f"{parent.rstrip('/')}/{_CLONE_STAGING_NAME}"
    access.run(Command(("mkdir", "-p", "--", parent), deadline=files.deadline), check=True)
    created = access.run(Command(("mktemp", "-d", f"{prefix}XXXXXX"), deadline=files.deadline), check=True)
    staging = created.stdout.decode("utf-8", "replace").strip()
    if not staging.startswith(prefix) or "/" in staging.removeprefix(prefix):
        raise SourceRefError(f"mktemp on VM '{access.vm_name}' did not name a staging directory")
    checkout = f"{staging}/checkout"
    try:
        clone = ("git", "clone", "--no-checkout", "--", source.path, checkout)
        access.run(Command(clone, deadline=DOTFILES_DEADLINE), check=True)
        _fetch_and_detach(files, checkout, source.ref, deadline=DOTFILES_DEADLINE)
        access.run(Command(("mv", "-T", "--", checkout, dest), deadline=files.deadline), check=True)
    finally:
        _remove_staging(files, staging)


def _fetch_and_detach(files: Files, repo: str, ref: str, *, deadline: float, depth: int | None = None) -> None:
    """Fetch ``ref`` (the remote's ``HEAD`` when empty) from ``repo``'s origin and check it out detached.

    A branch, a tag (annotated or not) and a full commit hash are fetched
    alike; a short hash is not, since a remote serves only names and whole
    object ids.
    """
    shallow = ("--depth", str(depth)) if depth is not None else ()
    for argv in (("fetch", *shallow, "--", "origin", ref or "HEAD"), ("checkout", "--detach", "FETCH_HEAD")):
        files.access.run(Command(("git", "-C", repo, *argv), deadline=deadline), check=True)


UPLOAD_TREE_SCRIPT = r"""set -eu
dest=$AGW_TREE_DEST
parent=${dest%/*}
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -d "$dest" ]; }; then
    exit 4
fi
mkdir -p -- "${parent:-/}"
# A tree may hold directories its owner cannot write or search, which rm cannot empty until it may.
# Only its directories are opened up (find follows no link): a file's mode lives on its inode, which a
# hard link outside the tree may share. Removing what an upload made is best effort.
remove() {
    for path in "$@"; do
        find "$path" -type d ! -perm -u=rwx -exec chmod u+rwx {} \; 2>/dev/null || :
    done
    rm -rf -- "$@" || :
}
# Each sibling is one mktemp makes for this run, so restore and the last removal touch only this run's.
new=
old=
restore() {
    if [ -n "$new" ]; then
        remove "$new"
        new=
    fi
    if [ -n "$old" ]; then
        if [ -e "$dest" ] || [ -L "$dest" ]; then
            remove "$old"
        else
            mv -T -- "$old" "$dest"
        fi
        old=
    fi
}
trap restore EXIT
trap 'restore; exit 129' HUP
trap 'restore; exit 130' INT
trap 'restore; exit 143' TERM
new=$(mktemp -d "$parent/.agentworks-new-XXXXXXXXXX")
/usr/bin/tar -x -z -f - -C "$new" --no-same-owner --no-same-permissions
if [ -e "$dest" ]; then
    # The previous tree is renamed over the empty directory made for it.
    old=$(mktemp -d "$parent/.agentworks-old-XXXXXXXXXX")
    mv -T -- "$dest" "$old"
fi
mv -T -- "$new" "$dest"
trap - EXIT HUP INT TERM
if [ -n "$old" ]; then
    remove "$old"
fi
"""
"""Replace the directory ``$AGW_TREE_DEST`` with the gzip tar archive on stdin, as one ``sh`` script.

The archive is extracted into a sibling, ``.agentworks-new-<random>``, and
only then is the previous tree moved aside to ``.agentworks-old-<random>``
and the new one moved in. Any failure or signal from the extraction on puts
the previous tree back. ``mktemp`` makes each sibling, so the script removes
or moves only what it made, never a sibling that merely has such a name; a
run killed outright between its two renames leaves the previous tree at its
``old`` sibling and no destination, for the operator to recover. A sibling's
directories are made writable and searchable before it is removed, so a tree
with read-only directories is replaced like any other, and a sibling that
cannot be removed never stops the previous tree coming back. A link or
anything but a directory at the destination is status 4.
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
    request = Script(
        UPLOAD_TREE_SCRIPT,
        Shell.SH,
        env={"AGW_TREE_DEST": dest},
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

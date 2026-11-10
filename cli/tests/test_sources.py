"""Focused coverage for source-reference parsing and file fetching."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.execution.errors import CommandFailed, PathRefused
from agentworks.execution.requests import Bytes, Command, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.sources import (
    UPLOAD_TREE_SCRIPT,
    DestinationError,
    SourceRef,
    SourceRefError,
    expand_destination,
    fetch_dir,
    fetch_file,
    normalize_destination,
    parse_source_ref,
    upload_tree,
)
from agentworks.vms.admin import AdminConfig
from agentworks.vms.home_layout import ADMIN_MANAGED, MANAGED
from tests.phase_b_support import StepGuest, requires_file_program
from tests.vm_access_support import account_shell, local_vm_access

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.execution.files import Files

requires_posix_tree = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("tar") != "/usr/bin/tar" or not Path("/bin/mv").exists(),
    reason="the tree upload script runs GNU /usr/bin/tar and mv -T under /bin/sh, as a Linux guest has them",
)


def test_parse_git_source_with_subpath_and_ref() -> None:
    assert parse_source_ref("git::https://example.com/repo.git//locks/mise.lock?ref=v1.2") == SourceRef(
        kind="git",
        path="https://example.com/repo.git",
        subpath="locks/mise.lock",
        ref="v1.2",
    )


@pytest.mark.parametrize(
    "source",
    [
        "",
        "file::",
        "git::",
        "s3::bucket/key",
        "git::http://example.com/repo.git",
        "git::https://x/y//../lock",
        "git::https://x/y//a/./lock",
        "git::https://x/y//a//lock",
        "git::https://x/y//lock/",
        "git::https://x/y?depth=1",
        "git::https://x/y?ref",
        "git::https://x/y?ref=",
        "git::https://x/y?ref=main&ref=other",
        "git::https://x/y?ref&ref=main",
        "git::https://x/y?ref=&ref=main",
    ],
)
def test_parse_source_ref_rejects_malformed_inputs(source: str) -> None:
    with pytest.raises(SourceRefError):
        parse_source_ref(source)


@pytest.mark.parametrize(
    "url",
    [
        "https://ghp-abc123@example.com/r.git",
        "https://alice:sekrit-value@example.com/r.git",
        "https://u9:p9@example.com",
    ],
)
def test_a_git_url_that_carries_a_credential_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(SourceRefError) as refused:
        parse_source_ref(f"git::{url}//mise.lock?ref=main")

    assert url.split("@")[0].removeprefix("https://") not in str(refused.value)


def test_an_at_sign_after_the_host_is_not_a_credential() -> None:
    ref = parse_source_ref("git::https://example.com/team/@scope.git")
    assert ref.path == "https://example.com/team/@scope.git"


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/d.git",
        "file::ssh://example.com/d",
        "git@example.com:me/d.git",
        "me@host:dots",
        "user:tok@host:path",
        "github:me/dotfiles",
        "github.com:me/d.git",
        "file::a::b",
    ],
)
def test_a_remote_written_without_git_is_not_read_as_a_local_path(source: str) -> None:
    with pytest.raises(SourceRefError) as refused:
        parse_source_ref(source)
    assert source not in str(refused.value)


@pytest.mark.parametrize(
    "source",
    [
        "~/dots",
        "./dots",
        "/srv/a@b:c",
        "./a@b:c",
        "~/me@x:y",
        "me@host",
        "file::~/m.lock",
        "C:/Users/me/dots",
        "C:\\Users\\me@corp.com\\dots",
    ],
)
def test_a_local_path_is_still_a_local_path(source: str) -> None:
    assert parse_source_ref(source).kind == "file"


@pytest.mark.parametrize(
    ("query", "expected_ref"),
    [
        ("depth=1&ref=main", "main"),
        ("ref=main&depth=1", "main"),
    ],
)
def test_parse_git_source_accepts_additional_query_parameters(query: str, expected_ref: str) -> None:
    ref = parse_source_ref(f"git::https://example.com/repo.git?{query}", default_filename="mise.lock")
    assert ref == SourceRef("git", "https://example.com/repo.git", "mise.lock", expected_ref)


def test_parse_git_source_applies_default_filename() -> None:
    ref = parse_source_ref("git::git@example.com:infra/locks.git?ref=main", default_filename="mise.lock")
    assert ref == SourceRef("git", "git@example.com:infra/locks.git", "mise.lock", "main")


def test_parse_scp_style_git_source_with_subpath() -> None:
    ref = parse_source_ref("git::git@example.com:infra/locks.git//nested/mise.lock")
    assert ref == SourceRef("git", "git@example.com:infra/locks.git", "nested/mise.lock", "")


STAGING = "/var/tmp/agentworks-source-ref-abcd12"
URL = "https://example.com/dots.git"


def _staging_guest(clone: Path | None = None) -> StepGuest:
    """A guest whose ``mktemp`` names ``clone`` (or :data:`STAGING`) and whose file operations run here."""
    guest = StepGuest()
    staging = str(clone) if clone is not None else STAGING
    guest.respond(MKTEMP, Result(Completed(0), f"{staging}\n".encode()))
    return guest


@pytest.fixture
def clone() -> Iterator[Path]:
    """A real staging directory where ``mktemp`` would make one, standing in for the shallow clone."""
    staging = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="agentworks-source-ref-"))
    yield staging
    shutil.rmtree(staging, ignore_errors=True)


MKTEMP = Command(("mktemp", "-d", "/var/tmp/agentworks-source-ref-XXXXXX"), deadline=60)


def _shallow_fetch(staging: str, url: str, ref: str) -> list[Command]:
    """The requests that fetch ``ref`` of ``url`` at depth 1 into ``staging`` and check it out."""
    return [
        Command(("git", "init", "-q", "--", staging), deadline=60),
        Command(("git", "-C", staging, "remote", "add", "origin", "--", url), deadline=60),
        Command(("git", "-C", staging, "fetch", "--depth", "1", "--", "origin", ref), deadline=60),
        Command(("git", "-C", staging, "checkout", "--detach", "FETCH_HEAD"), deadline=60),
    ]


@requires_file_program
def test_fetch_file_publishes_a_local_source_at_0644(tmp_path: Path) -> None:
    source = tmp_path / "mise.lock"
    source.write_bytes(b"lock\r\n")
    destination = tmp_path / "home" / ".config" / "mise" / "mise.lock"
    guest = StepGuest()

    fetch_file(parse_source_ref(str(source)), guest.files, str(destination))

    assert destination.read_bytes() == b"lock\r\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert guest.commands() == []


@pytest.mark.windows
def test_fetch_file_refuses_a_local_source_it_cannot_read(tmp_path: Path) -> None:
    guest = StepGuest(local_files=False)

    with pytest.raises(SourceRefError):
        fetch_file(parse_source_ref(str(tmp_path / "absent.lock")), guest.files, "/home/a/mise.lock")
    with pytest.raises(SourceRefError):
        fetch_file(parse_source_ref(str(tmp_path)), guest.files, "/home/a/mise.lock")
    assert guest.access.requests() == []


@requires_file_program
def test_fetch_file_reads_a_git_source_back_from_a_shallow_fetch_and_removes_it(tmp_path: Path, clone: Path) -> None:
    (clone / "locks").mkdir(parents=True)
    (clone / "locks" / "mise.lock").write_bytes(b"pinned")
    destination = tmp_path / "home" / "mise.lock"
    guest = _staging_guest(clone)
    ref = parse_source_ref("git::https://example.com/repo.git//locks/mise.lock?ref=v1")

    fetch_file(ref, guest.files, str(destination))

    assert destination.read_bytes() == b"pinned"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert guest.commands() == [
        MKTEMP,
        *_shallow_fetch(str(clone), "https://example.com/repo.git", "v1"),
        Command(("rm", "-rf", "--", str(clone)), deadline=60),
    ]
    assert [operation for operation, _ in guest.file_operations()] == ["read", "write"]


@requires_file_program
@pytest.mark.parametrize("planted", ["absent", "link"])
def test_a_git_source_without_a_regular_file_publishes_nothing(tmp_path: Path, clone: Path, planted: str) -> None:
    if planted == "link":
        (tmp_path / "elsewhere").write_bytes(b"not in the repository")
        (clone / "mise.lock").symlink_to(tmp_path / "elsewhere")
    destination = tmp_path / "home" / "mise.lock"
    guest = _staging_guest(clone)

    with pytest.raises(SourceRefError if planted == "absent" else PathRefused):
        fetch_file(
            parse_source_ref("git::https://example.com/repo.git", default_filename="mise.lock"),
            guest.files,
            str(destination),
        )

    assert not destination.exists()
    assert guest.commands()[-1] == Command(("rm", "-rf", "--", str(clone)), deadline=60)


@pytest.mark.parametrize(
    "removal",
    [Result(Unknown(UnknownReason.CONNECTION)), Result(Completed(1))],
    ids=["incomplete", "failed"],
)
def test_a_staging_removal_that_does_not_succeed_warns_and_keeps_the_fetch_error(
    captured_output,  # noqa: ANN001
    removal: Result,
) -> None:
    guest = _staging_guest()
    primary = CommandFailed("box", 128, Result(Completed(128)), streams_logged=True)
    guest.respond(_shallow_fetch(STAGING, URL, "HEAD")[2], primary)
    guest.respond(Command(("rm", "-rf", "--", STAGING), deadline=60), removal)
    ref = parse_source_ref(f"git::{URL}//locks/mise.lock")

    with pytest.raises(CommandFailed) as caught:
        fetch_file(ref, guest.files, "/remote/mise.lock")

    assert caught.value is primary
    assert len(captured_output.warnings) == 1


@pytest.mark.parametrize("named", ["/tmp/elsewhere", "relative", ""])
def test_a_mktemp_that_names_no_staging_directory_clones_nothing(named: str) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(MKTEMP, Result(Completed(0), f"{named}\n".encode()))

    with pytest.raises(SourceRefError):
        fetch_file(parse_source_ref(f"git::{URL}//mise.lock"), guest.files, "/remote/mise.lock")

    assert guest.commands() == [MKTEMP]


# -- Git directories ----------------------------------------------------------

DEST = "/home/a/.dotfiles"
IS_CLONE = Command(("test", "-d", f"{DEST}/.git"), deadline=60)
EXISTS = Command(("test", "-d", DEST), deadline=60)
ORIGIN = Command(("git", "-C", DEST, "config", "--get", "remote.origin.url"), deadline=60, log_streams=False)


def _fetch_and_detach(ref: str) -> list[Command]:
    return [
        Command(("git", "-C", DEST, "fetch", "--", "origin", ref), deadline=120),
        Command(("git", "-C", DEST, "checkout", "--detach", "FETCH_HEAD"), deadline=120),
    ]


@pytest.mark.parametrize(("query", "ref"), [("?ref=main", "main"), ("", "HEAD")])
def test_fetch_dir_clones_an_absent_destination_then_checks_out_its_ref(query: str, ref: str) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(IS_CLONE, Result(Completed(1)))
    guest.respond(EXISTS, Result(Completed(1)))

    fetch_dir(parse_source_ref(f"git::{URL}{query}"), guest.files, DEST)

    assert guest.commands() == [
        IS_CLONE,
        EXISTS,
        Command(("git", "clone", "--no-checkout", "--", URL, DEST), deadline=120),
        *_fetch_and_detach(ref),
    ]


@pytest.mark.parametrize(("query", "ref"), [("?ref=v2", "v2"), ("", "HEAD")])
def test_fetch_dir_checks_out_the_ref_of_a_clone_of_the_same_repository(query: str, ref: str) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(ORIGIN, Result(Completed(0), f"{URL}\n".encode()))

    fetch_dir(parse_source_ref(f"git::{URL}{query}"), guest.files, DEST)

    assert guest.commands() == [IS_CLONE, ORIGIN, *_fetch_and_detach(ref)]


@pytest.mark.parametrize("failing", [0, 1], ids=["fetch", "checkout"])
def test_a_failed_fetch_or_checkout_of_the_ref_fails_the_sync(failing: int) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(ORIGIN, Result(Completed(0), f"{URL}\n".encode()))
    failed = _fetch_and_detach("v2")[failing]
    guest.respond(failed, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    with pytest.raises(CommandFailed):
        fetch_dir(parse_source_ref(f"git::{URL}?ref=v2"), guest.files, DEST)

    assert guest.commands()[-1] == failed


@pytest.mark.parametrize("found", ["other-clone", "not-a-clone"])
def test_fetch_dir_refuses_a_destination_it_did_not_clone(found: str) -> None:
    guest = StepGuest(local_files=False)
    if found == "other-clone":
        guest.respond(ORIGIN, Result(Completed(0), b"https://example.com/other.git\n"))
    else:
        guest.respond(IS_CLONE, Result(Completed(1)))

    with pytest.raises(SourceRefError):
        fetch_dir(parse_source_ref(f"git::{URL}"), guest.files, DEST)

    assert not any(
        isinstance(request, Command) and {"clone", "fetch"} & set(request.argv) for request in guest.commands()
    )


def test_a_credential_in_an_existing_clone_s_origin_reaches_neither_the_warning_nor_the_log() -> None:
    from agentworks.execution.oplog import format_record
    from agentworks.vms.initializer.dotfiles import sync_dotfiles

    guest = StepGuest(local_files=False)
    secret = "ghp-origin-secret"
    answer = Result(Completed(0), f"https://u:{secret}@example.com/dots.git\n".encode())
    guest.access.answer = lambda call: answer if call.request.argv[-1:] == ("remote.origin.url",) else None

    sync_dotfiles(
        guest.files,
        source=f"git::{URL}",
        destination="~/.dotfiles",
        install_cmd="./install.sh",
        shell="bash",
        home="/home/a",
        managed=ADMIN_MANAGED,
        log=guest.log,
    )

    (warning,) = guest.log.warned
    assert secret not in warning
    (probe,) = [r for r in guest.commands() if isinstance(r, Command) and r.argv[-1:] == ("remote.origin.url",)]
    assert secret not in format_record(probe, answer, label="Dotfiles")


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="the checkout tests run git")


def _git(repo: Path, *argv: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false")
    done = subprocess.run(["git", *identity, "-C", str(repo), *argv], capture_output=True, check=True, text=True)
    return done.stdout.strip()


def _commit(repo: Path, content: str) -> str:
    (repo / "rc").write_text(content)
    _git(repo, "add", "rc")
    _git(repo, "commit", "-q", "-m", content)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "one")
    _git(repo, "tag", "-a", "v1", "-m", "v1")
    return repo


@requires_git
def test_a_changed_tag_ref_takes_effect_on_an_existing_clone(lane: Files, tmp_path: Path, origin: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    fetch_dir(SourceRef("git", str(origin), "", "v1"), lane, str(dest))
    _commit(origin, "two")
    _git(origin, "tag", "-a", "v2", "-m", "v2")

    fetch_dir(SourceRef("git", str(origin), "", "v2"), lane, str(dest))

    assert (dest / "rc").read_text() == "two"
    assert _git(dest, "rev-parse", "HEAD") == _git(origin, "rev-parse", "v2^{commit}")


@requires_git
def test_a_branch_ref_advances_on_an_existing_clone(lane: Files, tmp_path: Path, origin: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    fetch_dir(SourceRef("git", str(origin), "", "main"), lane, str(dest))
    advanced = _commit(origin, "two")

    fetch_dir(SourceRef("git", str(origin), "", "main"), lane, str(dest))

    assert _git(dest, "rev-parse", "HEAD") == advanced


@requires_git
def test_an_existing_clone_is_matched_by_its_configured_origin_not_an_insteadof_rewrite(
    lane: Files, tmp_path: Path, origin: Path
) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    subprocess.run(["git", "clone", "-q", "--", str(origin), str(dest)], check=True)
    _git(dest, "remote", "set-url", "origin", URL)
    _git(dest, "config", f"url.{origin}.insteadOf", URL)
    advanced = _commit(origin, "two")

    fetch_dir(SourceRef("git", URL, "", "main"), lane, str(dest))

    assert _git(dest, "rev-parse", "HEAD") == advanced


@requires_git
def test_dropping_the_ref_converges_on_the_remote_s_default_branch(lane: Files, tmp_path: Path, origin: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    fetch_dir(SourceRef("git", str(origin), "", "v1"), lane, str(dest))
    advanced = _commit(origin, "two")

    fetch_dir(SourceRef("git", str(origin), "", ""), lane, str(dest))

    assert _git(dest, "rev-parse", "HEAD") == advanced


@requires_git
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
def test_a_full_commit_hash_ref_is_checked_out(lane: Files, tmp_path: Path, origin: Path, existing: bool) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    pinned = _git(origin, "rev-parse", "HEAD")
    _commit(origin, "two")
    if existing:
        fetch_dir(SourceRef("git", str(origin), "", ""), lane, str(dest))

    fetch_dir(SourceRef("git", str(origin), "", pinned), lane, str(dest))

    assert _git(dest, "rev-parse", "HEAD") == pinned
    assert (dest / "rc").read_text() == "one"


@requires_git
@pytest.mark.parametrize("by", ["tag", "hash"])
def test_a_git_file_source_reads_the_file_at_a_tag_or_a_full_commit_hash(
    lane: Files, tmp_path: Path, origin: Path, by: str
) -> None:
    pinned = _git(origin, "rev-parse", "HEAD")
    _commit(origin, "two")
    destination = tmp_path / "home" / "rc"

    fetch_file(SourceRef("git", str(origin), "rc", "v1" if by == "tag" else pinned), lane, str(destination))

    assert destination.read_text() == "one"


# -- The tree upload ----------------------------------------------------------


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    return root


def _contents(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _siblings(dest: Path) -> list[str]:
    return sorted(name for name in os.listdir(dest.parent) if name.startswith(".agentworks-"))


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Files:
    if sys.platform != "linux":
        pytest.skip("the L lane runs agw-launch, a Linux guest program, on this machine")
    return local_vm_access(monkeypatch, home=tmp_path, shell=account_shell(tmp_path)).files(deadline=60)


def test_upload_tree_replaces_the_destination_whole(lane: Files, tmp_path: Path) -> None:
    dest = _tree(tmp_path / "home" / ".dotfiles", {"stale": b"gone", "keep/rc": b"old"})
    local = _tree(tmp_path / "local", {"keep/rc": b"new", "install.sh": b"#!/bin/sh\n"})

    fetch_dir(parse_source_ref(str(local)), lane, str(dest))

    assert _contents(dest) == {"install.sh": b"#!/bin/sh\n", "keep/rc": b"new"}
    assert _siblings(dest) == []


def test_upload_tree_creates_a_missing_destination_and_its_parent(lane: Files, tmp_path: Path) -> None:
    local = _tree(tmp_path / "local", {"rc": b"new"})
    dest = tmp_path / "home" / "nested" / ".dotfiles"

    upload_tree(lane, local, str(dest))

    assert _contents(dest) == {"rc": b"new"}


def test_a_truncated_archive_leaves_the_destination_byte_identical(
    lane: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt archive changes nothing: nothing moves before ``tar`` succeeds."""
    dest = _tree(tmp_path / "home" / ".dotfiles", {"rc": b"old", "nested/file": b"kept"})
    before = _contents(dest)
    local = _tree(tmp_path / "local", {"rc": b"new" * 4096})
    truncate = Bytes.__init__

    def truncated(self: Bytes, data: bytes) -> None:
        truncate(self, data[: len(data) // 2])

    monkeypatch.setattr(Bytes, "__init__", truncated)

    with pytest.raises(CommandFailed):
        upload_tree(lane, local, str(dest))

    assert _contents(dest) == before
    assert _siblings(dest) == []


@pytest.mark.windows
def test_upload_tree_sends_the_local_tree_as_one_unelevated_script_with_its_two_paths(tmp_path: Path) -> None:
    local = _tree(tmp_path / "local", {"rc": b"new", "nested/file": b"kept"})
    guest = StepGuest(local_files=False)

    upload_tree(guest.files, local, "/home/a/.dotfiles")

    (request,) = guest.access.requests()
    assert isinstance(request, Script) and request.source == UPLOAD_TREE_SCRIPT and not request.elevated
    assert request.env == {
        "AGW_TREE_DEST": "/home/a/.dotfiles",
        "AGW_TREE_TAG": hashlib.sha256(b".dotfiles").hexdigest()[:16],
    }
    assert isinstance(request.stdin, Bytes)
    with tarfile.open(fileobj=io.BytesIO(request.stdin.data), mode="r:gz") as archive:
        members = {member.name: archive.extractfile(member) for member in archive.getmembers() if member.isfile()}
        assert {name: handle.read() for name, handle in members.items() if handle} == {
            "./rc": b"new",
            "./nested/file": b"kept",
        }


def test_a_source_tree_with_a_read_only_directory_is_replaced_on_every_upload(lane: Files, tmp_path: Path) -> None:
    """A read-only directory the local tree itself holds arrives read-only, and the next upload still replaces it."""
    dest = tmp_path / "home" / ".dotfiles"
    try:
        for generation in range(3):
            local = _tree(tmp_path / f"local-{generation}", {"locked/rc": f"{generation}".encode(), "top": b"t"})
            (local / "locked").chmod(0o555)

            upload_tree(lane, local, str(dest))

            assert _contents(dest) == {"locked/rc": f"{generation}".encode(), "top": b"t"}
            assert _siblings(dest) == []
    finally:
        subprocess.run(["chmod", "-R", "u+rwx", "--", str(tmp_path)], check=False)


@pytest.mark.parametrize("mode", [0o555, 0o444], ids=["read-only", "unsearchable"])
def test_a_tree_with_a_locked_directory_is_replaced_on_every_upload(lane: Files, tmp_path: Path, mode: int) -> None:
    """A directory in the previous tree that its owner cannot write or search is opened up to remove it."""
    dest = tmp_path / "home" / ".dotfiles"
    try:
        for generation in range(3):
            local = _tree(tmp_path / f"local-{generation}", {"locked/rc": f"{generation}".encode(), "top": b"t"})

            upload_tree(lane, local, str(dest))

            assert _contents(dest) == {"locked/rc": f"{generation}".encode(), "top": b"t"}
            assert _siblings(dest) == []
            # As an installer might leave it, for the next upload to replace.
            (dest / "locked").chmod(mode)
    finally:
        subprocess.run(["chmod", "-R", "u+rwx", "--", str(tmp_path)], check=False)


@pytest.mark.parametrize("planted", ["link", "file"])
def test_upload_tree_refuses_a_link_or_file_at_the_destination(lane: Files, tmp_path: Path, planted: str) -> None:
    target = _tree(tmp_path / "target", {"rc": b"theirs"})
    dest = tmp_path / "home" / ".dotfiles"
    dest.parent.mkdir()
    if planted == "link":
        dest.symlink_to(target, target_is_directory=True)
    else:
        dest.write_bytes(b"a file")

    with pytest.raises(PathRefused):
        upload_tree(lane, _tree(tmp_path / "local", {"rc": b"new"}), str(dest))

    assert _contents(target) == {"rc": b"theirs"}
    assert dest.is_symlink() if planted == "link" else dest.read_bytes() == b"a file"


def _run_script(dest: Path, archive: bytes, *, path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess[bytes]:
    tag = hashlib.sha256(dest.name.encode()).hexdigest()[:16]
    return subprocess.run(
        ["/bin/sh", "-c", UPLOAD_TREE_SCRIPT],
        input=archive,
        env={"PATH": path, "AGW_TREE_DEST": str(dest), "AGW_TREE_TAG": tag},
        capture_output=True,
        check=False,
        timeout=30,
    )


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@requires_posix_tree
def test_a_signal_between_the_renames_restores_the_previous_tree(tmp_path: Path) -> None:
    dest = _tree(tmp_path / "home" / ".dotfiles", {"rc": b"old"})
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    # mv moves the destination aside, then signals the script before the second rename.
    (shadow / "mv").write_text(
        '#!/bin/sh\n/bin/mv "$@" || exit\ncase "$4" in */.agentworks-old-*) kill -TERM "$PPID"; sleep 5;; esac\n'
    )
    (shadow / "mv").chmod(0o755)

    result = _run_script(dest, _archive({"rc": b"new"}), path=f"{shadow}:/usr/bin:/bin")

    assert result.returncode == 143, result.stderr
    assert _contents(dest) == {"rc": b"old"}
    assert _siblings(dest) == []


@requires_posix_tree
def test_the_next_upload_moves_back_a_tree_a_killed_run_left_aside(tmp_path: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    tag = hashlib.sha256(dest.name.encode()).hexdigest()[:16]
    _tree(dest.parent / f".agentworks-old-{tag}", {"rc": b"old"})
    # A corrupt archive fails the extraction after recovery, so what is left is what recovery did.
    result = _run_script(dest, b"not an archive")

    assert result.returncode != 0
    assert _contents(dest) == {"rc": b"old"}
    assert _siblings(dest) == []


@pytest.mark.windows
@pytest.mark.parametrize("prefix", ["", "file::"])
def test_workstation_snapshot_uses_invoking_directory(tmp_path, monkeypatch, prefix: str) -> None:
    from agentworks.sources import snapshot_workstation_file

    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.json").write_bytes(b"captured\r\nbytes")
    snapshot = snapshot_workstation_file(f"{prefix}settings.json")
    (tmp_path / "settings.json").write_bytes(b"changed")
    assert snapshot == b"captured\r\nbytes"


@pytest.mark.windows
def test_workstation_snapshot_expands_invoking_home(tmp_path, monkeypatch) -> None:
    from agentworks.sources import snapshot_workstation_file

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "settings.toml").write_bytes(b"a = 1")
    assert snapshot_workstation_file("file::~/settings.toml") == b"a = 1"


@pytest.mark.parametrize("source", ["git::https://example.com/repo.git", "git::invalid-private-input", "file::", ""])
def test_workstation_snapshot_rejects_nonlocal_and_invalid_references(source: str) -> None:
    from agentworks.sources import snapshot_workstation_file

    with pytest.raises(SourceRefError):
        snapshot_workstation_file(source)


@pytest.mark.windows
def test_workstation_snapshot_rejects_missing_and_directory_sources(tmp_path) -> None:
    from agentworks.sources import snapshot_workstation_file

    for path in (tmp_path, tmp_path / "missing"):
        with pytest.raises(SourceRefError):
            snapshot_workstation_file(str(path))


@pytest.mark.windows
def test_workstation_snapshot_error_identifies_declared_source_without_io_cause(tmp_path) -> None:
    from agentworks.sources import snapshot_workstation_file

    source = str(tmp_path / "missing")
    with pytest.raises(SourceRefError) as caught:
        snapshot_workstation_file(source)
    assert repr(source) in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


# -- Dotfiles destinations ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("~/x", "~/x"),
        ("~/a/b/", "~/a/b"),
        ("x", "~/x"),
        ("a/b/", "~/a/b"),
        ("/x/y", "/x/y"),
        ("/x/y/", "/x/y"),
    ],
)
def test_normalize_destination_accepts_a_path_in_the_home_or_an_absolute_path(value: str, normalized: str) -> None:
    assert normalize_destination(value, managed=MANAGED) == normalized


@pytest.mark.parametrize(
    "value", ["~", "~/", "/", "", "./x", "~x", "~/~x", "~/../x", "~/a//b", "/a/./b", "/a/../b", "~/a\nb"]
)
def test_normalize_destination_refuses_the_home_another_users_home_and_unnormalized_components(value: str) -> None:
    with pytest.raises(DestinationError):
        normalize_destination(value, managed=MANAGED)


@pytest.mark.parametrize("managed", ADMIN_MANAGED)
def test_a_destination_that_is_holds_or_lies_inside_a_managed_path_is_refused(managed: str) -> None:
    parts = [*managed.split("/"), "inside"]
    for depth in range(1, len(parts) + 1):
        relative = "/".join(parts[:depth])
        for value in (f"~/{relative}", relative):
            with pytest.raises(DestinationError):
                normalize_destination(value, managed=ADMIN_MANAGED)
        with pytest.raises(SourceRefError):
            expand_destination(f"/home/admin/{relative}", "/home/admin", managed=ADMIN_MANAGED)


@pytest.mark.parametrize(
    "value",
    [
        "~/.ssh",
        "~/.ssh/dots",
        "~/.agentworks",
        "~/.agentworks/git-credentials",
        "~/.agentworks-artifacts",
        "~/.agentworks-artifacts/user",
        "~/.agentworks-profile.sh",
        "~/.agentworks-rc.sh",
        "~/.agentworks/codex",
    ],
)
def test_the_paths_initialization_writes_in_every_home_are_refused_for_both_accounts(value: str) -> None:
    for managed in (MANAGED, ADMIN_MANAGED):
        with pytest.raises(DestinationError):
            normalize_destination(value, managed=managed)


@pytest.mark.parametrize("value", ["~/.config", "~/.config/tmuxinator", "~/.config/tmuxinator/dots"])
def test_the_workspace_tmuxinator_links_are_refused_for_the_admin_and_not_an_agent(value: str) -> None:
    with pytest.raises(DestinationError):
        normalize_destination(value, managed=ADMIN_MANAGED)
    with pytest.raises(SourceRefError):
        expand_destination(f"/home/admin/{value[2:]}", "/home/admin", managed=ADMIN_MANAGED)
    assert normalize_destination(value, managed=MANAGED) == value
    assert expand_destination(f"/home/agent/{value[2:]}", "/home/agent", managed=MANAGED)


def test_each_template_applies_its_account_s_set() -> None:
    with pytest.raises(ValidationError):
        AdminConfig(name="t", dotfiles_destination="~/.config")
    assert AgentTemplate(name="t", dotfiles_destination="~/.config").dotfiles_destination == "~/.config"


@pytest.mark.parametrize("value", ["~/.config/mise", "~/.config/mise/dots", "~/.config/nvim", "~/.agentworks-dots"])
def test_a_destination_beside_or_over_the_mise_config_is_accepted(value: str) -> None:
    assert normalize_destination(value, managed=MANAGED) == value


def test_a_refused_destination_names_no_part_of_the_value() -> None:
    for value in ("~/secret-name/../x", "secret-name//x", "~secret-name"):
        with pytest.raises(DestinationError) as refused:
            normalize_destination(value, managed=MANAGED)
        assert "secret-name" not in str(refused.value)
        assert value not in str(refused.value)


@pytest.mark.parametrize("model", [AdminConfig, AgentTemplate])
def test_both_templates_refuse_an_unsuitable_destination_at_load(model: type) -> None:
    with pytest.raises(ValidationError) as refused:
        model(name="t", dotfiles_destination="~/")
    assert [error["loc"] for error in refused.value.errors()] == [("dotfiles_destination",)]
    assert model(name="t", dotfiles_destination="dots/").dotfiles_destination == "~/dots"


@pytest.mark.parametrize("model", [AdminConfig, AgentTemplate])
@pytest.mark.parametrize(
    "source",
    [
        "git::https://u:ghp-load-secret@example.com/d.git",
        "s3::ghp-load-secret",
        "https://u:ghp-load-secret@example.com/d.git",
        "file::https://ghp-load-secret@example.com/d.git",
        "git@ghp-load-secret.example.com:me/d.git",
        "user:ghp-load-secret@example.com:me/d.git",
    ],
)
def test_both_templates_refuse_an_unparsable_dotfiles_source_at_load_without_echoing_it(
    model: type, source: str
) -> None:
    with pytest.raises(ValidationError) as refused:
        model(name="t", dotfiles_source=source)

    assert [error["loc"] for error in refused.value.errors()] == [("dotfiles_source",)]
    assert not any("ghp-load-secret" in error["msg"] for error in refused.value.errors(include_input=False))
    assert model(name="t", dotfiles_source="git::https://example.com/d.git?ref=v1").dotfiles_source


def test_a_stored_unparsable_dotfiles_source_is_refused_with_the_accepted_forms() -> None:
    from agentworks.agents.instance_overlay import decode_overlay
    from agentworks.errors import ValidationError as OverlayError
    from agentworks.sources import SourceRefFormError, check_source_ref
    from agentworks.vms.instance_overlay import decode_admin_overlay

    stored = "git::https://u:ghp-stored-secret@example.com/d.git"
    with pytest.raises(SourceRefFormError) as direct:
        check_source_ref(stored, field="dotfiles_source")
    for decode in (decode_overlay, decode_admin_overlay):
        with pytest.raises(OverlayError) as decoded:
            decode({"dotfiles_source": stored})
        assert str(direct.value) in str(decoded.value)
        assert "ghp-stored-secret" not in str(decoded.value)


def test_a_stored_relative_destination_decodes_into_the_home_and_a_refusal_names_the_accepted_forms() -> None:
    from agentworks.agents.instance_overlay import decode_overlay
    from agentworks.errors import ValidationError as OverlayError
    from agentworks.vms.instance_overlay import decode_admin_overlay

    for decode in (decode_overlay, decode_admin_overlay):
        stored = decode({"dotfiles_destination": "dotfiles"})
        assert stored.dotfiles_destination is not None
        assert expand_destination(stored.dotfiles_destination, "/home/agent", managed=MANAGED) == "/home/agent/dotfiles"

        with pytest.raises(DestinationError) as direct:
            normalize_destination("~/secret/../x", managed=MANAGED)
        with pytest.raises(OverlayError) as decoded:
            decode({"dotfiles_destination": "~/secret/../x"})
        assert str(direct.value) in str(decoded.value)
        assert "secret" not in str(decoded.value)


def test_expand_destination_replaces_only_the_leading_tilde() -> None:
    assert expand_destination("~/dots~1/a~b", "/home/agent", managed=MANAGED) == "/home/agent/dots~1/a~b"
    assert expand_destination("/srv/dots~", "/home/agent", managed=MANAGED) == "/srv/dots~"


@pytest.mark.parametrize("value", ["/home/agent", "/home", "/"])
def test_expand_destination_refuses_the_home_and_its_parents(value: str) -> None:
    with pytest.raises(SourceRefError):
        expand_destination(value, "/home/agent", managed=MANAGED)

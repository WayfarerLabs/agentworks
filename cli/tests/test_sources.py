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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, PathRefused
from agentworks.execution.requests import Bytes, Command
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
from agentworks.vms.home_layout import MANAGED
from tests.phase_b_support import StepGuest, requires_file_program
from tests.vm_access_support import account_shell, local_vm_access

if TYPE_CHECKING:
    from agentworks.execution.files import Files

requires_posix_tree = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tar") != "/usr/bin/tar" or not Path("/bin/mv").exists(),
    reason="the tree upload script runs /usr/bin/tar and mv -T under /bin/sh",
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


MKTEMP = Command(("mktemp", "-d", "/var/tmp/agentworks-source-ref-XXXXXX"), deadline=60)


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


def test_fetch_file_refuses_a_local_source_it_cannot_read(tmp_path: Path) -> None:
    guest = StepGuest(local_files=False)

    with pytest.raises(SourceRefError):
        fetch_file(parse_source_ref(str(tmp_path / "absent.lock")), guest.files, "/home/a/mise.lock")
    with pytest.raises(SourceRefError):
        fetch_file(parse_source_ref(str(tmp_path)), guest.files, "/home/a/mise.lock")
    assert guest.access.requests() == []


@requires_file_program
def test_fetch_file_reads_a_git_source_back_from_a_shallow_clone_and_removes_it(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
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
        Command(
            ("git", "clone", "--depth", "1", "--branch", "v1", "https://example.com/repo.git", str(clone)), deadline=60
        ),
        Command(("rm", "-rf", "--", str(clone)), deadline=60),
    ]
    assert [operation for operation, _ in guest.file_operations()] == ["read", "write"]


@requires_file_program
@pytest.mark.parametrize("planted", ["absent", "link"])
def test_a_git_source_without_a_regular_file_publishes_nothing(tmp_path: Path, planted: str) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
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


def test_fetch_file_cleanup_failure_preserves_fetch_error(captured_output) -> None:  # noqa: ANN001
    guest = _staging_guest()
    primary = CommandFailed("box", 128, Result(Completed(128)), streams_logged=True)
    guest.respond(Command(("git", "clone", "--depth", "1", URL, STAGING), deadline=60), primary)
    guest.respond(
        Command(("rm", "-rf", "--", STAGING), deadline=60),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
    )
    ref = parse_source_ref(f"git::{URL}//locks/mise.lock")

    with pytest.raises(CommandFailed) as caught:
        fetch_file(ref, guest.files, "/remote/mise.lock")

    assert caught.value is primary
    assert captured_output.warnings


# -- Git directories ----------------------------------------------------------

DEST = "/home/a/.dotfiles"
IS_CLONE = Command(("test", "-d", f"{DEST}/.git"), deadline=60)
EXISTS = Command(("test", "-d", DEST), deadline=60)
ORIGIN = Command(("git", "-C", DEST, "remote", "get-url", "origin"), deadline=60)
PULL = Command(("git", "-C", DEST, "pull", "--ff-only"), deadline=120)


def test_fetch_dir_clones_into_an_absent_destination() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(IS_CLONE, Result(Completed(1)))
    guest.respond(EXISTS, Result(Completed(1)))

    fetch_dir(parse_source_ref(f"git::{URL}?ref=main"), guest.files, DEST)

    assert guest.commands() == [
        IS_CLONE,
        EXISTS,
        Command(("git", "clone", "--branch", "main", "--", URL, DEST), deadline=120),
    ]


def test_fetch_dir_fast_forwards_a_clone_of_the_same_repository_with_no_ref() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(ORIGIN, Result(Completed(0), f"{URL}\n".encode()))

    fetch_dir(parse_source_ref(f"git::{URL}"), guest.files, DEST)

    assert guest.commands() == [IS_CLONE, ORIGIN, PULL]


@pytest.mark.parametrize("failing", ["fetch", "checkout"])
def test_a_failed_fetch_or_checkout_of_the_ref_fails_the_sync(failing: str) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(ORIGIN, Result(Completed(0), f"{URL}\n".encode()))
    fetch = Command(("git", "-C", DEST, "fetch", "origin", "v2"), deadline=120)
    checkout = Command(("git", "-C", DEST, "checkout", "--detach", "FETCH_HEAD"), deadline=120)
    failed = fetch if failing == "fetch" else checkout
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

    assert PULL not in guest.commands()
    assert not any(isinstance(request, Command) and request.argv[1] == "clone" for request in guest.commands())


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
    _git(repo, "tag", "v1")
    return repo


@requires_git
def test_a_changed_tag_ref_takes_effect_on_an_existing_clone(lane: Files, tmp_path: Path, origin: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    fetch_dir(SourceRef("git", str(origin), "", "v1"), lane, str(dest))
    _commit(origin, "two")
    _git(origin, "tag", "v2")

    fetch_dir(SourceRef("git", str(origin), "", "v2"), lane, str(dest))

    assert (dest / "rc").read_text() == "two"
    assert _git(dest, "rev-parse", "HEAD") == _git(origin, "rev-parse", "v2")


@requires_git
def test_a_branch_ref_advances_on_an_existing_clone(lane: Files, tmp_path: Path, origin: Path) -> None:
    dest = tmp_path / "home" / ".dotfiles"
    fetch_dir(SourceRef("git", str(origin), "", "main"), lane, str(dest))
    advanced = _commit(origin, "two")

    fetch_dir(SourceRef("git", str(origin), "", "main"), lane, str(dest))

    assert _git(dest, "rev-parse", "HEAD") == advanced


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


def test_a_tree_with_a_read_only_directory_is_replaced_on_every_upload(lane: Files, tmp_path: Path) -> None:
    """The previous tree's read-only directory is made writable to remove it, upload after upload."""
    dest = tmp_path / "home" / ".dotfiles"
    for generation in range(3):
        local = _tree(tmp_path / f"local-{generation}", {"locked/rc": f"{generation}".encode(), "top": b"t"})
        (local / "locked").chmod(0o555)

        upload_tree(lane, local, str(dest))

        assert _contents(dest) == {"locked/rc": f"{generation}".encode(), "top": b"t"}
        assert _siblings(dest) == []
    for directory in (path for path in tmp_path.rglob("*") if path.is_dir()):
        directory.chmod(0o755)


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
        ("~/.config/mise/dots", "~/.config/mise/dots"),
    ],
)
def test_normalize_destination_accepts_a_path_in_the_home_or_an_absolute_path(value: str, normalized: str) -> None:
    assert normalize_destination(value) == normalized


@pytest.mark.parametrize(
    "value", ["~", "~/", "/", "", "./x", "~x", "~/~x", "~/../x", "~/a//b", "/a/./b", "/a/../b", "~/a\nb"]
)
def test_normalize_destination_refuses_the_home_another_users_home_and_unnormalized_components(value: str) -> None:
    with pytest.raises(DestinationError):
        normalize_destination(value)


@pytest.mark.parametrize("managed", MANAGED)
def test_a_destination_that_is_or_holds_a_path_agentworks_manages_in_the_home_is_refused(managed: str) -> None:
    parts = managed.split("/")
    for depth in range(1, len(parts) + 1):
        relative = "/".join(parts[:depth])
        for value in (f"~/{relative}", relative):
            with pytest.raises(DestinationError):
                normalize_destination(value)
        with pytest.raises(SourceRefError):
            expand_destination(f"/home/agent/{relative}", "/home/agent")


def test_a_refused_destination_names_no_part_of_the_value() -> None:
    for value in ("~/secret-name/../x", "secret-name//x", "~secret-name"):
        with pytest.raises(DestinationError) as refused:
            normalize_destination(value)
        assert "secret-name" not in str(refused.value)
        assert value not in str(refused.value)


@pytest.mark.parametrize("model", [AdminConfig, AgentTemplate])
def test_both_templates_refuse_an_unsuitable_destination_at_load(model: type) -> None:
    with pytest.raises(ValidationError) as refused:
        model(name="t", dotfiles_destination="~/")
    assert [error["loc"] for error in refused.value.errors()] == [("dotfiles_destination",)]
    assert model(name="t", dotfiles_destination="dots/").dotfiles_destination == "~/dots"


def test_a_stored_relative_destination_decodes_into_the_home_and_a_refusal_names_the_accepted_forms() -> None:
    from agentworks.agents.instance_overlay import decode_overlay
    from agentworks.errors import ValidationError as OverlayError
    from agentworks.vms.instance_overlay import decode_admin_overlay

    for decode in (decode_overlay, decode_admin_overlay):
        stored = decode({"dotfiles_destination": "dotfiles"})
        assert stored.dotfiles_destination is not None
        assert expand_destination(stored.dotfiles_destination, "/home/agent") == "/home/agent/dotfiles"

        with pytest.raises(DestinationError) as direct:
            normalize_destination("~/secret/../x")
        with pytest.raises(OverlayError) as decoded:
            decode({"dotfiles_destination": "~/secret/../x"})
        assert str(direct.value) in str(decoded.value)
        assert "secret" not in str(decoded.value)


def test_expand_destination_replaces_only_the_leading_tilde() -> None:
    assert expand_destination("~/dots~1/a~b", "/home/agent") == "/home/agent/dots~1/a~b"
    assert expand_destination("/srv/dots~", "/home/agent") == "/srv/dots~"


@pytest.mark.parametrize("value", ["/home/agent", "/home", "/"])
def test_expand_destination_refuses_the_home_and_its_parents(value: str) -> None:
    with pytest.raises(SourceRefError):
        expand_destination(value, "/home/agent")

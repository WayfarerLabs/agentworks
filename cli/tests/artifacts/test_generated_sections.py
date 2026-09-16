"""Generated instruction sections coexist with edits outside their delimiters."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.artifacts.application import ArtifactFile, ArtifactPublication, OwnedArtifactFile
from agentworks.artifacts.publication import publish_artifacts
from agentworks.artifacts.sections import BEGIN, END
from agentworks.errors import StateError
from tests.native_setup_fixtures import LocalFixtureTransport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


@pytest.fixture
def target(tmp_path):
    return LocalFixtureTransport(tmp_path / "target")


def section(path: Path, body: bytes = b"managed instructions") -> ArtifactFile:
    return ArtifactFile(str(path), body, ("a" * 64,), generated_section=True)


def apply(
    target: LocalFixtureTransport,
    desired: tuple[ArtifactFile, ...],
    previous: tuple[OwnedArtifactFile, ...] = (),
) -> ArtifactPublication:
    return publish_artifacts(target, desired, previous, lambda files: None, roots=(str(target.home),))


@pytest.mark.parametrize("initial", [None, b"", b"operator content without newline", b"operator content\r\n"])
def test_append_replace_external_edits_and_retire(target, initial):
    path = target.home / "AGENTS.md"
    if initial is not None:
        path.write_bytes(initial)
        path.chmod(0o640)
    first = apply(target, (section(path),))
    assert first.files[0].generated_section
    assert not first.skipped
    if initial is not None:
        assert path.stat().st_mode & 0o777 == 0o640
        assert path.read_bytes().startswith(initial)
    path.write_bytes(b"new operator prefix\n" + BEGIN + b"\nchanged generated contents\n" + END + b"\noperator suffix")
    path.chmod(0o644)
    metadata = path.stat()
    second = apply(target, (section(path, b"replacement"),), first.files)
    assert path.read_bytes() == b"new operator prefix\n" + BEGIN + b"\nreplacement\n" + END + b"\noperator suffix"
    assert (path.stat().st_uid, path.stat().st_gid, path.stat().st_mode) == (
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
    )
    before = path.stat().st_mtime_ns
    assert apply(target, (section(path, b"replacement"),), second.files) == second
    assert path.stat().st_mtime_ns == before
    assert not apply(target, (), second.files).files
    assert path.read_bytes() == b"new operator prefix\n\noperator suffix"
    assert path.stat().st_mode == metadata.st_mode


def test_valid_unrecorded_section_is_replaced(target):
    path = target.home / "AGENTS.md"
    path.write_bytes(BEGIN + b"\nunrecorded bytes\n" + END)
    applied = apply(target, (section(path),))
    assert applied.files and not applied.skipped
    assert path.read_bytes() == BEGIN + b"\nmanaged instructions\n" + END


@pytest.mark.parametrize(
    "contents",
    [
        BEGIN + b"\npartial",
        END,
        END + b"\n" + BEGIN,
        BEGIN + b"\n" + BEGIN + b"\n" + END,
        BEGIN + b"\n" + END + b"\n" + END,
        b"prefix " + BEGIN + b"\n" + END,
    ],
)
def test_malformed_section_skips_without_blocking_other_artifacts(target, contents, captured_output):
    path = target.home / "AGENTS.md"
    path.write_bytes(contents)
    other = target.home / "skill.md"
    result = apply(target, (section(path), ArtifactFile(str(other), b"skill", ("b" * 64,))))
    assert path.read_bytes() == contents and other.read_bytes() == b"skill"
    assert [item.path for item in result.files] == [str(other)]
    assert result.skipped[0].path == str(path) and result.skipped[0].origins == ("a" * 64,)
    assert len(captured_output.warnings) == 1


def test_malformed_retirement_retains_cleanup_evidence(target):
    path = target.home / "AGENTS.md"
    first = apply(target, (section(path),))
    path.write_bytes(BEGIN + b"\noperator broke delimiter")
    result = apply(target, (), first.files)
    assert result.files == first.files and result.skipped
    path.write_bytes(b"operator removed section entirely")
    assert not apply(target, (), result.files).files
    assert path.read_bytes() == b"operator removed section entirely"


def test_section_race_does_not_overwrite_concurrent_operator_edit(target, monkeypatch):
    from agentworks.native_files import NativeFiles

    path = target.home / "AGENTS.md"
    path.write_bytes(b"initial operator text")
    publish = NativeFiles.publish

    def concurrent_edit(self, destination, content, **kwargs):
        path.write_bytes(b"concurrent edit")
        return publish(self, destination, content, **kwargs)

    monkeypatch.setattr(NativeFiles, "publish", concurrent_edit)
    with pytest.raises(StateError):
        apply(target, (section(path),))
    assert path.read_bytes() == b"concurrent edit"


def test_root_publication_elevates_only_guarded_operations_and_sets_public_modes(target):
    # Record elevation at the transport boundary, while executing inside this
    # unprivileged fixture. No test changes the host's machine-wide directories.
    run = target.run
    elevated = []

    def run_guarded(command, **kwargs):
        if command.startswith("sudo -n -- "):
            elevated.append(command)
            command = command.removeprefix("sudo -n -- ")
        return run(command, **kwargs)

    target.run = run_guarded
    directory = target.root / "machine/artifacts/skill"
    desired = (
        replace(section(directory / "AGENTS.md"), generated_section=True),
        ArtifactFile(str(directory / "run"), b"executable", ("b" * 64,), executable=True),
    )
    result = publish_artifacts(target, desired, (), lambda files: None, roots=("/",), root=True)
    assert len(result.files) == 2 and elevated
    assert (directory / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert (directory / "run").stat().st_mode & 0o777 == 0o755
    assert directory.stat().st_mode & 0o777 == 0o755
    assert directory.parent.stat().st_mode & 0o777 == 0o755
    assert all("python3 -c" in command for command in elevated)
    assert not list((target.root / "tmp").iterdir())
    # Reapplication reads a protected destination into transport-owned staging.
    assert publish_artifacts(target, desired, result.files, lambda files: None, roots=("/",), root=True) == result
    assert (directory / "AGENTS.md").stat().st_uid == os.getuid()


@pytest.mark.parametrize("original_section", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_ownership_mode_switch_refuses_existing_destination_before_any_writes(target, original_section, missing):
    path = target.home / "AGENTS.md"
    if original_section:
        path.write_bytes(b"unmanaged instructions\n")
    original = replace(section(path), generated_section=original_section)
    first = apply(target, (original,))
    before = path.read_bytes()
    if missing:
        path.unlink()
    other = target.home / "other-rule.md"
    desired = (
        replace(original, path=str(other)),
        replace(original, generated_section=not original_section, data=b"replacement"),
    )
    if missing:
        result = apply(target, desired, first.files)
        assert len(result.files) == 2
        assert next(item for item in result.files if item.path == str(path)).generated_section != original_section
        assert path.read_bytes() != before
    else:
        with pytest.raises(StateError):
            apply(target, desired, first.files)
        assert path.read_bytes() == before
        assert not other.exists()


def test_generated_section_preserves_extended_attributes_on_update_and_retirement(target):
    path = target.home / "AGENTS.md"
    path.write_bytes(b"operator instructions\n")
    os.setxattr(path, "user.artifact-test", b"operator metadata")
    first = apply(target, (section(path),))
    assert os.getxattr(path, "user.artifact-test") == b"operator metadata"
    os.setxattr(path, "user.artifact-test", b"updated metadata")
    second = apply(target, (section(path, b"new instructions"),), first.files)
    assert os.getxattr(path, "user.artifact-test") == b"updated metadata"
    assert not apply(target, (), second.files).files
    assert os.getxattr(path, "user.artifact-test") == b"updated metadata"


def test_generated_section_preserves_posix_acl(target):
    import shutil
    import subprocess

    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    path = target.home / "AGENTS.md"
    path.write_bytes(b"operator instructions\n")
    subprocess.run([setfacl, "-m", f"u:{os.getuid()}:r", str(path)], check=True, capture_output=True)
    acl = os.getxattr(path, "system.posix_acl_access")
    mode = path.stat().st_mode
    first = apply(target, (section(path),))
    assert os.getxattr(path, "system.posix_acl_access") == acl
    assert path.stat().st_mode == mode
    assert not apply(target, (), first.files).files
    assert os.getxattr(path, "system.posix_acl_access") == acl
    assert path.stat().st_mode == mode


def test_attribute_copy_failure_leaves_original_unchanged(target, monkeypatch):
    import agentworks.native_files as native

    path = target.home / "AGENTS.md"
    before = b"operator instructions\n"
    path.write_bytes(before)
    os.setxattr(path, "user.artifact-test", b"operator metadata")
    metadata = path.stat()
    monkeypatch.setattr(
        native,
        "_FILE_PROGRAM",
        "import os, errno\n"
        "def deny_attribute_copy(*args):\n"
        "    raise OSError(errno.EPERM, 'fixture attribute copy denial')\n"
        "os.setxattr = deny_attribute_copy\n" + native._FILE_PROGRAM,
    )
    with pytest.raises(StateError):
        apply(target, (section(path),))
    assert path.read_bytes() == before
    assert path.stat().st_ino == metadata.st_ino
    assert os.getxattr(path, "user.artifact-test") == b"operator metadata"
    assert not list(path.parent.glob(".agentworks-settings-*"))


def test_generated_section_does_not_inherit_a_new_directory_acl(target):
    import errno
    import shutil
    import subprocess

    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl is required to provision the ACL fixture")
    path = target.home / "AGENTS.md"
    path.write_bytes(b"operator instructions\n")
    assert "system.posix_acl_access" not in os.listxattr(path)
    subprocess.run([setfacl, "-d", "-m", f"u:{os.getuid()}:r", str(path.parent)], check=True, capture_output=True)
    apply(target, (section(path),))
    with pytest.raises(OSError) as error:
        os.getxattr(path, "system.posix_acl_access")
    assert error.value.errno == errno.ENODATA

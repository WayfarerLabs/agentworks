"""Source-boundary and normalized-input behavior without external services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import (
    AgentArtifactSpec,
    ArtifactSpec,
    HintArtifactSpec,
    RuleArtifactSpec,
    SkillArtifactSpec,
)
from agentworks.artifacts.model import ArtifactGroup, ArtifactOrigin
from agentworks.package_sources import DEFAULT_CAPTURE_LIMITS, CaptureLimits, PackageCapture, validate_member_set
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import group
from tests.conftest import requires_symlinks

pytestmark = pytest.mark.windows

ORIGIN = ArtifactOrigin("agent", "agent", "worker")


def skill(tmp_path: Path) -> Path:
    root = tmp_path / "review"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"---\r\nname: review\r\ndescription: Review code\r\n---\r\nRead it.\r")
    return root


def capture(
    spec: ArtifactSpec,
    *,
    origin: ArtifactOrigin = ORIGIN,
    limits: CaptureLimits = DEFAULT_CAPTURE_LIMITS,
    name: str = "review",
) -> ArtifactGroup:
    field = {
        HintArtifactSpec: "hints",
        RuleArtifactSpec: "rules",
        SkillArtifactSpec: "skills",
        AgentArtifactSpec: "agents",
    }[type(spec)]
    bundle = ArtifactBundle(name="team", **{field: {name: spec}})
    return capture_artifacts([("team", bundle)], origin, limits=limits)


def test_complete_skill_text_and_opaque_roundtrip(tmp_path):
    root = skill(tmp_path)
    (root / "asset.pdf").write_bytes(b"%PDF-ASCII\r\nopaque\r")
    (root / "fixture.txt").write_bytes(b"original\r\n")
    (root / "script").write_bytes(b"#!/bin/sh\r\nexit 0\r")
    (root / "script").chmod(0o755)
    inputs = capture(SkillArtifactSpec(source=str(root), preserve_bytes=["fixture.*"]))
    content = tuple(inputs.items())[0].content
    members = {member.path: member for member in content.members}
    assert b"\r" not in members["SKILL.md"].data
    assert members["asset.pdf"].data == b"%PDF-ASCII\r\nopaque\r"
    assert members["fixture.txt"].data == b"original\r\n"
    assert members["script"].data.endswith(b"\r")
    if os.name != "nt":
        assert members["script"].executable
    assert decode_inputs(json.loads(json.dumps(encode_inputs(inputs)))) == inputs
    metadata = content.metadata
    metadata["name"] = "changed"
    assert content.metadata["name"] == "review"


def test_content_identity_excludes_provenance_and_includes_executable(tmp_path):
    root = skill(tmp_path)
    (root / "script.sh").write_text("echo hi\n")
    item = tuple(capture(SkillArtifactSpec(source=str(root))).items())[0]
    other = replace(item, provenance=replace(item.provenance, source="elsewhere"))
    assert other.content.digest == item.content.digest
    assert other.identity == item.identity
    changed_member = replace(item.content.members[-1], executable=True)
    assert replace(item.content, members=(*item.content.members[:-1], changed_member)).digest != item.content.digest
    assert replace(item, origin=replace(item.origin, resource_name="another")).identity != item.identity


@pytest.mark.parametrize("name", ["../bad", "/bad", "a\\b", "AUX.txt", "a.", ".git/config", "a//b", "e\u0301.md"])
def test_unsafe_package_paths(name):
    with pytest.raises(SourceRefError):
        validate_member_set([name])


@pytest.mark.parametrize("paths", [["A/x", "a/y"], ["x", "x/y"], ["a", "a"]])
def test_portable_collisions(paths):
    with pytest.raises(SourceRefError):
        validate_member_set(paths)


@requires_symlinks
def test_symlinks_are_rejected(tmp_path):
    root = skill(tmp_path)
    link = root / "link"
    link.symlink_to(root / "SKILL.md")
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source=str(root)))


def test_metadata_and_special_files_are_rejected(tmp_path):
    root = skill(tmp_path)
    (root / ".git").mkdir()
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source=str(root)))
    (root / ".git").rmdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")
        with pytest.raises(SourceRefError):
            capture(SkillArtifactSpec(source=str(root)))


@pytest.mark.parametrize(
    "limits",
    [
        CaptureLimits(member_bytes=3),
        CaptureLimits(total_bytes=3),
        CaptureLimits(members=0),
        CaptureLimits(seconds=0),
        CaptureLimits(depth=1),
    ],
)
def test_capture_limits_and_temporary_cleanup(tmp_path, limits):
    root = skill(tmp_path)
    (root / "nested").mkdir()
    (root / "nested" / "file").write_text("abc")
    with PackageCapture(limits) as operation:
        staging = operation.root
        with pytest.raises(SourceRefError):
            operation.capture(str(root))
    assert not staging.exists()


def test_capture_accepts_exact_member_budget(tmp_path):
    root = skill(tmp_path)
    (root / "support.txt").write_text("support")
    with PackageCapture(CaptureLimits(members=2)) as operation:
        assert len(operation.capture(str(root)).members) == 2


def test_capture_failure_releases_source_handles(tmp_path):
    root = skill(tmp_path)
    with PackageCapture(CaptureLimits(member_bytes=1)) as operation, pytest.raises(SourceRefError):
        operation.capture(str(root))
    root.rename(tmp_path / "moved")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor lifetime")
@pytest.mark.parametrize("fail", [False, True])
def test_capture_closes_all_opened_descriptors(tmp_path, monkeypatch, fail):
    root = skill(tmp_path)
    opened = set()
    original_open, original_close = os.open, os.close

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def record_close(descriptor):
        original_close(descriptor)
        opened.discard(descriptor)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)
    with PackageCapture(CaptureLimits(member_bytes=1) if fail else DEFAULT_CAPTURE_LIMITS) as operation:
        if fail:
            with pytest.raises(SourceRefError):
                operation.capture(str(root))
        else:
            operation.capture(str(root))
    assert not opened


def test_observed_mutation_is_rejected(tmp_path, monkeypatch):
    root = skill(tmp_path)
    original = PackageCapture.account

    def mutate(operation, size):
        original(operation, size)
        (root / "SKILL.md").write_text("changed")

    monkeypatch.setattr(PackageCapture, "account", mutate)
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source=str(root)))


@requires_symlinks
@pytest.mark.parametrize("part", ["ancestor", "package", "nested", "rule.md"])
def test_linked_source_components_are_rejected(tmp_path, part):
    source = tmp_path / "ancestor" / "package" / "nested" / "rule.md"
    source.parent.mkdir(parents=True)
    source.write_text("selected content")
    target = next(item for item in (source, *source.parents) if item.name == part)
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with PackageCapture() as operation, pytest.raises(SourceRefError):
        operation.capture(str(source))


@requires_symlinks
@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race; Windows locks prevent replacement")
@pytest.mark.parametrize("part", ["ancestor", "package", "nested", "rule.md"])
@pytest.mark.parametrize("timing", ["before_open", "after_open"])
def test_component_swap_cannot_capture_outside_payload(tmp_path, monkeypatch, part, timing):
    import agentworks.package_sources as sources

    source = tmp_path / "ancestor" / "package" / "nested" / "rule.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"selected content")
    outside = tmp_path / "outside" / "package" / "nested" / "rule.md"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"outside content")
    target = next(item for item in (source, *source.parents) if item.name == part)
    replacement = (
        tmp_path / "outside"
        if part == "ancestor"
        else next(item for item in (outside, *outside.parents) if item.name == part)
    )
    original = os.open
    swapped = False
    payloads = []
    original_member = sources.PackageMember

    def record(*args):
        member = original_member(*args)
        payloads.append(member.data)
        return member

    def open_and_swap(name, flags, *args, **kwargs):
        nonlocal swapped
        if name != part or swapped:
            return original(name, flags, *args, **kwargs)
        swapped = True
        descriptor = original(name, flags, *args, **kwargs) if timing == "after_open" else None
        target.rename(tmp_path / "original")
        target.symlink_to(replacement, target_is_directory=replacement.is_dir())
        return descriptor if descriptor is not None else original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_and_swap)
    monkeypatch.setattr(sources, "PackageMember", record)
    with PackageCapture() as operation, pytest.raises(SourceRefError):
        operation.capture(str(source.parent.parent))
    assert swapped
    assert b"outside content" not in payloads


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing locks")
@pytest.mark.parametrize("part", ["ancestor", "package", "nested", "rule.md"])
def test_windows_capture_locks_each_component_until_read_finishes(tmp_path, monkeypatch, part):
    source = tmp_path / "ancestor" / "package" / "nested" / "rule.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"selected content")
    target = next(item for item in (source, *source.parents) if item.name == part)
    original = PackageCapture.account
    attempted = False

    def attempt_replace(operation, size):
        nonlocal attempted
        original(operation, size)
        attempted = True
        with pytest.raises(PermissionError):
            target.rename(tmp_path / "moved")

    monkeypatch.setattr(PackageCapture, "account", attempt_replace)
    with PackageCapture() as operation:
        package = operation.capture(str(source.parent.parent))
    assert attempted
    assert package.members[0].data == b"selected content"
    target.rename(tmp_path / "moved")  # All native handles were released.


def test_preserve_bytes_cannot_skip_skill_entrypoint(tmp_path):
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source=str(skill(tmp_path)), preserve_bytes=["*.md"]))


@pytest.mark.parametrize("body", [b"\xff", b"nul\0text"])
def test_invalid_designated_text_is_rejected(tmp_path, body):
    path = tmp_path / "rule.md"
    path.write_bytes(body)
    with pytest.raises(SourceRefError):
        capture(RuleArtifactSpec(source=str(path)))


def test_persona_metadata_and_options_are_immutable(tmp_path):
    path = tmp_path / "agent.md"
    path.write_text(
        "---\nname: reviewer\ndescription: Review\nnative_options:\n  codex:\n    model: example\n---\nCheck changes.\n"
    )
    item = tuple(capture(AgentArtifactSpec(source=str(path)), name="reviewer").items())[0]
    assert item.content.name == "reviewer"
    assert item.content.native_options == {"codex": {"model": "example"}}
    assert item.content.text == "Check changes.\n"
    assert decode_inputs(encode_inputs(group(item))) == group(item)


@pytest.mark.parametrize("metadata", ["hooks: {}", "mcpServers: {}", "x: &x [*x]", "compatibility: [wrong]"])
def test_invalid_skill_metadata_is_rejected(tmp_path, metadata):
    root = skill(tmp_path)
    (root / "SKILL.md").write_text(f"---\nname: review\ndescription: Review\n{metadata}\n---\nDo it.\n")
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source=str(root)))


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.test")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "core.autocrlf", "false")
    root = skill(repo)
    (root / "SKILL.md").write_bytes((root / "SKILL.md").read_bytes() + b"$Format:%H$\n")
    (root / ".gitattributes").write_text("*.txt export-ignore\n*.md export-subst\n*.dat filter=fixture\n")
    (root / "kept.txt").write_bytes(b"keep me\r\n")
    (root / "opaque.dat").write_bytes(b"$Format:%H$\r\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    original = subprocess.Popen

    def local_transport(command, *args, **kwargs):
        command = [str(repo) if value == "https://fixture.invalid/repo.git" else value for value in command]
        command = ["protocol.file.allow=always" if value == "protocol.file.allow=never" else value for value in command]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_transport)
    return repo


def test_git_committed_objects_complete_revision_shared_and_source_independent(repository, monkeypatch):
    calls = []
    original = PackageCapture._run

    def record(self, directory, *args, **kwargs):
        calls.append(args)
        return original(self, directory, *args, **kwargs)

    monkeypatch.setattr(PackageCapture, "_run", record)
    spec = SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review")
    items = capture_artifacts(
        [
            ("first", ArtifactBundle(name="first", skills={"review": spec})),
            ("second", ArtifactBundle(name="second", skills={"review": spec})),
        ],
        ORIGIN,
    )
    assert sum(args[0] == "fetch" for args in calls) == 1
    assert tuple(items.items())[0].provenance.commit == git(repository, "rev-parse", "HEAD").decode().strip()
    assert (
        tuple(items.items())[0].content.digest
        == tuple(capture(SkillArtifactSpec(source=str(repository / "review"))).items())[0].content.digest
    )
    members = {member.path: member.data for member in tuple(items.items())[0].content.members}
    assert members["kept.txt"] == b"keep me\n"
    assert members["opaque.dat"] == b"$Format:%H$\r\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require optional privileges")
def test_git_links_submodules_and_lfs_are_rejected(repository):
    (repository / "review" / "link").symlink_to("SKILL.md")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "link")
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review"))
    with pytest.raises(SourceRefError):
        capture(RuleArtifactSpec(source="git::https://fixture.invalid/repo.git//review/link"))
    git(repository, "rm", "review/link")
    (repository / "review" / "large").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 100\n"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "lfs")
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review"))
    git(repository, "rm", "review/large")
    commit = git(repository, "rev-parse", "HEAD").decode().strip()
    git(repository, "update-index", "--add", "--cacheinfo", f"160000,{commit},review/submodule")
    git(repository, "commit", "-qm", "submodule")
    with pytest.raises(SourceRefError):
        capture(SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review"))


def test_codec_rejects_old_corrupt_and_ambiguous_state():
    payload = encode_inputs(capture(HintArtifactSpec(text="hello")))
    old = {**payload, "version": 1}
    with pytest.raises(SourceRefError):
        decode_inputs(old)
    row = cast(dict[str, dict[str, Any]], payload["hints"])["review"]
    row["content"]["text"] = "changed"
    with pytest.raises(SourceRefError):
        decode_inputs(payload)
    row["content"]["text"] = "hello"
    row["origin"]["facet"] = "workspace"
    with pytest.raises(SourceRefError):
        decode_inputs(payload)


def test_codec_binary_corruption_and_total_bound(tmp_path):
    payload = encode_inputs(capture(SkillArtifactSpec(source=str(skill(tmp_path)))))
    cast(dict[str, dict[str, Any]], payload["skills"])["review"]["content"]["members"][0]["data"] = "%%%"
    with pytest.raises(SourceRefError):
        decode_inputs(payload)
    with pytest.raises(SourceRefError):
        decode_inputs({"version": 1, "inputs": [None] * 4097})


@pytest.mark.parametrize(
    "source",
    [
        "git::https://user:password@fixture.invalid/repo.git//review",
        "git::https://fixture.invalid/repo.git//review?ref=main&token=password",
        "git::https://fixture.invalid/repo.git//review?token=password",
        "git::ssh://user:password@fixture.invalid/repo",
    ],
)
def test_source_diagnostics_do_not_repeat_credentials(source):
    with PackageCapture() as operation, pytest.raises(SourceRefError) as failure:
        operation.capture(source)
    assert "password" not in str(failure.value)


def test_git_immutable_ref_and_refresh(repository):
    first = git(repository, "rev-parse", "HEAD").decode().strip()
    pinned = SkillArtifactSpec(source=f"git::https://fixture.invalid/repo.git//review?ref={first}")
    before = tuple(capture(pinned).items())[0]
    (repository / "review" / "new.txt").write_text("new revision")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "update")
    assert tuple(capture(pinned).items())[0].content.digest == before.content.digest
    updated = tuple(capture(SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review")).items())[0]
    assert updated.content.digest != before.content.digest
    assert updated.provenance.commit != first


def test_git_filters_hooks_and_export_substitution_do_not_run(repository, monkeypatch, tmp_path):
    sentinel = tmp_path / "executed"
    # Install a workstation Git filter and template hook only after fixture commit.
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.fixture.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch '{sentinel}'")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "filter.fixture.required")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")
    result = tuple(capture(SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review")).items())[0]
    assert "kept.txt" in {member.path for member in result.content.members}
    assert not sentinel.exists()


def test_git_storage_limit_and_failure_cleanup(repository):
    with PackageCapture(CaptureLimits(storage_bytes=1)) as operation:
        staging = operation.root
        with pytest.raises(SourceRefError):
            operation.capture("git::https://fixture.invalid/repo.git//review")
    assert not staging.exists()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("single_file", [False, True])
def test_local_lfs_pointers_are_rejected(tmp_path, newline, single_file):
    root = skill(tmp_path)
    pointer = root / "asset.dat"
    pointer.write_bytes(
        newline.join([b"version https://git-lfs.github.com/spec/v1", b"oid sha256:abc", b"size 100", b""])
    )
    with PackageCapture() as operation, pytest.raises(SourceRefError):
        operation.capture(str(pointer if single_file else root))


@pytest.mark.parametrize("length", [4096, 4097])
def test_git_member_path_persistence_bound(repository, length):
    path = "/".join(["x" * 240] * 16) + "/" + "y" * (length - 3856)
    blob = git(repository, "rev-parse", "HEAD:review/opaque.dat").decode().strip()
    git(repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},review/{path}")
    tree = git(repository, "write-tree").decode().strip()
    commit = git(repository, "commit-tree", tree, "-m", "long member").decode().strip()
    git(repository, "update-ref", "HEAD", commit)
    spec = SkillArtifactSpec(source="git::https://fixture.invalid/repo.git//review")
    if length > 4096:
        with pytest.raises(SourceRefError):
            capture(spec)
    else:
        inputs = capture(spec)
        assert path in {member.path for member in tuple(inputs.items())[0].content.members}
        assert decode_inputs(encode_inputs(inputs)) == inputs


@pytest.mark.parametrize(("vanished", "cache_metadata"), [("file", False), ("file", True), ("directory", False)])
def test_git_storage_accounting_tolerates_removed_temporary_entries(monkeypatch, vanished, cache_metadata):
    original = os.scandir
    with PackageCapture() as operation:
        (operation.root / "kept").write_bytes(b"abc")
        removed = operation.root / ("shallow.lock" if vanished == "file" else "temporary")
        if vanished == "file":
            removed.write_bytes(b"temporary")
        else:
            removed.mkdir()

        @contextmanager
        def changing_scan(path):
            if vanished == "directory" and Path(path) == removed:
                removed.rmdir()
            with original(path) as entries:

                def changing_entries() -> Iterator[os.DirEntry[str]]:
                    for entry in entries:
                        if vanished == "file" and entry.name == removed.name:
                            if cache_metadata:
                                entry.stat(follow_symlinks=False)
                            removed.unlink()
                        yield entry

                yield changing_entries()

        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", changing_scan)
            # An enumerated file can retain cached metadata after unlink (notably
            # on Windows); counting it conservatively is a valid in-flight scan.
            size = operation._storage_size()
            assert size in ({3, 12} if vanished == "file" else {3})
        assert not removed.exists()
        assert operation._storage_size() == 3


def test_git_storage_accounting_preserves_io_errors(monkeypatch):
    with PackageCapture() as operation:

        def denied(path):
            raise PermissionError(path)

        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", denied)
            with pytest.raises(PermissionError):
                operation._storage_size()


def test_git_process_cannot_consume_operator_stdin(monkeypatch):
    original = subprocess.Popen

    def reading_process(command, *args, **kwargs):
        kwargs.setdefault("stdin", subprocess.PIPE)
        process = original(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(repr(sys.stdin.buffer.read(1)).encode())"],
            *args,
            **kwargs,
        )
        if process.stdin is not None:
            process.stdin.write(b"x")
            process.stdin.close()
        return process

    monkeypatch.setattr(subprocess, "Popen", reading_process)
    with PackageCapture() as operation:
        assert operation._run(operation.root, "version") == b"b''"


def test_git_failure_has_no_stale_snapshot(repository):
    with PackageCapture() as operation:
        operation.capture("git::https://fixture.invalid/repo.git//review")
        with pytest.raises(SourceRefError):
            operation.capture("git::https://fixture.invalid/repo.git//missing")


def test_native_option_mutation_does_not_escape_input(tmp_path):
    path = tmp_path / "agent.md"
    path.write_text("---\nname: review\ndescription: Review\nnative_options:\n  codex:\n    model: x\n---\nDo it.\n")
    content = tuple(capture(AgentArtifactSpec(source=str(path))).items())[0].content
    first = cast(dict[str, Any], content.native_options)
    first["codex"]["model"] = "changed"
    assert content.native_options == {"codex": {"model": "x"}}


def test_source_credentials_rejected_before_declaration_persistence():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as failure:
        SkillArtifactSpec(source="git::https://user:password@fixture.invalid/repo.git//review")
    assert "password" not in str(failure.value)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX executable mode changes")
def test_rule_source_mode_is_part_of_identity(tmp_path):
    path = tmp_path / "rule.md"
    path.write_text("Read instructions.\n")
    before = tuple(capture(RuleArtifactSpec(source=str(path))).items())[0]
    path.chmod(0o755)
    after = tuple(capture(RuleArtifactSpec(source=str(path))).items())[0]
    assert before.content.text == after.content.text
    assert before.content.digest != after.content.digest
    assert after.content.members[0].executable


def test_rule_unknown_suffix_is_still_designated_text(tmp_path):
    path = tmp_path / "context.custom"
    path.write_bytes(b"Always follow\r\nthese instructions.\r")
    content = tuple(capture(RuleArtifactSpec(source=str(path))).items())[0].content
    assert content.members[0].data == b"Always follow\nthese instructions.\n"
    assert content.members[0].text


def test_codec_rejects_boolean_version():
    payload = encode_inputs(capture(HintArtifactSpec(text="hello")))
    payload["version"] = True
    with pytest.raises(SourceRefError):
        decode_inputs(payload)


@pytest.mark.skipif(not hasattr(os, "O_PATH") and not hasattr(os, "O_SEARCH"), reason="search-only directory handles")
def test_readable_file_under_unlistable_ancestor_can_be_captured(tmp_path):
    parent = tmp_path / "traverse-only"
    parent.mkdir()
    source = parent / "rule.md"
    source.write_bytes(b"readable content")
    parent.chmod(0o111)
    try:
        assert source.read_bytes() == b"readable content"
        with PackageCapture() as operation:
            captured = operation.capture(str(source))
        assert captured.members[0].data == b"readable content"
    finally:
        parent.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="Windows rejects control characters in directory names")
def test_unrepresentable_source_provenance_refuses_capture(tmp_path):
    parent = tmp_path / "parent\nname"
    parent.mkdir()
    source = parent / "rule.md"
    source.write_text("readable rule")
    with pytest.raises(SourceRefError):
        capture_artifacts(
            [("team", ArtifactBundle(name="team", rules={"rule": RuleArtifactSpec(source=str(source))}))], ORIGIN
        )

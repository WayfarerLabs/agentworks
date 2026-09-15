"""Focused coverage for source-reference parsing and file fetching."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.sources import SourceRef, SourceRefError, fetch_file, parse_source_ref
from agentworks.ssh import SSHError


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


@pytest.mark.windows
def test_fetch_file_copies_local_source(tmp_path) -> None:
    source = tmp_path / "mise.lock"
    source.write_text("lock")
    target = MagicMock()

    fetch_file(parse_source_ref(str(source)), target, "/remote/mise.lock")

    target.copy_to.assert_called_once_with(source, "/remote/mise.lock")


def test_fetch_file_clones_copies_and_cleans_git_source() -> None:
    target = MagicMock()
    target.run.return_value.stdout = "/var/tmp/agentworks-source-ref-abcd12\n"
    ref = parse_source_ref("git::https://example.com/repo.git//locks/mise.lock?ref=v1")

    fetch_file(ref, target, "/remote/mise.lock")

    commands = [call.args[0] for call in target.run.call_args_list]
    assert any("git clone --depth 1 --branch v1" in command for command in commands)
    assert any("test -f /var/tmp/agentworks-source-ref-abcd12/locks/mise.lock" in command for command in commands)
    assert any(
        "cp /var/tmp/agentworks-source-ref-abcd12/locks/mise.lock /remote/mise.lock" in command for command in commands
    )
    assert commands[-1] == "rm -rf /var/tmp/agentworks-source-ref-abcd12"


def test_fetch_file_cleanup_failure_preserves_fetch_error(captured_output) -> None:  # noqa: ANN001
    target = MagicMock()
    primary = SSHError("clone failed")

    def _run(command: str, **_kwargs: object) -> SimpleNamespace:
        if command.startswith("mktemp "):
            return SimpleNamespace(stdout="/var/tmp/agentworks-source-ref-abcd12\n")
        if command.startswith("git clone "):
            raise primary
        if command.startswith("rm -rf "):
            raise SSHError("cleanup failed")
        return SimpleNamespace(stdout="")

    target.run.side_effect = _run
    ref = parse_source_ref("git::https://example.com/repo.git//locks/mise.lock")

    with pytest.raises(SourceRefError) as caught:
        fetch_file(ref, target, "/remote/mise.lock")

    assert caught.value.__cause__ is primary
    assert captured_output.warnings


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
def test_workstation_snapshot_error_does_not_echo_path(tmp_path) -> None:
    import traceback

    from agentworks.sources import snapshot_workstation_file

    source = str(tmp_path / "external-sensitive-path")
    with pytest.raises(SourceRefError) as caught:
        snapshot_workstation_file(source)
    assert "external-sensitive-path" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__

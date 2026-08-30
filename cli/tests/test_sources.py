"""Focused coverage for source-reference parsing and file fetching."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.sources import SourceRef, SourceRefError, fetch_file, parse_source_ref


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

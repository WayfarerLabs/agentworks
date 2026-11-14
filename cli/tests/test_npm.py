"""Registry input restrictions for native npm package installation."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from agentworks.npm import NpmPackageSpec, validate_npm_package_spec


@pytest.mark.parametrize(
    "package",
    [
        "typescript",
        "@scope/tool",
        "@scope/tool@1.2.3",
        "@scope/tool.tgz@latest",
        "tool@latest",
        "tool@next-release",
        "tool@1.2.3-beta.1+build.2",
        "tool@^1.2.3",
        "tool@>=1.2.3 <2 || ^3",
        "tool@1.2 - 1.9",
        "tool@*",
    ],
)
def test_registry_package_specs_are_preserved(package: str) -> None:
    assert validate_npm_package_spec(package) == package
    assert TypeAdapter(NpmPackageSpec).validate_python(package) == package


@pytest.mark.parametrize(
    "package",
    [
        "",
        " ",
        "tool ",
        " tool",
        "tool@",
        "tool@ ",
        "tool@latest ",
        "--registry=evil",
        "-tool",
        "_tool",
        ".tool",
        "@scope",
        "@scope/",
        "@scope/tool/extra",
        "tool@npm:other",
        "tool@github:owner/repo",
        "tool@https://example.com/tool.tgz",
        "https://example.com/tool.tgz",
        "owner/repo",
        "git+ssh://git@example.com/repo",
        "file:tool",
        "./tool",
        "/tmp/tool",
        "tool@.next",
        "tool@.",
        "tool@..",
        "tool.tgz",
        "tool.TAR.GZ",
        "tool.tar@latest",
        "tool@release.tgz",
        "tool@1.TAR",
        "tool@$(whoami)",
        "tool@1;echo evil",
        "tool@1\n",
        "tool@1\x00",
        "tool@1\x7f",
        "tool@1\t2",
        "a" * 215,
    ],
)
def test_nonregistry_or_unsafe_specs_are_rejected(package: str) -> None:
    with pytest.raises(ValueError):
        validate_npm_package_spec(package)
    with pytest.raises(ValidationError):
        TypeAdapter(NpmPackageSpec).validate_python(package)

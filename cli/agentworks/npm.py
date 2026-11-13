"""Registry package specifications accepted by native npm installation."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PACKAGE = re.compile(rf"(?P<name>(?:@{_NAME}/)?{_NAME})(?:@(?P<selector>[A-Za-z0-9*~^<>=-][A-Za-z0-9.*+~^<>=| -]*))?")
_TARBALL_SUFFIXES = (".tgz", ".tar.gz", ".tar")


def validate_npm_package_spec(value: str) -> str:
    """Reject non-registry specifications at the operator declaration boundary.

    npm owns tag and semantic-version resolution. This check restricts the
    input to registry names and selectors, preventing npm's file, directory,
    URL, Git, alias, and command-option dispatch paths.
    """
    match = _PACKAGE.fullmatch(value)
    if match is None or value != value.strip():
        raise ValueError("npm_packages entries must be registry names with an optional version, tag, or range")
    name, selector = match.group("name", "selector")
    if len(name) > 214:
        raise ValueError("npm registry package names must not exceed 214 characters")
    # npm recognizes a bare tarball filename before it tries package names.
    # Explicit selectors with these suffixes also select its file path.
    if (not name.startswith("@") and name.lower().endswith(_TARBALL_SUFFIXES)) or (
        selector is not None and selector.lower().endswith(_TARBALL_SUFFIXES)
    ):
        raise ValueError("npm_packages entries must refer to registry packages, not tarball files")
    return value


NpmPackageSpec = Annotated[str, AfterValidator(validate_npm_package_spec)]

"""Validated sudoers installation and existing admin-grant mode repair."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

_SUDOERS_DIRECTORY = "/etc/sudoers.d"


def sudoers_install_script(path: str, body: str) -> str:
    """Stage, validate and atomically install a sudoers fragment as root.

    A dot in the same-filesystem staging name keeps sudo from loading it.
    Rejected content leaves the destination untouched; promotion refuses a directory.
    """
    staging_template = shlex.quote(str(PurePosixPath(path).parent / ".agentworks-sudoers.XXXXXXXX"))
    return f"""\
set -eu
staging=$(mktemp {staging_template})
trap 'rm -f -- "$staging"' EXIT
printf '%s' {shlex.quote(body)} > "$staging"
chmod 0440 "$staging"
visudo -cf "$staging"
mv -fT -- "$staging" {shlex.quote(path)}
"""


def admin_sudoers_script(admin_username: str) -> str:
    """Install the canonical admin grant during root-side bootstrap."""
    return sudoers_install_script(
        f"{_SUDOERS_DIRECTORY}/{admin_username}", f"{admin_username} ALL=(ALL) NOPASSWD:ALL\n"
    )


def repair_admin_sudoers_script(admin_username: str) -> str:
    """Validate an existing regular grant and repair its mode without replacing it."""
    path = shlex.quote(f"{_SUDOERS_DIRECTORY}/{admin_username}")
    return f"""\
set -eu
test -f {path}
visudo -cf {path}
chmod 0440 -- {path}
"""

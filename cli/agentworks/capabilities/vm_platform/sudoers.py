"""The admin sudo grant shared by create-time bootstrap and VM reinit."""

from __future__ import annotations

import shlex

_SUDOERS_DIRECTORY = "/etc/sudoers.d"


def admin_sudoers_script(admin_username: str, *, preserve_existing: bool = False) -> str:
    """Build a root script that validates and atomically installs the admin grant.

    Staging stays on the destination filesystem, with a dot in its name so sudo
    ignores it. A failed validation leaves the previous grant untouched.
    Reinit preserves the existing privilege text; fresh bootstrap writes the
    canonical grant. A missing or unreadable existing fragment fails the repair.
    """
    path = shlex.quote(f"{_SUDOERS_DIRECTORY}/{admin_username}")
    staging_template = shlex.quote(f"{_SUDOERS_DIRECTORY}/.agentworks-admin.XXXXXXXX")
    if preserve_existing:
        write = f'cp -- {path} "$staging"'
    else:
        body = shlex.quote(f"{admin_username} ALL=(ALL) NOPASSWD:ALL\n")
        write = f"printf '%s' {body} > \"$staging\""
    return f"""\
set -eu
staging=$(mktemp {staging_template})
trap 'rm -f -- "$staging"' EXIT
{write}
chmod 0440 "$staging"
visudo -cf "$staging"
mv -f -- "$staging" {path}
"""

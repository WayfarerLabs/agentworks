"""Native, unprivileged npm CLI installation shared by admin and agent setup."""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport

_MIN_NPM_VERSION = (11, 10, 0)
_AGE_EXCLUDE_VERSION = (11, 17, 0)
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


def run_npm_install(
    target: Transport,
    packages: list[str],
    shell: str,
    home: str,
    min_release_age_days: int,
    logger: SSHLogger | None = None,
) -> list[str]:
    """Install declared registry packages into ``~/.local`` without sudo.

    The caller gates this runner with ``npm_enabled``. npm supplies dependency
    resolution and package engine checks; it must support the requested age
    policy before any installation runs. Existing packages remain accessible
    if a subsequent installation fails. Runtime provisioning is independent.
    """
    if not packages:
        return []

    prefix = posixpath.join(home, ".local")
    paths = [posixpath.join(prefix, "bin")]
    if logger is not None:
        logger.step("Npm packages")

    def warn(message: str) -> None:
        if logger is not None:
            logger.warning(message)
        output.warn(message)

    def login(command: list[str]) -> str:
        return f"{shlex.quote(shell)} -lc {shlex.quote(shlex.join(command))}"

    for index, package in enumerate(packages, 1):
        # Installing npm itself can replace the executable selected by the next
        # login shell. Verify each invocation before choosing its policy flags.
        try:
            result = target.run(login(["npm", "--version"]), timeout=30)
        except SSHError as exc:
            warn(f"npm packages skipped: install Node.js and npm >= 11.10.0 for this user, then reinit ({exc})")
            return paths

        match = _VERSION.fullmatch(result.stdout.strip())
        version = tuple(int(part) for part in match.groups()) if match is not None else None
        if version is None or version < _MIN_NPM_VERSION:
            warn(
                "npm packages skipped: npm >= 11.10.0 is required to enforce "
                "npm_min_release_age_days; install a supported Node.js/npm release, then reinit"
            )
            return paths

        args = [
            "npm",
            "install",
            "--global",
            "--prefix",
            prefix,
            f"--min-release-age={min_release_age_days}",
            "--engine-strict",
            "--force=false",
        ]
        if version >= _AGE_EXCLUDE_VERSION:
            # Inherited exclusions bypass the age filter, including for dependencies.
            args.append("--min-release-age-exclude=")

        output.info(f"Npm package {index}/{len(packages)}: {package}")
        try:
            target.run(login([*args, "--", package]), timeout=300)
        except SSHError as exc:
            warn(f"npm package '{package}' failed: {exc}")

    return paths

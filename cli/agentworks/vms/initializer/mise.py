"""Mise (the polyglot tool-version manager) installation on the VM:
apt source consts, config.toml generation, lockfile fetch, and the
locked-then-unlocked install sequence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError, SSHLogger

if TYPE_CHECKING:
    from agentworks.transports import Transport

MISE_GPG_KEY_URL = "https://mise.jdx.dev/gpg-key.pub"
MISE_GPG_KEY_PATH = "/etc/apt/keyrings/mise-archive-keyring.asc"
MISE_SOURCE_LINE = f"deb [signed-by={MISE_GPG_KEY_PATH}] https://mise.jdx.dev/deb stable main"
MISE_SOURCE_FILE = "/etc/apt/sources.list.d/mise.list"


MISE_ACTIVATE_LINES = (
    "# agentworks-mise-activate\n"
    'if [ -n "$ZSH_VERSION" ]; then\n'
    '  eval "$(mise activate zsh)"\n'
    'elif [ -n "$BASH_VERSION" ]; then\n'
    '  eval "$(mise activate bash)"\n'
    "else\n"
    '  echo "agentworks: mise activate skipped (unsupported shell)" >&2\n'
    "fi"
)


def _mise_shims_path(home: str) -> list[str]:
    """Return PATH additions for mise shims (for non-interactive contexts)."""
    return [f"{home}/.local/share/mise/shims"]


def _write_mise_config(
    target: Transport,
    packages: list[str],
    install_before: str,
    home: str,
    logger: SSHLogger,
) -> None:
    """Write ~/.config/mise/config.toml from mise_packages list.

    Packages are name@version strings (e.g., "jq@1.8.1").
    """
    if not packages:
        return

    logger.step("Mise config")
    output.info(f"Writing mise config with {output.count(len(packages), 'package')}...")

    settings_lines = ["[settings]", f'install_before = "{install_before}"', ""]
    tools_lines = ["[tools]"]

    for pkg in packages:
        if "@" in pkg:
            name, version = pkg.rsplit("@", 1)
            tools_lines.append(f'"{name}" = "{version}"')
        else:
            tools_lines.append(f'"{pkg}" = "latest"')

    mise_config = "\n".join(settings_lines + tools_lines) + "\n"

    try:
        mise_config_dir = f"{home}/.config/mise"
        target.run(f"mkdir -p {mise_config_dir}")
        target.write_file(f"{mise_config_dir}/config.toml", mise_config)
    except SSHError as e:
        msg = f"mise config write failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _fetch_mise_lockfile(
    target: Transport,
    lockfile_source: str,
    home: str,
    logger: SSHLogger,
) -> None:
    """Fetch a mise lockfile from a source reference to ~/.config/mise/mise.lock."""
    from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

    logger.step("Mise lockfile")
    output.info(f"Fetching mise lockfile from {lockfile_source}...")

    try:
        ref = parse_source_ref(lockfile_source, default_filename="mise.lock")
        dest = f"{home}/.config/mise/mise.lock"
        target.run(f"mkdir -p {home}/.config/mise")
        fetch_file(ref, target, dest, logger=logger)
    except SourceRefError as e:
        msg = f"mise lockfile fetch failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _parse_mise_failures(error: SSHError) -> list[str]:
    """Extract failed tool names from mise stderr output.

    Parses lines like:
      mise ERROR Failed to install aqua:npryce/adr-tools@3.0.0: reason here
    The tool name can contain colons (backend:path@version), so we split
    on ": " (colon-space) to separate tool from reason.
    """
    failures: list[str] = []
    error_str = str(error)
    for line in error_str.splitlines():
        if "Failed to install" in line:
            part = line.split("Failed to install", 1)[1].strip()
            tool = part.split(": ", 1)[0].strip()
            if tool and tool not in failures:
                failures.append(tool)
    return failures


def _run_mise_install(
    target: Transport,
    shell: str,
    home: str,
    allow_unlocked: bool,
    logger: SSHLogger,
    *,
    prune: bool = True,
) -> None:
    """Run mise install, handling locked/unlocked modes.

    If a lockfile is present, tries --locked first. If that fails due to
    unlocked packages and allow_unlocked is true, retries without --locked.

    Runs without env injection: provisioning is hermetic. mise hooks see
    static identity via the system-wide profile fragment (login-shell
    sourcing) and have no access to operator env (those reach runtime
    shells only).
    """
    logger.step("Mise install")

    # Check if a lockfile is present
    lockfile_path = f"{home}/.config/mise/mise.lock"
    has_lockfile = False
    try:
        check = target.run(f"test -f {lockfile_path}", check=False)
        has_lockfile = check.ok
    except SSHError:
        pass

    installed = False

    if has_lockfile:
        output.info("Running mise install (locked)...")
        try:
            target.run(
                f"{shell} -lc 'mise install -y --locked'",
                timeout=300,
            )
            output.detail("Mise packages installed (locked)")
            installed = True
        except SSHError as e:
            logger.warning(f"mise install --locked failed: {e}")
            failures = _parse_mise_failures(e)
            for tool in failures:
                output.warn(f"Locked install failed, not in lockfile: {tool}")
            if not failures:
                output.warn("mise install --locked failed (see vm logs)")
            if not allow_unlocked:
                output.warn("Hint: set mise_allow_unlocked = true to install unlocked packages")
                return
            output.warn("Retrying unlocked...")

    if not installed:
        output.info("Running mise install...")
        try:
            target.run(
                f"{shell} -lc 'mise install -y'",
                timeout=300,
            )
            output.detail("Mise packages installed")
            installed = True
        except SSHError as e:
            logger.warning(f"mise install failed: {e}")
            failures = _parse_mise_failures(e)
            for tool in failures:
                output.warn(f"Failed: {tool}")
            if not failures:
                output.warn("mise install failed (see vm logs)")

    # Prune stale tool versions not in the current config
    if installed and prune:
        import contextlib

        with contextlib.suppress(SSHError):
            target.run(f"{shell} -lc 'mise prune -y'", timeout=60)

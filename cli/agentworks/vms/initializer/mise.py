"""Mise (the polyglot tool-version manager) installation on the VM:
apt source consts, config.toml generation, lockfile fetch, and the
locked-then-unlocked install sequence.

Each step runs as one account through its ``Files``: the admin's in Phase B,
the agent's own in agent initialization.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.errors import CommandFailed, PathRefusal, PathRefused
from agentworks.execution.requests import Command
from agentworks.vms.home_layout import MISE_CONFIG_DIR

from .steps import STEP_FAILURES, describe_failure, status, warn, warn_step

if TYPE_CHECKING:
    from agentworks.execution.files import Files

    from .steps import InitLog

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


def _has_mise_config(files: Files, home: str) -> bool:
    """Whether the account's home has a mise config (dotfiles may supply one); a probe that cannot run means no."""
    try:
        probe = Command(("test", "-f", f"{home}/{MISE_CONFIG_DIR}/config.toml"), deadline=files.deadline)
        return status(files.access, probe) == 0
    except STEP_FAILURES:
        return False


def _mise_shims_path(home: str) -> list[str]:
    """Return PATH additions for mise shims (for non-interactive contexts)."""
    return [f"{home}/.local/share/mise/shims"]


def _write_mise_config(
    files: Files,
    packages: list[str],
    install_before: str,
    home: str,
    log: InitLog,
) -> None:
    """Write ``~/.config/mise/config.toml``, 0644, from the ``mise_packages`` list, as ``files``' identity.

    Packages are name@version strings (e.g., "jq@1.8.1"). A link or another
    file at the config's path or its directory is refused, never written
    through, and warned about in the config's own terms.
    """
    if not packages:
        return

    log.step("Mise config")
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
    path = f"{home}/{MISE_CONFIG_DIR}/config.toml"
    try:
        files.write(path, mise_config.encode(), mode=0o644, parent_mode=0o755)
    except PathRefused as refused:
        if refused.reason is not PathRefusal.UNSUITABLE:
            warn_step(log, "mise config write failed", refused)
            return
        warn(
            log,
            f"the mise config {path} was not written: a link or another file is at it or its directory, "
            "which dotfiles usually own, so mise_packages does not reach it until the template drops "
            "mise_packages or the dotfiles drop the link",
        )
    except STEP_FAILURES as error:
        warn_step(log, "mise config write failed", error)


def _fetch_mise_lockfile(files: Files, lockfile_source: str, home: str, log: InitLog) -> None:
    """Publish a mise lockfile from a source reference at ``~/.config/mise/mise.lock``, 0644."""
    from agentworks.sources import fetch_file, parse_source_ref

    log.step("Mise lockfile")
    output.info(f"Fetching mise lockfile from {lockfile_source}...")

    try:
        ref = parse_source_ref(lockfile_source, default_filename="mise.lock")
        fetch_file(ref, files, f"{home}/{MISE_CONFIG_DIR}/mise.lock", log=log)
    except STEP_FAILURES as error:
        warn_step(log, "mise lockfile fetch failed", error)


def _parse_mise_failures(error: Exception) -> list[str]:
    """Extract failed tool names from a failed mise install's stderr.

    Parses lines like:
      mise ERROR Failed to install aqua:npryce/adr-tools@3.0.0: reason here
    The tool name can contain colons (backend:path@version), so we split
    on ": " (colon-space) to separate tool from reason. Only a
    ``CommandFailed`` whose streams were logged carries stderr.
    """
    if not isinstance(error, CommandFailed) or not error.streams_logged:
        return []
    failures: list[str] = []
    for line in error.result.stderr.decode("utf-8", "replace").splitlines():
        if "Failed to install" in line:
            part = line.split("Failed to install", 1)[1].strip()
            tool = part.split(": ", 1)[0].strip()
            if tool and tool not in failures:
                failures.append(tool)
    return failures


MISE_INSTALL_DEADLINE = 300
"""Seconds a ``mise install`` may take."""

MISE_PRUNE_DEADLINE = 60
"""Seconds a ``mise prune`` may take."""


def mise_run(shell: str, command: str, deadline: float) -> Command:
    """The request that runs a mise command in ``shell``'s login environment."""
    return Command((shell, "-lc", command), deadline=deadline)


def _run_mise_install(
    files: Files,
    shell: str,
    home: str,
    allow_unlocked: bool,
    log: InitLog,
    *,
    prune: bool = True,
) -> None:
    """Run mise install as ``files``' identity, handling locked/unlocked modes.

    If a lockfile is present, tries --locked first. If that fails due to
    unlocked packages and allow_unlocked is true, retries without --locked.
    A lockfile probe that cannot run counts as no lockfile, and a prune that
    fails is ignored.

    Runs without env injection: provisioning is hermetic. mise hooks see
    static identity via the system-wide profile fragment (login-shell
    sourcing) and have no access to operator env (those reach runtime
    shells only).
    """
    log.step("Mise install")
    access = files.access

    has_lockfile = False
    try:
        lockfile = Command(("test", "-f", f"{home}/{MISE_CONFIG_DIR}/mise.lock"), deadline=files.deadline)
        has_lockfile = status(files.access, lockfile) == 0
    except STEP_FAILURES:
        pass

    installed = False

    if has_lockfile:
        output.info("Running mise install (locked)...")
        try:
            access.run(mise_run(shell, "mise install -y --locked", MISE_INSTALL_DEADLINE), check=True)
            output.detail("Mise packages installed (locked)")
            installed = True
        except STEP_FAILURES as error:
            log.warning(f"mise install --locked failed: {describe_failure(error, log.sanitize)}")
            failures = _parse_mise_failures(error)
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
            access.run(mise_run(shell, "mise install -y", MISE_INSTALL_DEADLINE), check=True)
            output.detail("Mise packages installed")
            installed = True
        except STEP_FAILURES as error:
            log.warning(f"mise install failed: {describe_failure(error, log.sanitize)}")
            failures = _parse_mise_failures(error)
            for tool in failures:
                output.warn(f"Failed: {tool}")
            if not failures:
                output.warn("mise install failed (see vm logs)")

    # Prune stale tool versions not in the current config
    if installed and prune:
        with contextlib.suppress(*STEP_FAILURES):
            access.run(mise_run(shell, "mise prune -y", MISE_PRUNE_DEADLINE), check=True)

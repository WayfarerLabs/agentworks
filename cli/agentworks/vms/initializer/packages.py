"""Apt source configuration, system/apt package installation, and the
generic install-command runner (system-install-command /
user-install-command resources).

Every request runs on the admin's access through Phase B's ``Files``, as
root where it changes the system. Apt source lists are published with
``Files.update``, so an unchanged list is not rewritten and no
``apt-get update`` follows it.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.cloud_init import INIT_SYSTEM_PACKAGES
from agentworks.execution.requests import Command, Script, Shell

from .mise import MISE_GPG_KEY_PATH, MISE_GPG_KEY_URL, MISE_SOURCE_FILE, MISE_SOURCE_LINE
from .steps import STEP_FAILURES, describe_failure, status, warn, warn_step

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.apt import AptPackageEntry, AptSourceEntry
    from agentworks.debian import DebianRelease
    from agentworks.execution.files import Files
    from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
    from agentworks.vms.templates import ResolvedVMTemplate

    from .steps import InitLog

APT_SOURCES_DIR = "/etc/apt/sources.list.d"
"""Where each apt source's list is published."""

APT_UPDATE_DEADLINE = 120
"""Seconds an ``apt-get update`` may take."""

APT_INSTALL_DEADLINE = 300
"""Seconds one ``apt-get install`` may take."""

INSTALL_CHECK_DEADLINE = 10
"""Seconds an install command's completion test may take: it should be instant."""

INSTALL_COMMAND_DEADLINE = 120
"""Seconds one install command may take."""


def apt_install(packages: Sequence[str]) -> Command:
    """``apt-get install`` of ``packages`` as root, noninteractive, taking each package's new conffiles."""
    return Command(
        ("apt-get", "install", "-y", "-qq", "-o", "Dpkg::Options::=--force-confnew", *packages),
        env={"DEBIAN_FRONTEND": "noninteractive"},
        elevated=True,
        deadline=APT_INSTALL_DEADLINE,
    )


APT_UPDATE = Command(("apt-get", "update", "-qq"), elevated=True, deadline=APT_UPDATE_DEADLINE)
"""Refresh apt's package lists, as root."""


def _configure_apt_sources(
    files: Files,
    vm_template: ResolvedVMTemplate,
    apt_packages: Mapping[str, AptPackageEntry],
    apt_sources: Mapping[str, AptSourceEntry],
    log: InitLog,
    *,
    debian_release: DebianRelease,
    resolved_sources: Mapping[str, tuple[AptSourceEntry, str]] | None = None,
) -> None:
    """Configure apt sources required by selected apt_packages. Idempotent."""
    if resolved_sources is None:
        resolved_sources = _resolve_apt_sources(
            vm_template,
            apt_packages,
            apt_sources,
            debian_release=debian_release,
        )
    if not resolved_sources:
        return

    log.step("Apt sources")
    try:
        _apply_apt_sources(files, resolved_sources, log)
    except STEP_FAILURES as error:
        warn_step(log, "apt sources failed", error)


def _apply_apt_sources(files: Files, resolved_sources: Mapping[str, tuple[AptSourceEntry, str]], log: InitLog) -> None:
    """Fetch each missing key, publish each list that differs, then refresh apt once if any list was written."""
    arch_result = files.access.run(Command(("dpkg", "--print-architecture"), deadline=files.deadline))
    arch_ok = arch_result.require_completed(files.access.vm_name) == 0
    arch = arch_result.stdout.decode("utf-8", "replace").strip() if arch_ok else "amd64"

    newly_configured = False
    for name, (src, source_line) in resolved_sources.items():
        key_exists = status(files.access, Command(("test", "-f", src.key_path), deadline=files.deadline)) == 0

        if not key_exists:
            output.info(f"Configuring apt source '{name}'...")
            try:
                _fetch_apt_key(files, src)
            except STEP_FAILURES as error:
                warn_step(log, f"apt source '{name}' failed", error)
                continue

        # Always ensure the source list file has the correct content,
        # even when the key already existed (the source URL may have changed).
        source_path = f"{APT_SOURCES_DIR}/{src.source_file}"
        desired = (source_line.replace("{arch}", arch) + "\n").encode()
        try:
            current = files.read(source_path, elevated=True)
            if current == desired:
                if key_exists:
                    output.info(f"Apt source '{name}': already configured, skipping")
                    log.output(f"apt source {name}: key and source list up to date, skipping")
                continue
            if key_exists:
                output.info(f"Apt source '{name}': updating source list...")
                log.output(f"apt source {name}: key exists but source list needs update")
            files.update(source_path, desired, observed=current, mode=0o644, parent_mode=0o755, elevated=True)
            newly_configured = True
        except STEP_FAILURES as error:
            warn_step(log, f"apt source '{name}' failed", error)

    if newly_configured:
        output.info("Running apt-get update...")
        try:
            files.access.run(APT_UPDATE, check=True)
        except STEP_FAILURES as error:
            warn_step(log, "apt-get update failed after adding sources", error)


def _fetch_apt_key(files: Files, src: AptSourceEntry) -> None:
    """Download ``src``'s signing key to its path as root (dearmored when the source says so), world-readable."""
    deadline = files.deadline
    key_dir = str(PurePosixPath(src.key_path).parent)
    files.access.run(Command(("install", "-m", "0755", "-d", key_dir), elevated=True, deadline=deadline), check=True)
    if src.key_dearmor:
        fetch = Script(
            f"curl -fsSL {shlex.quote(src.key_url)} | gpg --dearmor -o {shlex.quote(src.key_path)}",
            Shell.SH,
            elevated=True,
            deadline=deadline,
        )
        files.access.run(fetch, check=True)
    else:
        files.access.run(
            Command(("curl", "-fsSL", src.key_url, "-o", src.key_path), elevated=True, deadline=deadline), check=True
        )
    files.access.run(Command(("chmod", "a+r", src.key_path), elevated=True, deadline=deadline), check=True)


def _resolve_apt_sources(
    vm_template: ResolvedVMTemplate,
    apt_packages: Mapping[str, AptPackageEntry],
    apt_sources: Mapping[str, AptSourceEntry],
    *,
    debian_release: DebianRelease,
) -> dict[str, tuple[AptSourceEntry, str]]:
    """Resolve every selected release map before any APT mutation."""
    required_sources: dict[str, AptSourceEntry] = {}
    for pkg_name in vm_template.apt_packages:
        pkg = apt_packages.get(pkg_name)
        if pkg is None:
            continue
        for src_name in pkg.apt_sources:
            if src_name not in required_sources:
                src = apt_sources.get(src_name)
                if src is not None:
                    required_sources[src_name] = src

    return {name: (source, source.source_for(debian_release)) for name, source in required_sources.items()}


def _install_system_packages(files: Files, log: InitLog) -> None:
    """Install system repos and packages. Always runs on every init/reinit.

    Only the package install itself is required: its failure propagates and
    stops initialization.
    """
    log.step("System packages")

    # Add mise apt source
    try:
        files.access.run(
            Command(("curl", "-fsSL", MISE_GPG_KEY_URL, "-o", MISE_GPG_KEY_PATH), elevated=True, deadline=30),
            check=True,
        )
        files.write(MISE_SOURCE_FILE, (MISE_SOURCE_LINE + "\n").encode(), mode=0o644, parent_mode=0o755, elevated=True)
    except STEP_FAILURES as error:
        warn_step(log, "mise apt source setup failed", error)

    output.info("Running apt-get update...")
    try:
        files.access.run(APT_UPDATE, check=True)
    except STEP_FAILURES as error:
        warn_step(log, "apt-get update failed", error)

    output.info(f"Installing {output.count(len(INIT_SYSTEM_PACKAGES), 'system package')}...")
    files.access.run(apt_install(INIT_SYSTEM_PACKAGES), check=True)


def _install_apt_packages(
    files: Files,
    vm_template: ResolvedVMTemplate,
    apt_packages: Mapping[str, AptPackageEntry],
    log: InitLog,
) -> None:
    """Install apt packages from both direct list and apt-package entries."""
    # Collect all apt packages: direct list + apt-package entries
    all_apt: list[str] = list(vm_template.apt)
    for pkg_name in vm_template.apt_packages:
        pkg = apt_packages.get(pkg_name)
        if pkg is not None:
            all_apt.extend(pkg.apt)

    if not all_apt:
        return

    log.step("Apt packages")
    output.info(f"Installing {output.count(len(all_apt), 'apt package')}...")
    try:
        files.access.run(apt_install(all_apt), check=True)
    except STEP_FAILURES as error:
        warn_step(log, "apt packages failed", error)


def _build_test_command(
    entry: SystemInstallCommandEntry | UserInstallCommandEntry,
    shell: str,
    home: str,
) -> str | None:
    """Build a shell command that passes when every declared test passes.

    test_exec uses a login shell (-l) with interactive flag (-i) to ensure
    all profile/rc files are sourced, matching a real login session.
    Empty tests are ignored. Returning ``None`` when none remain prevents
    an empty conjunction from vacuously skipping the install.
    """
    commands: list[str] = []
    if entry.test_exec:
        commands.append(f"{shell} -lic {shlex.quote(f'command -v {shlex.quote(entry.test_exec)}')} > /dev/null 2>&1")
    if entry.test_file:
        path = entry.test_file.replace("~", home, 1) if entry.test_file.startswith("~") else entry.test_file
        commands.append(f"test -f {shlex.quote(path)}")
    if entry.test_dir:
        path = entry.test_dir.replace("~", home, 1) if entry.test_dir.startswith("~") else entry.test_dir
        commands.append(f"test -d {shlex.quote(path)}")
    return " && ".join(commands) or None


def install_run(shell: str, command: str) -> Command:
    """The request that runs an install command in ``shell``'s login environment."""
    return Command((shell, "-lc", command), deadline=INSTALL_COMMAND_DEADLINE)


def install_command_paths(
    command_names: list[str],
    entries: Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
) -> list[str]:
    """The ``PATH`` additions the named install commands declare, in order; an undeclared name adds none.

    They are declarations, not results: a command's paths count whether it
    ran, was skipped as installed, or failed, so an account's profile can
    carry them before any of its install commands run.
    """
    return [path for name in command_names if (entry := entries.get(name)) is not None for path in entry.path]


def _run_install_commands(
    files: Files,
    command_names: list[str],
    entries: Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
    shell: str,
    home: str,
    log: InitLog,
    *,
    label: str = "Install command",
) -> None:
    """Run install commands from an install-command entry dict as ``files``' identity.

    Phase B runs the system and user install commands as the admin, and agent
    initialization runs the agent's user install commands as the agent.

    Runs without env injection: provisioning is hermetic. Install commands
    see static identity and the account's declared ``PATH`` entries
    (:func:`install_command_paths` and mise's shims) via the on-disk profile
    fragments (login-shell sourcing), and have no access to operator env
    (those reach runtime shells only). A completion test that cannot run
    counts as not installed.
    """
    # DRIFT GUARD (Phase 7, recipe use-gate): this runner consumes
    # install-command rows by name and must never run a disabled plugin's row.
    # It is reached only via ``_phase_b_setup`` (vm create / reinit) and
    # ``create_agent_on_vm`` (agent create / reinit); the recipe
    # gate lives at each command entry that reaches it (see
    # tests/agents/test_recipe_gate_drift.py's entry table).
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = entries.get(name)
        if entry is None:
            msg = f"'{name}' is not a declared {label.lower()}"
            warn(log, msg)
            continue
        log.step(f"{label} {i}/{total}: {name}")

        # Skip if already installed.
        test_cmd = _build_test_command(entry, shell, home)
        if test_cmd:
            try:
                if status(files.access, Script(test_cmd, Shell.SH, deadline=INSTALL_CHECK_DEADLINE)) == 0:
                    output.info(f"{label} {i}/{total} ({name}): already installed, skipping")
                    log.output(f"{name}: already installed ({test_cmd}), skipping")
                    continue
            except STEP_FAILURES as error:
                # A deadline or a dropped connection: assume not installed, proceed.
                reason = describe_failure(error, log.sanitize)
                log.output(f"{name}: install check failed ({reason}), assuming not installed")

        truncated = entry.command[:60]
        output.info(f"{label} {i}/{total} ({name}): {truncated}...")
        try:
            files.access.run(install_run(shell, entry.command), check=True)
        except STEP_FAILURES as error:
            warn_step(log, f"{label.lower()} '{name}' failed: {truncated}...", error)

"""Apt source configuration, system/apt package installation, and the
generic install-command runner (system-install-command /
user-install-command resources)."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.cloud_init import INIT_SYSTEM_PACKAGES
from agentworks.ssh import SSHError, SSHLogger

from .mise import MISE_GPG_KEY_PATH, MISE_GPG_KEY_URL, MISE_SOURCE_FILE, MISE_SOURCE_LINE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.apt import AptPackageEntry, AptSourceEntry
    from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
    from agentworks.transports import Transport
    from agentworks.vms.templates import ResolvedVMTemplate


def _configure_apt_sources(
    target: Transport,
    vm_template: ResolvedVMTemplate,
    apt_packages: Mapping[str, AptPackageEntry],
    apt_sources: Mapping[str, AptSourceEntry],
    logger: SSHLogger,
) -> None:
    """Configure apt sources required by selected apt_packages. Idempotent."""
    # Collect all apt sources needed by selected apt_packages
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

    if not required_sources:
        return

    logger.step("Apt sources")

    # Detect architecture
    arch_result = target.run("dpkg --print-architecture", check=False)
    arch = arch_result.stdout.strip() if arch_result.returncode == 0 else "amd64"

    newly_configured = False
    for name, src in required_sources.items():
        # Check if GPG key already exists
        key_exists = target.run(f"test -f {shlex.quote(src.key_path)}", check=False).returncode == 0

        if not key_exists:
            output.info(f"Configuring apt source '{name}'...")
            try:
                # Ensure parent directory for key_path exists
                from pathlib import PurePosixPath

                key_dir = str(PurePosixPath(src.key_path).parent)
                target.run(f"install -m 0755 -d {shlex.quote(key_dir)}", sudo=True)

                # Download GPG key
                if src.key_dearmor:
                    # Wrap in sh -c so sudo applies to the entire pipeline,
                    # not just the curl on the left side of the pipe.
                    inner = f"curl -fsSL {shlex.quote(src.key_url)} | gpg --dearmor -o {shlex.quote(src.key_path)}"
                    target.run(
                        f"sh -c {shlex.quote(inner)}",
                        sudo=True,
                        timeout=60,
                    )
                else:
                    target.run(
                        f"curl -fsSL {shlex.quote(src.key_url)} -o {shlex.quote(src.key_path)}",
                        sudo=True,
                        timeout=60,
                    )
                target.run(f"chmod a+r {shlex.quote(src.key_path)}", sudo=True)
            except SSHError as exc:
                msg = f"apt source '{name}' failed: {exc}"
                logger.warning(msg)
                output.warn(msg)
                continue

        # Always ensure the source list file has the correct content,
        # even when the key already existed (the source URL may have changed).
        resolved_source = src.source.replace("{arch}", arch)
        source_path = f"/etc/apt/sources.list.d/{src.source_file}"
        expected = resolved_source + "\n"
        current = target.run(f"cat {shlex.quote(source_path)}", check=False)
        if current.returncode == 0 and current.stdout == expected:
            if key_exists:
                output.info(f"Apt source '{name}': already configured, skipping")
                logger.output(f"apt source {name}: key and source list up to date, skipping")
            continue

        if key_exists:
            output.info(f"Apt source '{name}': updating source list...")
            logger.output(f"apt source {name}: key exists but source list needs update")

        try:
            target.run(
                f"bash -c {shlex.quote(f'printf "%s\\n" {shlex.quote(resolved_source)} > {source_path}')}",
                sudo=True,
            )
            newly_configured = True
        except SSHError as e:
            msg = f"apt source '{name}' failed: {e}"
            logger.warning(msg)
            output.warn(msg)

    if newly_configured:
        output.info("Running apt-get update...")
        try:
            target.run("apt-get update -qq", sudo=True, timeout=120)
        except SSHError as e:
            msg = f"apt-get update failed after adding sources: {e}"
            logger.warning(msg)
            output.warn(msg)


def _install_system_packages(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Install system repos and packages. Always runs on every init/reinit."""
    logger.step("System packages")

    # Add mise apt source
    try:
        target.run(
            f"curl -fsSL {MISE_GPG_KEY_URL} -o {MISE_GPG_KEY_PATH}",
            sudo=True,
            timeout=30,
        )
        inner = f"printf '%s\\n' '{MISE_SOURCE_LINE}' > {MISE_SOURCE_FILE}"
        target.run(f"sh -c {shlex.quote(inner)}", sudo=True)
    except SSHError as e:
        msg = f"mise apt source setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    output.info("Running apt-get update...")
    try:
        target.run("apt-get update -qq", sudo=True, timeout=120)
    except SSHError as e:
        msg = f"apt-get update failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    output.info(f"Installing {output.count(len(INIT_SYSTEM_PACKAGES), 'system package')}...")
    apt_str = " ".join(shlex.quote(p) for p in INIT_SYSTEM_PACKAGES)
    try:
        target.run(
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::=--force-confnew {apt_str}",
            sudo=True,
            timeout=300,
        )
    except SSHError as e:
        msg = f"system packages failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _install_apt_packages(
    target: Transport,
    vm_template: ResolvedVMTemplate,
    apt_packages: Mapping[str, AptPackageEntry],
    logger: SSHLogger,
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

    logger.step("Apt packages")
    output.info(f"Installing {output.count(len(all_apt), 'apt package')}...")
    apt_str = " ".join(shlex.quote(p) for p in all_apt)
    try:
        target.run(
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::=--force-confnew {apt_str}",
            sudo=True,
            timeout=300,
        )
    except SSHError as e:
        msg = f"apt packages failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _build_test_command(
    entry: SystemInstallCommandEntry | UserInstallCommandEntry,
    shell: str,
    home: str,
) -> str | None:
    """Build a shell command to check if an install command's tool is present.

    test_exec uses a login shell (-l) with interactive flag (-i) to ensure
    all profile/rc files are sourced, matching a real login session.
    """
    if entry.test_exec:
        return f"{shell} -lic {shlex.quote(f'command -v {shlex.quote(entry.test_exec)}')} > /dev/null 2>&1"
    if entry.test_file:
        path = entry.test_file.replace("~", home, 1) if entry.test_file.startswith("~") else entry.test_file
        return f"test -f {shlex.quote(path)}"
    if entry.test_dir:
        path = entry.test_dir.replace("~", home, 1) if entry.test_dir.startswith("~") else entry.test_dir
        return f"test -d {shlex.quote(path)}"
    return None


def _run_install_commands(
    target: Transport,
    command_names: list[str],
    entries: Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
    shell: str,
    home: str,
    logger: SSHLogger,
    *,
    label: str = "Install command",
) -> list[str]:
    """Run install commands from an install-command entry dict. Returns PATH additions.

    Runs without env injection: provisioning is hermetic. Install commands
    see static identity via the on-disk profile fragments (login-shell
    sourcing) and have no access to operator env (those reach runtime
    shells only).
    """
    if not command_names:
        return []

    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = entries.get(name)
        if entry is None:
            msg = f"'{name}' is not a declared {label.lower()}"
            logger.warning(msg)
            output.warn(msg)
            continue
        logger.step(f"{label} {i}/{total}: {name}")

        # Skip if already installed (short timeout -- this should be instant)
        test_cmd = _build_test_command(entry, shell, home)
        if test_cmd:
            try:
                check = target.run(test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    output.info(f"{label} {i}/{total} ({name}): already installed, skipping")
                    logger.output(f"{name}: already installed ({test_cmd}), skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                # Timeout or connection issue -- assume not installed, proceed
                logger.output(f"{name}: install check failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        output.info(f"{label} {i}/{total} ({name}): {truncated}...")
        try:
            target.run(
                f"{shlex.quote(shell)} -lc {shlex.quote(entry.command)}",
                timeout=120,
            )
        except SSHError as e:
            msg = f"{label.lower()} '{name}' failed: {truncated}... ({e})"
            logger.warning(msg)
            output.warn(msg)
        path_additions.extend(entry.path)

    return path_additions

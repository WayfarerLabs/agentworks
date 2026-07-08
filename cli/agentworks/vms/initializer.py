"""VM lifecycle: provisioning (one-time) and initialization (repeatable).

Two phases:
  A. Provisioning (over provisioning transport): bootstrap, SSH key, Tailscale join.
     One-time, platform-specific, pass/fail. Tracked via provisioning_status.
  B. Initialization (over Tailscale SSH): packages, install commands, git credentials,
     dotfiles. Repeatable via `vm reinit`. Tracked via init_status.

Phase A steps are fatal -- if they fail, the VM is unreachable and useless.
Phase B steps are non-fatal -- failures produce warnings and a 'partial' status.
"""

from __future__ import annotations

import ipaddress
import shlex
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.env import (
    ResourceContext,
    vm_stable_identity_env,
)
from agentworks.errors import ConnectivityError, NotFoundError, StateError
from agentworks.ssh import SSHError, SSHLogger
from agentworks.transports import (
    SSHTransport,
    Transport,
)
from agentworks.vms.cloud_init import INIT_SYSTEM_PACKAGES, PROVISIONING_PACKAGES
from agentworks.vms.skel import BASHRC, ZSHRC

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.catalog import AptSourceEntry, SystemInstallCommandEntry, UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.git_credentials.base import GitCredentialProvider
    from agentworks.resources.registry import Registry
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate


AGENTWORKS_PROFILE = ".agentworks-profile.sh"
AGENTWORKS_RC = ".agentworks-rc.sh"

SKEL_BASHRC_PATH = "/etc/skel/.bashrc"
SKEL_ZSHRC_PATH = "/etc/skel/.zshrc"


def _ensure_agentworks_files_sourced(
    target: Transport,
    *,
    home: str,
    shell: str,
    logger: SSHLogger,
) -> None:
    """Defensive final step: idempotently re-append `. ~/.agentworks-*`
    source lines to the user's shell rc files.

    Earlier steps in setup (``_write_agentworks_profile``,
    ``_write_agent_profile``, ``_write_agentworks_rc``, the mise rc-write
    in ``_run_agent_mise_setup``) each append their source line via
    grep-or-append when they run. Dotfiles install -- and any other
    later step that overwrites a shell rc file in place -- can clobber
    those lines. This helper runs LAST in setup so the source lines
    survive a dotfiles installer that ships its own ``.zprofile`` /
    ``.bashrc`` / etc.

    grep-or-append shape means this is a no-op when the source line is
    already present (the common case where nothing clobbered).

    ``home`` is the literal home directory path (e.g.
    ``/home/agentworks``). Admin and agent setup pass their respective
    values; the helper does not assume ``$HOME`` shell expansion.

    ``shell`` selects which zsh-specific files to ensure (``.zprofile``,
    ``.zshrc``). bash rc files are always checked because we always
    write source lines to them.
    """
    logger.step("Ensure agentworks files sourced")

    profile_source = f". {home}/{AGENTWORKS_PROFILE}"
    profile_rcs = [f"{home}/.profile", f"{home}/.bashrc"]
    if shell == "zsh":
        profile_rcs.append(f"{home}/.zprofile")
    for rc in profile_rcs:
        target.run(
            f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || "
            f"printf '%s\\n' '{profile_source}' >> {rc}",
            check=False,
        )

    rc_source = f". {home}/{AGENTWORKS_RC}"
    rc_files = [f"{home}/.bashrc"]
    if shell == "zsh":
        rc_files.append(f"{home}/.zshrc")
    for rc in rc_files:
        target.run(
            f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || "
            f"printf '%s\\n' '{rc_source}' >> {rc}",
            check=False,
        )


def _write_agentworks_profile(
    target: Transport,
    path_additions: list[str],
    logger: SSHLogger,
    *,
    identity_env: dict[str, str] | None = None,
) -> None:
    """Write the agentworks-managed login profile fragment.

    Writes $HOME/.agentworks-profile.sh with per-user static identity
    exports (e.g. ``AGENTWORKS_AGENT`` for agent users) followed by PATH
    exports. Sourced from ~/.profile (bash/sh) and ~/.zprofile (zsh) --
    runs once per login shell, inherited by child processes.

    System-wide identity vars (VM / VM_HOST / PLATFORM) live in
    ``/etc/profile.d/agentworks-identity.sh``, written by
    ``_write_agentworks_identity_profile``. The on-VM Linux user is
    already exposed by the standard ``$USER`` / ``$LOGNAME`` env vars,
    so admin users pass an empty ``identity_env``.

    Always written (even when both ``identity_env`` is empty and
    ``path_additions`` is empty) so that reinit can clear previously set
    paths or identity.
    """
    # Deduplicate paths while preserving order
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in path_additions:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    logger.step("Shell profile")
    output.detail(f"Writing agentworks profile ({len(unique_paths)} PATH entries)...")

    try:
        lines = ["# Managed by agentworks -- do not edit"]
        if identity_env:
            for key, value in identity_env.items():
                lines.append(f"export {key}={shlex.quote(value)}")
        for p in unique_paths:
            expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
            lines.append(f'export PATH="{expanded}:$PATH"')
        target.write_file(f"~/{AGENTWORKS_PROFILE}", "\n".join(lines) + "\n")

        # Source from ~/.profile (bash/sh) and ~/.zprofile (zsh)
        source_line = f". $HOME/{AGENTWORKS_PROFILE}"
        for rc in ("$HOME/.profile", "$HOME/.zprofile"):
            target.run(
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        msg = f"shell profile write failed: {e}"
        logger.warning(msg)
        output.warn(msg)


# -- VM-side identity / sshd / sudoers fragments -----------------------------
#
# Three on-VM files maintained by agentworks init (per the env-and-secrets
# SDD's Phase 4):
#
#   /etc/profile.d/agentworks-identity.sh
#     System-wide login-shell fragment with the VM-stable identity vars
#     (AGENTWORKS_VM / AGENTWORKS_VM_HOST / AGENTWORKS_PLATFORM). Sourced by
#     every login shell on the VM (including raw `ssh awvm--<name>` from
#     outside agentworks), so identity vars don't require agentworks to be
#     the one opening the shell. Also writes to /etc/zsh/zprofile because
#     zsh skips /etc/profile.d by default.
#
#   /etc/ssh/sshd_config.d/50-agentworks-accept-env.conf
#     `AcceptEnv *`. Allows agentworks-issued SSH commands to inject env
#     vars via the SetEnv mechanism (see docs/adrs/0014-sshd-accept-env-wildcard.md).
#     Validated with `sshd -t` before sshd is reloaded.
#
#   /etc/sudoers.d/50-agentworks-env-keep
#     `Defaults env_keep += "AGENTWORKS_* AW_*"`. Lets agentworks-managed
#     vars survive the sudo boundary in console add-shell agent panes
#     (where the tmux server runs as admin and a pane sudo's to the agent).
#     Validated with `visudo -c` before install.

AGENTWORKS_IDENTITY_PROFILE_PATH = "/etc/profile.d/agentworks-identity.sh"
AGENTWORKS_ZPROFILE_PATH = "/etc/zsh/zprofile"
AGENTWORKS_SSHD_ACCEPT_ENV_PATH = "/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf"
AGENTWORKS_SUDOERS_ENV_KEEP_PATH = "/etc/sudoers.d/50-agentworks-env-keep"

# Marker comment used to find and replace the identity block in
# /etc/zsh/zprofile across reinit cycles.
_ZSH_IDENTITY_MARKER = "# agentworks-identity"


def _write_skel_seeds(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Write the agentworks-managed shell rc seeds to ``/etc/skel``.

    Both ``/etc/skel/.bashrc`` and ``/etc/skel/.zshrc`` are
    agentworks-owned on the VM: every reinit overwrites them. Future
    ``useradd -m`` (e.g. agent creation) inherits the seed
    automatically -- no explicit copy in agent setup needed.

    Caller MUST schedule this AFTER ``_install_apt_packages``:
    ``/etc/skel/.bashrc`` is a Debian conffile shipped by ``bash``, so
    an apt upgrade under ``--force-confnew`` (the standard install
    flag) would silently replace the seed if we wrote it earlier.
    Same constraint -- and same rationale -- as
    ``_write_agentworks_identity_profile`` for ``/etc/zsh/zprofile``.

    Operators install their own dotfiles directly into a user's home
    AFTER user creation. The seed only ever lands in user homes ONCE
    (at provision / useradd time); agentworks never refreshes the
    user-home copies (see issue #121). The grep-or-append source-line
    machinery in ``_write_agentworks_rc`` /
    ``_ensure_agentworks_files_sourced`` continues to no-op cleanly
    on a seeded user because the seed already contains the
    ``.agentworks-rc.sh`` substring the grep matches against.
    """
    logger.step("Shell rc skel")
    output.detail(f"Writing {SKEL_BASHRC_PATH} and {SKEL_ZSHRC_PATH}...")

    try:
        for path, content in (
            (SKEL_BASHRC_PATH, BASHRC),
            (SKEL_ZSHRC_PATH, ZSHRC),
        ):
            q_content = shlex.quote(content)
            target.run(
                f"printf '%s' {q_content} | sudo tee {path} > /dev/null",
            )
            target.run(f"sudo chmod 644 {path}")
    except SSHError as e:
        msg = (
            f"skel seed write failed: {e}. "
            "Re-run `agw vm reinit` to retry."
        )
        logger.warning(msg)
        output.warn(msg)


def _write_agentworks_identity_profile(
    target: Transport,
    identity_env: dict[str, str],
    logger: SSHLogger,
) -> None:
    """Write the VM-stable identity profile fragment.

    System-wide; sourced by every login shell on the VM. ``identity_env``
    is the VM-stable subset (typically AGENTWORKS_VM, AGENTWORKS_VM_HOST,
    AGENTWORKS_PLATFORM) produced by ``agentworks.env.vm_stable_identity_env``.

    The ``/etc/profile.d/agentworks-identity.sh`` file is fully owned by
    agentworks: each reinit overwrites it. The block in ``/etc/zsh/zprofile``
    is bracketed by ``# agentworks-identity-begin`` / ``# agentworks-identity-end``
    marker comments; content between those markers is agentworks-owned and
    gets rewritten on every reinit. An operator who hand-edits between the
    markers is opting in to having that content overwritten.
    """
    logger.step("Identity profile")
    output.detail(
        f"Writing {AGENTWORKS_IDENTITY_PROFILE_PATH} ({len(identity_env)} vars)..."
    )

    lines = ["# Managed by agentworks -- do not edit"]
    for key, value in identity_env.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    body = "\n".join(lines) + "\n"
    q_body = shlex.quote(body)

    try:
        # System-wide /etc/profile.d/ fragment.
        target.run(
            f"printf '%s' {q_body} | sudo tee {AGENTWORKS_IDENTITY_PROFILE_PATH} > /dev/null",
        )

        # Mirror into /etc/zsh/zprofile because zsh skips /etc/profile.d
        # by default. Idempotent: strip any existing agentworks-identity
        # block (between marker and end-marker) before appending the new
        # one so reinit doesn't accumulate stale entries.
        marker = _ZSH_IDENTITY_MARKER
        q_zprofile = shlex.quote(AGENTWORKS_ZPROFILE_PATH)

        # On a fresh VM, /etc/zsh/ doesn't exist until apt installs
        # zsh-common (which happens later in init). Create it ourselves so
        # the mirror lands regardless of install order; when zsh is later
        # installed, dpkg's noninteractive default keeps our fragment
        # rather than overwriting it.
        target.run("sudo mkdir -p /etc/zsh")

        # Strip the prior block only if the file exists AND both markers
        # are present. A half-edited file (begin marker without matching
        # end) would otherwise cause sed's address range to delete from
        # the begin marker through end-of-file, potentially nuking
        # unrelated operator-added content.
        file_exists = target.run(f"sudo test -f {q_zprofile}", check=False).ok
        if file_exists:
            q_begin = shlex.quote(f"{marker}-begin")
            q_end = shlex.quote(f"{marker}-end")
            has_begin = target.run(
                f"sudo grep -qF {q_begin} {q_zprofile}", check=False,
            ).ok
            has_end = target.run(
                f"sudo grep -qF {q_end} {q_zprofile}", check=False,
            ).ok
            if has_begin and has_end:
                sed_script = shlex.quote(
                    f"/^{marker}-begin/,/^{marker}-end/d"
                )
                target.run(f"sudo sed -i {sed_script} {q_zprofile}")
            elif has_begin or has_end:
                output.warn(
                    f"{AGENTWORKS_ZPROFILE_PATH} has unmatched "
                    "agentworks-identity markers; leaving them in place "
                    "and appending a fresh block. Inspect the file and "
                    "remove the orphan marker manually."
                )

        zsh_block = (
            f"{marker}-begin\n"
            + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in identity_env.items())
            + f"{marker}-end\n"
        )
        q_zsh_block = shlex.quote(zsh_block)
        target.run(
            f"printf '%s' {q_zsh_block} | sudo tee -a {q_zprofile} > /dev/null",
        )
    except SSHError as e:
        msg = (
            f"identity profile write failed: {e}. "
            "Re-run `agw vm reinit` to retry."
        )
        logger.warning(msg)
        output.warn(msg)


def _write_sshd_accept_env(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Deploy ``AcceptEnv *`` to sshd_config.d/ and reload sshd.

    The directive lets ``ssh -o SetEnv=KEY=VALUE`` calls from the CLI flow
    through to the user's shell. See the agentworks AcceptEnv wildcard
    ADR for the trust-anchor analysis behind the wildcard.

    Validation strategy (backup-validate-restore-on-failure): ``sshd -t``
    validates the FULL merged config (it follows the
    ``Include /etc/ssh/sshd_config.d/*.conf`` directive in
    ``/etc/ssh/sshd_config``), so we cannot validate the snippet in
    isolation. Instead we back up any prior file, write the new content
    to the final path, validate, and restore the prior file (or remove
    if there wasn't one) when validation fails. The race window where
    a non-validated file sits at the final path is bounded by the
    ``sshd -t`` call and never affects the running sshd (the reload
    only happens on validation success). The only way the broken file
    could become active is if an unrelated process (a ``dpkg`` postinst
    on openssh-server triggering ``deb-systemd-invoke try-reload-or-
    restart ssh.service``, for instance, possibly invoked by
    ``unattended-upgrades`` transitively) reloads sshd in the millisecond-
    wide window between tee and sshd -t. The snippet is a single
    ``AcceptEnv *`` line and the chance of that one line failing
    ``sshd -t`` in isolation is essentially nil; we accept the bounded
    risk.

    Idempotent on reinit.
    """
    logger.step("sshd AcceptEnv")
    output.detail(f"Writing {AGENTWORKS_SSHD_ACCEPT_ENV_PATH}...")

    body = (
        "# Managed by agentworks -- do not edit.\n"
        "# Allows agentworks-issued SSH commands to inject env vars via\n"
        "# `-o SetEnv=KEY=VALUE`; see the agentworks AcceptEnv wildcard ADR.\n"
        "AcceptEnv *\n"
    )
    q_body = shlex.quote(body)
    q_path = shlex.quote(AGENTWORKS_SSHD_ACCEPT_ENV_PATH)
    q_bak = shlex.quote(AGENTWORKS_SSHD_ACCEPT_ENV_PATH + ".bak")

    try:
        # Capture any prior content so we can roll back on validate failure.
        had_prior = target.run(f"sudo test -f {q_path}", check=False).ok
        if had_prior:
            target.run(f"sudo cp {q_path} {q_bak}")

        target.run(f"printf '%s' {q_body} | sudo tee {q_path} > /dev/null")

        validate = target.run("sudo sshd -t", check=False)
        if not validate.ok:
            if had_prior:
                target.run(f"sudo mv {q_bak} {q_path}", check=False)
            else:
                target.run(f"sudo rm -f {q_path}", check=False)
            raise SSHError(
                f"sshd -t rejected the AcceptEnv fragment: {validate.stderr.strip()}"
            )

        # Validation OK: drop the backup (best-effort; orphaned .bak is
        # harmless since `.bak` doesn't match `*.conf` in sshd_config.d).
        if had_prior:
            target.run(f"sudo rm -f {q_bak}", check=False)

        target.run("sudo systemctl reload ssh", check=False)
    except SSHError as e:
        msg = (
            f"sshd AcceptEnv install failed: {e}. "
            "Re-run `agw vm reinit` to retry."
        )
        logger.warning(msg)
        output.warn(msg)


def _write_sudoers_env_keep(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Deploy ``env_keep += "AGENTWORKS_* AW_*"`` to sudoers.d/.

    Without this, sudo strips agentworks-managed env vars across the user
    switch in console add-shell agent panes (the tmux server runs as
    admin; the pane sudo's to the agent). Validated with ``visudo -c``
    before install; on validation failure the file is removed so the
    sudoers DB stays consistent.
    """
    logger.step("sudoers env_keep")
    output.detail(f"Writing {AGENTWORKS_SUDOERS_ENV_KEEP_PATH}...")

    body = (
        "# Managed by agentworks -- do not edit.\n"
        "# Preserves agentworks-managed env vars across sudo for the\n"
        "# console add-shell agent-pane path; see\n"
        "# docs/adrs/0014-sshd-accept-env-wildcard.md.\n"
        'Defaults env_keep += "AGENTWORKS_* AW_*"\n'
    )
    q_body = shlex.quote(body)
    q_path = shlex.quote(AGENTWORKS_SUDOERS_ENV_KEEP_PATH)

    # Write to a staging file, validate with visudo -cf, only then
    # promote to the real path. A broken sudoers fragment can lock
    # the operator out of sudo entirely, so the validate step is
    # load-bearing.
    #
    # The staging path uses a .tmp suffix; sudo's /etc/sudoers.d/ loader
    # only picks up files whose names don't contain a literal '.' AND
    # match the no-tilde rule, so .tmp files are safely ignored even if
    # cleanup races mid-init.
    staging = AGENTWORKS_SUDOERS_ENV_KEEP_PATH + ".tmp"
    q_staging = shlex.quote(staging)
    try:
        try:
            target.run(
                f"printf '%s' {q_body} | sudo tee {q_staging} > /dev/null",
            )
            target.run(f"sudo chmod 0440 {q_staging}")
            validate = target.run(
                f"sudo visudo -cf {q_staging}", check=False,
            )
            if not validate.ok:
                raise SSHError(
                    f"visudo -cf rejected the env_keep fragment: "
                    f"{validate.stderr.strip()}"
                )
            target.run(f"sudo mv {q_staging} {q_path}")
        finally:
            # Always best-effort-remove the staging path. On the success
            # path the mv above already moved the file, so rm is a no-op;
            # on any failure path we don't want orphaned .tmp files
            # accumulating under /etc/sudoers.d/.
            target.run(f"sudo rm -f {q_staging}", check=False)
    except SSHError as e:
        msg = (
            f"sudoers env_keep install failed: {e}. "
            "Re-run `agw vm reinit` to retry."
        )
        logger.warning(msg)
        output.warn(msg)


def _write_agentworks_rc(
    target: Transport,
    shell_snippets: list[str],
    logger: SSHLogger,
) -> None:
    """Write the agentworks-managed rc fragment for interactive shells.

    Writes $HOME/.agentworks-rc.sh with shell hooks (e.g., mise activate).
    Sourced from ~/.bashrc and ~/.zshrc -- runs per interactive shell instance.
    Always written (even if empty) so that reinit can clear previously set hooks.
    """
    logger.step("Shell rc")
    output.detail("Writing agentworks rc...")

    try:
        lines = ["# Managed by agentworks -- do not edit"]
        lines.extend(shell_snippets)
        target.write_file(f"~/{AGENTWORKS_RC}", "\n".join(lines) + "\n")

        # Source from ~/.bashrc and ~/.zshrc
        source_line = f". $HOME/{AGENTWORKS_RC}"
        for rc in ("$HOME/.bashrc", "$HOME/.zshrc"):
            target.run(
                f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        msg = f"shell rc write failed: {e}"
        logger.warning(msg)
        output.warn(msg)


# -- Mise installation ---------------------------------------------------------

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
    output.detail(f"Writing mise config with {len(packages)} package(s)...")

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
    output.detail(f"Fetching mise lockfile from {lockfile_source}...")

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
        output.detail("Running mise install (locked)...")
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
        output.detail("Running mise install...")
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


# -- SSH authorized keys ------------------------------------------------------

AUTHORIZED_KEYS_HEADER = """\
# Managed by agentworks -- manual edits will be overwritten on reinit.
# To add keys, use operator.extra_ssh_public_keys in your agentworks config.
"""


def _reconcile_authorized_keys(
    target: Transport,
    config: Config,
    home: str,
    logger: SSHLogger,
    *,
    owner: str | None = None,
) -> None:
    """Reconcile <home>/.ssh/authorized_keys with the configured key set.

    Writes the primary ssh_public_key plus any extra_ssh_public_keys from
    config. Full overwrite so that removed keys are cleaned up on reinit.

    When ``owner`` is None (default), writes directly via the connected SSH
    user (admin writing to admin's home). Failure is downgraded to a warning
    because the operator can recover on the next ``vm reinit``.

    When ``owner`` is set to a Linux username different from the SSH user
    (e.g. an agent's username), uses a stage-and-install path: ensures
    ``<home>/.ssh`` exists with correct ownership, scp's the file content
    to a private mktemp path with 0600 perms, then ``sudo install``s the
    staged file atomically into place with the requested owner / group /
    mode. The staging file is removed in a ``finally`` block so a partial
    failure doesn't leak it. Failure on this path RAISES (``SSHError``):
    the call is load-bearing for whether the operator can SSH to the agent
    at all, so a silent failure here would leave the caller running
    downstream commands that all fail with a cryptic ``exit 255``.
    """
    logger.step("SSH authorized keys")

    keys: list[str] = [config.operator.ssh_public_key.read_text().strip()]
    for path in config.operator.extra_ssh_public_keys:
        keys.append(path.read_text().strip())

    extra_count = len(keys) - 1
    label = f"1 primary + {extra_count} extra" if extra_count else "1 primary"
    if owner is not None:
        label = f"{label} for {owner}"
    output.detail(f"Reconciling authorized_keys ({label})...")

    content = AUTHORIZED_KEYS_HEADER + "\n".join(keys) + "\n"

    if owner is None:
        # Direct-write: the SSH user writes to its own home.
        try:
            target.write_file(f"{home}/.ssh/authorized_keys", content, mode="600")
        except SSHError as e:
            msg = f"authorized_keys reconciliation failed: {e}"
            logger.warning(msg)
            output.warn(msg)
        return

    # Stage-and-install: admin writes for a non-self uid (agent).
    quoted_owner = shlex.quote(owner)
    # Ensure <home>/.ssh exists with correct ownership/mode.
    # `useradd -m` doesn't create .ssh (not in /etc/skel), and install -d
    # is idempotent (creates if missing; sets owner/mode either way).
    target.run(
        f"install -d -o {quoted_owner} -g {quoted_owner} -m 0700 {home}/.ssh",
        sudo=True,
    )
    mktemp_result = target.run("mktemp --tmpdir agw-ak.XXXXXX")
    staging = (getattr(mktemp_result, "stdout", "") or "").strip()
    if not staging:
        raise SSHError("mktemp produced empty path")
    try:
        # Restrict the staging file before content lands; mktemp's
        # randomized suffix plus 0600 perms keep the contents private
        # between admin's write and the atomic install.
        target.write_file(staging, content, mode="0600")
        target.run(
            f"install -o {quoted_owner} -g {quoted_owner} -m 0600 "
            f"{shlex.quote(staging)} {home}/.ssh/authorized_keys",
            sudo=True,
        )
    finally:
        target.run(f"rm -f {shlex.quote(staging)}", check=False)


def _preserve_ssh_host_keys(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Stop cloud-init from regenerating SSH host keys on stop/start.

    Writes the cloud-init drop-in that pins existing host keys. This also runs
    during Phase A bootstrap, but reconciling it here means VMs provisioned
    before the drop-in existed get repaired on ``vm reinit`` -- otherwise their
    host key changes on the next reboot and SSH fails with a changed-host-key
    error until the operator clears known_hosts by hand.

    Inert on platforms without cloud-init (e.g. WSL2): the file is simply never
    read. Written unconditionally to keep the step platform-agnostic, matching
    the Phase A bootstrap step.
    """
    from pathlib import PurePosixPath

    from agentworks.vms.bootstrap_script import SSH_PRESERVE_KEYS_LINES, SSH_PRESERVE_KEYS_PATH

    logger.step("Preserve SSH host keys")
    output.detail("Ensuring SSH host key preservation...")

    parent = str(PurePosixPath(SSH_PRESERVE_KEYS_PATH).parent)
    printf_args = " ".join(shlex.quote(line) for line in SSH_PRESERVE_KEYS_LINES)
    try:
        target.run(
            f"mkdir -p {shlex.quote(parent)} && printf '%s\\n' {printf_args} > {shlex.quote(SSH_PRESERVE_KEYS_PATH)}",
            sudo=True,
        )
    except SSHError as e:
        msg = f"SSH host key preservation failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _configure_apt_sources(
    target: Transport,
    vm_template: ResolvedVMTemplate,
    catalog: object,
    logger: SSHLogger,
) -> None:
    """Configure apt sources required by selected apt_packages. Idempotent."""
    from agentworks.catalog import ResolvedCatalog

    assert isinstance(catalog, ResolvedCatalog)

    # Collect all apt sources needed by selected apt_packages
    required_sources: dict[str, AptSourceEntry] = {}
    for pkg_name in vm_template.apt_packages:
        pkg = catalog.apt_packages.get(pkg_name)
        if pkg is None:
            continue
        for src_name in pkg.apt_sources:
            if src_name not in required_sources:
                src = catalog.apt_sources.get(src_name)
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
            output.detail(f"Configuring apt source '{name}'...")
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
                output.detail(f"Apt source '{name}': already configured, skipping")
                logger.output(f"apt source {name}: key and source list up to date, skipping")
            continue

        if key_exists:
            output.detail(f"Apt source '{name}': updating source list...")
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
        output.detail("Running apt-get update...")
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

    output.detail("Running apt-get update...")
    try:
        target.run("apt-get update -qq", sudo=True, timeout=120)
    except SSHError as e:
        msg = f"apt-get update failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    output.detail(f"Installing {len(INIT_SYSTEM_PACKAGES)} system packages...")
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
    catalog: object,
    logger: SSHLogger,
) -> None:
    """Install apt packages from both direct list and catalog entries."""
    from agentworks.catalog import ResolvedCatalog

    assert isinstance(catalog, ResolvedCatalog)

    # Collect all apt packages: direct list + catalog entries
    all_apt: list[str] = list(vm_template.apt)
    for pkg_name in vm_template.apt_packages:
        pkg = catalog.apt_packages.get(pkg_name)
        if pkg is not None:
            all_apt.extend(pkg.apt)

    if not all_apt:
        return

    logger.step("Apt packages")
    output.detail(f"Installing {len(all_apt)} apt packages...")
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


def _run_catalog_commands(
    target: Transport,
    command_names: list[str],
    entries: Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
    shell: str,
    home: str,
    logger: SSHLogger,
    *,
    label: str = "Install command",
) -> list[str]:
    """Run install commands from a catalog entry dict. Returns PATH additions.

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
            msg = f"{label.lower()} '{name}' not found in catalog"
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
                    output.detail(f"{label} {i}/{total} ({name}): already installed, skipping")
                    logger.output(f"{name}: already installed ({test_cmd}), skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                # Timeout or connection issue -- assume not installed, proceed
                logger.output(f"{name}: install check failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        output.detail(f"{label} {i}/{total} ({name}): {truncated}...")
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


# VM hardening (sysctl baseline + /proc hidepid=1) lives in
# agentworks.vms.hardening per FRD R4a + R4b.


def verify_tailscale_available() -> None:
    """Pre-flight: verify the local machine is on Tailscale."""
    try:
        result = subprocess.run(
            ["tailscale", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
    except FileNotFoundError:
        raise ConnectivityError("'tailscale' command not found. Install Tailscale on this machine.") from None
    except subprocess.TimeoutExpired:
        raise ConnectivityError("'tailscale status' timed out. Is Tailscale running?") from None

    if result.returncode != 0:
        raise ConnectivityError(
            "This machine is not connected to Tailscale. "
            "VM initialization requires Tailscale to switch from the provisioning "
            "transport to direct SSH. Run 'tailscale up' first."
        )


def resolve_git_credential_providers(
    registry: Registry,
    names: list[str],
) -> dict[str, GitCredentialProvider]:
    """Resolve git credential provider instances from the registry.

    Names are the credential names to resolve (from the admin row's or
    an agent template's ``git_credentials`` list).
    """
    from agentworks.git_credentials.azdo import AzDOCredentialProvider
    from agentworks.git_credentials.github import GitHubCredentialProvider

    providers: dict[str, GitCredentialProvider] = {}
    if not names:
        return providers
    from agentworks.resources.access import git_credential

    for name in names:
        cred_config = git_credential(registry, name)
        if cred_config is None:
            raise NotFoundError(
                f"git credential '{name}' not found in config",
                entity_kind="git-credential",
                entity_name=name,
            )
        desc = cred_config.description
        if cred_config.provider == "azdo":
            org = cred_config.provider_config.get("org")
            assert isinstance(org, str)  # loader guarantees org for azdo
            providers[name] = AzDOCredentialProvider(config_name=name, org=org, description=desc)
        elif cred_config.provider == "github":
            providers[name] = GitHubCredentialProvider(config_name=name, description=desc)
    return providers


def announce_git_credentials(providers: dict[str, GitCredentialProvider]) -> None:
    """Tell the operator which git credentials the operation will
    configure. (The former per-provider auth pre-flight was vestigial:
    every provider's check returned True unconditionally once token
    resolution moved to the secret framework. Token health reports
    through doctor's Secrets group and resolution failures surface as
    ``SecretUnavailableError`` at collect time.)
    """
    if providers:
        labels = [p.display_name for p in providers.values()]
        output.info(f"Git credentials configured: {', '.join(labels)}")


def rejoin_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
) -> str:
    """Re-join Tailscale on a VM that lost its node (e.g. ephemeral key).

    Installs Tailscale if needed, joins the tailnet, and updates the DB
    with the new Tailscale IP. ``auth_key`` is resolved by the caller via
    the framework's eager-resolve (Phase 1c).

    Returns the new Tailscale IP.
    """
    output.info("Tailscale node not reachable. Re-joining tailnet...")

    # Ensure Tailscale is installed (idempotent)
    exec_target.run(
        "bash -c 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh'",
        sudo=True,
        check=False,
    )

    return _join_tailscale(db, vm_name, exec_target, auth_key=auth_key)


def _join_tailscale(
    db: Database,
    vm_name: str,
    exec_target: Transport,
    *,
    auth_key: str,
    logger: SSHLogger | None = None,
) -> str:
    """Join Tailscale, update DB. Returns the Tailscale IP.

    Phase 1c of the Resource Registry SDD: the Tailscale auth key
    arrives via the ``auth_key`` keyword argument from the framework's
    eager-resolve at manager-entry. The legacy env-var fallback and
    prompt-here-if-missing path are gone; callers must thread the
    resolved value in.
    """
    quoted_key = shlex.quote(auth_key)
    # Daemon-side flags (e.g. --tun=userspace-networking for WSL2) live in
    # /etc/default/tailscaled, set during bootstrap. `tailscale up` is the
    # client and only takes client-side flags.
    ts_cmd = f"tailscale up --auth-key {quoted_key}"

    # Redact the auth key from any attached loggers before it appears in logs.
    if exec_target.logger is not None:
        exec_target.logger.add_redaction(auth_key)
    if logger is not None:
        logger.add_redaction(auth_key)

    exec_target.run(ts_cmd, sudo=True)
    result = exec_target.run("tailscale ip -4", sudo=True)

    raw_ip_output = result.stdout.strip()
    tailscale_ip = raw_ip_output.splitlines()[0].strip() if raw_ip_output else ""
    try:
        ipaddress.IPv4Address(tailscale_ip)
    except ValueError:
        raise SSHError(f"tailscale ip -4 returned invalid address: {raw_ip_output!r}") from None
    output.detail(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    return tailscale_ip


def initialize_vm(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    exec_target: Transport,
    providers: dict[str, GitCredentialProvider],
    *,
    admin_username: str = "agentworks",
    tailscale_auth_key: str,
    git_tokens: dict[str, str],
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
    on_tailscale_ready: Callable[[], None] | None = None,
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal -- any failure aborts initialization.
    Phase B (setup) steps are non-fatal -- failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.

    Phase 1c (Tailscale) + Phase 1d (git credentials): both
    ``tailscale_auth_key`` and ``git_tokens`` are required; ``create_vm``
    resolves them via the framework at manager-entry and threads them in.
    """
    from agentworks.ssh import SSHLogger
    from agentworks.vms.manager import keep_vm_active

    home = f"/home/{admin_username}"
    logger = SSHLogger(vm_name, "vm-create")
    logger.add_redaction(tailscale_auth_key)
    if git_tokens:
        for token in git_tokens.values():
            logger.add_redaction(token)

    # Attach logger to the provisioning transport. ``Transport`` declares
    # ``logger`` on the ABC; the assignment is polymorphic.
    exec_target.logger = logger

    transport = exec_target.describe()

    # Anchor the VM in an active state for the full init span. No-op for
    # Lima/Azure/Proxmox; WSL2 holds a wsl.exe subprocess open so the distro
    # doesn't idle-shut between Phase A (wsl.exe transport) and Phase B
    # (Tailscale SSH).
    vm_for_keepalive = db.get_vm(vm_name)
    assert vm_for_keepalive is not None, "create_vm inserts the row before init"
    with keep_vm_active(db, config, vm_for_keepalive):
        try:
            db.insert_vm_event(vm_name, "provisioning_started", transport)
            ts_target = _phase_a_bootstrap(
                db,
                config,
                vm_template,
                vm_name,
                exec_target,
                home,
                admin_username,
                vm_for_keepalive.platform,
                logger,
                tailscale_auth_key=tailscale_auth_key,
                bootstrap_complete=bootstrap_complete,
                tailscale_ip=tailscale_ip,
            )
            db.insert_vm_event(
                vm_name,
                "provisioning_complete",
                ts_target.host if isinstance(ts_target, SSHTransport) else None,
            )
        except Exception as e:
            db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
            db.insert_vm_event(vm_name, "provisioning_failed", str(e))
            logger.close()
            output.warn(f"Log: {logger.path}")
            raise

        # Tailscale is up; caller can clean up provisioning-only resources
        # (e.g., detach Azure public IP since Phase B uses Tailscale SSH).
        # Removing the public IP can destabilize the network stack briefly,
        # so we wait for Tailscale SSH to be reliably reachable before
        # proceeding with Phase B.
        if on_tailscale_ready is not None:
            try:
                on_tailscale_ready()
            except Exception as e:
                output.warn(f"post-provisioning cleanup failed: {e}")

            # Wait for Tailscale SSH to reconnect after network changes
            from agentworks.transports import wait_for_reconnect

            wait_for_reconnect(ts_target)

        run_initialization(
            db,
            config,
            registry,
            vm_template,
            admin,
            vm_name,
            ts_target,
            providers,
            home,
            admin_username,
            logger,
            git_tokens=git_tokens,
            is_first_init=True,
        )


def run_initialization(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
    is_first_init: bool = False,
) -> None:
    """Run Phase B (initialization) with status tracking and event logging.

    This is called both from initialize_vm() after provisioning and
    from reinit_vm() for repeatable re-initialization. Pass
    ``is_first_init=True`` from initialize_vm so steps that expect prior
    state (e.g. tmux socket dirs) can skip warnings on missing state.
    Phase 1d: ``git_tokens`` is required (no provider-side fallback);
    callers must thread the framework-resolved dict in.
    """
    db.insert_vm_event(vm_name, "init_started")

    try:
        _phase_b_setup(
            db,
            config,
            registry,
            vm_template,
            admin,
            vm_name,
            ts_target,
            providers,
            home,
            admin_username,
            logger,
            git_tokens=git_tokens,
            is_first_init=is_first_init,
        )
    except Exception as e:
        db.update_vm_init_status(vm_name, InitStatus.FAILED)
        db.insert_vm_event(vm_name, "init_failed", str(e))
        logger.close()
        raise

    if logger.has_warnings:
        db.update_vm_init_status(vm_name, InitStatus.PARTIAL)
        db.insert_vm_event(vm_name, "init_partial", f"{len(logger.warnings)} warning(s)")
    else:
        db.update_vm_init_status(vm_name, InitStatus.COMPLETE)
        db.insert_vm_event(vm_name, "init_complete")

    logger.close()


def _phase_a_bootstrap(
    db: Database,
    config: Config,
    vm_template: ResolvedVMTemplate,
    vm_name: str,
    exec_target: Transport,
    home: str,
    admin_username: str,
    platform: str,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str,
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
) -> Transport:
    """Phase A: Bootstrap (over provisioning transport). All steps are fatal.

    Three paths depending on how much the provisioner already handled:

    1. bootstrap_complete=True (Lima/Azure): The provisioner already ran the
       full bootstrap. Skip straight to Tailscale SSH verification.
    2. Otherwise (WSL2): Run full bootstrap script over the provisioning
       transport (user, packages, SSH key, swap, Tailscale).

    Returns the Tailscale ``Transport`` for Phase B.
    """
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)

    if bootstrap_complete and tailscale_ip:
        # Lima/Azure: provisioner already ran the full bootstrap.
        # Just update DB and move on to SSH verification.
        logger.step("Bootstrap (provisioner)")
        logger.output(f"Tailscale IP: {tailscale_ip}")
        db.update_vm_tailscale(vm_name, tailscale_ip)
        db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)
    else:
        # WSL2: run bootstrap script over the provisioning transport
        tailscale_ip = _run_bootstrap_script(
            db,
            config,
            vm_template,
            vm_name,
            exec_target,
            admin_username,
            platform,
            logger,
            tailscale_auth_key=tailscale_auth_key,
        )

    # Sync the operator's SSH config now that the VM's Tailscale IP is
    # known. Phase B issues many SSH calls; having the managed aliases in
    # place first means operator-facing ``ssh awvm--<name>`` works as soon
    # as the VM is reachable.
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)

    # Switch to Tailscale SSH, carrying over the SSH logger.
    # On Windows, force TTY to prevent zsh/login shell pipe hangs.
    import sys

    ts_target = SSHTransport(
        host=tailscale_ip,
        user=admin_username,
        identity_file=config.operator.ssh_private_key,
        force_tty=sys.platform == "win32",
        default_timeout=60,
        logger=logger,
    )

    # Verify Tailscale SSH works (retry -- peer connection may take time)
    logger.step("Verify Tailscale SSH")
    output.detail("Verifying Tailscale SSH...")
    import time

    for attempt in range(5):
        try:
            ts_target.run("echo ok", timeout=15)
            break
        except SSHError:
            if attempt == 4:
                raise
            output.detail(f"Tailscale SSH not ready, retrying ({attempt + 1}/5)...")
            time.sleep(3)

    return ts_target


def _run_bootstrap_script(
    db: Database,
    config: Config,
    vm_template: ResolvedVMTemplate,
    vm_name: str,
    exec_target: Transport,
    admin_username: str,
    platform: str,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str,
) -> str:
    """Generate, copy, and run a bootstrap script on the VM. Returns Tailscale IP.

    Used for WSL2 where the bootstrap cannot be embedded in a provisioner's
    native mechanism (Lima provision block, Azure cloud-init). Phase 1c:
    ``tailscale_auth_key`` is required; the framework-resolved value
    arrives from ``create_vm`` -> ``initialize_vm`` -> ``_phase_a_bootstrap``.
    """
    import tempfile

    from agentworks.vms.bootstrap_script import generate_bootstrap_script, parse_bootstrap_output, vm_hostname

    output.info("Bootstrapping VM...")

    ssh_public_key = config.operator.ssh_public_key.read_text().strip()
    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=tailscale_auth_key,
        hostname=vm_hostname(platform, vm_name),
        # WSL2 provisioner handles swap natively before bootstrap; every other
        # platform lets the script create the swapfile.
        swap=0 if platform == "wsl2" else vm_template.swap,
    )

    # Copy script to VM and execute synchronously over the provisioning transport
    remote_script = "/tmp/agentworks-bootstrap.sh"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".sh", delete=False) as f:
        f.write(script.encode("utf-8"))
        local_script = f.name

    try:
        exec_target.copy_to(local_script, remote_script)
    finally:
        import os

        os.unlink(local_script)

    # Run the bootstrap script synchronously over the platform's provisioning
    # transport. WSL2 is the only consumer here (Lima/Azure embed the bootstrap
    # in their native delivery mechanisms and arrive with bootstrap_complete=True),
    # and the WSL2 transport is a local wsl.exe subprocess -- there is no
    # network session to disconnect from, so the detached-poll pattern brings
    # no benefit. It also actively breaks WSL2 under systemd: each `wsl.exe`
    # invocation is its own systemd-logind user session, and the default
    # KillUserProcesses=yes reaps every process in the session cgroup when the
    # foreground shell exits -- nohup blocks SIGHUP, not cgroup teardown.
    #
    # The wrapping bits (in order) defend against terminal-related hangs:
    #   setsid       - new session, no controlling TTY. Without this, sudo's
    #                  default `Defaults use_pty` allocates a pty whose
    #                  foreground PGID is sudo's monitor; any dpkg trigger
    #                  that touches /dev/tty from a background PGID then
    #                  SIGTTIN/SIGTTOU-stops apt mid-`dist-upgrade`.
    #   </dev/null   - stdin = EOF, so anything that reads from stdin (rather
    #                  than /dev/tty directly) returns immediately.
    #   2>&1         - merge stderr into captured stdout so apt-get noise
    #                  lands alongside the script's ##STEP## markers when
    #                  we need to diagnose a failure.
    output.detail("Running bootstrap script...")
    result = exec_target.run(
        f"setsid sudo -n /bin/bash {remote_script} </dev/null 2>&1",
        check=False,
        timeout=900,  # 15 min hard cap; apt-get dist-upgrade is the long pole
    )
    exec_target.run(f"rm -f {remote_script}", sudo=True, check=False)

    # Parse structured output
    bootstrap = parse_bootstrap_output(result.stdout, result.returncode)

    # Feed results into logger and console
    for step in bootstrap.steps:
        logger.step(step.name)
        if step.success_msg:
            output.detail(f"{step.name}: {step.success_msg}")
            logger.output(step.success_msg)
        for warning in step.warnings:
            output.warn(warning)
            logger.warning(warning)
        if step.error:
            output.warn(f"Error: {step.error}")
            logger.log_error(step.error)

    # Log full output for troubleshooting
    if result.stdout:
        logger.output(result.stdout)

    if not bootstrap.ok:
        msg = f"Bootstrap script failed (exit {result.returncode})"
        if result.stdout:
            msg += f"\n{result.stdout[-500:]}"
        raise SSHError(msg)

    # Update DB with Tailscale info
    assert bootstrap.tailscale_ip is not None
    tailscale_ip = bootstrap.tailscale_ip
    output.detail(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)

    return tailscale_ip


def _phase_b_setup(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
    is_first_init: bool = False,
) -> None:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue.

    ``git_tokens`` is required (Phase 1d): every provider listed in
    ``providers`` must have a pre-resolved token value in the dict.
    """
    from agentworks.catalog import catalog_from_registry

    output.info("Initializing VM...")
    db.update_vm_init_status(vm_name, InitStatus.IN_PROGRESS)
    # Phase 2b: catalog reference validation moved to the framework
    # (catalog kinds' error miss policy fires at build_registry time,
    # which the manager-entry hoist runs before reaching this point).
    catalog = catalog_from_registry(registry)

    # Non-fatal: ensure cloud-init won't regenerate SSH host keys on reboot.
    # Runs first so VMs predating the Phase A step are repaired on reinit
    # even if a later step warns. Idempotent overwrite with identical
    # content.
    _preserve_ssh_host_keys(ts_target, logger)

    # Non-fatal: VM hardening (sysctl baseline + /proc hidepid>=1).
    # Runs before the rest of init so subsequent steps execute under the
    # hardened baseline. Depends only on coreutils + procps (always
    # present); nothing here needs apt-installed packages. Idempotent on
    # reinit.
    from agentworks.vms.hardening import apply_vm_hardening

    apply_vm_hardening(ts_target, logger)

    # Check VM DNS works before subsequent steps that need external
    # resolution (apt-get update, source fetches, etc.) fail cryptically.
    # When DNS is broken AND the failure matches the known issue #117
    # latched shape AND the heal applies to this resolver setup, raises
    # StateError with the manual heal block as a hint. When DNS is broken
    # for any other reason, surfaces a non-fatal warning so the operator
    # has a visible link to the apt failure that will follow.
    from agentworks.vms.tailscale_dns import (
        apply_tailscaled_dns_fix,
        check_vm_dns,
    )

    check_vm_dns(ts_target, logger)

    # Non-fatal: tailscaled cold-boot DNS race fix (GitHub issue #117).
    # Drops in a systemd override that orders tailscaled after the DNS
    # layer is up so its DNS-manager probe finds a resolver instead of
    # falling back to direct mode. Applied early in Phase B so existing
    # VMs pick up the fix on the first reinit. Does not restart
    # tailscaled (would disconnect us); takes effect on next cold boot.
    apply_tailscaled_dns_fix(ts_target, logger)

    # Non-fatal: VM-wide SetEnv plumbing (env-and-secrets SDD Phase 4).
    # Runs before apt install so subsequent SSH commands within init can
    # rely on the SetEnv path. These targets don't touch zsh-shipped files,
    # so dpkg conffile handling doesn't apply.
    _write_sshd_accept_env(ts_target, logger)
    _write_sudoers_env_keep(ts_target, logger)
    vm_row = db.get_vm(vm_name)
    # Init runs against a VM that exists in the DB (initialize_vm fetches the
    # row up front). A None here is an internal invariant violation, not a
    # recoverable state, so surface it loudly.
    assert vm_row is not None, f"VM '{vm_name}' missing from DB mid-init"
    identity_ctx = ResourceContext(
        vm_name=vm_row.name,
        platform=vm_row.platform,
        user=admin_username,
        vm_host=vm_row.vm_host_name,
    )

    # Provisioning is hermetic: no operator env, no per-context identity,
    # no secrets from env tables are injected into install commands. Static
    # identity (AGENTWORKS_VM / VM_HOST / PLATFORM) reaches install commands
    # via /etc/profile.d/agentworks-identity.sh sourcing. Tailscale auth key
    # and git credentials -- the only provisioning-time secrets -- have
    # their own dedicated config paths outside [admin.env]. Operator env
    # only reaches RUNTIME shells (vm shell, agent shell, sessions,
    # consoles), never build-time install machinery.

    # Non-fatal: system repos + packages (mise repo added, then all packages)
    _install_system_packages(ts_target, logger)

    # Non-fatal: apt sources required by selected apt_packages
    _configure_apt_sources(ts_target, vm_template, catalog, logger)

    # Non-fatal: apt packages (direct list + catalog entries)
    _install_apt_packages(ts_target, vm_template, catalog, logger)

    # Identity profile fragments. Runs AFTER apt install because apt uses
    # `--force-confnew`, which would replace the agentworks block in
    # `/etc/zsh/zprofile` with zsh-common's package default if zsh got
    # installed after we wrote our fragment. Post-install, we append cleanly
    # on top of whatever the package shipped. The mirror is idempotent on
    # reinit (strip-and-rewrite via begin/end markers).
    _write_agentworks_identity_profile(
        ts_target, vm_stable_identity_env(identity_ctx), logger,
    )

    # /etc/skel seeds. MUST run AFTER apt for the same reason as the
    # identity profile above: `/etc/skel/.bashrc` is a Debian conffile
    # shipped by the `bash` package. Running before apt's
    # `--force-confnew` would let a bash upgrade silently replace the
    # seed with Debian's stock skel (saving ours as .dpkg-old). Future
    # `useradd -m` would then inherit Debian's skel instead.
    _write_skel_seeds(ts_target, logger)

    # Non-fatal: snap packages
    if vm_template.snap:
        logger.step("Snap packages")
        output.detail(f"Installing {len(vm_template.snap)} snap packages...")
        for pkg in vm_template.snap:
            try:
                ts_target.run(f"snap install {shlex.quote(pkg)}", sudo=True, timeout=120)
            except SSHError as e:
                msg = f"snap install '{pkg}' failed: {e}"
                logger.warning(msg)
                output.warn(msg)

    # Non-fatal: set default shell (before install commands so installers
    # write to the correct rc file). The zsh ``zsh-newuser-install``
    # first-run wizard is pre-empted by the skel seed.
    logger.step("Shell configuration")
    admin_shell = admin.shell
    output.detail(f"Setting shell to {admin_shell}...")
    try:
        ts_target.run(
            f"usermod -s $(which {shlex.quote(admin_shell)}) {shlex.quote(admin_username)}",
            sudo=True,
        )
    except SSHError as e:
        msg = f"shell configuration failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    # Non-fatal: reconcile authorized_keys
    _reconcile_authorized_keys(ts_target, config, home, logger)

    # Non-fatal: workspaces directory with ACLs for group-writable files.
    # Default ACLs ensure new files/dirs inherit group rwx regardless of umask.
    # Access ACLs fix existing files. Applied recursively to cover all workspaces.
    workspaces_dir = config.paths.vm_workspaces
    if workspaces_dir.startswith("/home/"):
        output.warn(
            f"vm_workspaces is under /home ({workspaces_dir}). "
            "This may require the home directory to be world-traversable."
        )
    try:
        # acl is now installed as a system package in _install_system_packages
        ts_target.run(f"mkdir -p {workspaces_dir}", sudo=True)
        # Ensure all parent directories are traversable by agents
        ts_target.run(
            f'sh -c \'p={workspaces_dir}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
            sudo=True,
        )
        # Default ACLs on directories only (setfacl -R -d warns on files)
        ts_target.run(
            f"find {workspaces_dir} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
            sudo=True,
            timeout=120,
        )
        # Access ACLs on all existing files and dirs
        ts_target.run(
            f"setfacl -R -m g::rwx -m m::rwx {workspaces_dir}",
            sudo=True,
            timeout=120,
        )
    except SSHError as e:
        msg = f"workspaces directory setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    # Non-fatal: agent tmux socket directory infrastructure.
    # Creates the shared group, root directory, and per-agent subdirectories.
    try:
        from agentworks.sessions.tmux import (
            cleanup_stale_sockets,
            ensure_agent_socket_dir,
            ensure_agent_socket_root,
        )

        logger.step("Agent tmux socket directories")
        output.detail("Setting up agent tmux socket infrastructure...")

        ensure_agent_socket_root(ts_target, admin_username, warn_if_missing=not is_first_init)
        for agent in db.list_agents(vm_name=vm_name):
            ensure_agent_socket_dir(ts_target, agent.linux_user)
            removed = cleanup_stale_sockets(ts_target, agent.linux_user)
            if removed:
                output.detail(f"Cleaned up {removed} stale socket(s) for {agent.linux_user}")
    except SSHError as e:
        msg = f"agent tmux socket setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)

    # Non-fatal: system install commands
    system_path = _run_catalog_commands(
        ts_target,
        vm_template.system_install_commands,
        catalog.system_install_commands,
        admin_shell,
        home,
        logger,
        label="System install command",
    )

    # Non-fatal: mise config (written before dotfiles so dotfiles can override)
    mise_path: list[str] = _mise_shims_path(home)
    if admin.mise_packages:
        _write_mise_config(ts_target, admin.mise_packages, admin.mise_install_before, home, logger)

    # Non-fatal: git safe.directory wildcard (disables ownership checks for the
    # multi-user workspace model where agents access repos owned by admin)
    if admin.git_force_safe_directory:
        try:
            ts_target.run("git config --global --add safe.directory '*'")
            output.detail("Git safe.directory wildcard configured")
        except SSHError as e:
            msg = f"git safe.directory setup failed: {e}"
            logger.warning(msg)
            output.warn(msg)

    # Non-fatal: git credentials (before dotfiles and mise lockfile for private repos)
    if providers:
        _configure_git_credentials(vm_name, ts_target, providers, logger, git_tokens=git_tokens)

    # Non-fatal: dotfiles (can override mise config, can provide lockfile)
    if admin.dotfiles_source:
        logger.step("Dotfiles")
        dest = admin.dotfiles_destination.replace("~", home)
        try:
            from agentworks.sources import SourceRefError, fetch_dir, parse_source_ref

            ref = parse_source_ref(admin.dotfiles_source)
            output.detail(f"Syncing dotfiles from {admin.dotfiles_source}...")
            fetch_dir(ref, ts_target, dest, logger=logger)

            output.detail(f"Running dotfiles install: {admin.dotfiles_install_cmd}")
            ts_target.run(
                f"cd {dest} && {admin.dotfiles_install_cmd}",
                timeout=120,
            )
        except (SourceRefError, Exception) as e:
            msg = f"dotfiles install failed: {e}"
            logger.warning(msg)
            output.warn(msg)

    # Non-fatal: mise lockfile (after git creds and dotfiles; overrides dotfiles lockfile)
    if admin.mise_lockfile:
        _fetch_mise_lockfile(ts_target, admin.mise_lockfile, home, logger)

    # Non-fatal: mise install (after config + dotfiles + lockfile are all settled)
    prune = admin.mise_prune_on_reinit
    if admin.mise_packages or admin.mise_lockfile:
        _run_mise_install(
            ts_target, admin_shell, home, admin.mise_allow_unlocked, logger,
            prune=prune,
        )
    else:
        try:
            check = ts_target.run(f"test -f {home}/.config/mise/config.toml", check=False)
            if check.ok:
                _run_mise_install(
                    ts_target, admin_shell, home, admin.mise_allow_unlocked, logger,
                    prune=prune,
                )
        except SSHError:
            pass

    # Non-fatal: user install commands for admin user (may depend on mise tools)
    user_path = _run_catalog_commands(
        ts_target,
        admin.user_install_commands,
        catalog.user_install_commands,
        admin_shell,
        home,
        logger,
        label="User install command",
    )

    # Non-fatal: shell profile (PATH exports sourced at login)
    all_paths = system_path + mise_path + user_path
    _write_agentworks_profile(ts_target, all_paths, logger)

    # Non-fatal: shell rc (interactive shell hooks like mise activate)
    rc_snippets = [MISE_ACTIVATE_LINES] if admin.mise_activate else ["# mise activation disabled"]
    _write_agentworks_rc(ts_target, rc_snippets, logger)

    # Non-fatal: Claude Code marketplaces and plugins for admin user
    def _admin_run_cmd(cmd: str, timeout: int) -> object:
        inner = shlex.quote(cmd)
        return ts_target.run(f"{admin_shell} -lc {inner}", timeout=timeout)

    install_claude_plugins(_admin_run_cmd, admin.claude_marketplaces, admin.claude_plugins, logger)

    # Defensive final step: re-ensure source lines in case any earlier
    # step (dotfiles install in particular) overwrote a shell rc file
    # in place. Idempotent grep-or-append.
    _ensure_agentworks_files_sourced(
        ts_target, home=home, shell=admin_shell, logger=logger,
    )


RunCmd = Callable[[str, int], object]
"""Callable that runs a shell command with a timeout. Used to abstract
the choice of ``Transport`` (admin vs agent) at the call site."""


def install_claude_plugins(
    run_cmd: RunCmd,
    marketplaces: list[str],
    plugins: list[str],
    logger: SSHLogger | None = None,
) -> None:
    """Register Claude Code marketplaces and install plugins. Non-fatal.

    The caller provides a ``run_cmd`` that wraps the command in a login
    shell (``{shell} -lc <cmd>``) so the calling user's PATH (mise shims,
    ``~/.local/bin``, etc.) is in scope. A plain non-interactive SSH
    invocation gets a non-login shell that sources neither ``.bashrc``
    nor ``.profile``, so ``command -v claude`` would falsely fail. Both
    the admin call site (``_phase_b_setup`` in this file) and the agent
    call site (``_create_agent_on_vm`` in ``agents/manager.py``) wrap
    accordingly; the helper itself stays transport- and user-agnostic.
    """
    if not marketplaces and not plugins:
        return

    if logger:
        logger.step("Claude plugins")

    try:
        # Verify claude is available before attempting marketplace/plugin setup
        run_cmd("command -v claude >/dev/null 2>&1", 10)
    except SSHError as e:
        msg = (
            f"claude CLI not available; skipping marketplace/plugin setup ({e}). "
            "Install claude (e.g. via user_install_commands or any other method) and rerun init."
        )
        if logger:
            logger.warning(msg)
        output.warn(msg)
        return

    try:
        for source in marketplaces:
            output.detail(f"Registering Claude marketplace: {source}")
            run_cmd(f"claude plugin marketplace add {shlex.quote(source)}", 60)

        for plugin in plugins:
            output.detail(f"Installing Claude plugin: {plugin}")
            run_cmd(f"claude plugin install {shlex.quote(plugin)} --scope user", 60)
    except SSHError as e:
        msg = f"Claude plugin install failed: {e}"
        if logger:
            logger.warning(msg)
        output.warn(msg)


def _configure_git_credentials(
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
) -> None:
    """Configure git credential store on the VM with the pre-resolved
    framework tokens.

    Phase 1d of the Resource Registry SDD: ``git_tokens`` is required
    (no provider-side fallback); the framework resolves every token
    at manager-entry and threads the ``{credential_name: value}``
    dict in. Any name in ``providers`` that doesn't have a matching
    key in ``git_tokens`` is a contract violation (caller bug); we
    raise loudly rather than silently dropping the credential, since
    silently shipping a VM with a missing credential the operator
    asked for is the worst kind of footgun.
    """
    logger.step("Git credentials")
    output.detail("Configuring git credentials...")

    missing = [name for name in providers if name not in git_tokens]
    if missing:
        raise StateError(
            f"git credential setup: token(s) not resolved by the framework "
            f"for {missing!r}; caller must pre-resolve every provider's "
            f"token via _collect_git_tokens before invoking this function",
            entity_kind="git-credential",
            entity_name=missing[0],
        )

    # Collect credential lines from all providers.
    credential_lines: list[str] = []
    for name, provider in providers.items():
        try:
            credential_lines.extend(
                provider.credential_lines(git_tokens[name])
            )
        except Exception as e:
            msg = f"git credential setup failed for {name}: {e}"
            logger.warning(msg)
            output.warn(msg)

    if not credential_lines:
        return

    # Write credentials and configure git on the VM
    try:
        cred_content = "\n".join(credential_lines) + "\n"
        ts_target.write_file("~/.git-credentials", cred_content, mode="600")
        ts_target.run(
            "git config --global credential.helper store",
        )
        output.detail(f"Git credentials configured for {len(providers)} provider(s)")
    except SSHError as e:
        msg = f"git credential store setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)

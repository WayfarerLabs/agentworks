"""Profile / rc / identity / sshd / sudoers / skel fragment writers.

These write the on-VM shell and system files that make agentworks-managed
env vars, PATH entries, and identity visible to login shells, interactive
shells, sshd, and sudo. All idempotent (grep-or-append / stage-validate-
promote / full overwrite), so every function here is safe to re-run on
``vm reinit``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC
from agentworks.ssh import SSHError, SSHLogger

if TYPE_CHECKING:
    from agentworks.transports import Transport

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
            f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{profile_source}' >> {rc}",
            check=False,
        )

    rc_source = f". {home}/{AGENTWORKS_RC}"
    rc_files = [f"{home}/.bashrc"]
    if shell == "zsh":
        rc_files.append(f"{home}/.zshrc")
    for rc in rc_files:
        target.run(
            f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || printf '%s\\n' '{rc_source}' >> {rc}",
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
    output.info(f"Writing agentworks profile ({output.count(len(unique_paths), 'PATH entry', 'PATH entries')})...")

    try:
        lines = ["# Managed by agentworks -- do not edit"]
        # Cross-agent isolation of the admin's files is enforced by the 0750
        # admin home + private primary group (see the "Admin home permissions"
        # step in driver._phase_b_setup): once agent users cannot search the
        # home, files inside it are unreachable regardless of their own mode,
        # so the umask adds nothing there. Its real value is defense-in-depth
        # for artifacts the admin writes OUTSIDE its home: /tmp, $TMPDIR, and
        # any world-traversable shared directory, where the file's own mode is
        # what protects it. 027 is portable across sh/bash/zsh. It does NOT
        # reduce group access inside a workspace: workspace dirs carry a POSIX
        # default ACL (setfacl -d, see workspaces/backends/vm.py) that makes new
        # files inherit group rwx regardless of the process umask, so
        # collaboration is preserved. Coverage is partial by design: this rides
        # the login-shell profile chain, so non-login `sh -c`, cron, systemd
        # user units, and sftp/scp keep the default umask 022. The 0750 home is
        # the boundary; the umask is a supplement. Emitted here (not appended
        # separately) so it survives every rewrite of this file on reinit.
        lines.append("umask 027")
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


def _harden_admin_home(
    target: Transport,
    *,
    home: str,
    admin_username: str,
    logger: SSHLogger,
) -> None:
    """Tighten the admin's home to mode 0750 and verify the primary group is
    private. The admin counterpart of the agent-home hardening in
    ``agentworks.agents.initializer.create_agent_on_vm``.

    The bootstrap ``useradd -m`` honors the system umask (022), leaving the
    admin home world-readable (0755), which lets any agent user on the VM read
    the admin's git credentials, shell history, tool caches, and dotfiles.
    Workspaces live at ``paths.vm_workspaces`` (validated outside ``/home``, see
    ``config.validation.validate_vm_workspaces``), not under the admin home, so
    nothing an agent must reach lives here and 0750 breaks no cross-user access.

    The admin owns its own home, so the ``chmod`` needs no sudo (contrast the
    agent path, where the admin transport chmods a DIFFERENT user's home). The
    chmod is idempotent, so this is safe on both initial provision and reinit.

    Post-condition guard: the bootstrap forces a private primary group on the
    create path (``groupadd -f`` + ``useradd -g``), but reinit, VMs provisioned
    before that step existed, and odd images can still leave a shared primary
    group, which would make the 0750 home group-readable by whoever shares that
    group. Warn (do not fail) so drift is surfaced with a fix hint rather than
    silently defeating isolation; a hard fail could block a legitimately custom
    setup, and the bootstrap already covers fresh creates.
    """
    logger.step("Admin home permissions")
    try:
        target.run(f"chmod 0750 {shlex.quote(home)}")

        primary_group = target.run(f"id -gn {shlex.quote(admin_username)}", check=False)
        if primary_group.ok and primary_group.stdout.strip() != admin_username:
            msg = (
                f"admin '{admin_username}' primary group is "
                f"'{primary_group.stdout.strip()}', not a private per-user group; "
                f"its home ({home}) cannot be made private and may be readable by "
                f"other members of that group. Fix on the VM with "
                f"'sudo groupadd {admin_username} && sudo usermod -g {admin_username} {admin_username}' "
                f"and reinit, or check the image's group model."
            )
            logger.warning(msg)
            output.warn(msg)
    except SSHError as e:
        msg = f"admin home permissions setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)


# -- VM-side identity / sshd / sudoers fragments -----------------------------
#
# Three on-VM files maintained by agentworks init:
#
#   /etc/profile.d/agentworks-identity.sh
#     System-wide login-shell fragment with the VM-stable identity vars
#     (AGENTWORKS_VM / AGENTWORKS_PLATFORM / AGENTWORKS_SITE). Sourced by
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
#     Validated with `visudo -cf` on a staging path before install.
#
#   /etc/sudoers.d/51-agentworks-console-setenv
#     `Defaults:<admin> setenv`. Permits the admin user to use
#     `sudo --preserve-env=<keys>` so arbitrarily-named operator env and
#     secrets (not just AGENTWORKS_*/AW_*) survive the same sudo boundary
#     in console add-shell agent panes. See
#     docs/adrs/0017-console-pane-preserve-env.md. Validated with
#     `visudo -cf` on a staging path before install.

AGENTWORKS_IDENTITY_PROFILE_PATH = "/etc/profile.d/agentworks-identity.sh"
AGENTWORKS_ZPROFILE_PATH = "/etc/zsh/zprofile"
AGENTWORKS_SSHD_ACCEPT_ENV_PATH = "/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf"
AGENTWORKS_SUDOERS_ENV_KEEP_PATH = "/etc/sudoers.d/50-agentworks-env-keep"

# The env_keep allowlist, as sudoers glob patterns. Vars matching these cross a
# sudo boundary without needing `--preserve-env`; everything else in a console
# agent pane's composed env rides the setenv fragment instead (ADR 0017). The
# console pane's capability probe picks a var no pattern here covers, so this
# is the shared source of truth for both.
AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS = ("AGENTWORKS_*", "AW_*")
AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH = "/etc/sudoers.d/51-agentworks-console-setenv"

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
    output.info(f"Writing {SKEL_BASHRC_PATH} and {SKEL_ZSHRC_PATH}...")

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
        msg = f"skel seed write failed: {e}. Re-run `agw vm reinit` to retry."
        logger.warning(msg)
        output.warn(msg)


def _write_agentworks_identity_profile(
    target: Transport,
    identity_env: dict[str, str],
    logger: SSHLogger,
) -> None:
    """Write the VM-stable identity profile fragment.

    System-wide; sourced by every login shell on the VM. ``identity_env``
    is the VM-stable subset (AGENTWORKS_VM, AGENTWORKS_PLATFORM,
    AGENTWORKS_SITE) produced by ``agentworks.env.vm_stable_identity_env``.

    The ``/etc/profile.d/agentworks-identity.sh`` file is fully owned by
    agentworks: each reinit overwrites it. The block in ``/etc/zsh/zprofile``
    is bracketed by ``# agentworks-identity-begin`` / ``# agentworks-identity-end``
    marker comments; content between those markers is agentworks-owned and
    gets rewritten on every reinit. An operator who hand-edits between the
    markers is opting in to having that content overwritten.
    """
    logger.step("Identity profile")
    output.info(f"Writing {AGENTWORKS_IDENTITY_PROFILE_PATH} ({output.count(len(identity_env), 'var')})...")

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
                f"sudo grep -qF {q_begin} {q_zprofile}",
                check=False,
            ).ok
            has_end = target.run(
                f"sudo grep -qF {q_end} {q_zprofile}",
                check=False,
            ).ok
            if has_begin and has_end:
                sed_script = shlex.quote(f"/^{marker}-begin/,/^{marker}-end/d")
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
        msg = f"identity profile write failed: {e}. Re-run `agw vm reinit` to retry."
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
    output.info(f"Writing {AGENTWORKS_SSHD_ACCEPT_ENV_PATH}...")

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
            raise SSHError(f"sshd -t rejected the AcceptEnv fragment: {validate.stderr.strip()}")

        # Validation OK: drop the backup (best-effort; orphaned .bak is
        # harmless since `.bak` doesn't match `*.conf` in sshd_config.d).
        if had_prior:
            target.run(f"sudo rm -f {q_bak}", check=False)

        target.run("sudo systemctl reload ssh", check=False)
    except SSHError as e:
        msg = f"sshd AcceptEnv install failed: {e}. Re-run `agw vm reinit` to retry."
        logger.warning(msg)
        output.warn(msg)


def _install_sudoers_fragment(
    target: Transport,
    *,
    path: str,
    body: str,
    label: str,
) -> None:
    """Install one sudoers.d/ fragment via stage -> validate -> promote.

    ``label`` names the fragment in the error message (e.g. "env_keep").
    A broken sudoers fragment can lock the operator out of sudo entirely,
    so the ``visudo -cf`` validate step is load-bearing: the body is
    written to a ``.tmp`` staging path, validated, and only then moved
    onto the real path. Raises ``SSHError`` if validation fails; callers
    decide whether that is fatal.

    The staging path uses a ``.tmp`` suffix; sudo's /etc/sudoers.d/ loader
    only picks up files whose names don't contain a literal '.' AND match
    the no-tilde rule, so ``.tmp`` files are safely ignored even if cleanup
    races mid-init.
    """
    q_body = shlex.quote(body)
    q_path = shlex.quote(path)
    staging = path + ".tmp"
    q_staging = shlex.quote(staging)
    try:
        target.run(
            f"printf '%s' {q_body} | sudo tee {q_staging} > /dev/null",
        )
        target.run(f"sudo chmod 0440 {q_staging}")
        validate = target.run(f"sudo visudo -cf {q_staging}", check=False)
        if not validate.ok:
            raise SSHError(f"visudo -cf rejected the {label} fragment: {validate.stderr.strip()}")
        target.run(f"sudo mv {q_staging} {q_path}")
    finally:
        # Always best-effort-remove the staging path. On the success path
        # the mv above already moved the file, so rm is a no-op; on any
        # failure path we don't want orphaned .tmp files accumulating
        # under /etc/sudoers.d/.
        target.run(f"sudo rm -f {q_staging}", check=False)


def _write_sudoers_env_keep(
    target: Transport,
    logger: SSHLogger,
) -> None:
    """Deploy ``env_keep += "AGENTWORKS_* AW_*"`` to sudoers.d/.

    Preserves agentworks-managed vars (``AGENTWORKS_*`` / ``AW_*``) across
    the user switch in console add-shell agent panes (the tmux server runs
    as admin; the pane sudo's to the agent). Arbitrarily-named operator env
    and secrets ride a separate mechanism (``--preserve-env`` + the console
    setenv fragment); see ``_write_sudoers_console_setenv`` and
    docs/adrs/0017-console-pane-preserve-env.md.
    """
    logger.step("sudoers env_keep")
    output.info(f"Writing {AGENTWORKS_SUDOERS_ENV_KEEP_PATH}...")

    body = (
        "# Managed by agentworks -- do not edit.\n"
        "# Preserves agentworks-managed env vars across sudo for the\n"
        "# console add-shell agent-pane path; see\n"
        "# docs/adrs/0014-sshd-accept-env-wildcard.md.\n"
        f'Defaults env_keep += "{" ".join(AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS)}"\n'
    )
    try:
        _install_sudoers_fragment(
            target,
            path=AGENTWORKS_SUDOERS_ENV_KEEP_PATH,
            body=body,
            label="env_keep",
        )
    except SSHError as e:
        msg = f"sudoers env_keep install failed: {e}. Re-run `agw vm reinit` to retry."
        logger.warning(msg)
        output.warn(msg)


def _write_sudoers_console_setenv(
    target: Transport,
    logger: SSHLogger,
    admin_user: str,
) -> None:
    """Deploy ``Defaults:<admin> setenv`` to sudoers.d/.

    Lets the admin user carry arbitrarily-named operator env and secrets
    across the sudo boundary in console add-shell agent panes via
    ``sudo --preserve-env=<keys>`` (see ``_split_shell_pane``). Without
    ``setenv``, sudo refuses ``--preserve-env`` for any var outside the
    ``env_keep`` allowlist, so only ``AGENTWORKS_*`` / ``AW_*`` would
    survive. Scoped to the admin user (``Defaults:<user>``), not global.

    The admin already holds ``ALL=(ALL) NOPASSWD:ALL``, so granting it
    ``setenv`` is no meaningful privilege change: it only permits
    command-line env preservation, which a full-root user could already
    achieve. See docs/adrs/0017-console-pane-preserve-env.md.
    """
    logger.step("sudoers console setenv")
    output.info(f"Writing {AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH}...")

    body = (
        "# Managed by agentworks -- do not edit.\n"
        "# Permits `sudo --preserve-env=<keys>` for the admin user so\n"
        "# composed operator env and secrets survive the sudo boundary in\n"
        "# console add-shell agent panes; see\n"
        "# docs/adrs/0017-console-pane-preserve-env.md.\n"
        f"Defaults:{admin_user} setenv\n"
    )
    try:
        _install_sudoers_fragment(
            target,
            path=AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH,
            body=body,
            label="console setenv",
        )
    except SSHError as e:
        msg = f"sudoers console setenv install failed: {e}. Re-run `agw vm reinit` to retry."
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
    output.info("Writing agentworks rc...")

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

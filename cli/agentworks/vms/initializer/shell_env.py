"""Profile / rc / identity / sshd / sudoers / skel fragment writers.

These write the on-VM shell and system files that make agentworks-managed
env vars, PATH entries, and identity visible to login shells, interactive
shells, sshd, and sudo. All idempotent (grep-or-append / stage-validate-
promote / full overwrite), so every function here is safe to re-run on
``vm reinit``.

The system fragments (identity profile, zprofile block, sshd, sudoers,
skel) are written as root through the file program: ``Files.write`` for a
file Agentworks owns whole, ``Files.update`` over what was read for one it
edits. Neither writes through a link.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC
from agentworks.execution.requests import Command
from agentworks.ssh import SSHError, SSHLogger

from .steps import STEP_FAILURES, update_and_apply, warn_step

if TYPE_CHECKING:
    from agentworks.execution.files import Files
    from agentworks.transports import Transport

    from .steps import InitLog

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


def _write_skel_seeds(files: Files, log: InitLog) -> None:
    """Write the agentworks-managed shell rc seeds to ``/etc/skel``.

    Both ``/etc/skel/.bashrc`` and ``/etc/skel/.zshrc`` are
    agentworks-owned on the VM: every reinit overwrites them, root-owned
    0644. Future ``useradd -m`` (e.g. agent creation) inherits the seed
    automatically, so agent setup needs no explicit copy.

    Caller MUST schedule this AFTER ``_install_apt_packages``:
    ``/etc/skel/.bashrc`` is a Debian conffile shipped by ``bash``, so
    an apt upgrade under ``--force-confnew`` (the standard install
    flag) would silently replace the seed if we wrote it earlier.
    Same constraint, and same rationale, as
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
    log.step("Shell rc skel")
    output.info(f"Writing {SKEL_BASHRC_PATH} and {SKEL_ZSHRC_PATH}...")

    try:
        for path, content in ((SKEL_BASHRC_PATH, BASHRC), (SKEL_ZSHRC_PATH, ZSHRC)):
            files.write(path, content.encode(), mode=0o644, parent_mode=0o755, elevated=True)
    except STEP_FAILURES as error:
        warn_step(log, "skel seed write failed; re-run 'agw vm reinit' to retry", error)


def _identity_markers(current: str) -> tuple[bool, bool]:
    """Whether ``current`` has a line starting with the identity block's begin marker, and one with its end."""
    lines = current.splitlines()
    begin, end = f"{_ZSH_IDENTITY_MARKER}-begin", f"{_ZSH_IDENTITY_MARKER}-end"
    return any(line.startswith(begin) for line in lines), any(line.startswith(end) for line in lines)


def with_identity_block(current: str | None, block: str) -> str:
    """``/etc/zsh/zprofile`` holding ``block`` as its identity block, from its ``current`` content.

    An absent file gets the block alone. With both markers present, every
    line from one starting with the begin marker through the next one
    starting with the end marker is removed, as ``sed '/^begin/,/^end/d'``
    removes it, and the block is appended. With one marker, or none, the
    block is appended and the rest is left alone: deleting from an orphan
    begin marker would run to the end of the file and take unrelated
    content with it. A marker counts only at the start of a line, the rule
    the removal uses. A last line with no newline gets one before the block.
    """
    if current is None:
        return block
    begin, end = f"{_ZSH_IDENTITY_MARKER}-begin", f"{_ZSH_IDENTITY_MARKER}-end"
    if all(_identity_markers(current)):
        kept: list[str] = []
        inside = False
        for line in current.splitlines(keepends=True):
            if inside:
                inside = not line.startswith(end)
            elif line.startswith(begin):
                inside = True
            else:
                kept.append(line)
        current = "".join(kept)
    if current and not current.endswith("\n"):
        current += "\n"
    return current + block


def _write_agentworks_identity_profile(files: Files, identity_env: dict[str, str], log: InitLog) -> None:
    """Write the VM-stable identity profile fragment.

    System-wide; sourced by every login shell on the VM. ``identity_env``
    is the VM-stable subset (AGENTWORKS_VM, AGENTWORKS_PLATFORM,
    AGENTWORKS_SITE) produced by ``agentworks.env.vm_stable_identity_env``.

    The ``/etc/profile.d/agentworks-identity.sh`` file is fully owned by
    agentworks: each reinit overwrites it. The block in ``/etc/zsh/zprofile``
    is bracketed by ``# agentworks-identity-begin`` / ``# agentworks-identity-end``
    marker comments; content between those markers is agentworks-owned and
    gets rewritten on every reinit (:func:`with_identity_block`). An operator
    who hand-edits between the markers is opting in to having that content
    overwritten. The rest of the file, and its owner, mode and attributes,
    are kept, and an unchanged file is not rewritten.
    """
    log.step("Identity profile")
    output.info(f"Writing {AGENTWORKS_IDENTITY_PROFILE_PATH} ({output.count(len(identity_env), 'var')})...")

    lines = ["# Managed by agentworks -- do not edit"]
    for key, value in identity_env.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    body = "\n".join(lines) + "\n"
    marker = _ZSH_IDENTITY_MARKER
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in identity_env.items())
    zsh_block = f"{marker}-begin\n{exports}{marker}-end\n"

    try:
        # System-wide /etc/profile.d/ fragment.
        files.write(AGENTWORKS_IDENTITY_PROFILE_PATH, body.encode(), mode=0o644, parent_mode=0o755, elevated=True)

        # Mirror into /etc/zsh/zprofile because zsh skips /etc/profile.d by
        # default. On a fresh VM, /etc/zsh/ doesn't exist until apt installs
        # zsh-common; the write creates it with the absent file, and when zsh is
        # later installed, dpkg's noninteractive default keeps our fragment.
        observed = files.read(AGENTWORKS_ZPROFILE_PATH, elevated=True)
        # surrogateescape keeps any byte that is not UTF-8 exactly as it was.
        current = None if observed is None else observed.decode("utf-8", "surrogateescape")
        if current is not None and len(set(_identity_markers(current))) == 2:
            output.warn(
                f"{AGENTWORKS_ZPROFILE_PATH} has unmatched "
                "agentworks-identity markers; leaving them in place "
                "and appending a fresh block. Inspect the file and "
                "remove the orphan marker manually."
            )
        files.update(
            AGENTWORKS_ZPROFILE_PATH,
            with_identity_block(current, zsh_block).encode("utf-8", "surrogateescape"),
            observed=observed,
            mode=0o644,
            parent_mode=0o755,
            preserve_metadata=True,
            elevated=True,
        )
    except STEP_FAILURES as error:
        warn_step(log, "identity profile write failed; re-run 'agw vm reinit' to retry", error)


def _write_sshd_accept_env(files: Files, log: InitLog) -> None:
    """Deploy ``AcceptEnv *`` to sshd_config.d/ and reload sshd.

    The directive lets ``ssh -o SetEnv=KEY=VALUE`` calls from the CLI flow
    through to the user's shell. See the agentworks AcceptEnv wildcard
    ADR for the trust-anchor analysis behind the wildcard.

    Validation strategy (write, validate, restore on failure): ``sshd -t``
    validates the FULL merged config (it follows the
    ``Include /etc/ssh/sshd_config.d/*.conf`` directive in
    ``/etc/ssh/sshd_config``), so the snippet cannot be validated in
    isolation. The fragment is written only when its content differs from
    what was read, then validated and sshd reloaded; it counts as applied
    only once both succeed. On any failure after the write (a rejection, a
    timeout, a failed reload, an interrupt) the prior content is put back,
    or the fragment removed when there was none, conditional on the
    fragment still being the one this run wrote, so the next reinit
    retries. A steady-state reinit leaves sshd alone. The window where an unvalidated fragment sits at the real path
    is bounded by the ``sshd -t`` call and never affects the running sshd,
    unless an unrelated process reloads sshd inside it (a ``dpkg``
    postinst on openssh-server under ``unattended-upgrades``, say); the
    snippet is a single ``AcceptEnv *`` line, so that bounded risk is
    accepted.
    """
    log.step("sshd AcceptEnv")
    output.info(f"Writing {AGENTWORKS_SSHD_ACCEPT_ENV_PATH}...")

    body = (
        b"# Managed by agentworks -- do not edit.\n"
        b"# Allows agentworks-issued SSH commands to inject env vars via\n"
        b"# `-o SetEnv=KEY=VALUE`; see the agentworks AcceptEnv wildcard ADR.\n"
        b"AcceptEnv *\n"
    )
    path = AGENTWORKS_SSHD_ACCEPT_ENV_PATH

    def validate_and_reload() -> None:
        files.access.run(Command(("sshd", "-t"), elevated=True, deadline=files.deadline), check=True)
        files.access.run(Command(("systemctl", "reload", "ssh"), elevated=True, deadline=files.deadline), check=True)

    try:
        prior = files.read(path, elevated=True)
        update_and_apply(files, path, body, observed=prior, mode=0o644, parent_mode=0o755, apply=validate_and_reload)
    except STEP_FAILURES as error:
        warn_step(log, "sshd AcceptEnv install failed; re-run 'agw vm reinit' to retry", error)


def _install_sudoers_fragment(files: Files, log: InitLog, *, path: str, body: str) -> None:
    """Install one sudoers.d/ fragment via stage -> validate -> promote.

    A broken sudoers fragment can lock the operator out of sudo entirely,
    so the ``visudo -cf`` validate step is load-bearing: the body is
    written root-owned 0440 to a ``.tmp`` staging path, validated, and only
    then moved onto the real path with ``mv -T``, so a directory at the real
    path never receives the fragment inside it. A rejection raises
    ``CommandFailed``; callers decide whether that is fatal.

    sudo's /etc/sudoers.d/ loader skips files whose names contain a
    literal '.', so the ``.tmp`` staging file is ignored even if cleanup
    races mid-init. The staging file is removed on every path.
    """
    staging = path + ".tmp"
    try:
        files.write(staging, body.encode(), mode=0o440, parent_mode=0o755, elevated=True)
        files.access.run(Command(("visudo", "-cf", staging), elevated=True, deadline=files.deadline), check=True)
        files.access.run(
            Command(("mv", "-fT", "--", staging, path), elevated=True, deadline=files.deadline), check=True
        )
    finally:
        # On the success path the mv already moved the file, so this is a
        # no-op; on a failure it keeps .tmp files from accumulating. Its
        # status is not read, and a request that fails is warned about here,
        # so it never replaces the failure that brought us here.
        try:
            files.access.run(Command(("rm", "-f", "--", staging), elevated=True, deadline=files.deadline))
        except STEP_FAILURES as error:
            warn_step(log, f"removing the sudoers staging file {staging} failed", error)


def _write_sudoers_env_keep(files: Files, log: InitLog) -> None:
    """Deploy ``env_keep += "AGENTWORKS_* AW_*"`` to sudoers.d/.

    Preserves agentworks-managed vars (``AGENTWORKS_*`` / ``AW_*``) across
    the user switch in console add-shell agent panes (the tmux server runs
    as admin; the pane sudo's to the agent). Arbitrarily-named operator env
    and secrets ride a separate mechanism (``--preserve-env`` + the console
    setenv fragment); see ``_write_sudoers_console_setenv`` and
    docs/adrs/0017-console-pane-preserve-env.md.
    """
    log.step("sudoers env_keep")
    output.info(f"Writing {AGENTWORKS_SUDOERS_ENV_KEEP_PATH}...")

    body = (
        "# Managed by agentworks -- do not edit.\n"
        "# Preserves agentworks-managed env vars across sudo for the\n"
        "# console add-shell agent-pane path; see\n"
        "# docs/adrs/0014-sshd-accept-env-wildcard.md.\n"
        f'Defaults env_keep += "{" ".join(AGENTWORKS_SUDOERS_ENV_KEEP_PATTERNS)}"\n'
    )
    try:
        _install_sudoers_fragment(files, log, path=AGENTWORKS_SUDOERS_ENV_KEEP_PATH, body=body)
    except STEP_FAILURES as error:
        warn_step(log, "sudoers env_keep install failed; re-run 'agw vm reinit' to retry", error)


def _write_sudoers_console_setenv(files: Files, log: InitLog, admin_user: str) -> None:
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
    log.step("sudoers console setenv")
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
        _install_sudoers_fragment(files, log, path=AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH, body=body)
    except STEP_FAILURES as error:
        warn_step(log, "sudoers console setenv install failed; re-run 'agw vm reinit' to retry", error)


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

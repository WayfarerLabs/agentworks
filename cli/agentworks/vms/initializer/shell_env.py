"""Profile / rc / identity / skel fragment writers, and the retired sshd and sudoers fragments' removal.

These write the on-VM shell and system files that make agentworks-managed
env vars, PATH entries, and identity visible to login shells and
interactive shells. All idempotent (append-if-missing, full overwrite,
removal of a path Agentworks owns), so every function here is safe to
re-run on ``vm reinit``.

Every file is written through the file program: ``Files.write`` for a file
Agentworks owns whole, ``Files.update`` over what was read for one it edits.
The system fragments (identity profile, zprofile block, skel)
are written as root; the per-user profile and rc fragments and the rc files'
source lines as the account itself, admin or agent. Nothing is written
through a link: a linked rc file is accepted only when the file it links to
already sources the fragment, and otherwise warned about with the line to add.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.skel import BASHRC, ZSHRC
from agentworks.errors import ExternalError
from agentworks.execution.errors import PathRefusal, PathRefused
from agentworks.execution.files import Absent
from agentworks.execution.requests import Command, Script, Shell
from agentworks.vms.home_layout import AGENTWORKS_PROFILE, AGENTWORKS_RC

from .steps import STEP_FAILURES, status, warn, warn_step

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.files import Files

    from .steps import InitLog

SKEL_BASHRC_PATH = "/etc/skel/.bashrc"
SKEL_ZSHRC_PATH = "/etc/skel/.zshrc"


def source_files(home: str, shell: str) -> dict[str, tuple[str, ...]]:
    """The rc files that source each Agentworks fragment, keyed by the fragment's file name.

    The profile fragment is sourced from ``~/.profile`` and ``~/.bashrc``,
    and ``~/.zprofile`` for a zsh account; the rc fragment from ``~/.bashrc``,
    and ``~/.zshrc`` for a zsh account. Every writer of a source line reads
    its files from here.
    """
    zsh = shell == "zsh"
    return {
        AGENTWORKS_PROFILE: (f"{home}/.profile", f"{home}/.bashrc", *((f"{home}/.zprofile",) if zsh else ())),
        AGENTWORKS_RC: (f"{home}/.bashrc", *((f"{home}/.zshrc",) if zsh else ())),
    }


class LinkedRcFile(ExternalError):
    """An rc file is a symbolic link whose target does not source the fragment; it is never written through."""

    def __init__(self, rc: str, line: str) -> None:
        super().__init__(
            f"{rc} is a symbolic link, so its source line was not added; "
            f"add '{line}' to the file it links to (usually in your dotfiles)"
        )


def ensure_source_line(files: Files, rc: str, marker: str, line: str, log: InitLog) -> None:
    """Make ``rc`` source a fragment: append ``line`` unless ``marker`` already occurs in it.

    The file is read and published with ``Files.update``, keeping its owner,
    mode and attributes; a missing one is created 0644, and a last line with
    no newline gets one first. A link at ``rc`` is never written through:
    when the file it links to already holds ``marker``, ``log`` says so and
    nothing is written; otherwise :class:`LinkedRcFile` names the line to add
    there. Any other file type is the read's own refusal.
    """
    access = files.access
    try:
        observed = files.read(rc)
    except PathRefused as refused:
        linked = Command(("test", "-L", rc), deadline=files.deadline)
        if refused.reason is not PathRefusal.UNSUITABLE or status(access, linked) != 0:
            raise
        if status(access, Command(("grep", "-qsF", "--", marker, rc), deadline=files.deadline)) != 0:
            raise LinkedRcFile(rc, line) from None
        log.output(f"{rc} is a link whose target already sources {marker}")
        return
    if observed is not None and marker.encode() in observed:
        return
    current = observed or b""
    if current and not current.endswith(b"\n"):
        current += b"\n"
    files.update(
        rc,
        current + line.encode() + b"\n",
        observed=observed,
        mode=0o644,
        parent_mode=0o755,
        preserve_metadata=True,
    )


def _ensure_fragment_sourced(files: Files, log: InitLog, *, home: str, shell: str, fragment: str) -> None:
    """Ensure each rc file :func:`source_files` names for ``fragment`` sources it; each failure warns on its own."""
    line = f". {home}/{fragment}"
    for rc in source_files(home, shell)[fragment]:
        try:
            ensure_source_line(files, rc, fragment, line, log)
        except STEP_FAILURES as error:
            warn_step(log, f"sourcing {fragment} from {rc} failed", error)


def _ensure_agentworks_files_sourced(files: Files, log: InitLog, *, home: str, shell: str) -> None:
    """Make the account's rc files source both Agentworks fragments, as a step of its own.

    The fragment writers ensure their own source lines when they run, but a
    dotfiles install, or any later step that replaces a shell rc file, can
    remove them. Both accounts write their fragments before their dotfiles
    and mise, run this again before their user install commands, whose login
    shells read the profile, and end with it. A file that already names a fragment is
    left alone, so this is a no-op in the common case. ``home`` is the
    account's literal home directory and ``shell`` its login shell, which
    selects the zsh files (:func:`source_files`).
    """
    log.step("Ensure agentworks files sourced")
    for fragment in (AGENTWORKS_PROFILE, AGENTWORKS_RC):
        _ensure_fragment_sourced(files, log, home=home, shell=shell, fragment=fragment)


def profile_fragment(path_additions: list[str], identity_env: Mapping[str, str] | None = None) -> str:
    """The content of ``~/.agentworks-profile.sh``: the umask, identity exports, then one PATH export per entry.

    A leading ``~`` in an entry becomes ``$HOME``. Each export prepends, so
    of an entry listed twice only the last is kept, which keeps its lookup
    precedence when commands contribute interleaved entries such as A, B, A.
    """
    lines = ["# Managed by agentworks -- do not edit"]
    # Cross-account isolation of a home is enforced by its 0750 mode and its
    # private primary group (the "Admin home permissions" step and agent
    # create): once other accounts cannot search the home, files inside it
    # are unreachable regardless of their own mode, so the umask adds nothing
    # there. Its real value is defense-in-depth for artifacts written OUTSIDE
    # the home: /tmp, $TMPDIR, and any world-traversable shared directory,
    # where the file's own mode is what protects it. 027 is portable across
    # sh/bash/zsh. It does NOT reduce group access inside a workspace:
    # workspace dirs carry a POSIX default ACL (setfacl -d, see
    # workspaces/acls.py) that makes new files inherit group rwx regardless of
    # the process umask, so collaboration is preserved. Coverage is partial by
    # design: this rides the login-shell profile chain, so non-login `sh -c`,
    # cron, systemd user units, and sftp/scp keep the default umask 022. The
    # 0750 home is the boundary; the umask is a supplement. Emitted here (not
    # appended separately) so it survives every rewrite of this file.
    lines.append("umask 027")
    for key, value in (identity_env or {}).items():
        lines.append(f"export {key}={shlex.quote(value)}")
    expanded = [p.replace("~", "$HOME", 1) if p.startswith("~") else p for p in path_additions]
    unique = list(dict.fromkeys(reversed(expanded)))
    unique.reverse()
    lines.extend(f'export PATH="{p}:$PATH"' for p in unique)
    return "\n".join(lines) + "\n"


def _write_agentworks_profile(
    files: Files,
    path_additions: list[str],
    log: InitLog,
    *,
    home: str,
    shell: str,
    identity_env: Mapping[str, str] | None = None,
) -> None:
    """Write the account's login profile fragment and make its rc files source it.

    Writes ``~/.agentworks-profile.sh`` (:func:`profile_fragment`), 0644, as
    the account ``files`` acts as: per-user static identity exports (e.g.
    ``AGENTWORKS_AGENT`` for agent users) followed by PATH exports. Sourced
    from the files :func:`source_files` names, so it runs once per login
    shell and is inherited by child processes.

    System-wide identity vars (VM / VM_HOST / PLATFORM) live in
    ``/etc/profile.d/agentworks-identity.sh``, written by
    ``_write_agentworks_identity_profile``. The on-VM Linux user is
    already exposed by the standard ``$USER`` / ``$LOGNAME`` env vars,
    so admin users pass no ``identity_env``.

    Always written (even when both ``identity_env`` and ``path_additions``
    are empty) so that reinit can clear previously set paths or identity.
    """
    log.step("Shell profile")
    output.info(f"Writing agentworks profile ({output.count(len(path_additions), 'PATH entry', 'PATH entries')})...")
    content = profile_fragment(path_additions, identity_env).encode()
    try:
        files.write(f"{home}/{AGENTWORKS_PROFILE}", content, mode=0o644, parent_mode=0o755)
    except STEP_FAILURES as error:
        warn_step(log, "shell profile write failed", error)
        return
    _ensure_fragment_sourced(files, log, home=home, shell=shell, fragment=AGENTWORKS_PROFILE)


def _harden_admin_home(files: Files, *, home: str, admin_username: str, log: InitLog) -> None:
    """Tighten the admin's home to mode 0750 and verify the primary group is
    private. The admin counterpart of the agent-home hardening in
    ``agentworks.agents.initializer.create_agent_on_vm``.

    The bootstrap ``useradd -m`` honors the system umask (022), leaving the
    admin home world-readable (0755), which lets any agent user on the VM read
    the admin's git credentials, shell history, tool caches, and dotfiles.
    Workspaces live at ``paths.vm_workspaces`` (validated outside ``/home``, see
    ``config.validation.validate_vm_workspaces``), not under the admin home, so
    nothing an agent must reach lives here and 0750 breaks no cross-user access.

    The admin owns its own home, so the ``chmod`` runs unelevated (contrast the
    agent path, where root chmods a DIFFERENT user's home). The chmod is
    idempotent, so this is safe on both initial provision and reinit.

    Post-condition guard: the bootstrap forces a private primary group on the
    create path (``groupadd -f`` + ``useradd -g``), but reinit, VMs provisioned
    before that step existed, and odd images can still leave a shared primary
    group, which would make the 0750 home group-readable by whoever shares that
    group. Warn (do not fail) so drift is surfaced with a fix hint rather than
    silently defeating isolation; a hard fail could block a legitimately custom
    setup, and the bootstrap already covers fresh creates.
    """
    log.step("Admin home permissions")
    try:
        files.access.run(Command(("chmod", "0750", home), deadline=files.deadline), check=True)
        primary_group = files.access.run(Command(("id", "-gn", admin_username), deadline=files.deadline))
        group = primary_group.stdout.decode("utf-8", "replace").strip()
        if primary_group.require_completed(files.access.vm_name) == 0 and group != admin_username:
            warn(
                log,
                f"admin '{admin_username}' primary group is "
                f"'{group}', not a private per-user group; "
                f"its home ({home}) cannot be made private and may be readable by "
                f"other members of that group. Fix on the VM with "
                f"'sudo groupadd {admin_username} && sudo usermod -g {admin_username} {admin_username}' "
                f"and reinit, or check the image's group model.",
            )
    except STEP_FAILURES as error:
        warn_step(log, "admin home permissions setup failed", error)


def _set_login_shell(files: Files, *, shell: str, admin_username: str, log: InitLog) -> None:
    """Set the admin's login shell to ``shell``, found on the guest's ``PATH``, as root.

    A shell that is not installed fails the step with a warning, before
    ``usermod`` runs.
    """
    log.step("Shell configuration")
    output.info(f"Setting shell to {shell}...")
    script = f'path=$(command -v {shlex.quote(shell)}) && usermod -s "$path" {shlex.quote(admin_username)}'
    try:
        files.access.run(Script(script, Shell.SH, elevated=True, deadline=files.deadline), check=True)
    except STEP_FAILURES as error:
        warn_step(log, "shell configuration failed", error)


# -- VM-side identity fragments, and the retired sshd and sudoers ones --------
#
# The on-VM system files agentworks init maintains:
#
#   /etc/profile.d/agentworks-identity.sh
#     System-wide login-shell fragment with the VM-stable identity vars
#     (AGENTWORKS_VM / AGENTWORKS_PLATFORM / AGENTWORKS_SITE). Sourced by
#     every login shell on the VM (including raw `ssh awvm--<name>` from
#     outside agentworks), so identity vars don't require agentworks to be
#     the one opening the shell. Also writes to /etc/zsh/zprofile because
#     zsh skips /etc/profile.d by default.
#
# Init also removes three files earlier releases installed to carry
# environment through sshd or across sudo, which nothing does now (ADR 0026):
# /etc/ssh/sshd_config.d/50-agentworks-accept-env.conf (`AcceptEnv *`),
# /etc/sudoers.d/50-agentworks-env-keep (`env_keep += "AGENTWORKS_* AW_*"`)
# and /etc/sudoers.d/51-agentworks-console-setenv (`Defaults:<admin> setenv`).

AGENTWORKS_IDENTITY_PROFILE_PATH = "/etc/profile.d/agentworks-identity.sh"
AGENTWORKS_ZPROFILE_PATH = "/etc/zsh/zprofile"
RETIRED_SSHD_ACCEPT_ENV_PATH = "/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf"
RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH = "/etc/ssh/agentworks-accept-env.reload-pending"
"""Where the retired fragment is moved until sshd has reloaded without it, outside sshd's ``Include`` directory."""
RETIRED_SUDOERS_FRAGMENTS = (
    "/etc/sudoers.d/50-agentworks-env-keep",
    "/etc/sudoers.d/51-agentworks-console-setenv",
    "/etc/sudoers.d/50-agentworks-env-keep.tmp",
    "/etc/sudoers.d/51-agentworks-console-setenv.tmp",
)
"""The sudoers fragments earlier releases installed, and the staging names their installer used.

Initialization removes them all. sudo skips a name with a ``.`` in it, so a
staging file an interrupted install left was never read, but it is ours to
clear.
"""

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
    user-home copies (see issue #121). ``ensure_source_line`` leaves a
    seeded user's rc file alone, because the seed already contains the
    ``.agentworks-rc.sh`` name it looks for.
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


def _remove_sshd_accept_env(files: Files, log: InitLog) -> None:
    """Remove the retired ``AcceptEnv *`` fragment from sshd_config.d/ and reload sshd when it was there.

    Earlier releases installed it so ``ssh -o SetEnv=...`` could carry
    environment into a guest shell. Every request now carries its
    environment in the request itself (ADR 0026), so sshd has no environment
    to accept.
    Agentworks owns the path outright: on create there is nothing to remove,
    and on reinit the fragment goes and sshd reloads (``systemctl reload``
    validates the merged config first), so the running sshd stops accepting
    client environment. A steady-state reinit leaves sshd alone.
    The fragment is moved to ``RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH``,
    which sshd does not read, and that file goes only once sshd has
    reloaded, so a reload that fails or never runs is retried by the next
    initialization; a failed one is warned about with what to run.
    """
    log.step("sshd AcceptEnv removal")
    path, pending = RETIRED_SSHD_ACCEPT_ENV_PATH, RETIRED_SSHD_ACCEPT_ENV_RELOAD_PENDING_PATH

    def run(argv: tuple[str, ...]) -> None:
        files.access.run(Command(argv, elevated=True, deadline=files.deadline), check=True)

    try:
        if not isinstance(files.stat(path, elevated=True), Absent):
            output.info(f"Removing {path}...")
            run(("mv", "-f", "-T", "--", path, pending))
        elif isinstance(files.stat(pending, elevated=True), Absent):
            return
    except STEP_FAILURES as error:
        warn_step(log, "removing the retired sshd AcceptEnv fragment failed; re-run 'agw vm reinit'", error)
        return
    try:
        run(("systemctl", "reload", "ssh"))
    except STEP_FAILURES as error:
        warn_step(
            log,
            f"removed {path}, but reloading sshd failed, so it keeps the old setting until it reloads; "
            "the next 'agw vm reinit' retries, or run 'sudo systemctl reload ssh' on the VM",
            error,
        )
        return
    try:
        run(("rm", "-f", "--", pending))
    except STEP_FAILURES as error:
        warn_step(log, f"sshd reloaded, but removing {pending} failed, so the next reinit reloads sshd again", error)


def _remove_retired_sudoers_fragments(files: Files, log: InitLog) -> None:
    """Remove ``RETIRED_SUDOERS_FRAGMENTS`` from sudoers.d/ in one elevated ``rm -f``.

    Earlier releases installed them to carry environment across sudo: the
    ``env_keep`` allowlist for ``AGENTWORKS_*`` and ``AW_*``, and the admin's
    ``setenv`` grant for a console agent pane's ``--preserve-env``. Every
    request now carries its environment itself, and a pane switches user
    with a plain ``sudo -n -u`` (ADR 0026), so neither has a user.
    Agentworks owns these paths outright, so the removal is right on create
    (nothing there) and on reinit (any release's fragments); sudo reads
    sudoers.d/ on every invocation, so it takes effect at once. Like the sshd
    removal it prints nothing: on most runs there is nothing to remove.
    """
    log.step("Retired sudoers fragments")
    try:
        files.access.run(
            Command(("rm", "-f", "--", *RETIRED_SUDOERS_FRAGMENTS), elevated=True, deadline=files.deadline), check=True
        )
    except STEP_FAILURES as error:
        warn_step(log, "removing the retired sudoers fragments failed; re-run 'agw vm reinit'", error)


def _write_agentworks_rc(files: Files, shell_snippets: list[str], log: InitLog, *, home: str, shell: str) -> None:
    """Write the account's rc fragment for interactive shells and make its rc files source it.

    Writes ``~/.agentworks-rc.sh``, 0644, with shell hooks (e.g. mise
    activate), sourced from the files :func:`source_files` names, so it runs
    per interactive shell. Always written (even if empty) so that reinit can
    clear previously set hooks.
    """
    log.step("Shell rc")
    output.info("Writing agentworks rc...")
    content = "\n".join(["# Managed by agentworks -- do not edit", *shell_snippets]) + "\n"
    try:
        files.write(f"{home}/{AGENTWORKS_RC}", content.encode(), mode=0o644, parent_mode=0o755)
    except STEP_FAILURES as error:
        warn_step(log, "shell rc write failed", error)
        return
    _ensure_fragment_sourced(files, log, home=home, shell=shell, fragment=AGENTWORKS_RC)

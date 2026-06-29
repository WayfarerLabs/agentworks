"""Tests for the shell rc seed constants and the init-time writer.

Pins:

- The bash and zsh seeds end with a source-line that references
  ``.agentworks-rc.sh`` literally, so ``_write_agentworks_rc``'s
  idempotent grep-or-append no-ops cleanly on a seeded user (no
  duplicate source-line on next reinit).
- The seeds contain the identity-aware prompt expression with
  fallbacks (``${AGENTWORKS_AGENT:-admin}`` / ``${AGENTWORKS_VM:-...}``)
  so a user who opens a shell before init populates the identity
  profile still sees a usable prompt.
- ``_write_skel_seeds`` writes both seeds to ``/etc/skel/.bashrc``
  and ``/etc/skel/.zshrc`` via ``sudo tee`` and chmods 644. The
  ``/etc/skel`` location is what makes ``useradd -m`` (i.e. agent
  creation) inherit the seed without an explicit copy step.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentworks.vms.initializer import (
    AGENTWORKS_RC,
    SKEL_BASHRC_PATH,
    SKEL_ZSHRC_PATH,
    _write_skel_seeds,
)
from agentworks.vms.skel import BASHRC, SKEL_HEADER, ZSHRC

# ---------------------------------------------------------------------------
# Seed content shape
# ---------------------------------------------------------------------------


def test_seeds_open_with_managed_by_agentworks_marker() -> None:
    """Both seeds start with the agentworks-owned marker comment so an
    operator inspecting their ``.bashrc`` / ``.zshrc`` knows the file is
    agentworks-shipped. The ``/etc/skel`` copies are rewritten on every
    reinit; the user-home copies are not refreshed after the initial
    seed lands at provision / useradd time."""
    assert BASHRC.startswith(SKEL_HEADER + "\n")
    assert ZSHRC.startswith(SKEL_HEADER + "\n")


def test_seeds_reference_agentworks_rc_filename() -> None:
    """The seed's source-line must reference the same filename
    (``.agentworks-rc.sh``) that ``_write_agentworks_rc`` greps for
    when deciding whether to append. Filename match means the existing
    grep-or-append correctly no-ops once the seed is present, so reinit
    doesn't duplicate the source-line."""
    # The grep in _write_agentworks_rc is `grep -q .agentworks-rc.sh <rc>`.
    # The seeds need that substring present somewhere for the grep to
    # match. (Substring match, not full-line match.)
    assert AGENTWORKS_RC in BASHRC
    assert AGENTWORKS_RC in ZSHRC


def test_seeds_use_guarded_source_line() -> None:
    """The source-line is ``[ -f ~/.agentworks-rc.sh ] && . ~/.agentworks-rc.sh``
    so a shell opening before init populates the rc file doesn't error
    -- the guard short-circuits cleanly."""
    guarded = "[ -f ~/.agentworks-rc.sh ] && . ~/.agentworks-rc.sh"
    assert guarded in BASHRC
    assert guarded in ZSHRC


def test_seeds_banner_has_agw_prefix_and_mode_label() -> None:
    """The status banner reads ``[AGW ADMIN ...]`` or
    ``[AGW AGENT ...]`` so it's unambiguously NOT a stock Unix
    ``user@host`` pair. Both branches must be present in each
    seed: the bash PROMPT_COMMAND / zsh precmd picks the right
    one at runtime based on whether ``AGENTWORKS_AGENT`` is set."""
    assert "[AGW ADMIN" in BASHRC
    assert "[AGW AGENT" in BASHRC
    assert "[AGW ADMIN" in ZSHRC
    assert "[AGW AGENT" in ZSHRC


def test_seeds_banner_uses_labeled_status_fields() -> None:
    """Status fields in the banner are labeled (``ag:``, ``vm:``,
    ``ws:``, ``se:``) so there's no ambiguity about what each
    value means at a glance."""
    for label in (" ag:", " vm:", " ws:", " se:"):
        assert label in BASHRC, f"missing {label!r} field in bash banner"
        assert label in ZSHRC, f"missing {label!r} field in zsh banner"


def test_seeds_mode_coded_colors() -> None:
    """ADMIN and AGENT modes use visually distinct colors so an
    operator can tell at a glance which identity is driving the
    shell. ADMIN uses cyan (``\\e[36m`` / ``%F{cyan}``); AGENT
    uses bold yellow (``\\e[1;33m`` / ``%F{yellow}%B``)."""
    # bash: ANSI escape sequences in PS1.
    assert "\\e[36m" in BASHRC      # cyan for ADMIN
    assert "\\e[1;33m" in BASHRC    # bold yellow for AGENT
    # zsh: prompt-escape color names.
    assert "%F{cyan}" in ZSHRC
    assert "%F{yellow}%B" in ZSHRC


def test_seeds_vm_hostname_fallback() -> None:
    """Both seeds fall back to the shell's hostname variable
    when ``AGENTWORKS_VM`` isn't set. bash uses ``$HOSTNAME``;
    zsh uses ``$HOST``. Parameter expansion inside the prompt
    builder isn't recursive, so we can't lean on bash's ``\\h``
    or zsh's ``%m`` escapes after a substitution -- a plain
    variable reference works in both directions."""
    assert "${AGENTWORKS_VM:-$HOSTNAME}" in BASHRC
    assert "${AGENTWORKS_VM:-$HOST}" in ZSHRC


def test_seeds_status_fields_render_conditionally() -> None:
    """ag/ws/se fields only render when their env var is set --
    a bare admin shell shows ``[AGW ADMIN vm:my-vm]``, not the
    full four-field shape with empty placeholders."""
    # Each conditional has the [ -n "${VAR-}" ] && pattern in
    # both seeds.
    for var in ("AGENTWORKS_AGENT", "AGENTWORKS_WORKSPACE", "AGENTWORKS_SESSION"):
        assert f'[ -n "${{{var}-}}" ]' in BASHRC, f"missing conditional for {var} in BASHRC"
        assert f'[ -n "${{{var}-}}" ]' in ZSHRC, f"missing conditional for {var} in ZSHRC"


def test_seeds_have_git_branch_support() -> None:
    """Both shells emit a ``(branch)`` indicator when the cwd is
    inside a git repo. bash sources git's contrib
    ``git-sh-prompt`` (silently no-ops if git isn't installed);
    zsh uses its built-in ``vcs_info``."""
    assert "__git_ps1" in BASHRC
    assert "/usr/lib/git-core/git-sh-prompt" in BASHRC
    assert "autoload -Uz vcs_info" in ZSHRC
    assert "vcs_info_msg_0_" in ZSHRC


def test_seeds_two_line_prompt_with_banner_above() -> None:
    """The layout is two-line: agentworks status banner on top,
    standard prompt + git on the bottom. The seed needs an
    embedded newline between them. bash uses ``$'\\n'``; zsh
    uses a literal ``\\n`` inside ``$'...'``."""
    # bash PS1 assembled with $'\n' between banner and command.
    assert "$'\\n'" in BASHRC
    # zsh PS1 declared as $'...\n...' (literal newline inside ANSI-C).
    assert "\\n%F{green}" in ZSHRC


def test_seeds_use_prompt_command_or_precmd_hooks() -> None:
    """The dynamic parts of the prompt (mode-coded banner with
    substituted fields) need to be recomputed before each prompt
    render. bash uses ``PROMPT_COMMAND``; zsh uses
    ``precmd_functions``."""
    assert "PROMPT_COMMAND='__agw_prompt_command'" in BASHRC
    assert "precmd_functions+=(__agw_precmd vcs_info)" in ZSHRC


def test_bash_seed_keeps_debian_baselines() -> None:
    """Light-touch baseline: the bash seed preserves Debian's
    interactive-shell early-return guard, history options, color
    aliases, and bash-completion sourcing -- so the seed is a
    superset of what a Debian user would get from the stock skel,
    not a replacement that strips functionality."""
    assert "*i*) ;;" in BASHRC  # interactive-only return
    assert "shopt -s histappend" in BASHRC
    assert "alias ls='ls --color=auto'" in BASHRC
    assert "bash_completion" in BASHRC


def test_zsh_seed_includes_compinit() -> None:
    """Debian ships no /etc/skel/.zshrc, so the zsh seed has to
    provide the basics itself: history file, completion init,
    identity-aware prompt."""
    assert "autoload -Uz compinit && compinit" in ZSHRC
    assert "HISTFILE" in ZSHRC
    assert "SAVEHIST" in ZSHRC


# ---------------------------------------------------------------------------
# _write_skel_seeds wiring
# ---------------------------------------------------------------------------


def test_write_skel_seeds_writes_both_files_to_etc_skel() -> None:
    """Both /etc/skel/.bashrc and /etc/skel/.zshrc get written via
    ``sudo tee`` (so the file ends up root-owned, which is correct for
    a system template directory)."""
    target = MagicMock()
    target.run.return_value = MagicMock(ok=True)
    logger = MagicMock()

    _write_skel_seeds(target, logger)

    commands = [call.args[0] for call in target.run.call_args_list]
    # Each seed -> one printf | sudo tee + one sudo chmod, so 4 calls total.
    assert len(commands) == 4
    bashrc_tee = [c for c in commands if SKEL_BASHRC_PATH in c and "tee" in c]
    zshrc_tee = [c for c in commands if SKEL_ZSHRC_PATH in c and "tee" in c]
    assert len(bashrc_tee) == 1
    assert len(zshrc_tee) == 1
    # chmod 644 on each seed file.
    assert f"sudo chmod 644 {SKEL_BASHRC_PATH}" in commands
    assert f"sudo chmod 644 {SKEL_ZSHRC_PATH}" in commands


def test_write_skel_seeds_skel_paths_are_etc_skel() -> None:
    """``/etc/skel`` is the standard Debian location ``useradd -m``
    copies from when creating a new user's home. Anything else
    wouldn't propagate to future agents automatically."""
    assert SKEL_BASHRC_PATH == "/etc/skel/.bashrc"
    assert SKEL_ZSHRC_PATH == "/etc/skel/.zshrc"


def test_write_skel_seeds_runs_after_apt_install_in_initializer() -> None:
    """Regression guard: ``/etc/skel/.bashrc`` is a Debian conffile
    shipped by ``bash``. Running ``_write_skel_seeds`` BEFORE
    ``_install_apt_packages`` would let apt's ``--force-confnew``
    silently replace the seed on any reinit that upgrades bash
    (the seed lands at ``/etc/skel/.bashrc.dpkg-old`` and future
    ``useradd -m`` inherits Debian's stock skel instead). Same
    conffile-clobber pattern as ``_write_agentworks_identity_profile``,
    which carries an explanatory comment for exactly this reason.

    This test scans the init source for the relative positions of the
    two calls so a refactor that reorders them has to update a test.
    """
    import inspect
    import re

    from agentworks.vms import initializer

    # Inspect ``_phase_b_setup`` -- the function that actually houses
    # both calls. ``run_initialization`` is the public entry; the
    # ordering constraint is on the helper.
    src = inspect.getsource(initializer._phase_b_setup)
    # Match only actual call sites (line starts with whitespace + the
    # function name + ``(``) so a stray reference inside a comment
    # can't trip the assertion.
    skel = re.search(r"^\s+_write_skel_seeds\(", src, re.MULTILINE)
    apt = re.search(r"^\s+_install_apt_packages\(", src, re.MULTILINE)
    assert skel is not None, "expected _write_skel_seeds call in _phase_b_setup"
    assert apt is not None, "expected _install_apt_packages call in _phase_b_setup"
    assert skel.start() > apt.start(), (
        "_write_skel_seeds must run AFTER _install_apt_packages so apt's "
        "--force-confnew doesn't replace /etc/skel/.bashrc (a bash-package "
        "conffile) with Debian's stock skel."
    )

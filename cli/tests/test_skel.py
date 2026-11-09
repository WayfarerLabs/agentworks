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
- The seeds' paths are ``/etc/skel/.bashrc`` and ``/etc/skel/.zshrc``:
  the ``/etc/skel`` location is what makes ``useradd -m`` (i.e. agent
  creation) inherit the seed without an explicit copy step. What
  ``_write_skel_seeds`` leaves on disk is in
  ``test_initializer_system_fragments.py``, and its order after apt in
  ``tests/vms/test_phase_b_driver.py``.
"""

from __future__ import annotations

from agentworks.capabilities.vm_platform.skel import BASHRC, SKEL_HEADER, ZSHRC
from agentworks.vms.initializer import (
    AGENTWORKS_RC,
    SKEL_BASHRC_PATH,
    SKEL_ZSHRC_PATH,
)

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
    assert "[agw admin" in BASHRC
    assert "[agw agent" in BASHRC
    assert "[agw admin" in ZSHRC
    assert "[agw agent" in ZSHRC


def test_seeds_banner_uses_labeled_status_fields() -> None:
    """Status fields in the banner are labeled (``vm:``, ``ws:``,
    ``ag:``, ``se:``) so there's no ambiguity about what each
    value means at a glance."""
    for label in (" vm:", " ws:", " ag:", " se:"):
        assert label in BASHRC, f"missing {label!r} field in bash banner"
        assert label in ZSHRC, f"missing {label!r} field in zsh banner"


def test_seeds_banner_field_order_is_vm_ws_ag_se() -> None:
    """Order in the banner is vm (background context) then the
    standard agentworks triad workspace / agent / session. Pinning
    via the order of the field-construction statements in the
    prompt builder."""
    # Each builder sets vm first (always present) then conditionally
    # appends ws / ag / se. Look at the assign / append statements
    # (not docstring text) so the test isn't fooled by example
    # snippets in the comment block above each function.
    for seed_name, seed in (("BASHRC", BASHRC), ("ZSHRC", ZSHRC)):
        vm_idx = seed.find('fields=" vm:')
        ws_idx = seed.find('fields+=" ws:')
        ag_idx = seed.find('fields+=" ag:')
        se_idx = seed.find('fields+=" se:')
        assert vm_idx >= 0 and ws_idx >= 0 and ag_idx >= 0 and se_idx >= 0, (
            f"{seed_name}: expected vm assign + ws/ag/se appends, got "
            f"vm={vm_idx}, ws={ws_idx}, ag={ag_idx}, se={se_idx}"
        )
        assert vm_idx < ws_idx < ag_idx < se_idx, (
            f"{seed_name}: builder order should be vm < ws < ag < se "
            f"(got vm={vm_idx}, ws={ws_idx}, ag={ag_idx}, se={se_idx})"
        )


def test_seeds_mode_coded_colors() -> None:
    """ADMIN and AGENT modes use visually distinct colors so an
    operator can tell at a glance which identity is driving the
    shell. ADMIN uses cyan (``\\e[36m`` / ``%F{cyan}``); AGENT
    uses bold yellow (``\\e[1;33m`` / ``%F{yellow}%B``)."""
    # bash: ANSI escape sequences in PS1.
    assert "\\e[36m" in BASHRC  # cyan for ADMIN
    assert "\\e[1;33m" in BASHRC  # bold yellow for AGENT
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


def test_write_skel_seeds_skel_paths_are_etc_skel() -> None:
    """``/etc/skel`` is the standard Debian location ``useradd -m``
    copies from when creating a new user's home. Anything else
    wouldn't propagate to future agents automatically."""
    assert SKEL_BASHRC_PATH == "/etc/skel/.bashrc"
    assert SKEL_ZSHRC_PATH == "/etc/skel/.zshrc"

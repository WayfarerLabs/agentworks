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
    operator inspecting their .bashrc / .zshrc knows the file is owned
    by agentworks and will be clobbered on reinit."""
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


def test_seeds_identity_aware_prompt_with_fallbacks() -> None:
    """The prompts reference ``AGENTWORKS_AGENT`` and ``AGENTWORKS_VM``
    with shell fallbacks so a user who opens a shell before the
    identity profile is populated still gets a meaningful prompt
    (admin / hostname instead of ``[@] $``)."""
    assert "${AGENTWORKS_AGENT:-admin}" in BASHRC
    assert "${AGENTWORKS_AGENT:-admin}" in ZSHRC
    # bash uses \h for hostname; zsh uses %m.
    assert "${AGENTWORKS_VM:-\\h}" in BASHRC
    assert "${AGENTWORKS_VM:-%m}" in ZSHRC


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

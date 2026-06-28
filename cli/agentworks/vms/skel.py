"""Default shell rc seed files for agentworks-managed users.

Two consumers share the constants in this module:

- ``vms/bootstrap_script.py`` writes ``~admin/.bashrc`` and
  ``~admin/.zshrc`` once at provision time, before init runs. Once
  written, agentworks never refreshes them: operator edits in the
  admin's home survive every subsequent reinit.
- ``vms/initializer.py`` writes ``/etc/skel/.bashrc`` and
  ``/etc/skel/.zshrc`` on every init / reinit so future ``useradd -m``
  (i.e. new agents) inherits the seed automatically. The ``/etc/skel``
  copies ARE refreshed on every reinit; the user-home copies are not.

The seed is intentionally light: Debian's stock ``.bashrc`` augmented
with an identity-aware prompt and a guarded source-line for
``~/.agentworks-rc.sh``; a fresh ``.zshrc`` (Debian ships none) with
the minimum a zsh user needs to be productive -- completions,
history, the same prompt convention, and the same source-line.

Both files are marked ``# Managed by agentworks`` at the top so an
operator inspecting them knows where they came from. Operators with
their own dotfiles (e.g. oh-my-zsh, starship) replace the user-home
copy directly; agentworks won't rewrite the seed content there. The
only continuing hook is the ``. ~/.agentworks-rc.sh`` source-line,
maintained idempotently on every reinit by
``_write_agentworks_rc`` / ``_ensure_agentworks_files_sourced`` in
``initializer.py`` -- a one-line append that no-ops cleanly when the
seed (or a previous reinit) has already added the substring (see
issue #121).
"""

from __future__ import annotations

# Marker line at the top of each seed so an operator inspecting their
# .bashrc / .zshrc knows the file is agentworks-shipped. The marker
# is informational only -- the in-/etc/skel copies are rewritten on
# every reinit, the user-home copies are not (agentworks never
# refreshes shell rc files in a user's home after the initial seed).
SKEL_HEADER = "# Managed by agentworks."

BASHRC = """\
# Managed by agentworks.
# Sourced by interactive non-login bash shells. Login shells source
# ~/.bash_profile (or ~/.profile), which itself sources this file.

# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac

# History
HISTCONTROL=ignoredups:ignorespace
HISTSIZE=5000
HISTFILESIZE=10000
shopt -s histappend
shopt -s checkwinsize

# Identity-aware prompt: [<agent-or-admin>@<vm>] <cwd> $
PS1='[${AGENTWORKS_AGENT:-admin}@${AGENTWORKS_VM:-\\h}] \\w \\$ '

# Color support for ls / grep
if [ -x /usr/bin/dircolors ]; then
    test -r ~/.dircolors && eval "$(dircolors -b ~/.dircolors)" || eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
    alias fgrep='fgrep --color=auto'
    alias egrep='egrep --color=auto'
fi

# Per-user bash aliases
[ -f ~/.bash_aliases ] && . ~/.bash_aliases

# Bash completion
if ! shopt -oq posix; then
    if [ -f /usr/share/bash-completion/bash_completion ]; then
        . /usr/share/bash-completion/bash_completion
    elif [ -f /etc/bash_completion ]; then
        . /etc/bash_completion
    fi
fi

# Agentworks shell hooks (mise activate, etc.) -- written by reinit
[ -f ~/.agentworks-rc.sh ] && . ~/.agentworks-rc.sh
"""

ZSHRC = """\
# Managed by agentworks.
# Sourced by interactive zsh shells.

# History
HISTFILE=~/.zsh_history
HISTSIZE=5000
SAVEHIST=10000
setopt SHARE_HISTORY
setopt HIST_IGNORE_DUPS
setopt HIST_IGNORE_SPACE
setopt APPEND_HISTORY

# Completion
autoload -Uz compinit && compinit

# Identity-aware prompt: [<agent-or-admin>@<vm>] <cwd> %
PS1='[${AGENTWORKS_AGENT:-admin}@${AGENTWORKS_VM:-%m}] %~ %# '

# Color support for ls / grep
if [ -x /usr/bin/dircolors ]; then
    test -r ~/.dircolors && eval "$(dircolors -b ~/.dircolors)" || eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
fi

# Agentworks shell hooks (mise activate, etc.) -- written by reinit
[ -f ~/.agentworks-rc.sh ] && . ~/.agentworks-rc.sh
"""

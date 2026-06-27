"""Default shell rc seed files for agentworks-managed users.

Two consumers share the constants in this module:

- ``vms/bootstrap_script.py`` writes ``~admin/.bashrc`` and
  ``~admin/.zshrc`` once at provision time, before init runs.
- ``vms/initializer.py`` writes ``/etc/skel/.bashrc`` and
  ``/etc/skel/.zshrc`` on every init / reinit so future ``useradd -m``
  calls (i.e. new agents) inherit the seed automatically.

The seed is intentionally light: Debian's stock ``.bashrc`` augmented
with an identity-aware prompt and a guarded source-line for
``~/.agentworks-rc.sh``; a fresh ``.zshrc`` (Debian ships none) with
the minimum a zsh user needs to be productive -- completions,
history, the same prompt convention, and the same source-line.

Both files are marked ``# Managed by agentworks -- edits will be
clobbered on reinit``. Operators with their own dotfiles (e.g.
oh-my-zsh, starship) replace these files; agentworks never writes
shell rc files into a user's home after the initial seed, so the
operator's installer wins automatically (see issue #121).
"""

from __future__ import annotations

# Marker line at the top of each seed. The reinit-writer matches on
# this so an operator who replaces a seed file with their own dotfiles
# isn't clobbered on the next reinit.
SKEL_HEADER = "# Managed by agentworks -- edits will be clobbered on reinit."

BASHRC = """\
# Managed by agentworks -- edits will be clobbered on reinit.
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
# Managed by agentworks -- edits will be clobbered on reinit.
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

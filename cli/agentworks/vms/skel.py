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

# Git prompt support (silently no-op if git isn't installed).
for __agw_gp in /usr/lib/git-core/git-sh-prompt /etc/bash_completion.d/git-prompt; do
    [ -r "$__agw_gp" ] && . "$__agw_gp" && break
done
unset __agw_gp
GIT_PS1_SHOWDIRTYSTATE=1
GIT_PS1_SHOWUNTRACKEDFILES=1

# Build PS1 before each prompt render. Two-line layout:
#   [AGW ADMIN vm:<vm> ws:<ws> se:<session>]    <- agentworks banner
#   <user>@<host>:<cwd> (branch) $              <- Debian-stock prompt + git
# The mode-coded banner (cyan for ADMIN, bold yellow for AGENT) is
# the visual signal an operator uses to tell at a glance which
# identity is driving the shell. The ``AGW`` prefix makes clear
# that the bracket is NOT a stock Unix ``user@host`` pair. The
# status fields (ag/vm/ws/se) are labeled so there is no ambiguity
# about what each value means; ag/ws/se render only when their env
# var is set. The command line below is Debian's stock colored PS1
# (green ``user@host``, blue ``path``) with a yellow git branch
# segment appended -- the only non-Debian addition.
__agw_prompt_command() {
    local fields=''
    [ -n "${AGENTWORKS_AGENT-}" ] && fields+=" ag:$AGENTWORKS_AGENT"
    fields+=" vm:${AGENTWORKS_VM:-$HOSTNAME}"
    [ -n "${AGENTWORKS_WORKSPACE-}" ] && fields+=" ws:$AGENTWORKS_WORKSPACE"
    [ -n "${AGENTWORKS_SESSION-}" ] && fields+=" se:$AGENTWORKS_SESSION"

    local banner
    if [ -n "${AGENTWORKS_AGENT-}" ]; then
        # Agent mode: bold yellow banner.
        banner='\\[\\e[1;33m\\][AGW AGENT'"$fields"']\\[\\e[0m\\]'
    else
        # Admin mode: cyan banner.
        banner='\\[\\e[36m\\][AGW ADMIN'"$fields"']\\[\\e[0m\\]'
    fi

    # Debian-stock colored prompt + git branch (the only addition).
    local p_userhost='\\[\\e[01;32m\\]\\u@\\h\\[\\e[00m\\]'
    local p_path='\\[\\e[01;34m\\]\\w\\[\\e[00m\\]'
    local p_git='\\[\\e[33m\\]$(declare -F __git_ps1 >/dev/null && __git_ps1 " (%s)")\\[\\e[00m\\]'

    PS1="$banner"$'\\n'"$p_userhost:$p_path$p_git"' \\$ '
}
PROMPT_COMMAND='__agw_prompt_command'

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

# Color support for ls / grep
if [ -x /usr/bin/dircolors ]; then
    test -r ~/.dircolors && eval "$(dircolors -b ~/.dircolors)" || eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
fi

# Git branch in the prompt via zsh's built-in vcs_info.
autoload -Uz vcs_info
zstyle ':vcs_info:*' enable git
zstyle ':vcs_info:git:*' formats ' (%b)'
zstyle ':vcs_info:git:*' actionformats ' (%b|%a)'

# PROMPT_SUBST: expand ${...} in PS1 at render time. Prompt escapes
# (%F, %B, etc.) are always processed.
setopt PROMPT_SUBST

# precmd hook: build the banner line with concrete substitutions
# for vm/workspace/session/agent. zsh parameter expansion isn't
# recursive in prompts, so we pre-substitute here.
#
# Two-line layout:
#   [AGW ADMIN vm:<vm> ws:<ws> se:<session>]    <- agentworks banner
#   <user>@<host>:<cwd> (branch) %              <- standard prompt + git
#
# The mode-coded banner (cyan for ADMIN, bold yellow for AGENT) is
# the visual signal an operator uses to tell at a glance which
# identity is driving the shell. The ``AGW`` prefix makes clear
# that the bracket is NOT a stock Unix ``user@host`` pair. ag/ws/se
# fields render only when their env var is set. The command line
# below is a standard zsh prompt (green user@host, blue path) with
# a yellow git branch segment -- the only non-standard addition.
__agw_precmd() {
    local fields=''
    [ -n "${AGENTWORKS_AGENT-}" ] && fields+=" ag:${AGENTWORKS_AGENT}"
    fields+=" vm:${AGENTWORKS_VM:-$HOST}"
    [ -n "${AGENTWORKS_WORKSPACE-}" ] && fields+=" ws:${AGENTWORKS_WORKSPACE}"
    [ -n "${AGENTWORKS_SESSION-}" ] && fields+=" se:${AGENTWORKS_SESSION}"

    if [ -n "${AGENTWORKS_AGENT-}" ]; then
        # Agent mode: bold yellow banner.
        __agw_banner="%F{yellow}%B[AGW AGENT${fields}]%b%f"
    else
        # Admin mode: cyan banner.
        __agw_banner="%F{cyan}[AGW ADMIN${fields}]%f"
    fi
}
precmd_functions+=(__agw_precmd vcs_info)

# Banner line on top, standard prompt + git below.
PS1=$'${__agw_banner}\\n%F{green}%n@%m%f:%F{blue}%~%f%F{yellow}${vcs_info_msg_0_}%f %# '

# Agentworks shell hooks (mise activate, etc.) -- written by reinit
[ -f ~/.agentworks-rc.sh ] && . ~/.agentworks-rc.sh
"""

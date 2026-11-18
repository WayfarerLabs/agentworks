# 26. Environment Travels Inside the Request

Date: 2026-11-18

## Status

Accepted

Supersedes [ADR 0014](0014-sshd-accept-env-wildcard.md) and
[ADR 0017](0017-console-pane-preserve-env.md).

## Context

Agentworks composes an environment (identity variables, operator variables and resolved secrets) for
every shell and command it starts on a VM. Earlier releases carried that environment through the
layers between the workstation and the shell:

- the SSH client sent it with `-o SetEnv`, and each VM's sshd accepted any name (`AcceptEnv *` in
  `50-agentworks-accept-env.conf`, ADR 0014);
- sudoers kept `AGENTWORKS_*` and `AW_*` across a user switch (`env_keep` in
  `50-agentworks-env-keep`, ADR 0014);
- a console's agent pane, started by the admin's tmux server, got its values as `tmux -e` arguments
  and named them on `sudo --preserve-env`, which a `Defaults:<admin> setenv` grant
  (`51-agentworks-console-setenv`) permitted, behind a probe that fell back to a degraded pane on a
  VM without the grant (ADR 0017).

Each layer was VM-side configuration to deploy and keep current, and each had its own failure: a
value with a newline could not travel through `SetEnv`, pane values sat on a world-readable `tmux`
command line, and a VM missing a fragment silently lost variables until it was reinitialized.

The execution channel
([`cli/agentworks/execution/README.md`](../../cli/agentworks/execution/README.md)) removes the need
for all of them. Every request writes, on the SSH client's stdin, the launcher and an envelope
holding the command, its working directory and its environment; on the guest the launcher rebuilds
the payload's environment from a fixed base plus the envelope's, so nothing sshd or sudo passes down
reaches the payload, except the terminal variables tmux sets for a pane
([What a request becomes](../../cli/agentworks/execution/README.md#what-a-request-becomes)). A tmux
pane cannot take a request on its stdin, so the pane's account parks the request first and the
pane's one-time command collects it
([Terminal handover](../../cli/agentworks/execution/README.md#terminal-handover)).
[Runtime context](../arch/runtime-context.md) describes the operator-facing result.

## Decision

Environment travels only inside the request: the envelope for every request, and the parked request
for a tmux pane. Agentworks never sends environment through sshd (`SetEnv`, `AcceptEnv`) and never
carries it across sudo (`env_keep`, `--preserve-env`, `setenv`).

A pane that another account's tmux server starts, such as an agent's pane in the admin's console,
switches user with a plain `sudo -n -u <account>` before it collects its request, which needs only
the server owner's existing passwordless sudo. Of the terminal variables tmux sets, sudo's default
policy passes `TERM`; nothing else crosses.

The three fragments have no user, so initialization stops installing them and removes them on
`vm create` and `vm reinit`: the sshd fragment followed by a reload of sshd when it was present, and
both sudoers fragments in one `rm -f`. Agentworks owns those paths outright, so the removal is right
on a new VM (nothing there) and on one any earlier release initialized.

## Positives

- **No VM-side configuration for environment.** A variable of any name reaches its shell with no
  sshd or sudoers change and no reinit, and there is no fragment whose absence degrades a shell.
- **No value on a guest command line or in an SSH option.** The envelope travels on stdin, and a
  pane's request through a socket only its account can reach.
- **The transport constrains no value.** The envelope carries any string without a NUL exactly,
  newlines included.
- **A narrower guest posture.** sshd accepts only the variables its own configuration names, sudo
  keeps only what its default policy keeps, and the admin holds no `setenv` grant.

## Negatives

- **Operator-visible changes after reinit.** An operator's own `ssh -o SetEnv` or `SendEnv` of a
  name outside sshd's own `AcceptEnv` list no longer reaches the VM, and an operator's own `sudo` no
  longer keeps `AGENTWORKS_*` or `AW_*`. Both were side effects of fragments Agentworks installed
  for itself; the upgrade guide says what to do instead.
- **One reinit per existing VM.** Until it runs, the old fragments stay in place, unused.
- **An agent's pane has `TERM` and none of tmux's other terminal variables.** sudo's environment
  reset drops `TMUX` and the rest, as the earlier `sudo --login` pane did.

## Alternatives considered

- **Leave the fragments in place, unused.** Rejected: each keeps a standing grant (any client
  environment accepted, variables kept across sudo, `setenv` for the admin) that nothing needs, and
  a reader of a VM's configuration would take them as load-bearing.
- **Keep `env_keep` alone** for operators who rely on their own `sudo` keeping `AGENTWORKS_*`.
  Rejected: Agentworks does not read it, an operator can pass the values a command needs as part of
  the command (`sudo env NAME="$NAME" command`), and a login shell still gets the VM's identity
  variables from `/etc/profile.d`.

## Consequences

- `vm create` and `vm reinit` remove `/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf`,
  `/etc/sudoers.d/50-agentworks-env-keep` and `/etc/sudoers.d/51-agentworks-console-setenv`, and a
  steady reinit leaves sshd alone.
- Console panes take their environment from the terminal handover; the `tmux -e` carry, the
  `--preserve-env` probe and its memo are gone.
- A request that Agentworks sends with an environment needs nothing on the VM beyond the execution
  channel's own contract.

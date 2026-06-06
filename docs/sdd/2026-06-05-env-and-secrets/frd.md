# Environment variables and secrets: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

Today there is no general mechanism for propagating environment variables or secrets into the
shells, sessions, consoles, and exec contexts that agentworks creates on VMs. The only env-related
facility is the `env` table on a session template, which is exported inline at session creation. The
only secret-related facility is `git_credentials`, which is narrowly scoped to writing
`~/.git-credentials` on the VM.

Existing workarounds are very poor:

- Hardcoding values into dotfiles delivered via `admin.config.dotfiles_source` (works, but mixes
  personal config with operational config and is opaque to agentworks).
- Hooking into their tooling

There are also no standard env vars that an agent or tool inside a session can read to discover
"which agentworks resources am I running in?". The VM, workspace, agent, and session names are not
exposed. Tools that want to integrate with agentworks have nothing to bind to.

This SDD establishes:

1. A standard set of `AGENTWORKS_*` env vars that identify the current resource context wherever
   agentworks opens a shell.
2. A unified env var schema where each entry is either a plaintext value or a reference to a named
   secret, definable at four scopes (admin, vm template, workspace template, agent template, and
   session template) with deterministic merge semantics.
3. A secret declaration mechanism that, like the existing `git_credentials` model, defines the
   _existence_ of a secret in config and resolves its _value_ from the CLI environment with an
   interactive prompt fallback. Values are never persisted by agentworks.

Future work (out of scope for this SDD but anticipated): folding `git_credentials` into the general
secret mechanism, and adding pluggable backends (keychain, 1Password, vault) beyond env-var +
prompt.

## Terminology

- **Resource context**: the chain of resources that scope a shell: the VM, optional workspace,
  optional agent, and optional session. Every shell agentworks opens has a context; some shells
  cover the full chain (a session shell), others cover only a subset (a provisioning shell has only
  a VM, a workspace shell has only a VM and workspace, ...).
- **Effective env**: the set of `KEY=value` pairs that an agentworks-opened shell receives after
  merging all scopes that apply to its context, in precedence order.
- **Env entry**: a single declaration; one key with either a `value =` (plaintext) or `secret =`
  (reference to a declared secret).
- **Secret**: a named credential whose _existence_ is declared in config but whose _value_ is never
  persisted by agentworks. Resolved at command time from a CLI env var; prompted interactively if
  the env var is unset.
- **Secret source**: the mechanism that produces the value of a secret at command time. For v1 the
  only source is env-var-then-prompt. The interface accommodates additional sources later.
- **AGENTWORKS\_\* vars**: the small fixed set of env vars that agentworks always sets to identify
  the resource context (e.g. `AGENTWORKS_VM`, `AGENTWORKS_SESSION`). Automatic; not
  user-configurable.

## Requirements

### R1: Standard AGENTWORKS\_\* identity variables

Whenever agentworks opens a shell on a VM (session creation, session attach, console window spawn,
exec, provisioning, agent setup, admin shell, etc.), it sets the subset of the following variables
that apply to the shell's resource context:

| Variable                   | Set when             | Value                                                   |
| -------------------------- | -------------------- | ------------------------------------------------------- |
| `AGENTWORKS_VM`            | Always (on-VM shell) | VM name                                                 |
| `AGENTWORKS_VM_HOST`       | Always (on-VM shell) | VM host name (per `vm_hosts` config)                    |
| `AGENTWORKS_PLATFORM`      | Always (on-VM shell) | One of `lima`, `azure`, `wsl2`, `proxmox`               |
| `AGENTWORKS_USER`          | Always (on-VM shell) | The on-VM Linux user (admin username or agent username) |
| `AGENTWORKS_WORKSPACE`     | Workspace context    | Workspace name                                          |
| `AGENTWORKS_WORKSPACE_DIR` | Workspace context    | Absolute path to the workspace dir on the VM            |
| `AGENTWORKS_AGENT`         | Agent context        | Agent name                                              |
| `AGENTWORKS_SESSION`       | Session context      | Session name                                            |
| `AGENTWORKS_SESSION_KIND`  | Session context      | `admin` or `agent`                                      |

The existing `AGENTWORKS_NERF_HOME` (set by VM initializer) is grandfathered into this scheme; it is
VM-scoped and continues to live in the system-wide profile fragment.

There is no opt-out. These vars are always set when their scope applies. Operators may override them
in user-defined env (R2), but that is discouraged and may be rejected in a future iteration.

### R2: User-defined env vars across the resource graph

User-defined env vars are declared in config at five scopes:

- `[admin.env]`: applies to every shell opened as the admin user.
- `[vm_templates.<name>.env]`: applies to every shell on VMs derived from this template.
- `[workspace_templates.<name>.env]`: applies whenever a shell has workspace context.
- `[agent_templates.<name>.env]`: applies to every shell opened as an agent derived from this
  template.
- `[session_templates.<name>.env]`: applies to shells that are sessions of this template (or derived
  contexts, e.g. session attach).

Each scope is a table of `KEY = "plaintext"` or `KEY = { secret = "name" }`:

```toml
[admin.env]
HTTP_PROXY = "http://proxy.example:3128"

[agent_templates.claude.env]
ANTHROPIC_API_KEY = { secret = "anthropic-api-key" }
LOG_LEVEL = "info"

[session_templates.shell.env]
EDITOR = "nvim"
```

#### Precedence

Each scope applies to a shell when its predicate matches:

- `admin` applies to shells opened as the admin Linux user. That covers admin-mode sessions,
  `vm shell`, `vm exec`, console admin shells (e.g. console layout's `--include-admin-shell`), and
  provisioning shells.
- `vm` applies to every on-VM shell.
- `workspace` applies when the shell has workspace context.
- `agent` applies to shells opened as an agent's Linux user.
- `session` applies to shells that belong to a session.

Exactly one of `admin` or `agent` applies to any given shell (a shell runs as one Linux user). The
precedence ladder, highest wins:

```text
session > (agent | admin) > workspace > vm
```

The same key appearing at multiple applicable scopes takes the highest-specificity value. Keys
appearing at only one scope contribute directly. There is no list / string append semantics (e.g.
`PATH = "...:${PATH}"`); shell expansion of references is not interpreted by agentworks. Operators
needing PATH-style appends can write a full expansion (`PATH = "/foo:/usr/bin:/bin"`) or use shell
profile mechanisms outside this system. (Future iteration may add an explicit append/prepend
syntax.)

#### Template inheritance

For templates that support `inherits`, env entries inherit along the inheritance chain in the
standard "child overrides parent by key" pattern that other template fields already use. The
inheritance-resolved env for a template is what feeds into the cross-scope merge described above.

#### Validation

- Keys must match `^[A-Za-z_][A-Za-z0-9_]*$` (POSIX env var name).
- Values are strings or `{ secret = "<name>" }` inline tables. No other shapes.
- `secret = "<name>"` must reference a declared secret (see R3). Unknown references are a config
  error.
- Keys starting with `AGENTWORKS_` may be set by users but emit a config warning (R1 sets these
  automatically and overrides risk confusing downstream consumers).

### R3: Secret declarations

Secrets are declared in a top-level `[secrets]` table, each entry containing at minimum a
description for prompt UX:

```toml
[secrets.anthropic-api-key]
description = "Anthropic API key for Claude agents"

[secrets.openai-api-key]
description = "OpenAI API key"
hint = "https://platform.openai.com/api-keys"
```

Fields:

- `description` (required): one-line description shown in prompts and `agw doctor`.
- `hint` (optional): additional guidance shown with the prompt (e.g. where to create the secret).

Secret names use the same character rules as resource names (lowercase alphanumeric, hyphens,
underscores; see `NAME_RE` in `config.py`). The namespace is flat and global.

Declaring a secret is the _only_ way to make a `{ secret = "..." }` reference resolvable. There is
no implicit lookup of arbitrary env vars by name; this avoids "ambient capability" surprises and
gives `agw doctor` a complete view of what the operator needs to provide.

### R4: Secret resolution and prompting

When a command needs a secret value, the resolution order is:

1. **CLI environment variable**: `AW_SECRET_<NAME>` (uppercase, hyphens to underscores). The
   operator is expected to source this from their personal vault (1Password, keychain, etc.) before
   invoking `agw`. Mirrors the existing `GIT_CREDENTIALS_<NAME>` pattern.
2. **Interactive prompt** (only when stdin is a TTY and `--non-interactive` is not set):
   `output.prompt_secret(...)` with the declared description and hint. Values entered at the prompt
   are used for the current invocation only; not persisted, not cached, not echoed.

If the env var is unset and the CLI is non-interactive, the command fails with a clear error naming
the unset secret(s) and the env var(s) that would satisfy them.

#### Eager, batched prompting at command start

Before performing any action, the command computes the union of secrets needed for the entire
command (across all target resources) and prompts for any missing values _immediately, up front_.
Once execution begins, the operator is not interrupted by a prompt halfway through a multi-VM batch
operation. This is the same UX principle that drove the git-credential design.

For commands that determine their target set partway through (e.g. discovering session targets via
filters), the command resolves the set first and then prompts. The contract is both:

- Prompt for all needed/unset secrets as soon as possible (within the first few seconds of the
  command)
- Prompt for all needed/unset secrets before making any changes

#### Scope of need

A command needs secrets only when it OPENS a new shell. Commands that join, list, describe, or
otherwise interact with existing shells consume no secrets and prompt for none.

Commands that open new shells (and therefore consume secrets):

- Provisioning: `vm create`, `vm reinit`, `agent create`, `agent reinit`.
- Session create / restart: `session create`, `session restart`, `session start`.
- Console window creation: `console create`, `console add-shell`, `console add-session`.
- Interactive ad-hoc shells: `vm shell`, `agent shell`.
- Non-interactive exec: `vm exec`, `agent exec`.

Commands that consume no secrets:

- Attach / inspection: `session attach`, `session list`, `session describe`, `console attach`,
  `vm list`, `agent list`, `workspace list`, etc.
- Lifecycle that does not open a new shell on the target: `session stop`, `session delete`,
  `agent delete`, `vm stop`, `vm delete`. (These run admin-side maintenance, not new agent shells.)

For commands that DO consume secrets, the manager layer enumerates the targets the command might
touch and walks their env chains. The candidate set is computed from **static filters** only --
those resolvable from config and the DB without observing remote state (positional targets, `--vm`,
`--workspace`, `--agent`, `--admin`, etc.). **Dynamic filters**: those that require probing the
system to determine inclusion (`--all-stopped`, status-based predicates) are applied AFTER prompting
completes. As a result, the prompted set may over-approximate what the command actually consumes
when static and dynamic filters combine. This is intentional: we prefer asking for one extra
credential up front to ambushing the operator with a prompt minutes into a batch operation. The HLA
elaborates the mechanism.

Examples:

- `agw vm create vm1`: opens provisioning shells (admin user) and the agent-setup shells if any
  agents are derived from the template. Needs the secrets referenced by the admin / vm-template env
  chain, plus any agent-template env for derived agents.
- `agw session create s1 -t claude`: opens a new session shell as the agent's Linux user. Needs the
  secrets referenced by the full chain: vm-template, workspace-template, agent-template,
  session-template (and admin if the session is admin-mode).
- `agw session attach s1`: opens no new shell on the target; the existing tmux server retains its
  original env. Consumes no secrets.
- `agw session list`: opens no shells. Consumes no secrets.
- `agw session restart --all-stopped --vm vm1` over-approximates: it prompts for secrets across all
  sessions on `vm1`, even ones that turn out to be running and will not be restarted. In
  `--non-interactive` mode this can surface as a failure on a secret the command would not have
  actually consumed; narrow the filter to recover.

### R5: Effective env propagation

The effective env (R2 merge) plus the applicable AGENTWORKS\_\* vars (R1) are propagated to every
shell agentworks opens. Propagation happens at shell-creation time only:

| Surface                   | Commands                                                       | Mechanism                                  |
| ------------------------- | -------------------------------------------------------------- | ------------------------------------------ |
| Provisioning              | `vm create` / `vm reinit` / `agent create` / `agent reinit`    | Inline `export` before the work            |
| Session create / restart  | `session create` / `session restart` / `session start`         | Inline `export` in the new-session payload |
| Console window creation   | `console create` / `console add-shell` / `console add-session` | Inline `export` per window                 |
| Multi-console panes       | Each pane created via the named-console layout                 | Inline `export` per pane                   |
| Interactive ad-hoc shells | `vm shell` / `agent shell`                                     | Inline `export` for the interactive shell  |
| Non-interactive exec      | `vm exec` / `agent exec`                                       | Inline `export` before the command         |

#### Attach inherits create-time env

`session attach`, `console attach`, and any other surface that joins an EXISTING shell process
inherit the env that was captured at create time. They do not consume or inject secrets, do not
re-export, and do not modify the running shell's env. This is the same contract the previous SDD
(`2026-06-06-direct-user-ssh-access` FRD R6) established for "old sessions" generally: changes to
config or secret values take effect only when the shell is created. To pick up new values, restart
the session (`session restart`) or recreate the console window.

#### Profile-fragment role

For the VM-stable identity vars (`AGENTWORKS_VM`, `AGENTWORKS_VM_HOST`, `AGENTWORKS_PLATFORM`,
`AGENTWORKS_USER`), the implementation places them in the existing `.agentworks-profile.sh` during
VM / agent init so that any shell on the VM (even ones not opened through agentworks) sees them.
Per-context vars (workspace, agent, session) and ALL user-defined env, plaintext or secret, are
always set inline at the shell-open site. User-defined env is never cached on the VM disk; the
authoritative source is the merge computed at command time.

### R6: Doctor integration

`agw doctor` reports:

- Declared secrets that are not referenced by any env entry (unused declarations).
- `{ secret = "..." }` references to undeclared secrets (broken references).
- Effective env conflicts that arise from valid configurations (informational; show which scope wins
  per key).
- Whether the CLI environment currently has each declared secret's `AW_SECRET_*` set (a "would I get
  prompted?" preview).

Doctor does not prompt for secrets; it only reports state.

### R7: CLI surface for env inspection

A small inspection facility helps operators understand what an opened shell will see:

```text
agw env show (--vm NAME | --workspace NAME | --agent NAME | --session NAME)
             [--reveal-secrets]
```

- At least one of `--vm` / `--workspace` / `--agent` / `--session` is required. Without a context,
  the command fails with a message explaining that an env table is always relative to some resource
  scope.
- Default output: plaintext entries show their actual values; secret-backed entries show as
  `<from secret: NAME>`. Plaintext values are safe to display because they already live in config in
  cleartext.
- `--reveal-secrets`: resolves secret-backed entries through the normal env-or-prompt path and
  prints their values. Without this flag, secret values are never read from operator env or prompted
  for during `env show`.

Future iterations may add `agw secret list` (lists declared secrets and their presence in CLI env)
and `agw secret show <name>` (gated; resolves a single secret through the normal path).

## Non-goals

- **Persistent secret storage by agentworks**: prompted values are not saved. Operators wanting
  persistence use their own vault (1Password, keychain) and export the `AW_SECRET_*` env var from
  there before running `agw`.
- **Pluggable secret backends in v1**: only env-var-then-prompt is implemented. The interface is
  designed to allow additional providers (keychain, 1Password CLI, Vault) in a later iteration.
- **Replacing `git_credentials`**: kept separate for this SDD. Folding git credentials into the
  general secret mechanism is anticipated future work but not part of this design.
- **PATH-style append/prepend**: only direct override semantics in v1. Operators needing list
  appends write the full value or use shell profile mechanisms.
- **Per-command env overrides**: there is no `--env KEY=VAL` flag in this SDD. Config is the source
  of truth. Operators wanting one-off overrides set the value in their CLI env (where it is visible
  to provisioning subprocesses) or edit config.
- **Encryption of config-on-disk**: the config file is plaintext. Secret _values_ never appear
  there, but their _names_ do, and the `description`/`hint` fields are visible. Operators with
  stricter requirements can keep their config in an encrypted home dir.
- **VM-side secret distribution beyond shell env**: writing secrets to files on the VM (e.g. for
  consumers that read `~/.somecred`) is out of scope. The mechanism is env-var-shaped end-to-end.

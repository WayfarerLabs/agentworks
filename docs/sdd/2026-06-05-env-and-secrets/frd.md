# Environment variables and secrets: functional requirements

**Status:** Locked **Repo:** `agentworks` **Path:** `cli/agentworks/`

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
   secret, definable across the resource graph (admin, vm template, workspace template, agent
   template, session template) with deterministic merge semantics.
3. A pluggable secret-backend system. Backends (env var, interactive prompt, and later 1Password,
   keychain, vault, ...) are uniform members of a `SecretSource` protocol. Operators configure which
   backends are active and in what order via a single precedence list. Secret declarations in config
   declare the _existence_ of a secret; backends produce the _value_ at command time. Values are
   never persisted by agentworks.

v1 ships with two backends: `env-var` (operator-side env-var lookup) and `prompt` (interactive
last-resort). Future work, out of scope for this SDD: additional backends (keychain, 1Password CLI,
vault); folding `git_credentials` into the general secret mechanism.

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
  persisted by agentworks. Resolved at command time through the configured backend chain.
- **Secret backend**: a provider that can produce secret values at command time (`env-var`,
  `prompt`, future `onepassword`, `keychain`, etc.). All backends implement the `SecretSource`
  protocol. Operators configure which backends are active and in what order via
  `[secret_config].backends`.
- **Backend mapping**: per-secret, per-backend identifier override. For backends with a default
  convention (e.g. `env-var` derives `AW_SECRET_<NAME>`), absent = use the default. For backends
  without one (e.g. 1Password), absent = skip this backend for this secret.
- **AGENTWORKS\_\* vars**: the small fixed set of env vars that agentworks always sets to identify
  the resource context (e.g. `AGENTWORKS_VM`, `AGENTWORKS_SESSION`). Automatic; not
  user-configurable.

## Requirements

### R1: Standard AGENTWORKS\_\* identity variables

Whenever agentworks opens a shell on a VM (session creation, session attach, console window spawn,
exec, provisioning, agent setup, admin shell, etc.), it sets the subset of the following variables
that apply to the shell's resource context:

| Variable                   | Set when             | Value                                                                                                                                             |
| -------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENTWORKS_VM`            | Always (on-VM shell) | VM name                                                                                                                                           |
| `AGENTWORKS_VM_HOST`       | VMs with a host      | VM host name from the `vm_hosts` registry (e.g. `lima-local`). Set only when the VM has an entry in the registry (today: Lima-platform VMs only). |
| `AGENTWORKS_PLATFORM`      | Always (on-VM shell) | One of `lima`, `azure`, `wsl2`, `proxmox`                                                                                                         |
| `AGENTWORKS_AGENT`         | Agent users (static) | Friendly agent name. Written to the agent's `~/.agentworks-profile.sh` at agent setup time; reaches every login shell as that user via sourcing.  |
| `AGENTWORKS_WORKSPACE`     | Workspace context    | Workspace name                                                                                                                                    |
| `AGENTWORKS_WORKSPACE_DIR` | Workspace context    | Absolute path to the workspace dir on the VM                                                                                                      |
| `AGENTWORKS_SESSION`       | Session context      | Session name                                                                                                                                      |
| `AGENTWORKS_SESSION_KIND`  | Session context      | `admin` or `agent`                                                                                                                                |

There is no opt-out. These vars are always set when their scope applies. User-defined env (R2) may
shadow the names, in which case the loader emits a config-load warning; identity values win at the
runtime prelude regardless, so the override has no effect at command time. The warning is the only
signal the operator gets that the override is dead; a future iteration may upgrade it to a
config-load error.

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
  `vm shell`, `vm exec`, console admin shells (`console create --add-admin-shell`), and provisioning
  shells.
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
`PATH = "...:${PATH}"`); shell variable expansion inside values is not interpreted by agentworks.
Values are passed through verbatim and shlex-quoted at shell-open time. Operators needing PATH-style
appends can write a full expansion (`PATH = "/foo:/usr/bin:/bin"`) or use shell profile mechanisms
outside this system. (Future iteration may add an explicit append/prepend syntax.)

#### Template inheritance

For templates that support `inherits`, env entries inherit along the inheritance chain in the
standard "child overrides parent by key" pattern that other template fields already use. The
inheritance-resolved env for a template is what feeds into the cross-scope merge described above.

The child's per-key entry replaces the parent's wholesale; form changes (plaintext to secret-ref and
vice versa) are allowed. For example:

```toml
[agent_templates.base.env]
LOG_LEVEL = "info"
API_KEY = { secret = "default-api-key" }

[agent_templates.coder]
inherits = ["base"]

[agent_templates.coder.env]
LOG_LEVEL = "debug"                              # plaintext overrides plaintext
API_KEY = "literal-test-value-for-coder"         # plaintext overrides a secret-ref
EXTRA = { secret = "coder-only-secret" }         # secret-ref added by child
```

The resolved env for `coder` is the union: `LOG_LEVEL=debug` (child), `API_KEY` literal (child),
`EXTRA` secret-ref (child).

#### Validation

- Keys must match `^[A-Za-z_][A-Za-z0-9_]*$` (POSIX env var name).
- Values are strings or `{ secret = "<name>" }` inline tables. No other shapes.
- `secret = "<name>"` must reference a declared secret (see R3). Unknown references are a config
  error.
- Keys starting with `AGENTWORKS_` may be set by users but emit a config-load warning (every CLI
  invocation, so the operator sees it quickly; R1 sets these automatically and overrides risk
  confusing downstream consumers).

### R3: Secret declarations

Secrets are declared in `[secrets.<name>]` blocks. Each declares the _existence_ of a secret and
optionally describes per-backend mapping overrides (R4):

```toml
[secrets.anthropic-api-key]
description = "Anthropic API key for Claude agents"
hint = "https://console.anthropic.com/settings/keys"

[secrets.github-token]
description = "GitHub PAT for repo access"
backend_mappings.env-var = "GITHUB_TOKEN"
backend_mappings.onepassword = "op://Personal/GitHub/token"
```

Fields:

- `description` (required): one-line description shown in prompts and `agw doctor`.
- `hint` (optional): additional guidance shown with the prompt (e.g. where to create the secret).
- `backend_mappings.<backend>` (optional): per-backend identifier override for this secret. See R4
  for value forms and semantics.

Secret names use the same character rules as resource names (lowercase alphanumeric, hyphens,
underscores; see `NAME_RE` in `config.py`). The namespace is flat and global.

Declaring a secret is the _only_ way to make a `{ secret = "..." }` reference resolvable. There is
no implicit lookup of arbitrary env vars by name; this avoids "ambient capability" surprises and
gives `agw doctor` a complete view of what the operator needs to provide.

### R4: Secret backends and resolution

Secret values are sourced through an operator-configurable chain of backends. Each backend is a
provider that can answer "do you have a value for this secret?" Backends include things like "the
operator's CLI environment" (`env-var`), "1Password CLI" (`onepassword`), and "interactive prompt"
(`prompt`). All are uniform members of the same `SecretSource` protocol; prompt is just the last
source in the chain by convention.

#### Backend declarations

Backends are configured in `[secret_backends.<kind>]` blocks. Each block carries the backend's
connection / global config:

```toml
[secret_backends.env-var]
# Always available; no config needed. Default convention: secret "github-token" maps to env var
# AW_SECRET_GITHUB_TOKEN. Override per-secret via backend_mappings (R3).

[secret_backends.onepassword]
account = "wfscot@example.com"
vault = "Personal"
# No default convention. Each secret must declare backend_mappings.onepassword to be resolvable
# from this backend.

[secret_backends.prompt]
# Effective only when stdin is a TTY and --non-interactive is not set. No config.
```

#### Active backends and precedence

A single top-level `[secret_config]` table holds the list of active backends, in precedence order:

```toml
[secret_config]
# This list controls BOTH which backends are active and the order they are tried in.
# A backend declared in [secret_backends.*] but absent from this list is dormant.
# First backend to return a value wins.
backends = ["env-var", "onepassword", "prompt"]
```

#### Per-secret mappings

Some backends have a default name-to-identifier convention (e.g. `env-var` derives
`AW_SECRET_<NAME>` from the secret's name). Others do not (1Password item paths, vault paths, etc.
have no automatic mapping). Per-secret overrides live in `[secrets.<name>].backend_mappings` (R3),
keyed by backend kind. Value forms:

| Value         | Meaning                                                                |
| ------------- | ---------------------------------------------------------------------- |
| `"some-id"`   | Simple identifier (e.g. env var name, `op://...` URI).                 |
| `{ ... }`     | Structured identifier (for backends whose ID has multiple fields).     |
| `false`       | Opt out: skip this backend for this secret, regardless of any default. |
| (key omitted) | Use the backend's default convention if one exists, else soft-skip.    |

Soft-skip means the backend returns no value and the resolver moves on; `agw doctor` surfaces
secrets where a configured backend is skipped due to missing mapping, so silent typos still get
caught.

A secret that is unreachable from every active backend (every active backend is either `false` in
its mapping or has no default convention and no explicit mapping) is a config-time error.

#### Resolution at command time

The resolver walks `secret_config.backends` in order, asking each source for the missing secrets.
First source to return a value wins; the resolver caches the value for the rest of the command
invocation. Prompt is just the last source in the chain; in `--non-interactive` mode or when stdin
is not a TTY, `PromptSource` returns `None` and the resolver raises `SecretUnavailableError` naming
the unsatisfied secret(s) and which backends were tried.

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
- Session create / restart: `session create`, `session restart`.
- Console window creation: `console create` (any sessions named in the create), `console add-shell`.
  _Implementation note (Phase 6):_ `console create` is DB-only in the current implementation; window
  creation defers to first attach inside `attach_console`'s build path (and the related
  `restore_session` repair path). Eager-resolve fires at the actual shell-opening sites
  (`attach_console` when building, `restore_session`, `console add-shell`) rather than at
  `console create` itself. The operator-facing UX ("prompted up front, before any tmux work") is
  preserved.
- Interactive ad-hoc shells: `vm shell`, `agent shell`.
- Non-interactive exec: `vm exec`, `agent exec`.

Commands that consume no secrets:

- Attach / inspection: `session attach`, `session list`, `session describe`, `console attach`,
  `console add-sessions`, `vm list`, `agent list`, `workspace list`, etc. (`console add-sessions`
  joins existing tmux sessions via wrapper windows; no new agent shells are opened.)
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

- `agw vm create vm1`: provisioning is hermetic. The only secrets prompted up front are
  provisioning-required (Tailscale auth key, git credentials), which live outside the env-block
  system. Operator `[admin.env]` / `[vm_templates.*.env]` secrets get prompted at the runtime _use_
  site (vm shell, session create, etc.) instead.
- `agw agent create a1 --vm vm1`: same hermeticity. Operator `[agent_templates.*.env]` secrets get
  prompted at agent shell / session create time, not at agent create.
- `agw session create s1 -t claude`: opens a new session shell as the agent's Linux user. Needs the
  secrets referenced by the full chain: vm-template, workspace-template, agent-template,
  session-template (and admin if the session is admin-mode).
- `agw session attach s1`: opens no new shell on the target; the existing tmux server retains its
  original env. Consumes no secrets.
- `agw session list`: opens no shells. Consumes no secrets.
- `agw session restart --all-stopped --vm vm1` over-approximates: it prompts for secrets across all
  sessions on `vm1`, even ones that turn out to be running and will not be restarted. In
  `--non-interactive` mode this can surface as a failure on a secret the command would not have
  actually consumed. Recover by either setting the missing secret in your CLI env
  (`AW_SECRET_<NAME>` or whichever backend mapping applies) or narrowing the static filter (e.g. add
  an explicit `--workspace`).

### R5: Effective env propagation

The effective env (R2 merge) plus the applicable AGENTWORKS\_\* vars (R1) are propagated to every
shell agentworks opens. Propagation happens at shell-creation time only:

| Surface                   | Commands                                                    | Mechanism                                                                                                                             |
| ------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Provisioning              | `vm create` / `vm reinit` / `agent create` / `agent reinit` | **No operator env injected; hermetic.** Static identity via on-disk profile fragments.                                                |
| Session create / restart  | `session create` / `session restart`                        | SSH SetEnv + `tmux new-session -e` for the new session                                                                                |
| Console build             | `console attach` first attach / `--recreate`                | SSH SetEnv + `tmux new-session -e` per session window                                                                                 |
| Console add-shell panes   | `console add-shell`                                         | SSH SetEnv + `tmux split-window -e` per pane (workspace scope; no session scope -- panes are sidecar shells, not part of the session) |
| Console restore-session   | `console restore-session` (window-missing branch)           | SSH SetEnv when the window has to be recreated                                                                                        |
| Interactive ad-hoc shells | `vm shell` / `agent shell` (with `--workspace` when set)    | SSH SetEnv on the interactive shell                                                                                                   |
| Non-interactive exec      | `vm exec` / `agent exec`                                    | SSH SetEnv on the command                                                                                                             |

#### Attach inherits create-time env

`session attach`, `console attach`, and any other surface that joins an EXISTING shell process
inherit the env that was captured at create time. They do not consume or inject secrets, do not
re-export, and do not modify the running shell's env.

This SDD establishes the contract: changes to config or secret values take effect only when a shell
is created. To pick up new values, restart the session (`session restart`) or recreate the console
window. The prior direct-target-user-SSH SDD's FRD R6 forward-referenced this need; this is where it
lands.

#### Static vs dynamic identity

Identity vars split into three kinds with three placements:

- **VM-stable static** (`AGENTWORKS_VM`, `AGENTWORKS_VM_HOST`, `AGENTWORKS_PLATFORM`). Same for
  every Linux user on the VM and every shell that VM hosts. Lives in the system-wide
  `/etc/profile.d/agentworks-identity.sh` fragment (and a marker-bracketed block in
  `/etc/zsh/zprofile` because zsh skips `/etc/profile.d/*` by default). Any login shell on the VM
  picks these up by sourcing -- no inline injection needed, even ones that aren't opened through
  agentworks.
- **Per-user static** (`AGENTWORKS_AGENT` for agent users). Same value every time a given Linux user
  logs in. Lives in the user's `~/.agentworks-profile.sh`. Written at agent setup time before any
  install command runs, so the install machinery sees it via the standard login-shell sourcing
  chain. Admin users get the empty subset -- their identity is the standard POSIX `$USER` /
  `$LOGNAME`.
- **Per-context dynamic** (`AGENTWORKS_WORKSPACE`, `AGENTWORKS_WORKSPACE_DIR`, `AGENTWORKS_SESSION`,
  `AGENTWORKS_SESSION_KIND`). Vary per shell-open invocation. Cannot live on disk; injected via SSH
  SetEnv / `tmux -e` at shell-open time.

User-defined env (R2) is treated the same as per-context dynamic identity at runtime sites (SetEnv
at shell-open). User-defined env is never cached on the VM disk; the authoritative source is the
merge computed at command time. **Provisioning** (VM init, agent setup install commands) is
hermetic: it sees static identity via profile fragments but NEVER per-context identity or
user-defined env -- those are operator preferences that only reach runtime shells.

### R6: Doctor integration

`agw doctor` reports:

- Declared secrets that are not referenced by any env entry (unused declarations).
- `{ secret = "..." }` references to undeclared secrets (broken references).
- Effective env conflicts that arise from valid configurations (informational; show which scope wins
  per key).
- For each declared secret: the first active backend whose `would_attempt(secret)` returns True, and
  whether resolution would succeed without prompting (a "would I get prompted?" preview).
- Soft-skip findings: secrets where one or more active backends return False from
  `would_attempt(secret)` because the secret has no mapping and the backend has no default
  convention. Helps surface typos in `backend_mappings.<backend>` keys and incomplete migrations
  when an operator adds a new backend.
- `backend_mappings.<kind>` keys referencing a backend that is not declared in
  `[secret_backends.*]`: reported as an error (the kind does not exist in this config).
- `backend_mappings.<kind>` keys referencing a backend that is declared in `[secret_backends.*]` but
  not present in `[secret_config].backends`: reported as a warning. This shape is legitimate (an
  operator may keep mappings authored for a backend they have temporarily disabled, ready to
  re-enable later) but worth flagging because the mapping has no effect in the current
  configuration.

Doctor does not prompt for secrets; it only reports state.

### R7: CLI surface for env inspection

A small inspection facility helps operators understand what an opened shell will see:

```text
agw env show [--vm NAME] [--workspace NAME] [--agent NAME] [--session NAME] [--reveal-secrets]
```

- At least one of `--vm` / `--workspace` / `--agent` / `--session` is required. Without a context,
  the command fails with a message explaining that an env table is always relative to some resource
  scope.
- When `--session` is given, the VM, workspace, and agent for that session are auto-resolved from
  the DB row (the session knows its own chain). Same for `--workspace` (resolves to its VM) and
  `--agent` (resolves to its VM). Manually-passed flags override the auto-resolution.
- Default output: plaintext entries show their actual values; secret-backed entries show as
  `<from secret: NAME>`. Plaintext values are safe to display because they already live in config in
  cleartext.
- `--reveal-secrets`: resolves secret-backed entries through the active backend chain and prints
  their values. Without this flag, `env show` never consults any backend for secret-backed entries
  (no env reads, no prompts).

Future iterations may add `agw secret list` (lists declared secrets and their presence in CLI env)
and `agw secret show <name>` (gated; resolves a single secret through the normal path).

## Non-goals

- **Persistent secret storage by agentworks**: prompted values are not saved. Operators wanting
  persistence use their own vault, exposed either via the matching backend (`onepassword`,
  `keychain`, etc.) or by exporting `AW_SECRET_<NAME>` into the operator shell.
- **Additional secret backends in v1**: v1 ships `env-var` and `prompt`. The `SecretSource` protocol
  is shaped to accommodate later additions (keychain, 1Password CLI, Vault), but those
  implementations are out of scope here.
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

## Migration notes

Operators upgrading across the env-and-secrets SDD see two observable changes that aren't otherwise
documented in the requirements above:

- **Per-VM / per-agent template env now picked up at session create / restart.** The pre-SDD
  `_build_session_command` only consulted the session template's `env`. After this SDD,
  `_resolve_session_env` walks `VMRow.template` and `AgentRow.template` to resolve the VM and agent
  template env that applies to the session. Operators who populated
  `[vm_templates.<non-default>.env]` or `[agent_templates.<non-default>.env]` now see those vars in
  the next session create / restart; before this SDD those tables were silently dead config for any
  non-default-template VM / agent.
- **Admin sessions move to per-session sockets.** Pre-SDD admin sessions used the shared default
  tmux server per admin user; after this SDD each admin session gets its own server on
  `/run/agentworks/admin-tmux-sockets/<admin>/<session>.sock`, mirroring the agent-mode pattern.
  Sessions created before the upgrade have `socket_path = NULL` in the DB. `check_session_status`
  surfaces a typed `StateError` for those rows pointing at `agw session restart <name>` (the
  primitive that can safely migrate); `agw session list` still lists them but emits a one-time
  warning naming the affected sessions. `agw session restart` performs the migration in place:
  surgical `tmux kill-session -t <name>` on the default server (so other unrelated tmux sessions on
  the shared server survive), then `create_tmux_session` produces a fresh per-session socket and the
  new path is persisted to the DB row. Callers other than restart (attach, stop, etc.) can't safely
  migrate and surface the typed error.
- **Restricted tmux config sets `default-command "$SHELL -l"`.** Agentworks sessions now invoke a
  login shell for no-command panes so the Phase 4 profile fragments
  (`/etc/profile.d/agentworks-identity.sh`, `~/.agentworks-profile.sh`) get sourced. An operator who
  customized `default-command` in their `~/.tmux.conf` to something other than a login shell will
  find the agentworks-restricted config overriding that (the restricted config is sourced AFTER the
  user's `~/.tmux.conf` and wins). Realistic impact is small (most operators don't customize
  `default-command`, and those who do typically want a login shell), but the override is a behavior
  change worth surfacing.

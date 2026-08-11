# Agentworks CLI

The operator's command-line interface for managing agentic workloads on Agentworks.

For the product overview and core concepts, see the [top-level README](../README.md). The project's
values and design rationale are in the [Manifesto](../docs/manifesto.md). This document covers
installing the CLI, the command surface, configuration, and operational details.

## Getting Started

Install from PyPI:

```bash
uv tool install agentworks-cli
# or:  pipx install agentworks-cli
```

The everyday command is `agw`. The longer form `agentworks` is also installed if you ever want to
type it out; examples throughout this document use `agw`.

```bash
# Initial setup
agw config init                          # creates ~/.config/agentworks/config.toml
agw config edit                          # opens the config in your $EDITOR (or $VISUAL) to fill in required fields
agw doctor                               # sanity-checks installed tools, Tailscale, config validity, and the local DB

# Create a VM, workspace, agent, and session to see how the pieces fit together
agw vm create my-vm
agw workspace create my-workspace --vm my-vm
agw agent create my-agent --vm my-vm
agw session create my-session --workspace my-workspace --agent my-agent

# Attach to the session's tmux session to drive it
agw session attach my-session
# Use tmux's 'detach' command (default Ctrl-b unless overridden by config) to disconnect while
# leaving everything running on the VM.
agw session attach my-session    # You'll pick up right where you left off
agw session stop my-session      # Sessions can be stopped (or can exit on their own)
agw session list
agw session resume my-session
agw session attach my-session
agw session delete my-session    # When you're done with it. Agent and workspace are preserved unless this was their last session (see below).

# Alternatively, you can create ephemeral workspaces and agents along with your sessions
agw session create my-ephemeral-session --vm my-vm --new-workspace --new-agent
agw session attach my-ephemeral-session
agw session delete my-ephemeral-session    # This will prompt you to delete the associated workspace and agent, too

# Deleting a session also checks whether its workspace and agent are now unused
# (whether or not this session created them). A workspace is unused once it has
# no sessions; an agent is unused only once it has no sessions AND no standing
# workspace grant (no explicit grant and grant-all unset; a standing grant
# means you still intend to use the agent, so it is left alone). For each
# resource now unused, session delete offers to delete it interactively.
# Under --yes it auto-deletes only a workspace/agent
# this session created; anything else now unused is reported (naming
# `agw workspace delete <name>` / `agw agent delete <name>`) and left in place
# for you to remove by hand.
# One guard applies: if any agent holds an explicit per-workspace grant on the
# now-unused workspace (deleting it would silently revoke that grant), the
# --yes auto-delete is refused and the workspace is reported (naming the
# granting agents) instead, while the interactive offer discloses whose grants
# a delete would revoke. Grant-all agents don't trigger the guard: blanket
# access is policy, not per-workspace intent.

# Finally, create two sessions and a named console
agw session create s1 --vm my-vm --new-workspace --new-agent
agw session create s2 --vm my-vm --new-workspace --new-agent
agw console create my-console s1 s2+1      # The + syntax gives you extra shells as that agent
agw console attach my-console

# Deleting a session drops it from any console that referenced it (no dangling
# references are left behind). session delete lists the affected consoles, and
# for any console left with no sessions it offers to delete the now-empty
# console (interactively). Under --yes it reports the empty console but leaves
# it for you to remove with `agw console delete <name>`. `console remove-sessions`
# gets the same now-empty treatment when it drops a console's last session.
agw session delete s1                      # Reports that my-console still referenced s1

agw console delete my-console              # Extra shells are lost but sessions are preserved
```

## Prerequisites

- Python 3.12+ (uv will install one for you if needed)
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for installation
- [Tailscale](https://tailscale.com/) installed and connected (for VM workspaces)
- One of: [Lima](https://lima-vm.io/), Azure CLI (`az`), AWS credentials for EC2, Google Cloud
  credentials for GCE, [Proxmox](https://www.proxmox.com/), or WSL2 (for VM provisioning; Azure,
  AWS, GCP, and Proxmox also need their [system plugin](#system-plugins) enabled). The optional
  guest `aws-cli` and `gcloud-cli` install commands are not host prerequisites.

## Global Options

| Flag                | Description                                                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `--non-interactive` | Disable all interactive prompts                                                                                                             |
| `--debug`           | Print the full traceback on unhandled errors, and show the Azure SDK's own log lines that are otherwise suppressed (also via `AGW_DEBUG=1`) |
| `--no-deprecations` | Silence the ambient per-command deprecation banner (`agw doctor` always reports deprecation health)                                         |

When `--non-interactive` is set (or stdin is not a TTY), commands that would normally prompt for
missing values (VM selection, workspace selection, name generation) will fail with a clear error
indicating which flag is required. VM auto-selection still works: if there is exactly one usable VM,
it is used without prompting. `session create` is an intentional exception: it always prompts for
workspace and mode (even when only one choice exists) since those are part of the session's identity
and should be an explicit operator decision.

Domain errors (SSH timeouts, validation failures, missing resources, etc.) surface as a single clean
line: `Error: <message>`. Truly unexpected failures (internal bugs, OS-level errors, third-party
library failures) also get a clean single-line message, plus the full traceback appended to
`~/.config/agentworks/logs/error.log` for debugging. Pass `--debug` (or set `AGW_DEBUG=1`) to print
the traceback to stderr instead. Debug mode also restores the Azure SDK's own credential-chain log
lines, which are otherwise suppressed so a credential failure renders once as the typed error.

On an interactive terminal, output is colorized by role: a yellow `Warning:` prefix, a red `Error:`
prefix, bold section headers, a dim-green result line (the closing "VM deleted", "rekeyed", etc.),
and dimmed secondary detail. `agw doctor` colors its per-check status labels the same way (green
`[ok]`, yellow `[warn]`, red `[FAIL]`, and unstyled `[info]`), plus its summary line's
`fail`/`warn`/`ok` counts. Color is a presentation aid only, never carried in the message text. It
is suppressed automatically when the target stream is not a terminal (pipes, redirects, CI capture)
and under `--non-interactive`, so scripted and captured output stays byte-plain. Set the `NO_COLOR`
environment variable (any value, honored by its presence) to opt out of color even on a terminal.

Pressing Ctrl-C during a long-running operation triggers best-effort cleanup. Where the operation
can roll back (e.g. `vm create` during the provisioning phase, `workspace create`, `agent create`,
`session create`) it undoes the partial DB / on-VM state and prints `Cancelling X... rolling back.`.
On every platform the `vm create` provisioning-phase rollback also deletes the partially created
backend state: Azure the cloud resource set (VM, NIC, public IP, NSG, vnet, disk), GCE the
provider-ID-owned instance and allow/deny rules, which can take a minute or two; Proxmox the
partially cloned VM (cancelling a still-running clone task first); Lima the instance (local, or on
the site's placement host for an ssh-placed site); WSL2 the distro plus its install directory. A
second Ctrl-C abandons that cleanup, printing what to remove manually: the resource group and name
prefix, the node and VMID, or the exact removal command (`limactl delete --force <name>`, run on the
placement host for an ssh-placed site, or `wsl --unregister <name>` plus deleting the install
directory it names). Where rollback isn't possible (`vm reinit`, `agent reinit`, the init phase of
`vm create`) it prints a recovery hint: the next command to run (`vm reinit`, `vm delete --force`,
...). Every cancellation exits with the conventional SIGINT exit code (130).

## Commands

The complete command surface, machine-readable JSON v1 contract, guide behavior, and session/tmux
details live in the focused [CLI command reference](command-reference.md). The VM, workspace, and
session list and describe paths project persisted provisioning, initialization, and session-mode
values through frozen JSON v1 vocabularies owned by the output contract. On those operational
surfaces, valid values retain their existing human bytes and corrupt values render as the stable
`unknown` sentinel without echoing their stored text. Doctor diagnostics are a separate surface.

## Configuration

Configuration splits into two surfaces:

- **Settings** live in `~/.config/agentworks/config.toml`: your identity, paths, defaults, database
  safety policy, and the secret source chain. Run `agw config init` to generate a sample; see
  [sample-config.toml](agentworks/sample-config.toml) for the full reference.
- **Resources** (secrets, templates, git credentials, vm-sites, apt / install-command entries) are
  declared as YAML manifests under `~/.config/agentworks/resources/`, auto-loaded whenever a command
  needs them. `agw resource sample <kind>` prints a commented starter (`--all` for every kind). The
  classic TOML resource sections are no longer supported: a `config.toml` that still declares
  resources is a hard error at load (they were previously deprecated with a warning). Rewriting
  those sections as manifests is a manual step, walked through by
  [docs/guides/upgrading-to-0.14.md](../docs/guides/upgrading-to-0.14.md).

Settings sections (`config.toml`, permanent):

- `[operator]` -- SSH keys (required), additional authorized keys, SSH config management
- `[paths]` -- VM workspace, VS Code workspace file, and backup directories
- `[defaults]`: `site`, the default vm-site for `vm create`
- `[database]`: automatic pre-migration backup policy (safe default: enabled)
- `[session.config]` -- session defaults (history limit)
- `[secret_config]` -- active secret source chain; its `backends` key keeps the established spelling
  but contains `secret-source` names
- `[plugins]`: the plugin-subsystem namespace; its `system` key is the opt-in list of enabled system
  plugins (see [System Plugins](#system-plugins) below)

Resources are declared as YAML manifests. `agw resource kinds` lists every kind with its category
and purpose, and `agw resource describe-kind KIND` documents what one accepts, field by field.

The table below exists for one thing this repository is otherwise the only record of: which removed
legacy TOML section used to declare each kind. Those sections no longer load; the mapping is a
reference for reading an old `config.toml` and for rewriting it as manifests.

| Kind                                                                          | Removed TOML section                        |
| ----------------------------------------------------------------------------- | ------------------------------------------- |
| `vm-site`                                                                     | `[azure]` / `[proxmox]` (flat legacy shape) |
| `vm-template`                                                                 | `[vm_templates.*]`                          |
| `admin-template`                                                              | `[admin.config]`                            |
| `agent-template`                                                              | `[agent_templates.*]`                       |
| `session-template`                                                            | `[session_templates.*]`                     |
| `workspace-template`                                                          | `[workspace_templates.*]`                   |
| `named-console-template`                                                      | `[named_console]`                           |
| `git-credential`                                                              | `[git_credentials.*]`                       |
| `secret`                                                                      | `[secrets.*]`                               |
| `secret-source`                                                               | none; introduced as YAML                    |
| `apt-source`, `apt-package`, `system-install-command`, `user-install-command` | `[apt_sources.*]` and siblings              |

The four capability kinds (`vm-platform`, `harness-integration`, `git-credential-provider`,
`secret-backend`) are read-only rows for registered code, never declared and never in TOML.

Env vars ride their owning resource, as an `env` map in the template's `spec` (the removed TOML
shape used `[<scope>.env]` subsections), at vm / workspace / admin / agent / session scope. The
`lima-local` and `wsl2` vm-sites ship built in and their names are reserved.

### Environment Variables and Secrets

Env tables can be declared at five scopes; for any given session the merged value is computed in
this precedence order (highest scope wins; identity vars win over everything):

```text
session > (agent | admin) > workspace > vm           (AGENTWORKS_* identity overrides all)
```

Admin and agent scopes are mutually exclusive: a shell opened as the admin user (e.g.
`agw vm shell`) sees admin scope; an agent-mode session sees agent scope. Each scope is an env map
on the owning resource, mapping env-var name to either a plaintext string or a secret reference:

```yaml
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: default
spec:
  env:
    HTTP_PROXY: http://proxy:3128
    NPM_TOKEN: { secret: npm-token }
```

Every secret reference points to a `secret` resource declaration (auto-declared with a
framework-synthesized description if you skip it). `[secret_config].backends` lists configured
`secret-source` resource names in precedence order. Agentworks synthesizes two sources, so the
simple default stays behavior-identical without a manifest:

- `env-var` -- reads from the operator's process env. Default convention is
  `AW_SECRET_<UPPER_SNAKE_CASE>`, overridable per secret via the secret's `backend_mappings`
  (`env-var: CUSTOM_NAME`).
- `prompt` -- interactive prompt; you are never asked for the same secret twice in one command.
  Plan-wide prompting happens before the command starts changing anything. Conditional Tailscale
  repair is deliberately lazy: healthy and already-connected paths never ask for a repair key, and a
  stopped VM may start before late key delivery. The delivered key is validated before any
  rejoin-specific mutation, transport, installation, or daemon action.

**Resolve before plan mutation:** a command resolves all the secrets its static plan needs up front,
before it starts changing anything. The conditional Tailscale repair exception stays lazy so healthy
paths do not prompt: a stopped VM may start before Agentworks discovers that repair is required,
then the late key is validated before all rejoin-specific work. Preflight first performs a pure
applicability screen: a declaration with no ready, permitted source that would even attempt it fails
with a hint (`agw secret describe <name>` shows how each source maps it), before any prompt and
before any VM is started. Actual presence, authentication, transport, and provider failures remain
the typed resolution boundary's job. The set of secrets is computed from the command's static
filters (positional targets, `--vm`, `--workspace`, `--agent`, etc.); dynamic predicates like
`--all-stopped` apply later, so the prompted set may over-approximate. Non-interactive mode (no TTY
or `--non-interactive`) surfaces missing secrets as `SecretUnavailableError` with a per-secret hint
naming which sources were tried. Commands that join existing shells (`session attach`,
`session list`, `console attach` against a live tmux session, `console add-sessions`) consume no
secrets.

**Miss semantics:** what "not found" means depends on the selected backend. Conventional sources
(`env-var`, `prompt`) treat a missing value as a soft miss and fall through to the next source. A
`GITHUB_TOKEN` env var that isn't set is just-not-set, not a config error. Persistent-store clients
treat an explicit mapping that does not resolve as a typed hard mapping failure, and the chain halts
for that secret so a wrong `op://` URI cannot be masked by a prompt.

Inspect the merged result for any context with `agw env show`:

```bash
agw env show --session my-session              # secrets redacted as <from secret: name>
agw env show --vm my-vm --resolve              # resolves through the active source chain
```

(The flag was formerly spelled `--reveal-secrets`; it was renamed to `--resolve` as a breaking
change, the old spelling no longer works.)

Inspect how each active source would attempt each declared or auto-declared secret (e.g. "which env
var name does this secret read from?") with `agw secret list`:

```bash
agw secret list
# 4 secrets (2 operator-declared, 2 auto-declared)
#
# NAME                 DESCRIPTION                                                                env-var                       prompt
# ----                 -----------                                                                -------                       ------
# api-key              OpenAI key for the operator's service                                      OPENAI_API_KEY                would attempt
# force-prompt         Always prompted at command time                                            won't attempt                 would attempt
# git-token-github     (auto) the auth token for git_credentials:github                           AW_SECRET_GIT_TOKEN_GITHUB    would attempt
# tailscale-auth-key   (auto) the Tailscale auth key for vm-template:default (and 1 more)          AW_SECRET_TAILSCALE_AUTH_KEY  would attempt
```

Columns are the active sources in `[secret_config].backends` precedence order. Cells show each
source's static lookup identifier (env var name, vault path, `op://` URI), `won't attempt`,
`would attempt`, or `not ready: <reason>`. The Description column shows the operator-supplied text
for operator-declared secrets, or a framework-synthesized `(auto) <usage> for <kind>:<name>` (plus
`(and N more)` when more than one source requires the secret) for auto-declared ones. The
synthesized text reads as "what this secret is for, and who's asking." The summary line breaks the
rows down by origin. Values are never resolved.

For the full per-secret detail view, including the structured origin block, usage list (who requires
this secret), source-keyed `backend_mappings` table, and a resolution preview, use
`agw secret describe`:

```bash
agw secret describe tailscale-auth-key
# Secret: tailscale-auth-key
#   Kind: secret
#   Description: (auto) the Tailscale auth key for vm-template:default (and 1 more)
#   Origin: auto-declared (vm-template:default)
#
# Referenced by:
#   - vm-template:default -- the Tailscale auth key
#   - vm-template:heavy -- the Tailscale auth key
#
# Backend mappings:
#   - env-var (env-var, synthesized default): AW_SECRET_TAILSCALE_AUTH_KEY
#   - prompt (prompt, synthesized default): (prompt at resolution time)
#
# Resolution preview:
#   would attempt via env-var
```

`describe` never prompts, opens a source client, reads the environment, or displays a secret value.
Its preview is mapping applicability, not proof: it reports `would attempt via`, and verification or
the command's resolution boundary determines whether a value is actually present.

To prove that a declared secret resolves through the configured source chain, use `verify`:

```bash
agw secret verify tailscale-auth-key deploy-token
# NAME                 CATEGORY  SOURCE   IDENTIFIER                    DETAIL    REMEDIATION
# -------------------------------------------------------------------------------------------
# tailscale-auth-key   resolved  env-var  AW_SECRET_TAILSCALE_AUTH_KEY  resolved  none
# deploy-token         resolved  work-op  op://Engineering/deploy/token resolved  none
```

Verification deduplicates names in first-written order, performs one real ordered resolution pass,
and prints one value-free row per unique name. The columns report category, source, safe lookup
identifier, typed detail, and remediation. If any row is not `resolved`, every row is still shown
and the command exits 1; an all-resolved batch exits 0. Registry, configuration, and usage failures
occur before the table and use normal CLI error framing.

By default verification refuses interactive sources, so it cannot unexpectedly prompt or initiate
provider authentication. Opt in explicitly when an interactive source is required:

```bash
agw secret verify tailscale-auth-key --allow-interaction
```

`--allow-interaction` permits prompts, biometric checks, and renewed authentication. It is rejected
when the global `--non-interactive` flag is set. Outcome rows use only framework-owned categories
and remediation; resolved values and provider-authored payloads are never rendered.

`agw doctor` keeps three adjacent secret groups. `Secret backends` reports implementation readiness;
`Secret sources` shows every declared source with its selected backend, active/inactive,
enabled/disabled, provenance, and folded readiness; `Secrets` emits exactly one row per registry
secret -- operator-declared and auto-declared alike (auto-declared rows, e.g. `tailscale-auth-key`
and the `git-token-*` family, carry an `(auto)` marker; they are exactly the secrets most likely to
prompt at command time):

- **OK** when at least one active source would attempt the secret (`would attempt via env-var`,
  `would attempt via prompt`, ...). `would attempt via prompt` is the heads-up that the next command
  needing this secret will ask for it interactively.
- **WARN** when nothing in the chain is attemptable (config-valid but no mapping path, e.g. a
  mapping-required source has no mapping and `prompt` is opted out via
  `backend_mappings.prompt = false`). An unknown `backend_mappings` source name fails Registry
  construction first, so doctor reports it under Configuration and does not construct the Secrets
  group.

Source-applicability detail (per-source soft-skip reasons, inactive mappings, per-secret references)
lives in `agw secret list` and `agw secret describe`. `AGENTWORKS_*` identity overrides surface in
the Configuration group (they're a config-load warning). Broken `{ secret: ... }` references are
caught earlier as a hard config-load error before doctor runs. Git-credential tokens are just
secrets: their _resolvability_ reports as ordinary `git-token-<name>` rows in the Secrets group,
like any other secret. Doctor never opens a source, reads an environment variable, invokes a client,
or prompts. Its preview is a value-free applicability prediction, not proof that a value exists. Use
`agw secret verify NAME...` for an explicit value-free proof; interactive sources require
`--allow-interaction`. Capability token authentication still occurs at the capability `runup()`
stage inside provisioning operations. The Tailscale group checks only workstation connectivity; the
auth key is the `tailscale-auth-key` secret row.

When the config or a resource manifest fails to load, the groups that depend on them (VM sites,
Secrets) do not vanish: each renders a single
`[info] ... skipped (config or manifests unavailable; see the Configuration group)` row, so a
degraded run keeps the same section skeleton as a healthy one and the Configuration group carries
the actual failure.

### Secret Sources and Backends

A **source** is a declarable `secret-source` resource that selects one read-only `secret-backend`
implementation in `spec.backend`. The chain and every `backend_mappings` key name sources, not
implementations. The synthesized `env-var` and `prompt` source names remain valid unchanged. For
1Password, enable the plugin, declare a source with `backend.name: onepassword`, move the old
mapping account to that source, optionally set the new source timeout, and map each secret's source
key to one scalar `op://` reference. A direct configured-backend reference such as `onepassword`
breaks in 0.14 with an exact source declaration and mapping rewrite; no compatibility row is
created.

### System Plugins

Agentworks ships some vendor- and tool-specific capabilities (VM platforms, harness integrations,
git-credential providers, secret backends) as **system plugins**: separable bundles that are
installed but off by default. The shipped build installs `azure` (the `azure-vm` VM platform, the
`azdo` git-credential provider, and the `az-cli` install-command), `proxmox` (the `proxmox` VM
platform), `aws` (the `aws-ec2` VM platform and optional guest `aws-cli`), `gcp` (the `gcp-gce` VM
platform and optional guest `gcloud-cli`), `onepassword` (the `onepassword` secret backend),
`claude` (the `claude-code` harness integration and the `claude` CLI install-command), and `codex`
(the `codex` harness integration and the `codex` CLI install-command). (This is a different sense of
"plugin" from [Claude Code Plugins](#claude-code-plugins) below, which installs marketplace plugins
into Claude Code itself.)

Opt in by name in `config.toml`:

```toml
[plugins]
system = ["azure", "aws", "gcp", "proxmox", "onepassword", "claude", "codex"] # only those you use
```

A resource that references a not-enabled plugin's contribution (an `azure-vm` vm-site, a
`claude-code` session-template, a secret mapped to a source selecting `onepassword`, ...) is
not-ready, or refused at use, with an "enable plugin `<name>`" hint, never an unknown-name error.
The default local path (the `lima` / `wsl2` platforms, the `shell` harness integration, the
`env-var` / `prompt` secret backends, and the `github` git-credential provider) is built in, always
on, and needs no `[plugins]` entry.

A not-enabled plugin's rows are hidden from `agw resource list` by default; pass
`--include-disabled` to reveal them (see the
[Resource Registry](command-reference.md#resource-registry)). `agw doctor` has a **System plugins**
group listing every installed plugin, its description, and whether it is enabled.

See [docs/guides/resources.md](../docs/guides/resources.md#system-plugins) for the full model
(origins, the disabled-resource semantics, config-error deferral) and the upgrade note for configs
that relied on Azure, Proxmox, 1Password, or Claude Code before they became opt-in. Google Compute
Engine setup, firewall prerequisites, whole-document JSON-secret setup, and provider-ID-safe
recovery are covered in [Using Google Compute Engine](../docs/guides/gcp.md); AWS guest CLI
boundaries are in [Using AWS with Agentworks](../docs/guides/aws.md).

### Mise (Polyglot Tool Manager)

Agentworks installs [mise](https://mise.jdx.dev/) by default on all VMs for managing CLI tools
(terraform, adr-tools, node, etc.) with optional lockfile-based integrity verification. See
[Using mise](../docs/guides/mise.md) for the full guide.

### Claude Code Plugins

Agentworks can register Claude Code marketplaces and install plugins automatically per user (admin
and per-agent). Configure via `claude_marketplaces` and `claude_plugins` on the admin template or
any agent template. Requires the `claude` CLI on PATH (typically installed via
`user_install_commands`). To install nerftools this way:

```yaml
apiVersion: agentworks/v1
kind: admin-template
metadata:
  name: default
spec:
  claude_marketplaces: ["https://github.com/WayfarerLabs/nerftools#4.1.0"]
  claude_plugins: [nerftools-default@nerftools]
```

### Built-in Apt / Install-Command Entries

Agentworks ships built-in entries for common tools (apt sources, apt packages, system install
commands, and user install commands), bundled as YAML manifests under
`agentworks/manifests/builtin/`. Run
`agw resource list --kind apt-package,system-install-command,user-install-command,apt-source` to see
what is available (or filter to any single kind). Reference these entries by name from VM, admin,
and agent templates. User-defined entries override built-in entries with the same name.

## VM Initialization

VM creation follows a two-phase lifecycle tracked by separate status columns:

1. **Provisioning** (`provisioning_status`) -- one-time, platform-specific, over the provisioning
   transport (Lima shell, SSH, or WSL2 exec): create user, install system packages, add SSH key,
   install and join Tailscale

2. **Initialization** (`init_status`) -- repeatable via `vm reinit`, over Tailscale SSH: configure
   apt sources, install apt packages, install snap packages, install mise, set shell, reconcile SSH
   authorized keys, run system install commands, write mise config, configure PATH, configure git
   credentials, sync dotfiles, fetch mise lockfile, run mise install, run user install commands for
   the admin user

Initialization is fully declarative, driven entirely by config. `vm create` only accepts a name,
`--template`, `--admin-template`, and `--site`; the immutable provisioning parameters (resources,
admin username) come from the selected templates. `vm reinit` takes only the VM name and re-runs
initialization using the current config.

Non-fatal initialization failures (packages, dotfiles) produce a `partial` status rather than
aborting. Fatal failures prompt for deletion or reinit. Use `vm describe` to view the full event
log.

## Shell Completion

```bash
agw completion install
```

The shell is autodetected from `$SHELL`; pass `--shell {bash|zsh|powershell}` to override (or when
autodetection isn't unambiguous, e.g. on Windows). `completion install` writes the script to the
standard location for that shell. For PowerShell it also appends a dot-source line (`. "..."`) to
`$PROFILE`. For bash and zsh, if your rc file is missing the loader (`bash-completion` for bash,
`fpath=(~/.zfunc $fpath)` for plain zsh without a plugin manager), the installer prints a one-line
note telling you what to add.

To print the script without installing, use `agw completion show` (handy for piping into your own
config-management flow). To remove completions installed here, use
`agw completion uninstall --shell {bash|zsh|powershell}`. For PowerShell, uninstall also strips the
dot-source line the installer appended to `$PROFILE`; user-authored lines around it are left
untouched.

Completions include dynamic VM, vm-site, workspace, session, secret, and template name lookups.
`agw secret verify` completes registered secret names at every positional argument.

## State

All state is stored in `~/.config/agentworks/agentworks.db` (SQLite). Schema migrations are
forward-only and run automatically when a normal Agentworks command opens stale state. Before the
first migration statement, Agentworks announces the source and target versions on stderr and, by
default, completes an online snapshot. An interactive terminal asks
`Back up the state database before migrating?` with yes as the default. Automation uses
`[database] auto_backup_before_migration = true`; set it to `false` only when deliberately accepting
migration without that recovery point. `agw doctor` uses a WAL-aware read-only inspection and never
runs migrations. See the [doctor JSON contract](command-reference.md#doctor-json-schema) for the
machine-readable result.

Create a consistent on-demand snapshot, including committed WAL content, with:

```console
agw database backup
```

The command prints the completed backup path to stdout. Backups live in
`~/.config/agentworks/database-backups/` beside the live database. On-demand files use the
`agentworks-manual-...db` prefix and are never removed by automatic retention. Automatic
pre-migration files use `agentworks-pre-migration-...-vN.db`; only that category is pruned, keeping
the five newest automatic files. Unrecognized files in the directory are left alone.

If a selected automatic backup fails, migration does not start; the error explains how an
interactive retry can explicitly decline or how automation can use the documented opt-out. If a
migration itself fails, the error either prints an exact `agw database restore ...` command for the
completed pre-migration snapshot or states explicitly that no snapshot was selected.

Restore a selected snapshot with `agw database restore BACKUP_PATH`. The command validates the
backup, shows the backup and live paths on stderr, and asks before replacing the live database. Pass
`--yes` (or `-y`) for intentional non-interactive recovery. Restore does not first back up the
database it replaces and does not run schema migrations. If the restored snapshot is older, the next
ordinary command owns any forward migration. A backup from a newer schema is preserved but must be
restored by an Agentworks release that understands that schema. Before downgrading Agentworks,
restore a backup whose schema the older release understands; do not open newer state with the older
release first.

SQLite may leave user-only `-shm` and zero-byte `-wal` coordination files beside a selected backup
after validation or restore. This is expected: the backup database remains unchanged, valid, and
retryable. Agentworks uses an ordinary read-only open here so committed WAL content is not ignored,
and it does not race SQLite by deleting those coordination files.

## Environment Variables

Secret values are read from the operator's shell via the `env-var` backend, which follows the
convention `AW_SECRET_<UPPER_SNAKE_CASE>` derived from the secret's name. The Tailscale auth key
(secret `tailscale-auth-key`) reads from `AW_SECRET_TAILSCALE_AUTH_KEY`; a git credential's PAT
(secret `git-token-<name>`) reads from `AW_SECRET_GIT_TOKEN_<NAME>`; and so on. Override the
convention per secret via the secret's `backend_mappings` (`env-var: CUSTOM_NAME`).

Use `agw secret list` to see the exact env var name for each declared or auto-declared secret, and
`agw secret describe <name>` for the full per-secret view (origin, usages, backend mappings,
resolution preview).

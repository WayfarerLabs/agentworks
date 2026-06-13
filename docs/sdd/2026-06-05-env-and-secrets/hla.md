# Environment variables and secrets: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/env/`, `cli/agentworks/secrets/`

## Overview

Two new packages anchor this work:

- **`agentworks.secrets`**: declares the `SecretDecl`, `SecretBackendConfig`, and `SecretConfig`
  config types, the `SecretSource` protocol, the v1 source implementations (`EnvVarSource`,
  `PromptSource`), and a `SecretResolver` that batches lookups across the configured backend chain.
  Modeled in spirit on `agentworks.git_credentials`.
- **`agentworks.env`**: declares the `EnvEntry` config type (value-or-secret-ref), merge logic
  across the resource graph, the standard `AGENTWORKS_*` var producers, and a `compose_env(...)`
  helper that any shell-opening site uses to produce the final `dict[str, str]` of resolved env. The
  SSH layer accepts that dict and materializes one `-o SetEnv=K=V` argument per entry.

Both are pure Python with no Typer dependency, consistent with the typer-isolation rule. The CLI
layer (commands) calls into these packages; the manager layer composes them with the rest of the
shell-open call sites.

```text
+-------------------+      +--------------------+      +----------------+
|  config.py        |----->|  agentworks.env    |<-----| agentworks     |
|  (loads tables)   |      |  - EnvEntry         |      |  .secrets      |
|  - admin.env      |      |  - effective_env()  |      |  - SecretDecl  |
|  - vm_tpl.env     |      |  - AGENTWORKS_*     |      |  - SecretSource|
|  - ws_tpl.env     |      |  - compose_env()    |      |  - resolver    |
|  - agent_tpl.env  |      +---------+-----------+      +--------+-------+
|  - sess_tpl.env   |                |                          |
|  - [secrets]      |                |  dict[str, str]          |  resolved values
+-------------------+                v                          v
                            +--------------------------------------+
                            |   shell-opening sites                 |
                            |   compose env, hand to SSH layer:     |
                            |     target.run(cmd, env=...)          |
                            |   SSH layer materializes              |
                            |     ssh -o SetEnv=K=V user@host cmd   |
                            |   sshd (AcceptEnv *) injects into shell.|
                            +--------------------------------------+
```

## Data model

### Config types (in `config.py`)

```python
@dataclass(frozen=True)
class EnvEntry:
    """One env var declaration: either a plaintext value or a secret reference."""
    key: str
    value: str | None = None           # plaintext
    secret: str | None = None          # name of a [secrets.<name>] declaration
    # exactly one of value/secret is set

@dataclass(frozen=True)
class SecretDecl:
    name: str
    description: str
    hint: str | None = None
    # Per-backend mapping overrides, keyed by backend kind.
    # Value forms:
    #   str        -> backend's identifier for this secret (e.g. env var name, op:// URI)
    #   dict       -> structured identifier (for backends whose ID has multiple fields)
    #   False      -> opt out: skip this backend for this secret
    #   key absent -> use backend's default convention if any, else soft-skip
    backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = field(default_factory=dict)
```

Each scope that supports env adds `env: dict[str, EnvEntry] = field(default_factory=dict)`:

- `AdminConfig.env`
- `VMTemplate.env`
- `WorkspaceTemplate.env`
- `AgentTemplate.env`
- `SessionTemplate.env` (replaces the existing `env: dict[str, str] | None` with
  `dict[str, EnvEntry]`; bare string values in TOML continue to work as plaintext)

`Config` gains:

- `secrets: dict[str, SecretDecl]` (from `[secrets.*]`)
- `secret_backends: dict[str, SecretBackendConfig]` (from `[secret_backends.*]`)
- `secret_config: SecretConfig` (from `[secret_config]`)

### TOML shape

A complete, illustrative slice:

```toml
[secret_backends.env_var]
# Always available; default convention is AW_SECRET_<NAME>. No config needed.

[secret_backends.onepassword]
account = "wfscot@example.com"
vault = "Personal"

[secret_backends.prompt]
# Effective only when stdin is a TTY and the CLI is not --non-interactive.

[secret_config]
# Dual-role: enabled backends + precedence order. First-match wins.
backends = ["env_var", "onepassword", "prompt"]

[secrets.anthropic-api-key]
description = "Anthropic API key for Claude agents"
hint = "https://console.anthropic.com/settings/keys"
backend_mappings.onepassword = "op://Personal/Anthropic/key"

[secrets.github-token]
description = "GitHub PAT for repo access"
backend_mappings.env_var = "GITHUB_TOKEN"        # override AW_SECRET_<NAME> default
backend_mappings.onepassword = "op://Personal/GitHub/token"

[secrets.openai-key]
description = "OpenAI API key"
# Structured mapping form for backends whose ID has multiple fields.
backend_mappings.onepassword = { vault = "Shared", item = "OpenAI", field = "key" }

[secrets.super-sensitive-token]
description = "Force-prompt token"
backend_mappings.env_var = false     # opt out: skip even the default convention
backend_mappings.onepassword = false # opt out: only PromptSource can resolve this

[admin.env]
HTTP_PROXY = "http://proxy.example:3128"

[vm_templates.default.env]
EDITOR = "nvim"
GITHUB_TOKEN = { secret = "github-token" }

[agent_templates.claude.env]
ANTHROPIC_API_KEY = { secret = "anthropic-api-key" }
LOG_LEVEL = "info"

[session_templates.shell.env]
EDITOR = "nvim"
```

Parser accepts two shapes per key:

- Bare string: `KEY = "value"` -> `EnvEntry(key, value=...)`
- Inline table with `secret`: `KEY = { secret = "name" }` -> `EnvEntry(key, secret=...)`

Any other shape is a config error. Unknown `secret` names are a config error (validated after both
sections load).

## Merge algorithm

Exactly one of admin / agent applies to any given shell (a shell runs as one Linux user). The merge
walks low-to-high specificity, layering only the scopes that apply:

```python
def effective_env(
    *,
    admin: AdminConfig | None,             # set when the shell runs as the admin user
    vm: ResolvedVMTemplate,
    workspace: ResolvedWorkspaceTemplate | None = None,
    agent: ResolvedAgentTemplate | None = None,   # set when the shell runs as an agent
    session: ResolvedSessionTemplate | None = None,
) -> dict[str, EnvEntry]:
    """Layered merge: session > (agent | admin) > workspace > vm.

    Exactly one of `admin` / `agent` is non-None per call: the caller knows
    which Linux user the shell runs as.
    """
    assert not (admin and agent), "effective_env: pass exactly one of admin/agent"
    merged: dict[str, EnvEntry] = dict(vm.env)
    if workspace:
        merged.update(workspace.env)
    if agent:
        merged.update(agent.env)
    elif admin:
        merged.update(admin.env)
    if session:
        merged.update(session.env)
    return merged
```

Inheritance within a single template kind (e.g. `agent_templates.foo.inherits = ["base"]`) is
resolved by the existing template-resolution code, which gets a small extension: the resolved
template carries a fully merged `env` dict reflecting child-overrides-parent semantics. The
cross-scope merge above runs on already-resolved templates.

## AGENTWORKS\_\* identity vars

A pure-data producer; no I/O:

```python
@dataclass(frozen=True)
class ResourceContext:
    vm_name: str | None
    vm_host: str | None
    platform: str | None
    on_vm_user: str | None       # admin or agent username
    workspace_name: str | None = None
    workspace_dir: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    session_kind: Literal["admin", "agent"] | None = None

def agentworks_identity_env(ctx: ResourceContext) -> dict[str, str]:
    out: dict[str, str] = {}
    if ctx.vm_name:     out["AGENTWORKS_VM"] = ctx.vm_name
    if ctx.vm_host:     out["AGENTWORKS_VM_HOST"] = ctx.vm_host
    if ctx.platform:    out["AGENTWORKS_PLATFORM"] = ctx.platform
    if ctx.on_vm_user:  out["AGENTWORKS_USER"] = ctx.on_vm_user
    if ctx.workspace_name: out["AGENTWORKS_WORKSPACE"] = ctx.workspace_name
    if ctx.workspace_dir:  out["AGENTWORKS_WORKSPACE_DIR"] = ctx.workspace_dir
    if ctx.agent_name:     out["AGENTWORKS_AGENT"] = ctx.agent_name
    if ctx.session_name:   out["AGENTWORKS_SESSION"] = ctx.session_name
    if ctx.session_kind:   out["AGENTWORKS_SESSION_KIND"] = ctx.session_kind
    return out
```

These take precedence over user-defined env (and the loader emits a warning if a user attempts to
override an `AGENTWORKS_*` key).

`AGENTWORKS_NERF_HOME` is unaffected by this work; it stays in the existing system-wide profile
fragment, parallel to but separate from this scheme.

## Secret model

All secret-producing concerns implement a single `SecretSource` protocol. Interactive prompting is
just another source (`PromptSource`) that happens to interact with the operator instead of reading
from somewhere. The resolver iterates a configured chain of sources in precedence order; the first
to return a value wins.

```python
class SecretSource(Protocol):
    """A source that can produce a secret value at command time."""

    kind: str  # matches the [secret_backends.<kind>] key

    def would_attempt(self, secret: SecretDecl) -> bool:
        """Does this source ATTEMPT to resolve this secret? Determined from
        config alone, without network or vault I/O.

        - EnvVarSource: True unless backend_mappings.env_var is False.
        - OnePasswordSource: True only when backend_mappings.onepassword is
          a string or dict (no default convention for 1pw).
        - PromptSource: True (prompt always attempts if asked, modulo TTY /
          --non-interactive check at runtime).

        Used at config-load time to surface unreachable secrets and by
        `agw doctor` to show which backend would handle each secret."""
        ...

    def get(self, secret: SecretDecl) -> str | None: ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Optional batch optimization. Sources that authenticate (Vault,
        1Password CLI) override to amortize that cost across the resolve_all()
        pass. PromptSource overrides to emit all prompts in one operator
        interaction."""
        ...
```

`SecretSource` is a pure type-only protocol. The default `batch_get` (loop `.get` and drop None
values) lives on a sibling `SecretSourceBase(ABC)` with abstract `would_attempt` and `get`. Concrete
sources inherit from the ABC to pick up the default; this keeps the structural type contract clean
while still letting backends share one batch implementation. A class that does not need the default
may implement `SecretSource` structurally without inheriting from the base.

After all sources are instantiated, the loader walks every declared secret against the active chain:
a secret is **unreachable** if no source returns True from `would_attempt`. Unreachable secrets are
a config-load error (they reference values nothing in the chain can produce). Secrets where
some-but-not-all configured backends skip them are not errors; `agw doctor` surfaces those as
informational findings ("secret X has no mapping for active backend Y, will skip").

### v1 source implementations

```python
class EnvVarSource:
    """Reads from operator-side environment variables. Default convention:
    secret 'github-token' maps to env var AW_SECRET_GITHUB_TOKEN. Per-secret
    overrides via secret.backend_mappings.env_var (string for the env var name,
    or `false` to skip entirely)."""

    kind = "env_var"

    def _resolved_name(self, secret: SecretDecl) -> str | None:
        mapping = secret.backend_mappings.get("env_var")
        if mapping is False:
            return None  # explicit opt-out
        if isinstance(mapping, str):
            return mapping  # operator-supplied env var name
        return "AW_SECRET_" + secret.name.upper().replace("-", "_")  # default

    def get(self, secret: SecretDecl) -> str | None:
        from os import environ
        name = self._resolved_name(secret)
        return environ.get(name) if name else None


class PromptSource:
    """Interactive last-resort, normally the final entry in [secret_config].backends.
    Returns None when stdin is not a TTY or the CLI is in --non-interactive mode --
    the resolver then raises SecretUnavailableError naming the unsatisfied secrets.

    A future controller-process caller simply omits PromptSource from its backends
    list; the same None-then-raise path surfaces missing values as a typed error
    to the API client instead of prompting the controller."""

    kind = "prompt"

    def get(self, secret: SecretDecl) -> str | None:
        from agentworks import output
        if not output.is_interactive():
            return None
        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Override: emit all prompts in one operator interaction."""
        from agentworks import output
        if not output.is_interactive():
            return {}
        return {s.name: self.get(s) for s in secrets}
```

`output.is_interactive()` is a new helper this SDD adds to `agentworks.output`. It moves the
existing interactivity check from `cli/_app.py:is_interactive()` (which is Typer-aware and
inappropriate to import from the service layer) to `agentworks.output`, where the other prompt
helpers (`output.prompt_secret`, `output.confirm`) already live. The `--non-interactive` flag is
stored in `output` and seeded by the Typer callback at CLI entry, exactly the way the prompt state
already is.

A future `OnePasswordSource` would look like:

```python
class OnePasswordSource:
    kind = "onepassword"

    def __init__(self, config: OnePasswordBackendConfig) -> None:
        self._account = config.account
        self._vault = config.vault

    def get(self, secret: SecretDecl) -> str | None:
        mapping = secret.backend_mappings.get("onepassword")
        if mapping is False:
            return None  # explicit opt-out
        if mapping is None:
            return None  # no mapping; no default convention for 1pw (doctor surfaces this)
        ref = mapping if isinstance(mapping, str) else _build_op_ref(mapping, self._vault)
        return _shell_out("op", "read", ref, account=self._account)

    def batch_get(self, secrets): ...  # one `op` invocation for all needed at once
```

### Resolver

```python
class SecretResolver:
    def __init__(
        self,
        sources: list[SecretSource],   # ordered by [secret_config].backends precedence
        decls: dict[str, SecretDecl],
    ) -> None:
        self._sources = sources
        self._decls = decls
        self._cache: dict[str, str] = {}  # process-lifetime; CLI invocation bounded

    def required_for(self, env: dict[str, EnvEntry]) -> list[SecretDecl]:
        """Return the deduplicated list of secret declarations referenced by env."""
        ...

    def resolve_all(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch-resolve: try each source in precedence order. Each source's
        batch_get() is called once with the still-missing set; values it returns
        are cached and removed from the missing set; the next source sees only
        what is still unresolved. If a secret is still unresolved after every
        source (including PromptSource if present), raises SecretUnavailableError
        naming the unsatisfied secret and which backends were tried."""
        ...

    def render(self, env: dict[str, EnvEntry]) -> dict[str, str]:
        """Map EnvEntry dict to fully resolved {KEY: value} dict."""
        ...
```

`resolve_all` is the eager-prompting entry point. PromptSource's `batch_get` emits all prompts in
one operator interaction, preserving the "prompt once at the start" UX even though prompt is just
another source in the chain.

In-process cache means resolving the same secret twice in one command (e.g. for two sessions) hits a
backend at most once. The cache lifetime is the CLI invocation; rotation between commands picks up
the new value on the next invocation because the cache is rebuilt from scratch. A future controller
process will need to revisit cache lifetime (TTL or revocation hooks) since its process lifetime is
much longer than a single command.

### Backend configuration types

```python
@dataclass(frozen=True)
class SecretBackendConfig:
    """Connection / global config for one backend instance."""
    kind: str                      # matches [secret_backends.<kind>] key
    # Additional fields per backend kind (e.g. account, vault for onepassword).
    # Concrete subclasses (OnePasswordBackendConfig, KeychainBackendConfig, ...)
    # carry their own fields.

@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table."""
    backends: tuple[str, ...]      # dual-role: active set + precedence order
```

`backends` is stored as a `tuple` rather than a `list` so the dataclass stays
`frozen=True`-compatible (hashable, immutable) without bespoke copy logic.

The loader instantiates one `SecretSource` per kind named in `backends`, in order, and hands the
list to the resolver. Backends declared in `[secret_backends.*]` but absent from `backends` are
inert (the source instance is never created).

## Env transport: SSH SetEnv

Env injection is a property of the SSH connection, not a property of the shell command. The CLI
composes a `dict[str, str]` of effective env (user-defined + per-context identity vars) and hands it
to the SSH layer; the SSH layer materializes one `-o SetEnv=KEY=VALUE` argument per entry. On the
remote side, sshd accepts the vars (per the `AcceptEnv *` directive deployed in Phase 4; see
`new-adrs/sshd-accept-env-wildcard.md`) and places them into the user's shell environment before the
shell is `exec`d. This happens inside sshd itself (its session-spawn code path), not via the
`pam_env` PAM module.

Sites compose like this:

```python
identity = per_context_identity_env(ctx)
user_env = resolver.render(effective_env(admin=..., vm=..., ...))
full_env = {**user_env, **identity}  # identity wins (FRD R1)
target.run(command, env=full_env)    # SSH layer materializes -o SetEnv=K=V args
```

No `build_export_block`. No prelude composition. No "outer shell vs login shell" question. The SSH
protocol carries the vars; the CLI's responsibility ends at handing the env dict to the SSH layer.

For tmux contexts specifically, env flows through two paths in tandem:

1. **SetEnv at the SSH layer** delivers env to the user's shell that runs `tmux new-session` (or
   `tmux split-window`). When tmux is creating a fresh server (no existing server on the chosen
   socket), the server inherits this env. Per-session sockets (Phase 3) ensure every admin and agent
   session creates a fresh server, so SetEnv-delivered env always reaches the pane.
2. **`tmux new-session -e KEY=VAL` / `tmux split-window -e KEY=VAL`** flags. Tmux's own
   per-session-environment table carries these to every pane in the session. Used belt-and-
   suspenders for per-context vars that should win on collision and for `tmux split-window` adds on
   a server that's already running.

Even with per-session sockets making the SetEnv path sufficient for new-session paths in steady
state, the `-e` flags are kept as defense-in-depth: they are load-bearing for `tmux split-window`
(the console add-shell pane path attaches to an existing console tmux server whose env was frozen at
server start; SetEnv on the new SSH connection wouldn't reach the new pane) and they survive a
future refactor that reintroduces shared servers for any path. The dual-channel design is the
explicit design intent, not a redundant artifact.

### Identity vars on the VM (independent of SetEnv)

VM-stable identity (`AGENTWORKS_VM`, `AGENTWORKS_VM_HOST`, `AGENTWORKS_PLATFORM`) and per-user
identity (`AGENTWORKS_USER`) live in profile fragments on the VM, NOT in the SetEnv payload:

- **System-wide fragment**: `/etc/profile.d/agentworks-identity.sh` plus a matching block in
  `/etc/zsh/zprofile` (zsh does not source `/etc/profile.d/*` by default). Written by VM init.
- **Per-user fragment**: `~/.agentworks-profile.sh` (one per Linux user on the VM). Holds
  `AGENTWORKS_USER`.

These fragments serve operators who reach the VM via raw SSH (`ssh awvm--<vm>`) outside agentworks:
their login shell sources the fragments and sees the identity vars without agentworks doing
anything. Per-context identity vars (`AGENTWORKS_SESSION` etc.) do not have a sensible static value;
they only enter via SetEnv when agentworks opens the shell.

### Sudo boundaries

The console add-shell pane path uses `sudo --login -u <agent>` to switch a tmux pane's process to
the agent's user (the tmux server runs as admin; the pane needs to run as the agent). Sudo strips
env by default. VM init writes `/etc/sudoers.d/50-agentworks-env-keep` with
`Defaults env_keep += "AGENTWORKS_* AW_*"` so agentworks-managed vars survive the boundary;
non-agentworks vars from operator env tables are stripped at the sudo crossing (intended; the
operator-defined vars were scoped to the SSH session, not to delegated subprocesses).

The admin-shell windows in the console paths historically wrapped in `sudo su --login admin`. Post
FRD R1 the SSH user IS the admin user, so the sudo was a no-op user-switch that wiped env for no
benefit. Phase 3 removes it.

## Shell-opening surfaces

Each site composes the effective env from the appropriate context layers and hands the dict to the
SSH layer (`ExecTarget.run(env=...)` / `interactive(target, command, env=...)`) which materializes
`-o SetEnv=K=V` args on the SSH command line. For tmux sites the env is additionally passed via
`tmux new-session -e K=V` / `tmux split-window -e K=V` so it lands in tmux's session-environment
table. The site set, drawn from the FRD R5 propagation table:

| Module / function                                                         | Context layers (admin shells)      | Context layers (agent shells)      |
| ------------------------------------------------------------------------- | ---------------------------------- | ---------------------------------- |
| `vms/initializer.*` (provisioning, vm reinit)                             | admin + vm                         | n/a                                |
| `agents/manager._create_agent_on_vm` Phase 2 (agent self-configure)       | n/a                                | vm + agent                         |
| `sessions/tmux.create_session` (admin-mode)                               | admin + vm + workspace + session   | n/a                                |
| `sessions/tmux.create_session` (agent-mode)                               | n/a                                | vm + workspace + agent + session   |
| `sessions/console.*` / `sessions/multi_console.*` (per pane / per window) | admin + vm (+ workspace if scoped) | vm + workspace + agent (+ session) |
| `agents/manager.exec_agent` / `shell_agent`                               | n/a                                | vm + agent (+ workspace if scoped) |
| `vms/manager.exec_vm` / `shell_vm`                                        | admin + vm                         | n/a                                |

The two-phase split in `_create_agent_on_vm` from the direct-target-user-SSH SDD interacts cleanly
with this work: Phase 1 (admin bootstrap) runs over `admin_target` and uses the admin env merge;
Phase 2 (agent self-configure) runs over `agent_target` and uses the agent env merge. The two phases
never share env.

Each site is a small refactor: collect context, build the env via the resolver, pass to the SSH
layer via the `env=` kwarg. What differs across sites is which scopes feed `compose_env`.

Attach surfaces (`session attach`, `console attach`) do NOT appear in this table. They join existing
shell processes and inherit those processes' create-time env. See FRD R5 "Attach inherits
create-time env."

### Identity vars on the VM

Two write surfaces, chosen by whether the value varies per Linux user:

- **System-wide fragment** (new): written by `vms/initializer.py` at the SAME two locations the
  existing `AGENTWORKS_NERF_HOME` install uses (`/etc/profile.d/agentworks-identity.sh` AND an
  appended block in `/etc/zsh/zprofile`, because zsh does not source `/etc/profile.d/*` by default).
  Contains the truly VM-stable identity vars: `AGENTWORKS_VM`, `AGENTWORKS_VM_HOST`,
  `AGENTWORKS_PLATFORM`. Any shell on the VM, including ones started outside agentworks (e.g. an
  operator landing via the `awvm--<vm>` alias), sees these vars.
- **Per-user fragment** (existing, extended): `~/.agentworks-profile.sh`, written per Linux user by
  the existing `_write_agentworks_profile`. Gains `AGENTWORKS_USER` (per-user value). Written for
  admin during VM init and for each agent during Phase 2 of `agents/manager._create_agent_on_vm`.
  Reinit-idempotent.

Per-context vars (`AGENTWORKS_WORKSPACE`, `AGENTWORKS_WORKSPACE_DIR`, `AGENTWORKS_AGENT`,
`AGENTWORKS_SESSION`, `AGENTWORKS_SESSION_KIND`) are set inline at shell-open time because their
values depend on the context, not the user or VM.

User-defined env (plaintext or secret) is NEVER cached on the VM. It is always computed at command
time and injected inline at the shell-open site. Single authoritative source (config + CLI
environment); no stale-cache surprises when config changes between reinit and the next shell.

## Eager prompting flow

### Static vs. dynamic filters

Filters on commands divide into two classes:

- **Static filters** resolve from config and the DB alone, without observing remote state. Examples:
  `--vm vm1,vm2`, `--workspace ws1`, `--agent claude`, the `--admin` mode flag, and the command's
  positional targets. A bare-name selector like `agw session restart s1` is static (the session row
  and its template chain come from config + DB). The command's positional target itself counts as a
  static filter when it's a name.
- **Dynamic filters** require observing the system to determine inclusion. Examples:
  `session restart --all-stopped` (needs SSH probes per VM to know which sessions are stopped); any
  filter keyed on session status, VM liveness, or other remote state.

**Eager prompting consults static filters only.** The candidate target set used to compute the union
of needed secrets is the set of resources matching the command's static filters, _unfiltered_ by any
dynamic predicate. Dynamic filters apply later, in the execution phase, after prompting has already
completed.

This is what makes the immediacy contract achievable. Computing the secret union from config + DB
alone is sub-second for any command; computing it precisely (including dynamic filters) would
require probes that can block for minutes on cold-start cloud VMs.

The tradeoff: for commands that combine static and dynamic filters, the resolver may prompt for
secrets the command never actually consumes (the dynamic filter would have eliminated the target).
See the FRD's `--all-stopped` example. In `--non-interactive` mode this can manifest as a failure on
a secret that would not have been consumed, with the recovery being a narrower static filter.

### Flow

```text
agw <command> ...
  |
  v
load config (admin.env, vm_tpl.env, ws_tpl.env, agent_tpl.env, sess_tpl.env, [secrets])
  |
  v
resolve candidate target set using STATIC filters only
   (positional targets + --vm/--workspace/--agent/--admin/...; no SSH, no liveness)
  |
  v
compute the union of effective envs across all candidate targets
  |
  v
extract all referenced SecretDecls
  |
  v
resolver.resolve_all(decls)  # prompts up-front for any not in CLI env
  |
  v
proceed with command execution
  |
  apply dynamic filters here (probe VM state, filter to --all-stopped, etc.)
  |
  for each shell opened:
    |
    v
    compose_env(resolver, ctx, vm=..., admin=..., ...) -> dict[str, str]
    |
    v
    pass to the SSH layer via env= kwarg; SSH layer adds -o SetEnv=K=V args
```

For commands with a single, obvious target (e.g. `session create s1 -t claude`), the candidate set
has one entry. For broader commands (`session restart --all --vm vm1,vm2`), the candidate set spans
all matching targets.

Manager-layer functions declare their candidate set via the same mechanism they already use to load
targets (via `db.list_sessions`, etc., with only the static filters applied). A new
`secrets_needed_for(targets, config)` helper walks the chain.

## CLI changes

### `agw env show`

```text
agw env show [--vm NAME] [--workspace NAME] [--agent NAME] [--session NAME] [--reveal-secrets]
```

- At least one context flag is required. Without one, the command fails with a message explaining
  that an env table is always relative to some resource scope.
- Auto-resolves the chain from a single named entity's DB row: `--session s1` infers the workspace,
  agent, and VM; `--workspace ws1` infers the VM; `--agent a1` infers the VM. Manually-passed flags
  override the inferred chain.
- Prints the effective env in precedence-sorted order, annotating each row with the winning scope.
- Plaintext entries show their actual values (already cleartext in config; no disclosure).
- Secret-backed entries show as `<from secret: NAME>` by default.
- `--reveal-secrets`: resolves secret-backed entries through the active backend chain and prints the
  values. Without this flag, `env show` never consults any backend for secret-backed entries (no env
  reads, no prompts).

### `agw doctor` additions

- "Secrets" section: lists declared secrets, marks each as "available in CLI env" or "would prompt."
- "Env" section: lists conflicts (same key set at multiple scopes), unused secret declarations,
  broken `secret =` references.

### No new flags on existing commands

Per scope discussion: no `--env KEY=VAL` overrides. Operators set CLI env or edit config.

## Validation and error handling

- Loader rejects entries that don't match the bare-string or `{ secret = ... }` shape, with a
  pointer to the offending key/scope.
- Loader rejects `secret = "..."` references to undeclared secrets at config load time (after
  parsing all scopes).
- Loader emits a config warning for user-defined keys beginning with `AGENTWORKS_`.
- Resolver raises a typed `SecretUnavailableError` (subclass of `agentworks.errors.AgentworksError`)
  when no source in the active chain can satisfy a secret. CLI renders this with the unsatisfied
  secret name(s) and which backends were tried (the error's own attributes).
- `compose_env` is intentionally not given access to raw secrets vs plaintext info; by the time it
  returns, the dict is already a flat `{KEY: value}` of resolved strings. Sites are responsible for
  not logging the dict.

## Logging and disclosure controls

- `agentworks.output.detail("...")` and similar may print env _keys_ but never _values_.
- `agw env show` defaults to redaction (`<from secret: ...>`); explicit `--reveal-secrets` is the
  only path that prints secret values to a terminal.
- Env values transported via SSH SetEnv ride the SSH protocol's environment channel, not the SSH
  command-line. On the VM side they are NOT visible to anyone reading `ps` (sshd places them in the
  spawned shell's environment before exec, not via a `bash -c "export ..."` step). On the operator's
  machine the SetEnv args ARE on the local `ssh` invocation's command line and visible to `ps -e` on
  that machine, which is consistent with the trust-anchor analysis in `cli-side-secret-injection.md`
  (the operator workstation is the trust anchor; processes on it can already read the secrets the
  CLI would otherwise feed to a remote shell).

## DB schema impact

None. Env and secrets are derived from config at command time. No persistence in `agentworks.db`.

## Interaction with existing systems

### Existing `session_templates.*.env`

Today: `dict[str, str]` plaintext only. After: `dict[str, EnvEntry]`. Bare string TOML values
continue to work (the loader maps `KEY = "v"` to `EnvEntry(key, value="v")`). Inline-table secret
references are the new addition.

This is a _schema extension_, not a breaking change for plaintext users.

### Existing `git_credentials`

Untouched by this SDD. The mechanism in `agentworks.git_credentials.base.GitCredentialProvider`
remains the way git tokens are sourced. Future iteration (anticipated soon per the user) folds git
credentials into the general `Secret` mechanism by:

1. Defining a `git-credential` secret kind that produces a token (existing prompt UX preserved).
2. Adding a synthesized step that writes `~/.git-credentials` from the resolved secrets.
3. Migrating existing `git_credentials.<name>` config to `[secrets.<name>]` + an
   `[admin.git_credentials]` or equivalent mapping table that names which secrets feed git.

This is out of scope here; noted to ensure the abstraction is shaped to accommodate it.

### Existing `AGENTWORKS_NERF_HOME`

Set by `vms/initializer.py` in `/etc/zsh/zprofile`. Unchanged. Lives parallel to the new scheme.

## Phasing (for the plan)

The plan will phase the work, but the full design above is the target. Anticipated shape:

1. **Foundations**: `agentworks.secrets` package, `[secrets]` / `[secret_backends]` /
   `[secret_config]` config sections, `SecretDecl` / `SecretBackendConfig` / `SecretConfig` types,
   `SecretSource` protocol, `EnvVarSource`, `PromptSource`, `SecretResolver`. No consumers yet.
2. **Env model**: `agentworks.env` package, `EnvEntry`, config sections at all five scopes,
   `effective_env()`, `agentworks_identity_env()` (and per-context / VM-stable / per-user subset
   helpers), `compose_env()`. Migrate existing `session_templates.*.env` parsing to the new type
   (plaintext-compatible).
3. **SSH SetEnv pivot + session/console wiring**: thread `env=` kwarg through `agentworks.ssh`
   (materialized as `-o SetEnv=K=V` args), wire `sessions/manager`, `sessions/tmux`,
   `sessions/console.*`, `sessions/multi_console.*` to compose env and pass to the SSH layer. Switch
   admin sessions to per-session sockets (mirror agent mode) so each session creates a fresh tmux
   server that inherits the SSH-delivered env. Drop the redundant `sudo su --login admin` in console
   paths.
4. **Provisioning + agent setup wiring**: thread context through `vms/initializer.*` and
   `agents/manager.*`. Write VM-stable identity to the profile fragment, deploy `AcceptEnv *` sshd
   config, deploy `env_keep += "AGENTWORKS_* AW_*"` sudoers config.
5. **CLI**: `agw env show`, `agw doctor` additions, sample-config + docs.
6. **Eager prompting orchestration**: hook `SecretResolver.resolve_all` into command entry for
   anything that opens a shell; thread the resolver through manager calls.

Each phase ends at a green CI and a usable intermediate state.

## Design decisions

### Unified value-or-secret schema

A single env table per scope, where each entry is either plaintext or a secret reference, is
strictly simpler than parallel `[env]` and `[secret_env]` tables. The mental model ("this is the env
that gets exported; some entries draw their value from a vault") matches how operators think about
it.

The inline-table form `{ secret = "name" }` is TOML-idiomatic and unambiguous. String reference
syntaxes like `"$secret:name"` were considered and rejected: they introduce a parser, conflict with
literal `$` in legitimate values, and lose IDE/lint support.

### Closer-scope-overrides, with implicit inheritance

`session > (agent | admin) > workspace > vm`. A higher-specificity scope replaces lower-specificity
entries by key; non-overridden entries inherit. This matches operator intuition (the session
template is the most specific declaration available, so it wins) and matches how other agentworks
template fields already compose.

No list-append semantics (no automatic `PATH = "...:${PATH}"` interpretation) keeps the merge
deterministic and avoids shell-expansion-in-config surprises. Operators needing concatenation can
write the full string or use profile mechanisms.

### Eager prompting

Computing the full set of needed secrets up front and prompting once before any work starts is the
single most important UX property. The alternative (prompting lazily as work proceeds) creates
"started a 10-minute batch, walked away, came back to a prompt" failure modes. Git's credential flow
has this same property and is well-loved for it.

The cost is that the manager layer must declare its target set early. This is already the pattern
for most commands (load targets, then act), so it falls out naturally.

### No persistence

Prompted secrets live only in process memory. Operators wanting persistence already have it via
their personal vault (1Password, keychain, etc.): they export `AW_SECRET_<NAME>` and agentworks
finds it. Reimplementing vault storage inside agentworks adds little value and a lot of security
surface. This matches the existing `GIT_CREDENTIALS_*` model.

### Uniform SecretSource protocol with prompt as one source among many

Every backend (env var, prompt, future 1Password / keychain / vault) implements the same
`SecretSource` protocol. The resolver walks a configured chain in precedence order and the first
source to return a value wins. Prompting is just `PromptSource` sitting at the end of the chain by
convention; it returns `None` when stdin is not a TTY or `--non-interactive` is set, and the
resolver then raises `SecretUnavailableError`.

Why one protocol instead of separating "sources" from "fallback":

- A future controller-process caller composes its own chain without `PromptSource`. The
  no-value-found path naturally surfaces as a typed error to API clients, exactly as the CLI's
  non-interactive path does today. No special-casing.
- Adding new sources doesn't require touching the resolver: they just go into the chain.
- `batch_get` lets each source decide its own batching strategy. `PromptSource.batch_get` emits all
  prompts in one operator interaction; `OnePasswordSource.batch_get` issues one `op` call for the
  whole batch; `EnvVarSource.batch_get` just loops the dict. The resolver doesn't need to know.

### Per-secret backend mappings (not per-backend)

Each `[secrets.<name>]` block carries its own `backend_mappings` table, keyed by backend kind. The
alternative (per-backend `[secret_backends.<kind>].mappings.<secret>` tables) was considered and
rejected: secret adds are more frequent than backend adds, and the operator's primary "where does
this secret come from?" question is answered from one block under per-secret mappings; under
per-backend mappings it would require a scan across all backend declarations. Adding a new backend
to a 30-secret repo is mitigated by per-backend default conventions (secrets that use the default
need no per-secret config) and by `agw doctor` surfacing secrets that have no mapping for a newly
configured backend.

Default name-to-identifier conventions live with the source implementation. `EnvVarSource` derives
`AW_SECRET_<NAME>`; backends without a sensible default (1Password, vault, etc.) soft-skip secrets
that have no mapping. Operators opt out of a backend for a specific secret with
`backend_mappings.<backend> = false`. A secret with no resolvable backend (every active source is
`false` or no-default-no-mapping) is a config-time error.

### Identity vars are not opt-out

`AGENTWORKS_*` vars are always set when their scope applies, with no toggle. Tools and agents inside
sessions should be able to rely on them being present. Operators can override them in user env (with
a warning), but the default contract is "these are always there."

### Workspace dir as identity

`AGENTWORKS_WORKSPACE_DIR` is included alongside `AGENTWORKS_WORKSPACE` because it is the more
useful value for tooling (e.g. `cd "$AGENTWORKS_WORKSPACE_DIR"`). Both are cheap to compute: the
workspace dir is derived from `paths.vm_workspaces` and the workspace name.

### Future: folding git credentials

The `Secret`/`SecretSource` abstraction is intentionally shaped so that `agentworks.git_credentials`
providers can become specializations of secret sources in a later iteration, with
`~/.git-credentials` writing becoming a secret-consumer just like env export. Doing it as a
separate, follow-up SDD keeps this one focused and avoids a destabilizing migration.

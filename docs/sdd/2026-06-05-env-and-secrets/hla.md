# Environment variables and secrets: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/env/`, `cli/agentworks/secrets/`

## Overview

Two new packages anchor this work:

- **`agentworks.secrets`**: declares the `Secret` config type, the `SecretSource` protocol, an
  `EnvVarSource` (the v1 source), and a `PromptFallback` (CLI-only interactive last-resort). Also
  exposes a `SecretResolver` that batches lookups and prompts up front. Modeled on
  `agentworks.git_credentials`.
- **`agentworks.env`**: declares the `EnvEntry` config type (value-or-secret-ref), merge logic
  across the resource graph, the standard `AGENTWORKS_*` var producers, and a single
  `build_export_block(...)` that returns the `export KEY=...` shell prelude any shell-opening site
  can prepend.

Both are pure Python with no Typer dependency, consistent with the typer-isolation rule. The CLI
layer (commands) calls into these packages; the manager layer composes them with the rest of the
shell-open call sites.

```text
+-------------------+      +--------------------+      +----------------+
|  config.py        |----->|  agentworks.env    |<-----| agentworks     |
|  (loads tables)   |      |  - EnvEntry         |      |  .secrets      |
|  - admin.env      |      |  - merge()          |      |  - Secret      |
|  - vm_tpl.env     |      |  - AGENTWORKS_*     |      |  - SecretSource|
|  - ws_tpl.env     |      |  - build_export...  |      |  - resolver    |
|  - agent_tpl.env  |      +---------+-----------+      +--------+-------+
|  - sess_tpl.env   |                |                          |
|  - [secrets]      |                |  effective env           |  resolved values
+-------------------+                v                          v
                            +--------------------------------------+
                            |   shell-opening sites                 |
                            |   - sessions/tmux.create_session      |
                            |   - sessions/console add-* surfaces   |
                            |   - sessions/multi_console panes      |
                            |   - vms/initializer provisioning      |
                            |   - agents/manager setup              |
                            |   - exec / admin-shell helpers        |
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
```

Each scope that supports env adds `env: dict[str, EnvEntry] = field(default_factory=dict)`:

- `AdminConfig.env`
- `VMTemplate.env`
- `WorkspaceTemplate.env`
- `AgentTemplate.env`
- `SessionTemplate.env` (replaces the existing `env: dict[str, str] | None` with
  `dict[str, EnvEntry]`; bare string values in TOML continue to work as plaintext)

`Config` gains `secrets: dict[str, SecretDecl]`.

### TOML shape

```toml
[secrets.anthropic-api-key]
description = "Anthropic API key for Claude agents"

[admin.env]
HTTP_PROXY = "http://proxy.example:3128"

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

    Exactly one of `admin` / `agent` should be non-None per call. The caller
    decides based on which Linux user the shell will run as.
    """
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

Sources and prompting are distinct concerns. A **source** answers "what value does this secret have,
if you already know"; a **prompt fallback** asks the operator when no source knows. Today's v1 has
one source (env vars) and one fallback (interactive prompt), but they compose independently so
additional sources can land without touching the prompt path, and a future non-interactive caller
(e.g. a controller process) can simply omit the fallback.

```python
class SecretSource(Protocol):
    """A source that can produce a secret value at command time."""

    def get(self, secret: SecretDecl) -> str | None: ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Optional batch optimization. Default impl loops .get(). Sources that
        authenticate (Vault, 1Password CLI) override to amortize that cost across
        the resolve_all() pass."""
        return {s.name: v for s in secrets if (v := self.get(s)) is not None}


class EnvVarSource:
    """Reads from operator-side environment variables. The v1 source."""

    def env_var_name(self, name: str) -> str:
        return "AW_SECRET_" + name.upper().replace("-", "_")

    def get(self, secret: SecretDecl) -> str | None:
        from os import environ

        return environ.get(self.env_var_name(secret.name))


class PromptFallback:
    """Interactive last-resort. NOT a SecretSource: sources return existing values;
    a fallback interacts with the operator to produce one. The resolver invokes the
    fallback only when stdin is a TTY and the CLI is not --non-interactive.

    A future controller-process caller omits the fallback entirely; missing values
    surface as a typed error to the API client instead of prompting the controller."""

    def prompt(self, secret: SecretDecl) -> str:
        from agentworks import output

        label = f"Secret '{secret.name}': {secret.description}"
        return output.prompt_secret(label, hint=secret.hint)
```

The protocol shape leaves room for future `KeychainSource`, `OnePasswordSource`, `VaultSource`, etc.
The resolver tries sources in order, then invokes the fallback (if configured) for any remaining
unknowns:

```python
class SecretResolver:
    def __init__(
        self,
        sources: list[SecretSource],
        decls: dict[str, SecretDecl],
        *,
        prompt_fallback: PromptFallback | None = None,
    ) -> None:
        self._sources = sources
        self._decls = decls
        self._fallback = prompt_fallback
        self._cache: dict[str, str] = {}  # process-lifetime; CLI invocation bounded

    def required_for(self, env: dict[str, EnvEntry]) -> list[SecretDecl]:
        """Return the deduplicated list of secret declarations referenced by env."""
        ...

    def resolve_all(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch-resolve: try sources in order, then prompt for the rest (one prompt
        session, only if a fallback is configured). Raises SecretUnavailableError
        when no source has the value and no fallback is configured (e.g. non-interactive
        CLI, or future controller-process caller)."""
        ...

    def render(self, env: dict[str, EnvEntry]) -> dict[str, str]:
        """Map EnvEntry dict to fully resolved {KEY: value} dict."""
        ...
```

`resolve_all` is the eager-prompting entry point. It groups remaining-after-sources secrets and
emits all prompts before returning, so command bodies that call `resolver.render(env)` later get a
fully-populated dict back synchronously.

In-process cache means resolving the same secret twice in one command (e.g. for two sessions)
prompts once. The cache lifetime is the CLI invocation; rotation between commands picks up the new
value on the next invocation because the cache is rebuilt from scratch.

## Building the export block

A single helper produces the shell prelude any shell-opening site can prepend:

```python
def build_export_block(env: dict[str, str]) -> str:
    """Return 'export KEY=value && export KEY2=value2 && ...' with proper quoting."""
    parts = [f"export {k}={shlex.quote(v)}" for k, v in env.items()]
    return " && ".join(parts)
```

Sites compose like this:

```python
identity = agentworks_identity_env(ctx)
user_env = resolver.render(effective_env(admin=..., vm=..., ...))
full_env = {**user_env, **identity}  # identity wins
prelude = build_export_block(full_env)
command = f"{prelude} && {original_command}" if prelude else original_command
```

## Shell-opening surfaces

Each site builds an env prelude from the appropriate context layers and prepends it to the shell
payload. The site set, drawn from the FRD R5 propagation table:

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
never share a prelude.

Each site is a small refactor: collect context, build the env via the resolver, prepend the prelude.
The prelude shape is identical across sites; what differs is which scopes feed in.

Attach surfaces (`session attach`, `console attach`) do NOT appear in this table. They join existing
shell processes and inherit those processes' create-time env. See FRD R5 "Attach inherits
create-time env."

### VM-stable identity in the profile fragment

`vms/initializer._write_agentworks_profile` learns to write the VM-scoped identity vars
(`AGENTWORKS_VM`, `AGENTWORKS_VM_HOST`, `AGENTWORKS_PLATFORM`, `AGENTWORKS_USER`) so any shell on
the VM (even one started outside agentworks) sees them. These are reinit-time-stable values, fine to
cache on disk.

User-defined env (plaintext or secret) is NOT cached in the profile fragment. It is always computed
at command time and injected inline at the shell-open site. This keeps a single authoritative source
for env values (config + CLI environment) and avoids stale-cache surprises when config changes
between reinit and the next shell.

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
    build_export_block(identity + resolver.render(effective_env(ctx)))
    |
    v
    prepend to the shell command
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
agw env show (--vm NAME | --workspace NAME | --agent NAME | --session NAME)
             [--reveal-secrets]
```

- At least one context flag is required. Without one, the command fails with a message explaining
  that an env table is always relative to some resource scope.
- Resolves the context implied by the flags. Omitted flags = scope not in context.
- Prints the effective env in precedence-sorted order, annotating each row with the winning scope.
- Plaintext entries show their actual values (already cleartext in config; no disclosure).
- Secret-backed entries show as `<from secret: NAME>` by default.
- `--reveal-secrets`: resolves secret-backed entries through the normal env-or-prompt path and
  prints the values. Without this flag, `env show` never reads operator env for secrets and never
  prompts.

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
  when a non-TTY environment cannot satisfy a secret. CLI renders this with the env var name to set.
- `build_export_block` is intentionally not given access to raw secrets vs plaintext info; by the
  time it runs, the dict is already a flat `{KEY: value}`. Sites are responsible for not logging the
  dict.

## Logging and disclosure controls

- `agentworks.output.detail("...")` and similar may print env _keys_ but never _values_.
- `agw env show` defaults to redaction (`<from secret: ...>`); explicit `--reveal-secrets` is the
  only path that prints secret values to a terminal.
- The prelude that is sent over SSH is necessarily on the SSH command line, which is visible to
  anyone who can read `ps` on the VM during the brief window of process start. This is a known
  tradeoff for env-var-shaped credentials and matches the existing behavior of the session-template
  `env`. Documented in operator docs; not a new exposure.

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

1. **Foundations**: `agentworks.secrets` package, `[secrets]` config section, `Secret`,
   `SecretSource`, `EnvOrPromptSource`, `SecretResolver`. No consumers yet.
2. **Env model**: `agentworks.env` package, `EnvEntry`, config sections at all five scopes,
   `effective_env()`, `agentworks_identity_env()`, `build_export_block()`. Migrate existing
   `session_templates.*.env` parsing to the new type (plaintext-compatible).
3. **Session/console wiring**: replace inline `export` in `sessions/manager._build_session_command`,
   wire to `sessions/console.*` and `sessions/multi_console.*`.
4. **Provisioning + agent setup wiring**: thread context through `vms/initializer.*` and
   `agents/manager.*`. Write VM-stable identity to the profile fragment.
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

`session > agent > workspace > vm > admin`. A higher-specificity scope replaces lower-specificity
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

### Pluggable source interface, single source in v1

`SecretSource` is a Protocol with one implementation (`EnvVarSource`) in v1. Sources and the prompt
fallback are independent: a source returns existing values, a fallback interacts with the operator
to produce one. Defining both shapes now is cheap and lets later iterations add sources (keychain,
1Password CLI, Vault) without touching the prompt path, and lets future non-CLI callers (notably a
controller process that may replace this CLI's secret-loading role) omit the fallback entirely.

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

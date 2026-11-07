# Runtime context templates: architecture

Status: proposed design. The [FRD](frd.md) is a draft; its environment-reference question remains
open. This artifact describes a concrete context-only increment and evaluates the additional env
lookup feature without treating that feature as approved.

## Boundaries

Provide a shared pure renderer and a finite context vocabulary. The renderer accepts authored text,
explicit context values, and a diagnostic field location. It does not read configuration, the
process environment, secrets, the filesystem, or a VM. It has no expression engine or extension
registry. Reuse follows from callers supplying context and choosing which fields to render.

Use the existing `ResourceContext` model as the starting point for shared runtime facts rather than
creating a competing identity model. Separate its conversion to placeholder values from identity env
production. Context assembly remains with the lifecycle or command that knows the target.

## Context catalogue

| Placeholder      | Source                                 | Availability                                   |
| ---------------- | -------------------------------------- | ---------------------------------------------- |
| `user_name`      | Selected Linux username                | Every supported env composition                |
| `user_home_dir`  | Managed home of that username          | Every supported env composition                |
| `vm_name`        | Selected VM row or planned VM identity | Every supported env composition                |
| `agent_name`     | Selected agent resource name           | Agent execution and agent setup                |
| `workspace_name` | Selected workspace identity            | Workspace-rooted execution and workspace setup |
| `workspace_dir`  | Managed workspace root                 | Same as `workspace_name`                       |
| `session_name`   | Selected or planned session identity   | Session workload execution                     |

A declaration's scope does not limit which names it can contain. The actual consumer's context
determines availability. Thus a VM template can contain `user_home_dir` for multiple consuming
users, but a winning `session_name` reference cannot be used during VM setup.

The code currently provisions managed homes under `/home/<username>`. Centralize that existing rule
in an ordinary helper shared with provisioning and setup. `ResourceContext` carries the explicit
resulting home path, so the renderer never derives it from the agent resource name or reads local
`HOME`. This effort does not promise support for manually relocating managed accounts' homes without
updating their managed model. Supporting arbitrary home locations would need a separate
source-of-truth change, not a hidden SSH probe during inspection.

## Expansion sequence

1. Decode and validate declarations with existing schema rules.
2. Resolve template inheritance and stored instance overlays.
3. Determine the consuming identity and any workspace/session context.
4. Merge env scopes with the current precedence rules.
5. Render each winning plaintext value once against that context. Leave secret references intact.
6. Fill secret-backed values from the operation's existing eager resolution and apply existing
   consumer-specific line-safety checks.
7. Apply managed identity env precedence and deliver literal env strings through the transport.

Secret collection continues to depend on the same winning secret-reference map. Rendering does not
introduce a new secret resolver or require expanding secret names. Context failures should be
preflighted before secret prompts and mutations once the needed context is known; eager resolution
may still occur earlier in existing orchestration, but should not become necessary to validate
plaintext placeholders.

Retain scope provenance for inspection. A common per-entry plaintext renderer serves both
`compose_env` and `show_env`; inspection must not route through a function that requires resolving
redacted secrets. `env show` previews the managed context, not a live process's environment or the
effects of user shell startup files.

## Invocation coverage

| Invocation                   | Consuming user                     | Additional context                              |
| ---------------------------- | ---------------------------------- | ----------------------------------------------- |
| VM shell/exec                | VM admin                           | Optional selected workspace                     |
| Agent shell/exec             | Agent's Linux user                 | Agent; optional authorized workspace            |
| Session create/start/restart | Session's selected admin or agent  | Workspace and session; agent when applicable    |
| Console extra shell pane     | Pane's selected admin or agent     | Workspace and agent when applicable; no session |
| VM/admin harness setup       | VM admin                           | No workspace, agent, or session                 |
| Agent harness setup          | Agent's Linux user                 | Agent; no workspace or session                  |
| Workspace harness setup      | VM admin                           | Workspace; no agent or session                  |
| Offline `env show`           | Selected agent, otherwise VM admin | Selected/inferred workspace and session         |

Audit every `compose_env` consumer, including setup, rather than changing only session launch.
Native transports retain their existing env-delivery limitations; interpolation does not add new
transport capabilities.

## Commands and literal data

Existing manager interpolation applies to the full command returned by any integration, not just
`shell`. Integrations protect prompt and profile data using `quote_literal_argv`; some `extra_args`
intentionally participate in interpolation. Preserve those behaviors during migration.

Use the common parser and context catalogue at current command sites. Commands remain
operator-authored shell programs: a placeholder is textual substitution, not an argv boundary or
automatic shell quoting. Paths with spaces require ordinary shell quoting in command source. Do not
pass secret or arbitrary ambient-environment values through this command-source renderer. The
implementation must prove literal harness fields still arrive literally, including brace text,
quotes, dollar signs, and newlines. Future argv-oriented consumers render each argument as data
before quoting; they do not interpolate a completed shell program.

## Environment reference decision

Context and environment references answer different questions:

- `user_home_dir` asks for a managed target fact, available before a target shell exists.
- A workstation reference asks for a value inherited by the `agw` process.
- A target reference asks for a value in a particular remote execution environment at a specified
  phase. Exported variables and unexported shell-local variables are distinct capabilities.

Recommend the context increment first. Keep source-qualified forms such as
`{{workstation_env:NAME}}` and `{{target_env:NAME}}` as design candidates only. Do not install
placeholder resolvers, reserve working aliases, or add a remote probing framework for them now. Do
not accept `{{$$NAME}}` as a synonym while the source and phase remain unresolved.

Before including environment references, settle all of these together:

1. Source: workstation process, target exported environment, or shell-local variables.
2. Phase: baseline process environment versus login/interactive startup, and whether lookup occurs
   before or after managed env overrides. `PATH` referring to its baseline must not recursively
   refer to its own final assignment.
3. Missing/empty policy: distinguish an unset value from an explicitly empty value; avoid silent
   empty replacement. Decide whether a literal default is needed for the concrete use case.
4. Exposure: arbitrary env values may be secrets. Define default redaction, explicit reveal, error
   sanitization, and avoidance of values in logs or persisted desired state.
5. Inspection: offline output cannot promise an unobserved live value. Any live read needs an
   explicit operation and normal target access controls.
6. Execution: retrieve only requested exported values, never evaluate authored template fragments as
   shell source, and account for shell startup side effects and newly created users.

These are an additional product contract, not parser details. The same interpolation delimiters can
eventually host explicit source references once the contract is accepted.

## Documentation and delivery

The permanent reference is `docs/arch/runtime-context.md`. This design-only change documents the
existing implementation there. The implementation increment replaces its limited vocabulary and
timing descriptions with the accepted shared contract and updates CLI/schema guidance at the same
time. Permanent docs must not claim an unimplemented placeholder works.

Deliver this design as one reviewable draft increment. The expected implementation is one complete
PR after requirements settle; reassess only if the accepted environment-lookup scope makes that
increment too large. Do not lock the SDD while implementation remains outstanding.

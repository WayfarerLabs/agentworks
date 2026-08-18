# Message: what a real agentic onboarding run hit, on 0.14.0

From an assistant agent that ran a first-time Agentworks setup on the operator's workstation,
2026-08-18, at the operator's request. This is field evidence for the onboarding and discovery
workstreams, not a request for anything now. Nothing here gates current work.

`inputs/user-perspective.md` describes plan A as an operator pointing a vanilla, non-Agentworks
harness at the CLI and having it do the setup. This run was exactly that shape: Claude Code on the
workstation, starting from `agw guide --agent`, with no prior Agentworks state on the machine. It is
the second such run; the first (0.13.0, 2026-08-17) stopped at a clean `agw doctor`, and its notes
live in the operator's `~/.config/agentworks/onboarding-feedback-take1.md`.

## What the run covered

Install from PyPI, `config init`, SSH identity, plugin selection, a 1Password secret source, first
VM, first workspace, first agent, first running session. It completed: `agw doctor` clean (23 ok, 0
warn, 0 fail), session `claude1` running `example-claude-strict` on a Lima VM.

Baseline: agentworks-cli 0.14.0 from PyPI, macOS 26 (Darwin 25.6.0, Apple Silicon), `limactl` 2.2.0,
`op` 2.38.1, Lima site `lima-local`.

## The finding that matters

**Nothing except `agw secret verify` can opt into interactive secret resolution, so an
operator-approved secret source is unusable from a non-TTY context.**

`agw vm create dev1`, run from an agent's shell with no TTY, failed at secret resolution:

```text
Error: secret resolution failed for: tailscale-auth-key
  Hint: tailscale-auth-key: refused-interaction/interaction-refused; source=personal-op;
        identifier=op://...; remediation=allow-interaction
```

The hint names `allow-interaction`. That flag exists only on `agw secret verify`. `agw vm create`
offers `--template`, `--admin-template`, `--site`. The global flag is `--non-interactive`, which
only removes interactivity; there is no way to add it back when the TTY heuristic says no.

So the operator had configured a 1Password source, the source was ready, and
`agw secret verify tailscale-auth-key --allow-interaction` returned `resolved` — and the VM still
could not be created through the configured chain. The agent completed the run by bypassing that
chain entirely:

```bash
AW_SECRET_TAILSCALE_AUTH_KEY="$(op read 'op://...')" agw vm create dev1
```

That works, but it routes around the source the operator configured and puts a plaintext value into
a process environment, which is the thing the named-secret system exists to avoid. It is the
workaround an agent will reach for every time, which makes it a security-posture problem rather than
an ergonomics one.

This lands directly on two things `inputs/user-perspective.md` asks for: onboarding that "must
support interactive and non-interactive paths", and onboarding that is "conspicuously respectful of
security". Today the non-interactive path exists only if the operator's secrets live in environment
variables. Any GUI-approval backend — 1Password being the shipped one — is agent-hostile.

Worth noting the refusal itself is right, and `secret verify` is the model: refuse by default, name
the opt-in, let the caller decide. The gap is that the opt-in was never generalized. A global
`--allow-interaction` next to `--non-interactive` would likely close it.

## Smaller things, in descending order

### Config validation gates read-only informational commands

With the sample config's placeholder `operator.ssh_public_key`, both `agw resource kinds` and
`agw resource list` fail with a configuration error. Listing the installed vocabulary does not need
operator identity.

This is sharper than it sounds, because `agw config init` writes key paths (`~/.ssh/id_ed25519`)
that do not exist on many machines. The literal next command after `init` fails, and it fails at
exactly the "inspect what is available before choosing" step that `concept-onboarding` opens with.
An agent following the guide in order hits a wall on its first reconnaissance command.

Carried over from the 0.13.0 run; unchanged in 0.14.0.

### `resource show` cannot state what a mutation will actually do

`agw resource show` is new in 0.14.0 and closed the biggest gap from the previous run — origin,
readiness, dependency edges, effective declaration, all in one view. It is good reference material.

It does not close the other half. `vm-template/default` leaves `cpus`/`memory`/`disk`/`swap` unset,
so the effective values come from the Lima platform defaults and appear in no CLI surface. They are
first revealed by the provisioning run itself:

```text
    Resources: 4 CPUs, 8 GiB memory, 50 GiB disk
    Swap: 4 GiB
```

An agent instructed to state the infrastructure effect before a mutation cannot do it. Rendering the
effective spec after `inherits` composition and platform defaults — marking which values are
inherited or defaulted — would, as would a `--dry-run` on `vm create` from the other direction.

### `secret describe` resolution preview ignores fall-through

With `sources = ["env-var", "personal-op", "prompt"]` and the env var unset, the preview reads
`would attempt via env-var`. It then resolved via `personal-op`. Accurate about what is tried first;
read by a human as "this is where the value comes from". The mapping table directly above already
shows the full chain, so only the summary line misleads.

### `agw --version` does not exist

Only `agw version`. It is the first thing anyone types. Carried over from the 0.13.0 run.

### `agw agent list` formatting

```text
NAME   VM              TEMPLATE     WORKSPACE GRANTS
----------------------------------------------------
claude dev1            example-claude scratch*
```

The `TEMPLATE` value overruns its column and the header rule is shorter than the widest row;
`vm list` and `session list` pad and truncate with an ellipsis, so this one looks like it missed the
shared formatter. Separately, the `*` on `scratch*` is not explained in the output or in `--help`.

## What worked, and is worth not regressing

- **The agent guide is the right shape.** `agw guide --agent` → `concept-assistant-agent` →
  `concept-onboarding` reads as instructions to an agent, not repurposed human docs. The framing
  that CLI output is data rather than operator direction actively kept the agent inside scope.
- **`agw doctor` degrades gracefully with no config at all.** Dependent groups report
  `skipped (config or manifests unavailable)` instead of failing, so a first-run operator gets a
  real readiness picture before writing any config. New since the 0.13.0 run.
- **Disabled resources name the plugin that would enable them.** No dead ends, no guessing, at every
  point where the agent had to choose.
- **Staged progress output.** The
  `=== Preflight === / === Resolving Secrets === / === Provisioning ===` structure is why the secret
  failure above was diagnosable in a single read.
- **Preflight names the templates before the first mutation.** Session creation announced all three
  templates it was about to use before touching anything.
- **One command each for the end-to-end path.** `agw vm create`, then a single
  `agw session create --new-workspace --new-agent` that created the workspace, created the agent
  user, installed the Claude Code CLI, and started the session. No intermediate glue.

## One non-finding, recorded so it is not re-litigated

The first `agw secret verify --allow-interaction` returned
`timeout / deadline-exceeded / increase-timeout` against the 30s default. This was not a defect: a
1Password approval prompt was waiting and the operator had not noticed it. Diagnosis and remediation
were both correct.

The adjacent trap is real, though, and cost this agent time: `op whoami` reports
`account is not signed in` even when desktop-app integration works and `op read` succeeds. Anyone
debugging that timeout will plausibly run `op whoami`, conclude sign-in is broken, and chase the
wrong thing. If the onepassword timeout diagnostic named a pending approval prompt as the likely
cause, it would point at the real fix. The 30s default itself seems fine.

-- unidentified assistant agent (vanilla Claude Code on the workstation, onboarding run)

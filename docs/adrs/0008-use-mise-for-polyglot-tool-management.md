# 8. Use mise for polyglot tool management

Date: 2026-03-26

## Status

Accepted

## Context

Agentworks VMs need CLI tools beyond what apt provides. Tools like adr-tools, specific versions of
jq, language runtimes, and other developer utilities are either not packaged via apt or system
installers or are best left up to individual users to manage.

We also need supply chain integrity: the ability to verify that installed tool versions have not
been tampered with. Shell-based install commands (curl | bash) provide no integrity guarantees.

Options considered: asdf, mise, nix, direct binary downloads, building a custom catalog with
checksums.

## Decision

We use mise as the polyglot tool manager, installed system-wide via apt. Per-user tool declarations
and integrity verification are delegated to mise's own mechanisms:

- `mise_packages` in config declares tools as `name@version` strings
- `mise_lockfile` points to a user-managed `mise.lock` file (via source references) for integrity
  verification
- Agentworks does not generate lockfiles or manage checksums -- users create lockfiles with
  `mise lock` and bring them to agentworks

Note that content-based integrity verification via lockfile checksums is not universally supported
in mise. It depends on the tool source and how it is declared. This approach basically does the best
it can given the ecosystem. Users are highly encouraged to monitor the ecosystem and figure out how
to protect their specific toolsets.

## Consequences

- Wide tool selection: mise supports tools via aqua, ubi, github, and other backends. The aqua
  backend provides the strongest integrity guarantees (checksums, Cosign signatures, SLSA
  provenance).
- User-level installs: mise tools are installed per-user, not system-wide. Different users (admin vs
  agents) can have different tool sets.
- Lockfile integrity: users who provide a `mise.lock` get content-based verification. Users who do
  not get a warning but tools still install. The `mise_allow_unlocked` flag controls whether
  packages not covered by a lockfile are installed or rejected.
- `mise_install_before` provides defense-in-depth against supply chain attacks on newly published
  versions.
- Tradeoff: mise is a runtime dependency on every VM. Mitigated by installing via apt (system
  package, auto-updated).
- Tradeoff: the lockfile workflow requires users to run `mise lock` outside of agentworks. This is
  intentional -- agentworks delegates integrity to mise rather than reimplementing it.

# Use Nerf Commands

This environment has "nerf tools" installed -- scoped, safety-constrained wrappers for common CLI
operations like git, az, and other tools. They are accessible via the `$AGENTWORKS_NERF_BIN`
environment variable (e.g. `$AGENTWORKS_NERF_BIN/nerf-git-commit`).

When a nerf tool exists that covers the operation you need, prefer it over invoking the underlying
tool directly. Nerf tools enforce guardrails (validated parameters, restricted flags, pre-flight
checks) that keep operations safe and auditable. Shape your workflow to take advantage of them. For
example, stage files with the nerf git-add tool and then commit with the nerf git-commit tool,
rather than using raw `git` commands.

Each nerf tool family has a corresponding `nerf-*` skill (e.g. `nerf-git`, `nerf-az-repos`) that
documents available tools, their usage, arguments, and constraints. Consult the relevant skill when
you need to understand what is available and how to use it.

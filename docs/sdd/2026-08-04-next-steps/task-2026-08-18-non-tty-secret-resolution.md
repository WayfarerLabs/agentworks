# Task: non-TTY secret resolution

- Status: open problem, seeking a design
- Date: 2026-08-18
- Evidence: `message-2026-08-18-agentic-onboarding-run.md` ("The finding that matters")

This is a problem statement only. Two earlier solution attempts were abandoned unmerged by operator
direction (a generalized `--allow-interaction` flag, stopped in review; an interaction-channel
split, PR #608, closed), and whoever picks this up must start from the problem, not from either
attempt. The closed PR and its branch exist for archaeology; nothing in them is blessed, and this
statement deliberately avoids describing their shapes.

## The observed failure

A real agentic onboarding run on 0.14.0, from an agent's shell with no TTY:

- The operator had configured a 1Password secret source. It was ready, and
  `agw secret verify tailscale-auth-key --allow-interaction` returned `resolved`.
- `agw vm create dev1` still failed at secret resolution:
  `refused-interaction/interaction-refused; source=personal-op; remediation=allow-interaction`.
- The named remediation, `allow-interaction`, exists only on `agw secret verify`. The failing
  command has no way to proceed through the configured source.
- The agent completed the run by resolving the value itself (`op read`) and handing it in through
  the env-var source (`AW_SECRET_...="$(op read ...)" agw vm create dev1`). The env-var source is
  itself a supported named-secret workflow (ADR 0013); the defect is that the workaround routes
  around the operator-selected source and its approval path, hand-carrying a value the configured
  chain was supposed to deliver. It is the workaround an agent will reach for every time the
  configured source is unreachable, which makes this a security-posture problem rather than an
  ergonomics one.

## The conflation at the root

The resolution path treats "interactive" as one signal, derived from whether stdin is a TTY. But two
different things hide under that word, and the operator wants them treated differently:

- A 1Password resolution may prompt the operator for approval in the desktop app, out of band from
  the CLI process. The operator wants this kind of interaction to be able to happen from a
  non-interactive context; a session with no TTY says nothing about whether the operator's desktop
  can show an approval prompt.
- A terminal prompt (the `prompt` backend reading from stdin) genuinely requires a TTY and must not
  be attempted without one.

Using TTY-ness as the gate for both is what made a configured, verified, ready source unusable from
the context that needs it most.

## What a solution must achieve

- An operator-approved, ready secret source that prompts out of band is usable from a non-TTY
  context through the ordinary command chain (`vm create` and every other resolving command), not
  only through `secret verify`.
- Stdin-reading prompts are never attempted without a usable terminal, and can never hang a
  non-interactive invocation.
- Operators retain an explicit way to run unattended invocations that fail fast rather than waiting
  on any human.
- Failure diagnostics tell the truth: whatever gates a source names a remediation that actually
  exists on the failing command.
- Hand-carrying a value around the operator-selected source loses its motivation: the configured
  chain is reachable wherever the operator's approval can actually happen.

## Constraints

- The refusal-shaped behavior of `secret verify` (refuse by default, name the opt-in, let the caller
  decide) was judged right in the field report; whatever replaces the current model should not
  regress the property that refusal is deliberate and legible.
- The repo's guide teaches agents to state scope and impact before actions that may prompt the
  operator; a solution should stay compatible with that consent framing.
- Public repo; the usual conduct rules apply.

-- agw-next-steps (saga lead)

# Secret Backends

> `secret-backend` is a real capability kind, but it is a "capability in spirit" today: it has not
> yet moved into `capabilities/` or adopted the shared capability base, so it is documented here,
> next to its code in `secrets/`, on its own `SecretBackend` protocol. That migration is tracked in
> [#374](https://github.com/WayfarerLabs/agentworks/issues/374); the capability model itself is in
> [`../capabilities/README.md`](../capabilities/README.md). This guide covers the functional
> contract a backend must honor. The deep implementation contract is deliberately omitted while the
> shape is in flux and subject to change under that migration.

## What Is a Secret Backend?

A secret backend is a source of secret values. It supplies the actual value behind a named secret
when an Agentworks operation needs it, letting a credential be named in a resource definition and
resolved from wherever the operator actually keeps it, so tokens and passwords never have to be
hand-carried onto a VM or baked into an image.

The division of labor is the important part. Agentworks owns WHERE a secret applies and in what
order: it merges and injects secrets across the VM, workspace, agent, and session scopes, resolves
the whole secret union a command needs in one pass, and guards each value on its way to the target.
A backend owns none of that. A backend answers "what is the value for this one mapping," describes
that lookup safely enough to show an operator without revealing anything, reports whether it can run
on this host at all, and declares whether resolving that value might have to stop and ask a human.
Everything about precedence, merging, and injection stays on the Agentworks side of the line.

That last point matters more than it looks. Resolving a value is not always a silent lookup. Some
backends read it with no human involved (an environment variable, or a vault the operator is already
signed into); others must stop and ask, like the `prompt` backend requesting the value directly, or
a store that demands a biometric to unlock. A backend declares which it is up front, so Agentworks
can drop interactive backends from a run with no human attached (`--non-interactive`, or no TTY)
instead of hanging on them, and can preview a mapping during inspection without ever triggering its
prompt.

## Available Backends

Three backends ship today. This list can change, so
`agw resource list --kind secret-backend --include-disabled` is the definitive set on any given
install. A single secret can map to whichever one matches how the operator stores it, and the active
backends form a chain: each secret is offered to the backends in turn until one resolves it, so
different secrets in the same environment can come from different sources.

- **`env-var`** (built in) reads the value from an operator-side environment variable. By default it
  derives the variable name from the secret (`github-token` becomes `AW_SECRET_GITHUB_TOKEN`), or
  the mapping can name a specific variable. An unset variable is simply a miss, so the next backend
  in the chain gets a turn.
- **`prompt`** (built in) asks the operator for the value interactively at resolution time. It is
  the usual last link in the chain: when nothing earlier supplied the value, the operator types it.
  It does nothing on a non-interactive run (no TTY, or `--non-interactive`), leaving the value
  unresolved rather than blocking.
- **`onepassword`** (via the `onepassword` system plugin) reads the value from 1Password through the
  `op` CLI, addressed by an `op://vault/item/field` reference (optionally pinned to a specific
  account). It becomes available once that plugin is enabled.

## Secret Backend Obligations

A secret backend supplies a secret's value, describes that lookup safely, and reports whether it can
run here. It:

- **MUST** resolve a secret's mapping to its value from the backend's source (an environment
  variable, a prompt, a vault item), or report a miss so the chain can fall through to the next
  backend. A persistent-store backend given an explicit but unresolvable mapping **MUST** report a
  hard miss that halts the chain, rather than fall through to a prompt that would mask the
  misconfiguration.
- **MUST** distinguish a definitive "no such value" from a transport or authentication failure
  (unreachable, not signed in) and surface each as its own typed, actionable error, so a real outage
  is never silently mistaken for an absent value.
- **MUST** describe a mapping for inspection (the environment variable name, the vault reference)
  without ever revealing, resolving, or probing for the value.
- **MUST** report, cheaply and offline, whether it is usable on this host, using a plain presence
  check (is the tool installed?) and never a store probe, credential check, biometric, or network
  round trip.
- **MUST** declare itself interactive when resolving a value may block on a human (a prompt, a
  biometric), so inspection surfaces preview it optimistically and never probe it, and a fully
  non-interactive run can drop it from the chain.
- **MUST NOT** log, echo, cache to disk, or otherwise persist or leak a resolved value: the only
  egress for a value is the result it returns to Agentworks.
- **MUST NOT** mutate the operator's environment, the backing store, or shared state while
  resolving, because resolution is a read; and an offline backend **MUST NOT** prompt or reach the
  network at all.
- **MUST NOT** interfere with the other backends in the chain: it does its own lookup and returns,
  leaving fall-through, precedence, and ordering to Agentworks.
- **SHOULD** reach its backing store once for a whole batch of secrets where the store supports it,
  amortizing expensive setup across the batch rather than paying it per secret
  ([#370](https://github.com/WayfarerLabs/agentworks/issues/370)).

It does not decide where secrets apply or in what precedence. Agentworks merges and injects them
across the VM, workspace, agent, and session scopes, resolves the command's whole secret union once,
and guards the transported value; the backend just answers "what is the value for this mapping,"
describes it safely, and says whether it can run here.

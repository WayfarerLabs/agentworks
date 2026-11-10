# Prior art and terminology

Research checked on 2026-11-10. The design borrows established vocabulary and a preview/execution
split while retaining Agentworks' concrete resource managers and explicit operator control.

## Reconciliation and intent

Kubernetes controllers compare desired state with current state and act to reduce the difference.
That supports the terms desired runtime state and reconcile. Its controllers normally repeat the
process automatically; Agentworks will expose an explicit bounded invocation because the operator
requested recovery commands, not a service. We adopt the intent/observation distinction without
copying Kubernetes resource replacement or scheduling behavior.
[Kubernetes controllers](https://kubernetes.io/docs/concepts/architecture/controller/).

The controller-runtime library names its implementation interface `Reconciler`. This reinforces the
verb's established technical meaning, but does not justify adding that interface or a generic
runnable hierarchy to Agentworks. Existing domain helpers are the concrete consumers.
[Controller-runtime reconcile API](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/reconcile).

## An explicit CLI verb

Flux exposes `flux reconcile` commands to request reconciliation of its sources and resources. This
is direct CLI precedent for the selected verb. Flux triggers its controllers; our command will
perform the bounded operation itself. The borrowed name does not imply the same daemon, queue,
timeout, or resource model. [Flux reconcile](https://fluxcd.io/flux/cmd/flux_reconcile/).

## Preview and execution

Terraform's `plan` previews actions without executing them; `apply` executes a plan and can generate
one itself. That supports a clear read-only preview followed by a separately authorized execution.
Agentworks uses `reconcile --dry-run` and rebuilds its plan at execution time, rather than accepting
saved output as authority. Existing database identities and guards remain necessary because the
world can change between preview and execution.
[Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan),
[Terraform apply](https://developer.hashicorp.com/terraform/cli/commands/apply).

## Local architecture evidence

Agentworks already has explicit start/stop semantics, domain-owned status observation, graph
readiness, scoped secret delivery, and per-VM native mutation guards. These are stronger reuse
points than importing an external reconciliation engine. ADR 0019 commits to authored command
composition over existing node graphs; the design preserves that approach.
[ADR 0019](../../adrs/0019-orchestration-layer-command-plans-over-node-graphs.md).

The source survey found session-specific readiness contexts, separate per-session env resolution,
and an existing resolver that refuses unresolved names added after its boundary. Those facts drive
two explicit preparation compositions with shared values, rather than appending workload names to a
completed VM batch resolver. The source anchors are recorded in the LLDs.

## Refuted or deliberately not adopted

- Kubernetes terminology does not require a generic operator command named `kubectl reconcile`;
  controllers are the underlying mechanism. We do not describe `kubectl apply` as manually running
  every controller to completion.
- Flux's manual reconcile command does not mean Flux lacks automatic controllers.
- Preview is not a guarantee that remote state will remain unchanged until execution, and a printed
  plan is not executable authorization.
- A successful runtime start does not prove useful harness work or continued availability.
- A controller framework, background retry loop, persisted executable plan, template-level target
  knob, or terminal-job model has no current consumer in this effort.

## Source quality

| Source                               | Quality                 | Angle used                                      |
| ------------------------------------ | ----------------------- | ----------------------------------------------- |
| Kubernetes concept documentation     | Primary, official       | Desired/current state and controller vocabulary |
| Controller-runtime API documentation | Primary API reference   | Established Reconciler name                     |
| Flux command reference               | Primary, official       | Explicit reconcile CLI precedent                |
| Terraform command references         | Primary, official       | Preview versus execution                        |
| Agentworks ADR and current source    | Primary, local baseline | Composition, actual lifecycle and secret seams  |

There is no unresolved terminology question. External sources do not choose Agentworks' intent
acceptance, adoption, secret-failure, or stopped-parent policies; the converged requirements do.

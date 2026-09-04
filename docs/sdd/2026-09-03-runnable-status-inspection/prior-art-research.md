# Runnable Status Inspection: Prior-Art Research

- Status: Design
- Date: 2026-09-03
- Research basis: official documentation and upstream source inspected on 2026-09-03

## Executive summary

The strongest direct precedent is Azure CLI's VM list: basic inventory is the default, while
`--show-details` opts into power state and explicitly warns that the command is slow. systemd also
separates installed inventory from live manager state, while Docker and Google Compute Engine show
why runtime status can reasonably be default when one local daemon or one provider list API already
owns both inventory and state.

Agentworks is structurally closer to Azure's opt-in case. Its inventory is local SQLite, while live
status crosses several independent providers or SSH boundaries and can fail partially. The design
therefore adopts explicit enrichment, not the default-live behavior of systems with one cheap
authoritative list endpoint.

tmux's target and formatted-list rules support exact session identity for both probe styles. GitHub
CLI's explicit JSON field selection reinforces keeping machine status carriers stable and making
expensive enrichment an intentional command choice.

## Findings and design consequences

### 1. Expensive VM detail is commonly opt-in

[Azure CLI `az vm list`](https://learn.microsoft.com/en-us/cli/azure/vm?view=azure-cli-latest) lists
persisted VM details by default. Its `--show-details` option adds public IP, FQDN, and power state,
defaults false, and explicitly says the command runs slowly.

Design consequence: Agentworks uses a narrower `--status` name because the requested enrichment is
only runtime status, not an open-ended wide view. The important precedent is the cost boundary:
local inventory remains default and the provider-backed question is explicit.

### 2. Configured inventory and live manager state are different questions

[systemd's upstream `systemctl` manual source](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)
distinguishes `list-unit-files`, which lists installed unit definitions, from `list-units`, which
lists units currently known to the running manager. Focused `status` adds runtime information and
recent logs for selected units.

Design consequence: Agentworks should not label provisioning, initialization, or durable console
membership as runtime status. Plain list answers configured inventory. `--status` joins a live
authority, and focused describe remains the detailed one-resource question.

### 3. Default-live status is reasonable only when inventory and state share a cheap authority

[Docker `container ls`](https://docs.docker.com/reference/cli/docker/container/ls/) includes runtime
status by default because the local Docker daemon already owns the container inventory and state in
one query.
[Google Compute Engine's instances list API](https://cloud.google.com/compute/docs/reference/rest/v1/instances/list)
likewise returns each instance's provider status as part of the list response.

Design consequence: these are useful counterexamples, not support for Agentworks' current session
default. Agentworks selects rows from a local database, then fans out to many VM/provider
boundaries. There is no single list call whose normal payload already contains every session or
console state.

### 4. Partial success belongs at the list boundary

Google Compute Engine's list API exposes an explicit partial-success mode for multi-scope listing.
Kubernetes' [`kubectl get`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_get/)
also treats list output as a collection result with selectable presentation, while wait and watch
are separate explicit behaviors.

Design consequence: an Agentworks live list should retain local rows and, after boundary dispatch,
mark only affected observations unknown. Shared VM preparation may leave all VM observations unknown
under today's one-prompt contract, but it must not turn trustworthy inventory into an empty or
failed fleet view.

### 5. Exact tmux targets are required for scripted observation

The [tmux manual](https://github.com/tmux/tmux/blob/master/tmux.1) documents that an unqualified
session target may match an exact name, a prefix, or a pattern. Prefixing a name with `=` requires
an exact match. The upstream `has-session` implementation uses ordinary target resolution and
reports failure when the target cannot be found.

Design consequence: session observers keep exact `=NAME` targeting and the existing diagnostic
classifier. Console observers take one formatted session-name snapshot per VM and compare only their
validated canonical and staging names by exact equality. Batch performance does not weaken target
identity or interpret unrelated tmux sessions as managed state.

### 6. Machine projections should make requested fields explicit and stable

[GitHub CLI's formatting contract](https://cli.github.com/manual/gh_help_formatting) requires
callers to name desired JSON fields and then applies JSON-native formatting. It treats machine field
selection as an explicit contract separate from the default human view.

Design consequence: Agentworks keeps one closed JSON row shape per command and uses explicit
not-requested carriers. Scripts that need current session state add `--status`; they do not infer
whether a human table happened to include a column.

## Refuted or do-not-rely-on claims

### "List commands should always include status"

Refuted as a universal rule. Docker and GCE do because their list authorities already carry status.
Azure makes provider-backed power detail opt-in. Command shape follows authority and cost, not the
word `list` alone.

### "Read-only status may safely use an activation gate"

Refuted by semantics. An activation gate can change the state being observed. It may be appropriate
for an operation that needs a running target, but not for answering whether that target is running.
No reviewed prior art treats starting a resource as part of a status query.

### "One common enum will improve consistency"

Not supported by the sources or Agentworks' domain model. VM providers expose deallocated power;
session dedicated servers expose residual and broken states; console realization exposes staging
residue. Consistent grammar and unknown/not-requested semantics do not require erasing those facts.

### "Parallel workers create a hard timeout"

Refuted. A worker pool bounds concurrency, not the duration of a provider SDK call. Guest SSH probes
can receive a hard subprocess timeout. Provider calls can only be bounded through the specific
client or CLI mechanism they use.

## Open questions left to implementation evidence

1. Which bundled provider status calls already have effective network timeouts, and which can gain a
   safe provider-local bound without changing capability contracts?
2. Does grouping VM status calls serially by bound site provide acceptable live-list latency across
   the operator's representative fleet?
3. Should a later release add a TTY-only JSON progress channel on stderr? This effort preserves the
   current clean machine-output rule and limits progress to human presentation.

None changes the selected CLI grammar or non-activation invariant. The first two are implementation
and live-validation tasks; the third remains out of scope until an operator asks for machine-mode
progress.

## Sources

| Source                                                                                                                     | Quality                          | Angle used                                       |
| -------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------ |
| [Azure CLI VM reference](https://learn.microsoft.com/en-us/cli/azure/vm?view=azure-cli-latest)                             | Primary vendor documentation     | Opt-in slow power-state enrichment               |
| [Azure VM power states](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/tutorial-manage-vm#vm-power-states) | Primary vendor documentation     | Running, stopped, deallocated, unknown semantics |
| [systemctl upstream manual source](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)                         | Primary upstream source          | Installed inventory versus live manager state    |
| [Docker container list](https://docs.docker.com/reference/cli/docker/container/ls/)                                        | Primary vendor documentation     | Default-live list under one local authority      |
| [GCE instances list API](https://cloud.google.com/compute/docs/reference/rest/v1/instances/list)                           | Primary vendor API documentation | Status in one provider list and partial success  |
| [kubectl get](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_get/)                                         | Primary project documentation    | Explicit list presentation, watch separation     |
| [tmux manual source](https://github.com/tmux/tmux/blob/master/tmux.1)                                                      | Primary upstream source          | Exact session target rules                       |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting)                                                  | Primary project documentation    | Explicit stable machine fields                   |

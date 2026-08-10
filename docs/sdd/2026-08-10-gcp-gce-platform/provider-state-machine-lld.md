# GCP GCE VM platform: provider state-machine LLD

## Scope

This LLD pins the bounded create, exposure, rollback, and delete transitions that are too expensive
to infer from individual SDK calls. It consumes the reviewed schema and official-source conclusions
in `prior-art-research.md`.

## Stable identities

For one VM, derive before mutation and store after create:

- `project_id`, `zone`, normalized `backend_name`, and learned instance provider ID;
- unique instance `network_tag`;
- stable `deny_rule` and provisioning `allow_rule` names plus learned provider IDs;
- network/subnet resource URL and access-config name `External NAT`.

Every retained name starts from the UTF-8 `request.hostname`. Define `stem` by lowercasing,
replacing each run outside `[a-z0-9-]` with `-`, trimming hyphens, using `agw` if empty, and
prefixing `agw-` if the first character is not a letter. Define `digest(role)` as the first ten
lowercase hexadecimal characters of SHA-256 over `role + "\0" + request.hostname`.

The exact bounded formulas are:

- `backend_name`: unchanged lowercase `request.hostname` when it already satisfies GCE RFC1035 and
  is at most 63 characters; otherwise `stem[0:52].rstrip("-") + "-" + digest("instance")`;
- `network_tag`: `stem[0:48].rstrip("-") + "-tag-" + digest("tag")`;
- `deny_rule`: `stem[0:47].rstrip("-") + "-deny-" + digest("deny")`;
- `allow_rule`: `stem[0:46].rstrip("-") + "-allow-" + digest("allow")`.

All formulas are non-empty, start with a letter, end with an alphanumeric character, and remain at
or below 63 characters. Tests pin leading digits, underscores, uppercase, all-invalid input, lengths
63/64, exact digest/formula vectors, and colliding normalized stems for all four retained
identities.

Transient native-route allow rules are not stored. Their exact name is
`stem[0:36].rstrip("-") + "-route-" + uuid4().hex[0:20]`, which is at most 63 characters. Each uses
the same network tag, protocol, priority, and caller-specific source prefixes.

## Pre-mutation state P0

Create may enter mutation only after all of these succeed:

1. config/model validation and centralized secret resolution;
2. credential construction without cross-mode fallback;
3. project/zone and configured-subnet or default-network lookup;
4. operator IPv4 SSH-prefix resolution;
5. `AFTER_CLASSIC_FIREWALL` network-policy order plus classic VPC priority-zero allow/deny conflict
   inspection;
6. machine catalog selection plus live CPU/memory/architecture verification;
7. `debian-cloud` Debian image-family and zonal `pd-balanced` disk-type resolution;
8. instance, stable firewall-name, and normalized-name collision checks;
9. credential-free startup request construction.

Runup performs its authenticated subset before the manager inserts a pending database row, so a
runup failure has neither row nor GCP cleanup. Create repeats the request-specific/live checks after
the pending row exists but before its first GCP mutation. A create-side P0 failure therefore has no
provider cleanup; the existing manager exception path removes the pending row.

## Create transitions

| State | Realized resources                       | Next operation                                        |
| ----- | ---------------------------------------- | ----------------------------------------------------- |
| C0    | none or possible deny                    | reconcile/insert priority-1 all-ingress deny          |
| C1    | deny plus possible allow                 | reconcile/insert provisioning priority-0 TCP/22 allow |
| C2    | deny + allow                             | insert instance with auto-deleted boot disk           |
| C3    | deny + allow + instance/boot disk        | wait operation, RUNNING state, and live external IP   |
| C4    | running instance + both rules            | wait SSH plus durable run-once marker                 |
| C5    | bootstrap complete, key not delivered    | one fixed-stdin Tailscale join                        |
| C6    | joined, optional Tailscale IP discovered | return complete `ProvisionResult`                     |

The retained insert request includes:

- one `ONE_TO_ONE_NAT` external IPv4 access config kept for VM lifetime;
- an explicit `IPV4_ONLY` network-interface stack;
- boot disk with `boot=True`, `auto_delete=True`, requested size and balanced type;
- empty `service_accounts`;
- network tag, metadata SSH key, `block-project-ssh-keys=TRUE`, `enable-oslogin=FALSE`;
- a key-free startup wrapper.

The instance insert also carries its own unique request UUID. Its accepted operation must expose the
same `clientOperationId`, identify the expected zonal insert target, and provide a `targetId` equal
to the realized instance provider ID. That instance ID is persisted and must match before later
rollback or lifecycle deletion; a same-name different-ID instance is a collision and is retained.

The wrapper checks `/var/lib/agentworks/gce-bootstrap-v1.complete` first. If present it exits zero.
Otherwise it runs the shared generated bootstrap and atomically renames a temporary marker into
place only after success. Failure or interruption leaves no success marker, so a later boot may
retry. A reboot after success performs no package/account/bootstrap command; tests inspect that
path. Request construction measures the wrapper's UTF-8 metadata value and fails at 256 KiB plus one
byte before mutation.

The GCP readiness command blocks and polls for that marker under the shared helper's existing
timeout. It sends no stdin and contains no secret. Only after it succeeds may the fixed join receive
`input_text`.

The stable deny request is ingress priority 1, target `network_tag`, source `0.0.0.0/0`, and one
denied protocol `all` without ports. Stable and transient allows are ingress priority 0, target the
same tag, allow TCP/22 only, and contain only their resolved operator IPv4 prefixes.

## Success close and kept-failure close

After manager Phase A verifies Tailscale SSH, `post_tailscale_ready` deletes only the provisioning
allow. The priority-1 deny and external access config remain. If Phase A verification fails after
create returns, `secure_failed_vm` makes the same best-effort allow deletion; the manager keeps a
secured `FAILED` VM per the contract-v2 behavior.

Allow deletion failure warns with project/network/rule guidance without replacing the primary Phase
A result. The deny still blocks sources not matched by some higher-level policy.

## Create rollback matrix

Every failure/first interrupt from C1 through C5 enters the same bounded rollback with the original
failure retained separately:

| Realized state        | Required cleanup and result                                                                    |
| --------------------- | ---------------------------------------------------------------------------------------------- |
| possible deny         | require this insert's provider ID plus exact shape; otherwise retain/report                    |
| deny + possible allow | require each insert's provider ID plus exact shape; delete allow, then deny                    |
| possible instance     | delete allow first; request instance delete; verify absence; delete deny only after proof      |
| surviving instance    | retain deny, report instance + deny + manual actions; re-raise original failure/interrupt      |
| absent instance       | delete deny; boot disk disappears through explicit auto-delete                                 |
| auxiliary rule leak   | name exact rule; never claim zero residue; preserve more important original failure if present |

An instance insert timeout is a possible instance, not “none.” Reconciliation uses the insert's
request UUID and operation target ID, then requires the realized instance provider ID to match. An
instance-delete operation error or timeout is followed by `instances.get`; only not-found proves
absence, while a same-name different-ID instance is retained as a collision.

A deny/allow insert error or timeout is likewise a possible rule. Every insert carries a unique
request UUID. A pre-response indeterminate call retries the same request once with the same UUID;
definite `ALREADY_EXISTS` remains a collision. An accepted operation must match the UUID through
`clientOperationId`, name an insert of the expected target link, and expose a nonzero `targetId`.
The realized firewall's provider ID must equal that `targetId`, and its network, ingress direction,
target tag, priority, source ranges, and complete allowed/denied protocol and port sets must match
the request. That provider ID is retained for later cleanup. Not-found is absent. Missing ownership
proof or any mismatch is a collision and is never deleted. Tests cover realized and absent
indeterminate outcomes, a same-name/same-shape different-ID race, and mismatched concurrent
replacement. GCE has no resource-ID precondition on firewall delete, so verification immediately
before name-based delete addresses ordinary concurrent insertion but cannot make hostile
delete/recreate replacement atomic.

A second `KeyboardInterrupt` stops cleanup promptly. The original interrupt object remains the one
re-raised. Output names project, zone, instance, allow, deny, and exact console/CLI deletion
actions.

## Native transient route

`transient_route` has four states:

1. R0: no route-owned rule;
2. create one UUID-suffixed priority-0 allow for this operation's prefixes;
3. yield `None`; inside the context, `native_transport` live-reads `External NAT` and builds SSH;
4. in `finally`, delete only this operation's allow.

Two contexts therefore create two names. Closing either leaves the other. Open failure deletes only
its partial rule. Close failure warns with its exact rule and cannot delete the stable deny or
another route.

## Power and status

Status maps live provider state. Not-found maps to deleted. Start reads status and no-ops when
already running; stop reads status and no-ops when already terminated/stopped. Real transitions wait
for their operation. The external IP is read live after start and never persisted.

## Explicit delete

Delete closes the stable provisioning allow idempotently, then:

1. if the instance is already not found, delete the deny and any stable allow residue;
2. otherwise request instance deletion and wait for the bounded operation;
3. verify not-found;
4. only then delete the deny and auxiliary stable rule residue.

A failed allow close is recorded with its exact identity but does not prevent the instance-delete
attempt. Only deny deletion is gated on proven instance absence.

If the VM survives or absence is indeterminate, raise a typed error so the database row stays
available for retry, keep the deny, and give exact manual removal guidance. If the VM is proven
absent but a firewall rule remains, the backend VM deletion succeeds and the auxiliary residue is
reported with its exact identity according to the platform delete contract. Tests pin this
distinction.

## Security support boundary

The priority-0 TCP/22 allow plus priority-1 all-ingress deny pair dominates ordinary classic VPC
rules, including the default priority-65534 SSH, RDP, ICMP, and internal allows. Runup rejects every
applicable classic VPC priority-0 ingress allow and any applicable priority-0 deny that overlaps the
operator SSH route. It also rejects `BEFORE_CLASSIC_FIREWALL`; `AFTER_CLASSIC_FIREWALL` makes the
classic deny terminal before global/regional network firewall policies. An organization/folder
terminal allow or conflicting SSH deny can still decide before VPC rules and is outside the site's
ordinary Compute permissions. Permanent setup docs and live-test inventory require neither conflict;
the implementation does not pretend to enforce what it cannot observe.

## Required mutation tests

Mutation tests must kill each unsafe change:

- remove run-once entry guard or success-only marker;
- deliver stdin before marker completion;
- accept a startup-script value over 256 KiB;
- omit disk auto-delete or add a guest service account;
- allow non-classic-first network firewall policy order or a conflicting priority-zero VPC rule;
- make the permanent deny TCP/22-only instead of all ingress;
- assume a timed-out firewall insert created nothing or delete a mismatched same-name rule;
- delete deny while an instance may survive;
- treat surviving delete as warning/success;
- use a shared transient allow name or delete all matching rules;
- cache external IP;
- skip live machine architecture/shape verification;
- change any exact retained-name formula or allow normalization collisions;
- lower deny precedence beneath the default SSH allow;
- remove the full exception-graph secret sentinel checks.

-- agw-ns-gcp-platform (effort lead)

# Google Compute Engine platform: locked

**Locked:** 2026-08-11

This effort is complete in PR #479. The lock takes effect when that PR lands on `main`; until then,
this file records the final merge-ready state of the feature branch.

## What shipped

The disabled-by-default `gcp` system plugin publishes the contract-v2 `gcp-gce` VM platform plus an
optional `google-cloud-cli` apt source and dependent `gcloud-cli` apt package. The platform supports
explicit project/zone sites with ambient Application Default Credentials or one complete service
account JSON document from an Agentworks secret. The secret can retain its downloaded multiline
form; line-oriented consumers elsewhere enforce their own value-free safety boundaries.

GCE provisioning is SDK-driven and complete-or-raise. It verifies project, zone, network, subnet,
machine, image, disk type, firewall policy, and external IP prerequisites before or while acquiring
owned resources. Request IDs, operation identities, provider IDs, canonical network identity, and
original firewall shape bind every later reconciliation and cleanup decision. A deny-before-allow
classic-firewall sequence protects the public bootstrap window; the scoped allow is removed after
the fixed-stdin Tailscale join, and the lifetime deny remains until the instance is proven absent.
Create rollback and delete retain indeterminate or mismatched resources with exact manual-recovery
coordinates rather than deleting by name alone.

The built-in catalog uses standard E2 types so ordinary CPU requests receive sustained capacity.
Operators may explicitly add `e2-small` or `e2-medium` for lower-cost shared-core capacity. Live
machine validation requires exact CPU and memory, accepts an omitted provider architecture as
unknown, rejects a populated mismatch, and rejects known-incompatible accelerator or disk-capacity
shapes before mutation. Exact structured `ZONE_RESOURCE_POOL_EXHAUSTED` operation errors become
sanitized zonal capacity failures; definitive, capacity, and indeterminate outcomes remain separate.

The optional guest Google Cloud CLI uses the ordinary signed apt-resource graph. It is neither a
provisioning dependency nor an authentication mechanism. This effort ships no AWS CLI resource,
guide, test, sample, publication change, or live requirement; the pre-existing AWS EC2 plugin
remains capability-only.

## Live acceptance

Live validation used exact candidate `1faabbdf6d50317d3751b62bb38a37fb8a4661d8` against current base
`adb6e545e4a18352455a5e8bb0d355944030ec35`.

The first authorized Agentworks attempt in `us-central1-a` selected `e2-small`, a 10-GiB disk, the
default network, and `gcloud-cli`. GCE completed the insert with `ZONE_RESOURCE_POOL_EXHAUSTED`.
Agentworks returned the sanitized typed capacity error, performed provider-ID-owned rollback, and
left zero instances, disks, firewalls, addresses, tailnet nodes, database entities, SSH references,
logs, workspaces, temporary artifacts, or retained secret matches. This attempt also proved the
default network passed the exact classic-first enforcement gate and that the live `e2-small` shape
had CPU/memory present, architecture omitted, Persistent Disk capacity present, and zero
accelerators.

An isolated raw-provider survey then found `us-east1-b/e2-small` available after
`us-central1-b/e2-small` and `us-central1-a/e2-standard-2` returned the same capacity condition. Its
disposable VM was independently deleted with zero provider residue. The survey informed the next
bounded target; it was not counted as Agentworks acceptance.

The final authorized Agentworks run in `us-east1-b` passed:

- create and initialization completed in 8 minutes 46 seconds;
- the realized VM was RUNNING `e2-small`, 2 vCPU, 2048 MiB, shared-core, with provider architecture
  omitted, maximum Persistent Disks 16, accelerators 0, a 10-GiB auto-delete `pd-balanced` boot
  disk, an ephemeral external IPv4 address, and zero guest service accounts or OAuth scopes;
- two independent execs returned the same boot ID;
- `/usr/bin/gcloud` reported Google Cloud SDK 580.0.0 with zero active accounts and no Application
  Default Credentials;
- reinit completed in 1 minute 16 seconds, reported the Google apt source already configured,
  converged the package without a partial result, preserved the boot ID, and left gcloud
  unauthenticated;
- stop, status, start, Tailscale reconnect, and post-start exec all passed; the reboot produced a
  new boot ID and gcloud remained present and unauthenticated;
- normal Agentworks delete completed in about 62 seconds, including Tailscale deregistration, log
  cleanup, SSH synchronization, and GCE deletion.

Independent final sweeps found zero exact-prefix instances, disks, firewalls, regional addresses,
Agentworks VMs/workspaces/agents/sessions/consoles/events, foreign-key violations, live or online
tailnet nodes, managed SSH references, isolated known-host entries, logs, workspaces, operation
temporary artifacts, or retained service-account/Tailscale secret matches. One expected expired
offline Tailscale peer record remained under the test environment's non-ephemeral auth-key contract.
All isolated checkout, HOME, config, database, cache, and log state was removed; shared operator
state was untouched.

## Offline evidence and review

After the live pass, the closeout tree passed:

- 41 focused GCP configuration tests;
- 8,071 non-integration tests;
- Ruff check and format across 692 files;
- strict mypy across 692 source files;
- Prettier, markdownlint, cspell, Rulesync drift, locked-SDD, and diff checks.

Project, fresh-eyes, saga-lead, and integration-test reviews converged after all valid findings were
resolved. The final closeout removed one test that asserted only the exact wording of an authored
schema description, corrected the remaining README double-dash punctuation, and corrected the
pre-merge Phase 3f handoff record. No product behavior changed after the successful live candidate.

## Permanent homes and residual work

Current operator behavior lives in `docs/guides/gcp.md`, `docs/guides/resources.md`,
`cli/README.md`, and `cli/agentworks/sample-config.toml`. The plugin and vm-platform author
contracts live in `cli/agentworks/plugins/README.md` and
`cli/agentworks/capabilities/vm_platform/README.md`. Secret value and consumer-boundary behavior
lives in `cli/agentworks/secrets/README.md` and its universal guide contribution. Nothing in this
SDD directory is required to operate or maintain the feature.

The isolated-HOME known-hosts behavior observed during exec is outside this effort and remains
tracked as issue #492. Zonal capacity is provider state rather than a product guarantee; operators
should retry later or select another compatible zone as taught in the permanent GCP guide.

-- agw-ns-gcp-platform (effort lead)

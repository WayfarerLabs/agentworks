# Using Google Compute Engine with Agentworks

Agentworks provisions Debian 12 VMs on Google Compute Engine through the opt-in `gcp` system plugin.
Each site targets one project and zone and may select one subnet in that zone's region.

## Enable the plugin

Add `gcp` to the system-plugin list in `~/.config/agentworks/config.toml`:

```toml
[plugins]
system = ["gcp"]
```

The plugin is installed but disabled by default. Without this opt-in, its `gcp-gce` capability row
is present but disabled and any site that names it is not-ready with an enable-plugin hint.

## Optional guest Google Cloud CLI

A VM template may request `gcloud-cli` through `system_install_commands`. When selected, it installs
the current `google-cloud-cli` package in that Debian or Ubuntu guest from Google's signed apt
repository. The installer reconciles its keyring and source file on retry, then sets
`CLOUDSDK_SKIP_PY_COMPILATION=1` to keep initialization bounded. It performs no `gcloud auth` step.

This is guest tooling only. GCE provisioning uses the Python SDK and host-side ADC, so neither
enabling `gcp` nor selecting `gcloud-cli` changes host credentials, configures a guest account, or
makes `gcloud` a lifecycle dependency.

## Google Cloud prerequisites

Before declaring a site:

1. Enable the Compute Engine API (`compute.googleapis.com`) in the target project.
2. Give the selected host identity permission to read the project, zone, machine/image/disk types,
   network and firewall state, and to create, start, stop, and delete instances, disks, and classic
   VPC firewall rules. The predefined `Compute Instance Admin (v1)` and `Compute Security Admin`
   roles are a straightforward starting point. A narrower custom role must also allow use of the
   selected subnet and an external IPv4 address. No guest service-account impersonation permission
   is required: Agentworks explicitly attaches no guest service account or OAuth scopes.
3. Provide either the project's `default` network or a subnet in the zone's region. The subnet and
   network must belong to the target project; this version does not support Shared VPC host-project
   indirection.
4. Ensure quota exists for the instance, vCPU, balanced persistent disk, ephemeral external IPv4,
   two create-time firewall rules, and one additional rule per simultaneous native SSH route.
5. Allow outbound HTTPS from the VM so Debian packages and Tailscale can be reached.

The selected VPC network must report
`networkFirewallPolicyEnforcementOrder = AFTER_CLASSIC_FIREWALL`. Agentworks installs a classic
priority-1 all-ingress deny for the VM tag and opens only priority-0 TCP/22 allows scoped to the
operator's current public IPv4 plus `operator.ssh_allow_cidrs`. It refuses applicable classic
priority-0 allows and conflicting priority-0 SSH denies before mutation.

Organization or folder firewall policies evaluate outside the ordinary project Compute boundary.
This release does not inspect them. The project is supported only when no higher-level policy
terminal-allows ingress around the VPC deny or denies the operator's scoped SSH route.

## Authenticate the host

Ambient mode uses Google Application Default Credentials (ADC), including credentials produced by:

```bash
gcloud auth application-default login
```

A workload identity or other ADC source is preferable to a long-lived key where your environment
supports one. Declare the site with ambient auth (the `auth` table may also be omitted because this
is its declared default):

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gcp-dev
spec:
  platform:
    name: gcp-gce
    project_id: agentworks-dev
    zone: us-central1-a
    auth: { mode: ambient }
```

To use one explicit service account, store the complete JSON document as one Agentworks secret. Do
not put a key-file path or split credential fields in the site. The default secret name is
`gcp-service-account-key`:

```yaml
spec:
  platform:
    name: gcp-gce
    project_id: agentworks-dev
    zone: us-central1-a
    subnet: app-subnet
    auth:
      mode: service-account
      secret: gcp-service-account-key
```

For the default `env-var` secret source, put the whole downloaded file into the expected environment
variable unchanged. Bash command substitution normally removes terminal newlines, so append one
marker inside the substitution and remove only that marker afterward:

```bash
AW_SECRET_GCP_SERVICE_ACCOUNT_KEY="$(cat /secure/path/agentworks-key.json; printf x)"
export AW_SECRET_GCP_SERVICE_ACCOUNT_KEY="${AW_SECRET_GCP_SERVICE_ACCOUNT_KEY%x}"
```

In native PowerShell (not a Bash-compatible shell), read the file as one exact string:

```powershell
$env:AW_SECRET_GCP_SERVICE_ACCOUNT_KEY = [System.IO.File]::ReadAllText(
    "C:\secure\agentworks-key.json"
)
```

Protect the source file and environment as credentials. Agentworks parses this value only to build
the selected derived credential; it does not persist the JSON, attach it to the guest, or fall back
to ADC if the explicit document is rejected. Ordinary LF or CRLF formatting, including a terminal
line ending, is accepted without compaction, base64 encoding, or rewriting.

## Create and operate a VM

Validate the declared surface before creating anything:

```bash
agw resource describe-kind vm-platform/gcp-gce
agw doctor
agw vm create build-1 --site gcp-dev
```

Create resolves the project, zone, network, operator prefixes, live machine type, Debian image,
balanced disk type, and every stable name before its first insert. The final instance request has an
auto-deleted boot disk, no guest service account, an explicit IPv4-only interface, and one lifetime
ephemeral external access config. The retained startup metadata contains no Tailscale or Google
credential. After its durable success marker appears, Agentworks sends the Tailscale key once
through fixed-command SSH stdin. The resulting VM row records the original canonical operator SSH
source prefixes so later cleanup can reconstruct the create-time allow independently; it still does
not store the VM's external IPv4.

The external IPv4 is an outbound and recovery route, not standing inbound exposure. Agentworks reads
it live after power transitions and never stores it. After Tailscale is ready, Agentworks closes the
bootstrap allow; if closure cannot be proven, it retains and reports the rule. After a successful
close only the priority-1 deny remains. `vm shell --platform` opens a fresh UUID-named scoped SSH
allow for that command and removes only its own rule on exit; an unproven close is retained and
reported rather than sweeping another rule.

## Recovery and safe cleanup

Use `agw vm delete <name>` first. It closes the stable allow only when both its persisted provider
ID and its independently reconstructed full create-time shape still match, verifies the persisted
instance provider ID, deletes that exact incarnation, proves the instance absent, and only then
removes the lifetime deny. A same-ID allow whose shape changed is retained and reported, never
adopted as the expected shape. If reading or closing the allow is unavailable, instance deletion is
still attempted; the allow remains for inspection. A failed or indeterminate instance deletion keeps
the database row and deny for a retry. The boot disk is removed by the instance's explicit
auto-delete setting.

If create rollback or delete cannot prove cleanup, Agentworks prints the project, zone, resource
names, expected provider IDs, and safe next action. Verify each provider ID before any manual
name-based deletion:

```bash
gcloud compute instances describe INSTANCE \
  --project PROJECT --zone ZONE --format='value(id)'
gcloud compute firewall-rules describe RULE \
  --project PROJECT --format='value(id)'
```

Only when the observed ID still equals the expected ID printed by Agentworks may you run the exact
delete command it recommends. Close an owned allow first; delete an owned instance next; remove the
owned deny only after `instances describe` reports not found. This ordering preserves fail-closed
exposure around a possible survivor.

If the expected ID is unknown, the observed ID differs, or a same-name resource has a different
shape, treat it as a collision: inspect ownership and escalate. Do not delete it by name. Matching
shape alone never proves that Agentworks owns a resource, and changing the site's subnet or project
does not change the cleanup target because existing rows retain their canonical project, zone,
network, subnet, original allow-source prefixes, and provider IDs.

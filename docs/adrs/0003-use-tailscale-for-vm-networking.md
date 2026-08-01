# 3. Use Tailscale for VM networking

Date: 2026-03-05

## Status

Accepted

## Context

Agentworks VMs run on diverse platforms (local Lima VMs, Azure cloud VMs, WSL2 distros, remote Lima
VMs on a headless Mac). The operator needs reliable SSH access to all of them from their
workstation, regardless of network topology.

Direct SSH requires either public IPs (security risk, cost, not available for Lima/WSL2), VPNs
(complex setup, platform-specific), or port forwarding (fragile, manual, security concerns). We need
a secure networking layer that works uniformly across all platforms with minimal configuration.

## Decision

All VMs join a Tailscale tailnet during provisioning. After the initial bootstrap (which uses
platform-specific transports), all subsequent access goes over SSH via Tailscale.

## Consequences

- Zero-config mesh networking: VMs are reachable by Tailscale IP from the operator's workstation
  regardless of platform, NAT, firewall, or network topology.
- Secure by default: no open ports, encrypted traffic. Access is controlled via Tailscale ACLs and
  auth keys. Cloud VMs keep their public IP for outbound connectivity (Azure is retiring default
  outbound access, so removing the IP would take the VM offline), but their firewall denies all
  inbound traffic at baseline: no standing allow rule exists. SSH ingress happens only through an
  ephemeral allow rule scoped to the operator's egress IP (plus the `operator.ssh_allow_cidrs`
  config extras), created for cloud-init bootstrap and for each native-transport session, and
  deleted the moment it is no longer needed (Tailscale confirmed, session ended, or the create
  failed).
- Ephemeral key support: VMs can use ephemeral Tailscale keys that auto-deregister on stop, with
  automatic rejoin on start.
- Cross-platform consistency: the same SSH workflow works for local Lima, remote Lima, Azure, and
  WSL2 VMs.
- Dependency: requires Tailscale account and auth keys. This is a hard dependency for VM workspaces
  (local workspaces do not require Tailscale).
- Network disruptions: Azure network changes (firewall-rule toggles, healing a missing public IP)
  can temporarily destabilize Tailscale connectivity. Mitigated by a reconnect wait after network
  changes.

Amended 2026-07-31: this ADR originally said cloud VMs have their public IP removed as soon as
Tailscale is up. Azure's retirement of default outbound access made detached VMs go offline, so the
public IP is now kept for the VM's whole lifetime and exposure is controlled by NSG rules instead: a
permanent deny-all-inbound baseline, with ephemeral IP-scoped allow rules poked for bootstrap and
for transient native routes and deleted after; the consequences above reflect the amended behavior.
The deny baseline also blocks direct inbound (hole-punched) UDP, so Tailscale peer-to-peer paths
degrade to DERP relay (higher latency; reachability is unaffected). Cost consequence: a Standard SKU
static public IP bills continuously, including while the VM is deallocated.

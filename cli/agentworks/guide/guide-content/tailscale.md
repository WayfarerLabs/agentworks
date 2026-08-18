---
description: Configure Agentworks' Tailscale access and move a VM between tailnets safely.
index-order: 18
---

# Tailscale networking

Agentworks uses Tailscale as the routine network path to managed VMs and SSH as the control plane
over each VM's tailnet address. Platform-native access is limited to provisioning and explicit
recovery. This guide does not try to explain Tailscale itself; use the official documentation for
tailnet policy and key administration.

## Choose and store an auth key

A reusable, non-ephemeral auth key is the recommended default for managed VMs. Agentworks imposes no
key-type restriction, so another choice is valid when its Tailscale lifecycle fits the installation.
Review the
[official auth-key documentation](https://tailscale.com/docs/features/access-control/auth-keys)
before choosing differently.

The default VM template expects a secret named `tailscale-auth-key`. Another template may use a
different name. Keep the value in an appropriate secret source and run
`agw secret describe tailscale-auth-key` to inspect the predicted source without resolving it.
Agentworks passes the resolved value only to the Tailscale operation that needs it; it does not
store the value in its database or generated SSH configuration.

## Recover or move a VM's tailnet association

`agw vm rekey NAME` re-enrolls a VM with a separately obtained replacement auth key. Use it to
recover a VM whose ephemeral node expired or move a VM to another tailnet as described below. The
command does not create, rotate, or revoke the auth key. It uses the platform's recovery transport,
records the new tailnet address, refreshes SSH configuration, and verifies Tailscale SSH.
`--ignore-env` bypasses an environment-provided key so another configured secret source can supply
the replacement.

An advanced use case moves a VM to another tailnet. This is useful when workloads need access to
private infrastructure on another tailnet but the operator does not want the entire Agentworks
system there. After initial provisioning, `rekey` can move the VM to that tailnet. The old
connection is lost when the VM leaves its current tailnet, so first confirm the replacement key
joins the intended destination and that the operator can share the machine to an appropriate user on
the original tailnet. If either is unavailable, do not start the rekey.
`agw vm rekey NAME --wait-for-share` pauses after the move so the operator can complete that sharing
before Agentworks verifies connectivity. Consult Tailscale's
[machine-sharing workflow](https://tailscale.com/kb/1084/sharing) for more information.

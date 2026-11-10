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
`agw secret describe tailscale-auth-key` for a value-free, provider-aware preview. The backend may
read and safely discard a provider value while proving existence, but describe never receives it.
Agentworks passes an actually resolved value only to the Tailscale operation that needs it; it does
not store the value in its database or generated SSH configuration.

## Recover or move a VM's tailnet association

`vm start` does not rejoin Tailscale. A VM whose node was removed in the admin console, or whose
ephemeral key's node expired while it was offline, fails its start with a hint; on the running VM,
`agw vm recover NAME` rejoins it with the VM template's auth key and verifies Tailscale SSH.

`agw vm rekey NAME` re-enrolls a VM with a separately obtained replacement auth key. Use it to move
a VM to another tailnet as described below. The command does not create, rotate, or revoke the auth
key. It runs through the platform's own channel, records the new tailnet address, refreshes SSH
configuration, and verifies Tailscale SSH. By default it resolves the VM template's auth-key secret.
`--secret SECRET` selects another declared secret through the normal source chain for this call
only; it does not change the VM template. `--prompt` instead asks for the replacement key through
the terminal, bypassing configured sources for that key. It requires usable terminal input and
cannot be combined with `--secret` or global `--non-interactive`.

An advanced use case moves a VM to another tailnet. This is useful when workloads need access to
private infrastructure on another tailnet but the operator does not want the entire Agentworks
system there. After initial provisioning, `rekey` can move the VM to that tailnet. The old
connection is lost when the VM leaves its current tailnet, so first confirm the replacement key
joins the intended destination and that the operator can share the machine to an appropriate user on
the original tailnet. If either is unavailable, do not start the rekey.
`agw vm rekey NAME --secret DESTINATION_KEY --wait-for-share` pauses after the move so the operator
can complete that sharing before Agentworks verifies connectivity. Consult Tailscale's
[machine-sharing workflow](https://tailscale.com/kb/1084/sharing) for more information.

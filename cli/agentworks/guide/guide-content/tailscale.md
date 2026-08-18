---
description: Use Agentworks' Tailscale credential, routine SSH path, and explicit rekey workflow.
index-order: 18
---

# Tailscale networking

Agentworks uses Tailscale as the routine network fabric for managed VMs and SSH as the control plane
over each VM's tailnet address. Platform-native transport handles provisioning and explicit
recovery; normal VM, workspace, agent, and session operations return to SSH over the tailnet.

For ordinary managed VMs, Agentworks recommends a reusable, non-ephemeral auth key. Agentworks
imposes no key-type restriction and supports other choices, whose lifecycle, approval, and identity
consequences belong to Tailscale. Review the
[official Tailscale auth-key documentation](https://tailscale.com/docs/features/access-control/auth-keys)
before choosing differently. Keep reusable keys in an appropriate secret store and scope their
availability to the authorized operation.

The default VM template names the `tailscale-auth-key` secret; another template may name a different
secret. Agentworks resolves that name through the configured secret-source chain when an operation
needs it, validates it for the line-oriented Tailscale boundary, and delivers it as sensitive input.
It does not print the value or persist it in the Agentworks database or generated SSH configuration.
Use `agw secret describe tailscale-auth-key` for a non-resolving source prediction. Resolve or
verify the secret only when the operator's instruction covers that action.

`agw vm rekey NAME` assigns a separately obtained auth key, switches the VM to that key's tailnet,
or recovers a VM whose ephemeral node expired. Creating or rotating the credential is a separate,
external action that requires operator authorization. The command uses the platform-native recovery
transport, logs out and rejoins Tailscale, records the new tailnet address, refreshes SSH
configuration, and verifies Tailscale SSH. `--ignore-env` bypasses the environment mapping for the
auth-key secret so the configured fallback or prompt source can supply the new value.

Before rekeying to a different tailnet, account for the connectivity break: logging out of the
current tailnet can leave the VM unreachable until sharing or other authorized access is
established. If that disruption and access change are not authorized, stop before rekeying and use
an auth key for a tailnet where the VM is already authorized and reachable.

When the separately obtained key joins a different tailnet, run
`agw vm rekey NAME --wait-for-share`. After the VM joins, Agentworks pauses so the operator can
follow Tailscale's [machine-sharing workflow](https://tailscale.com/kb/1084/sharing) to share that
VM back before the command verifies connectivity. Sharing and its access policy remain
operator-owned external actions; the command does not perform them.

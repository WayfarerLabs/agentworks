---
description: Prepare the workstation, network, SSH identity, and provider access Agentworks needs.
index-order: 16
---

# Agentworks prerequisites

Install Agentworks with Python 3.12 or newer. `uv tool install agentworks-cli` is the recommended
isolated installation; `pipx install agentworks-cli` is also supported. Run `agw version` to confirm
the installed command before configuring resources.

VM operation requires a Tailscale account, a workstation connected to the intended tailnet, and a
Tailscale auth key available through Agentworks' secret sources. For ordinary managed VMs,
Agentworks recommends a reusable, non-ephemeral key. Other Tailscale key choices are supported;
review the
[official auth-key guidance](https://tailscale.com/docs/features/access-control/auth-keys) and
`agw guide show concept-tailscale` before choosing a different lifecycle. Tailscale is the routine
VM network path, while platform-native access is reserved for provisioning and explicit recovery.

The workstation also needs an SSH client and an existing public/private key pair that the operator
authorizes Agentworks to use. `agw config init` creates the settings sample without overwriting an
existing file; its operator fields name those key paths. Do not read or copy private-key content to
check the paths.

Finally, the chosen VM platform needs its own host tool or SDK credentials, provider account,
permissions, quota, and placement inputs. Use `agw resource explain vm-platform` and
`agw resource explain vm-platform/NAME` for the installed platform contracts, then run
`agw doctor --output json` when live workstation checks are in scope. Provider login, plugin
enablement, credential creation, and infrastructure changes remain separate operator-authorized
actions.

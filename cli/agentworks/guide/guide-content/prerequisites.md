---
description: Understand what must be in place before Agentworks can create and operate VMs.
index-order: 16
---

# Prerequisites

Agentworks runs on Linux, macOS, and Windows with Python 3.12 or newer. The remaining prerequisites
are about reaching and operating the VMs where Agentworks-managed workloads run.

## Agentworks CLI

`uv tool install agentworks-cli` is the recommended isolated installation.
`pipx install agentworks-cli` is also supported. Run `agw version` to confirm the active command.

## Tailscale

Routine communication with managed VMs uses Tailscale, including SSH. The workstation must be
connected to the intended tailnet, and Agentworks needs access to a Tailscale auth key when joining
a VM. A reusable, non-ephemeral key is the recommended default, but Agentworks does not restrict
which key type the operator chooses.

See `agw guide show concept-tailscale` and the
[official auth-key documentation](https://tailscale.com/docs/features/access-control/auth-keys) for
the key lifecycle and cross-tailnet workflow. Platform-native access is used during provisioning and
explicit recovery rather than routine operation.

## SSH

The workstation needs an SSH client and a public/private key pair that the operator authorizes
Agentworks to use. The configuration names the two paths; Agentworks does not need to inspect the
private-key contents.

## A place to run VMs

At least one supported VM platform and one configured VM site must be ready. A local platform may
need a host tool, while a cloud platform may need an account, credentials, permissions, quota, and
placement choices. Continue with `agw guide show concept-virtual-machines` for the installed options
and their configuration surfaces.

## Git identities for private repositories

Decide whether each admin or agent user should receive a secret-backed Git credential or use its
active `gh` or `az` CLI identity. CLI-backed credentials require the corresponding command to be
installed and authenticated for that target user; Agentworks does not perform the login. Azure
DevOps identities also need access to the configured organization and repositories.

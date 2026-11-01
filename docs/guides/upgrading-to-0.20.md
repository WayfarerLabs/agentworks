# Upgrading to 0.20

Agentworks 0.20 verifies every connection to a VM against a host-key store of its own instead of
trusting whatever it sees first. Existing VMs need a one-time import before most commands will
connect to them.

## Import existing host keys

Agentworks used to accept a VM's host key on first use and write it to `~/.ssh/known_hosts`. It now
keeps one record per VM under `~/.config/agentworks/known_hosts/`, keyed by VM name, and refuses to
connect to a VM that has no record. A missing record never falls back to first use.

Run the import once, from the workstation where you already connected to your VMs:

```console
agw vm import-host-keys --all
```

For each VM, the command asks OpenSSH's own `ssh-keygen -F` which keys `~/.ssh/known_hosts` holds
for the VM's tailnet address, and copies them into that VM's record. Your `known_hosts` is only
read. Pass `--known-hosts PATH` to read a different file, for example one copied from another
workstation.

Each VM gets one line in the report:

- `imported (N keys)`: the VM now has a record.
- `already current`: the VM already had a record that covers the matching keys. Nothing changed.
- `conflicts with the stored key`: the VM has a record and `known_hosts` disagrees. Nothing changed.
  A different key is worth understanding before you trust it.
- `no matching entry`: `known_hosts` has nothing for the VM's address.
- `trust marker present`: a matching `@cert-authority` or `@revoked` line exists. Nothing is
  imported for that VM.
- `no tailnet address`: the VM has no recorded Tailscale address yet.

The command exits non-zero while any selected VM ends without a record. Run
`agw vm import-host-keys --all` again at any time as a checklist of the VMs still missing one.

## A VM with no matching entry

When `known_hosts` has no entry for a VM, bring it in with a file you have checked:

1. Read the VM's host key fingerprints from its console or its provider.
2. Gather the keys from the VM's tailnet address into a file with `ssh-keyscan`.
3. Compare the file's fingerprints (`ssh-keygen -lf FILE`) with the ones you read in step 1.
4. Run `agw vm import-host-keys NAME --known-hosts FILE`.

The scanned file is trusted as given, so do not skip the comparison.

A VM whose creation failed before its first connection has no record either. Delete it with
`agw vm delete NAME` and create it again.

## What fails until the import runs

Commands that connect to a VM fail with a missing-record error that names this import. That includes
`vm shell`, `vm exec`, `vm reinit`, `vm backup`, `vm port-forward`, the workspace, agent and session
commands, and the activation of a stopped VM by `vm start` or by any command that would start it.
`vm start` and `vm rekey` refuse before they boot the VM or spend a Tailscale auth key. `vm delete`,
`vm list` and `vm describe` do not need a record.

Your own `ssh awvm--NAME` keeps working meanwhile: Agentworks adds the store options to a VM's
managed SSH entry only once the VM has a record. After the import, those entries verify against the
store as well, and your own `ssh` never writes to it.

## New VMs

`vm create` records the VM's key on its first connection, over the tailnet, and writes it to the
store only. Your `~/.ssh/known_hosts` is no longer touched. `vm delete` removes the record.

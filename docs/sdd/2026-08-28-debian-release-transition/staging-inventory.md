# Temporary staging inventory

This implementation inventory classifies active Agentworks paths that use a remote `/tmp`. Trixie
may mount `/tmp` as a memory-backed filesystem, so payload size rather than file lifetime determines
whether a path can remain there.

## Disk-backed payload staging

These size-bearing paths use `/var/tmp` and private `mktemp` names:

- VM workspace backup archives;
- workspace-copy destination archives;
- `Transport.copy_dir_to` archives;
- source-reference git clones;
- detached-command wrappers, status, and output;
- remote Lima host file relays; and
- the Proxmox setup image download.

Each runtime path cleans up on success and failure. The VM backup deliberately preserves its private
archive directory after an error for operator recovery and reports that path.

## Bounded `/tmp` use

The remaining guest or VM-host paths contain bounded control data rather than operator payloads:

- AWS, GCP, Proxmox, WSL2, and cloud-init bootstrap scripts;
- Lima template YAML and detached create control files;
- atomic staging for authorized keys, sysctl, fstab, and Tailscale DNS drop-ins;
- tmux sockets and other Unix-domain control paths.

Those files are limited by Agentworks-authored configuration, path lists, process status, or log
tailing. No workspace archive, arbitrary directory transfer, repository clone, provider image, or
other operator-sized payload remains on `/tmp`.

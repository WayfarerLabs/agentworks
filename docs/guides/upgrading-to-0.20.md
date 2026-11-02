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

The scanned file is trusted as given, so do not skip the comparison. Each line's host field must be
the VM's tailnet address, which is what `ssh-keyscan` prints when you scan that address. A stopped
VM has to be started with the platform's own tools first. On Windows PowerShell 5.1, `>` writes
UTF-16, which `ssh-keygen` cannot read, so write the file with
`ssh-keyscan ADDRESS | Out-File -Encoding ascii FILE`.

A VM whose creation failed before its first connection has no record either. Delete it with
`agw vm delete NAME` and create it again. A cloud VM whose create failed after it joined Tailscale
but before its key was recorded has no record, and deleting it cannot log its node out of Tailscale,
so remove the node in the Tailscale admin console. A VM that cannot rejoin Tailscale cannot be
imported until it can, so recreate it.

## What fails until the import runs

Commands that act on a VM refuse it; commands that only describe or list VMs report the missing
record for that VM and carry on. Every command that reaches a VM over SSH refuses a VM with no
record, before it starts the VM, resolves a secret or changes anything, and prints a hint that names
the import; the exception is `vm shell --platform` on a cloud VM (AWS, Azure, GCP), which resolves
credentials and opens its temporary network rule first and then refuses. That covers `vm shell`,
`vm exec`, `vm reinit`, `vm backup`, `vm port-forward`, `vm start`, `vm rekey`, and the workspace,
agent, session and console commands, including the start of a stopped VM that any of them would
trigger. `vm start` and `vm rekey` check first so they never boot the VM or spend a Tailscale auth
key. `vm describe`, `session describe` and the status listings that span several VMs warn that the
VM has no record, leave out what they would have read over SSH, and carry on.

`vm delete` still works without a record, because deleting is what you do with a broken VM. It
cannot reach a cloud VM (AWS, Azure, GCP) to log its node out of Tailscale, though, so that step is
skipped with a warning. Import first to get the logout, or remove the node in the Tailscale admin
console afterwards.

`agw database restore` onto another workstation carries no host-key records, because the store lives
beside the database, not in it. Copy `~/.config/agentworks/known_hosts/` from the old workstation
along with the database. VMs created on 0.20 have keys only there, because nothing writes
`~/.ssh/known_hosts` any more. For VMs that predate 0.20, the import (from `~/.ssh/known_hosts` or a
verified `--known-hosts` file) still works as the fallback.

Your own `ssh awvm--NAME` keeps working meanwhile: Agentworks adds the store options to a VM's
managed SSH entry only once the VM has a record. After the import, those entries verify against the
store as well, and your own `ssh` never writes to it.

## New VMs

`vm create` records the VM's key on its first connection, over the tailnet, and writes it to the
store only. Your `~/.ssh/known_hosts` is no longer touched. `vm delete` removes the record.

## Connecting for `exec`, `shell` and `port-forward`

`agw vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and `vm port-forward`
now run through a new connection layer that sends each request to the VM over the OpenSSH client's
standard input instead of on the `ssh` command line. `vm shell --platform` keeps its platform-native
path and needs none of what follows. Everything else keeps its current path for now.

### What the six commands need

- **OpenSSH 8.5 or newer on the workstation.** Agentworks uses the first `ssh` on your `PATH`
  (ignoring empty and relative `PATH` entries) and checks its version once per run. A missing or
  older client is refused before anything else happens, with the paths it tried. `agw doctor` shows
  which client Agentworks resolved.
- **The native Windows build on Windows.** Agentworks never searches `PATH` there, because `PATH`
  commonly finds the `ssh` bundled with Git for Windows, whose argument handling breaks these
  commands. It looks for Win32-OpenSSH in `System32\OpenSSH` and then `Program Files\OpenSSH`
  (including the 32-bit file system redirects) and judges each. Windows 10's inbox client is OpenSSH
  8.1, so install the Win32-OpenSSH MSI from <https://github.com/PowerShell/Win32-OpenSSH/releases>.
- **`python3` on the VM.** Every request starts a small Python program on the VM. A VM without
  `python3`, or with one that lacks its standard library, is reported as unable to run Agentworks
  requests. Run `agw vm reinit NAME`, which still works on such a VM and installs it.
- **A supported login shell for the admin and every agent.** The admin and agent template `shell`
  fields now accept only `sh`, `dash`, `bash` and `zsh`; a path such as `/usr/bin/zsh` is read as
  its basename. Any other shell, such as `fish` or `tcsh`, is a configuration error that names the
  field and the allowed values. Accounts that already exist are not checked yet. A stored agent
  instance spec that names another shell, such as `{"shell": "/bin/fish"}`, now reads as
  `instance-spec-malformed`; replace it with `agw agent reinit NAME --spec JSON`.

### What behaves differently

- **No ssh configuration is read.** These commands run `ssh -F none`, so `~/.ssh/config` and the
  system configuration do not apply to them. An `IdentityAgent` set only in `~/.ssh/config`
  (1Password, Secretive) is ignored; the agent named by `SSH_AUTH_SOCK` serves the key. Every
  connection runs in batch mode, so ssh never prompts for a key's passphrase: an encrypted key has
  to be loaded in that agent first. When a connection fails and the configured key is
  passphrase-protected, the error's hint mentions `ssh-add`; `vm port-forward` shows ssh's own log
  instead.
- **Exit statuses.** `vm exec` and `agent exec` still run the command line under the account shell,
  so a command that cannot start exits 127 or 126 with the shell's message, as before. A missing
  workspace directory exits 125 with one line from `agw`. A command that exits 255 is no longer
  confused with a lost connection: under `exec`, a lost connection now exits 1 with an error from
  `agw` instead of 255. `vm shell` and `agent shell` still return 255 for both and print a notice.
- **The guest environment is rebuilt.** The command sees a fixed `PATH`
  (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/games:/usr/games`),
  `LANG=C.UTF-8`, `SHLVL=1`, the account's `HOME`, `USER`, `LOGNAME` and `SHELL`, and your
  environment on top. It no longer sees `SSH_CLIENT`, `SSH_CONNECTION`, `/etc/environment` or other
  `pam_env` variables, or anything the account's shell exported while sshd ran it to start the
  request. Login startup files still run where the command asks for a login shell: `agent exec`,
  `vm shell` and `agent shell` read them after the environment is rebuilt, and zsh reads `.zshenv`
  for `vm exec`. `agw vm reinit` warns, without failing, when `/etc/environment`,
  `/etc/security/pam_env.conf` or the admin's `~/.pam_environment` holds settings, since these
  commands no longer apply them. The home file is checked only where sshd applies it (Debian 12).
  Lima's `env:` settings and its proxy propagation are written into `/etc/environment`, so on such a
  VM they no longer reach `agw` commands and reinit warns. Check now if you rely on them: the
  warning appears only when you run `vm reinit`.
- **`vm exec` takes its `PATH` from the fixed one.** `vm exec VM prog arg...` and
  `vm exec VM 'one argument'` are, as before, shell command lines run by the admin's default shell
  without login startup, so builtins and shell functions work. What changes is where `PATH` comes
  from: sshd's `PATH` plus `pam_env` before, the fixed `PATH` above now, so additions made only in
  profile files are not applied; use `vm exec VM bash -lc '...'` to get them. `agent exec` still
  runs in the agent's login shell.
- **Large environment values are refused up front.** One `NAME=VALUE` environment string over
  131,071 bytes is refused before anything runs, because Linux cannot execute it.
- **`vm shell` and `agent shell` without `--workspace` no longer print the message of the day or the
  last-login line.** sshd prints them only when it runs the login shell itself.
- **`vm port-forward` exits 0 when you stop it** with Ctrl-C or `SIGTERM`, exits non-zero when a
  forward cannot be set up (a busy local port, for example) instead of running with nothing
  forwarded.

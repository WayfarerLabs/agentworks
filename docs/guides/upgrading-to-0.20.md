# Upgrading to 0.20

Agentworks 0.20 verifies every connection to a VM against a host-key store of its own instead of
trusting whatever it sees first. Existing VMs need a one-time import before most commands will
connect to them. Third-party VM platform plugins must move to vm-platform contract version 2 (see
[Third-party VM platforms](#third-party-vm-platforms)), and third-party harness integrations, Git
credential providers and secret backends to new contract versions as well (see
[their section](#third-party-harness-integrations-git-credential-providers-and-secret-backends)).

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
read. It runs the `ssh-keygen` installed beside the OpenSSH client Agentworks connects with, so it
needs the same supported client (OpenSSH 8.5 or newer) that connecting does. Pass
`--known-hosts PATH` to read a different file, for example one copied from another workstation.

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
so remove the node in the Tailscale admin console. For a VM that cannot rejoin Tailscale, or has no
entry to import, run `agw vm recover NAME`: it reads the host keys through the platform's own
channel, records them once you confirm their fingerprints against the VM's console, and rejoins the
node.

## What fails until the import runs

Commands that act on a VM refuse it; commands that only describe or list VMs report the missing
record for that VM and carry on. Every command that reaches a VM over SSH refuses a VM with no
record, before it starts the VM, resolves a secret or changes anything, and prints a hint that names
the import. That covers `vm shell`, `vm exec`, `vm reinit`, `vm backup`, `vm port-forward`,
`vm rekey`, and the workspace, agent, session and console commands, including the start of a stopped
VM that any of them would trigger; `vm rekey` checks first so it never spends a Tailscale auth key.
Two commands differ. `vm start` resolves no key at all: it starts the VM, and its wait then refuses
with the same hint, so the VM is left running. `vm shell --platform` on a cloud VM (AWS, Azure, GCP)
resolves the site's credentials first and then refuses, before it opens any network rule.
`agw vm recover NAME` is the one command that repairs a VM without a record, starting it first if it
is stopped: it reads the keys through the platform and records them once you confirm their
fingerprints, without a file to import. `vm describe`, `session describe` and the status listings
that span several VMs warn that the VM has no record, leave out what they would have read over SSH,
and carry on.

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

## Connecting through the new layer

`agw vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection`, `vm port-forward`,
`vm create`, `vm reinit`, `vm confirm-release`, `vm start`, `vm rekey` and `vm recover` now reach
the VM through a new connection layer that sends each request over the OpenSSH client's standard
input instead of on the `ssh` command line. For `vm create` that is the checks of the VM after the
platform has created it and joined it to the tailnet; for `vm reinit`, its checks of the VM (see
[Create, reinit and the VM contract](#create-reinit-and-the-vm-contract)). For both it is also every
step of initialization, and `agent create` and `agent reinit` run all of agent initialization, as
the admin and then as the agent, through it (see [Initialization steps](#initialization-steps)).
`agent delete` and the workspace group grants run through it. `workspace create`,
`workspace repair`, `workspace rehome` and `workspace copy` apply the workspace ACLs through it, and
`session create` and the session launches set up the tmux socket directories through it. `vm start`,
and the automatic start of a VM that stopped on its own, use it to wait for the VM to answer;
`vm rekey` and `vm recover` use it for the same wait after their scripts, which run through the
platform's own channel (see [Start, recover, rekey and delete](#start-recover-rekey-and-delete)).
`vm shell --platform` uses the platform's own channel instead and needs no `python3`; on a cloud VM
that channel is SSH to its public address under the same rules. Everything else keeps its current
path for now.

### What these commands need

Each of them checks the client first, so an unsupported one is refused before `vm create` builds a
VM, `vm start` starts one, or `vm rekey` and `vm recover` spend a Tailscale auth key. The automatic
start, which other commands trigger, is the exception: it checks the client at its wait, once the VM
has started.

- **OpenSSH 8.5 or newer on the workstation.** Agentworks uses the first `ssh` on your `PATH`
  (ignoring empty and relative `PATH` entries) and checks its version once per run. A missing or
  older client is refused before anything else happens, with the paths it tried. `agw doctor` shows
  which client Agentworks resolved.
- **The native Windows build on Windows.** These commands never search `PATH` for `ssh` there,
  because `PATH` commonly finds the `ssh` bundled with Git for Windows, whose argument handling
  breaks them. It looks for Win32-OpenSSH in `System32\OpenSSH` and then `Program Files\OpenSSH`
  (including the 32-bit file system redirects) and judges each. Windows 10's inbox client is OpenSSH
  8.1, so install the Win32-OpenSSH MSI from <https://github.com/PowerShell/Win32-OpenSSH/releases>.
- **No program from the current directory.** Windows looks a bare program name up in the current
  directory before `PATH`, and on POSIX an empty or relative `PATH` entry means the current
  directory. Agentworks now starts every workstation program by its path in an absolute `PATH`
  directory: `tailscale`, PowerShell, the 1Password CLI `op`, `git`, `taskkill`, local Lima's
  `limactl`, VS Code's `code`, your `EDITOR`, and the `ssh` and `scp` of the requests that still use
  the older SSH path (sessions, workspaces and backups). A program found only in the current
  directory counts as not installed, so `agw doctor` and the 1Password backend's readiness no longer
  vouch for one. An `EDITOR` given as a path is run as given. On Windows,
  `workspace create --open-vscode` does not start VS Code: its `code` command is a batch file, which
  only `cmd.exe` can run, and `cmd.exe` parses the workspace path by its own rules. The warning
  names the workspace file to open in VS Code yourself.
- **`python3` on the VM.** Every request starts a small Python program on the VM. A VM without
  `python3`, or with one that lacks its standard library, is reported as unable to run Agentworks
  requests. New VMs get it at provisioning. `vm reinit` and `vm confirm-release` now connect the
  same way (see below), so they cannot run on such a VM either, and the wait of `vm start`, of the
  automatic start and of `vm rekey` fails on one. `agw vm recover NAME` installs it through the
  platform's own channel before its own wait.
- **A supported login shell for the admin and every agent.** The admin and agent template `shell`
  fields now accept only `sh`, `dash`, `bash` and `zsh`; a path such as `/usr/bin/zsh` is read as
  its basename. Any other shell, such as `fish` or `tcsh`, is a configuration error that names the
  field and the allowed values. `vm reinit` warns about an existing admin or agent account whose
  login shell is another one, naming the command that sets it from its template. A stored agent
  instance spec that names another shell, such as `{"shell": "/bin/fish"}`, now reads as
  `instance-spec-malformed`; replace it with `agw agent reinit NAME --spec JSON`.

### What behaves differently

- **No ssh configuration is read.** These commands run `ssh -F none` (for `vm create`, `vm reinit`,
  `vm start`, `vm rekey` and `vm recover`, the connections the new layer makes), so `~/.ssh/config`
  and the system configuration do not apply to them. An `IdentityAgent` set only in `~/.ssh/config`
  (1Password, Secretive) is ignored; the agent named by `SSH_AUTH_SOCK` serves the key. Every
  connection runs in batch mode, so ssh never prompts for a key's passphrase: an encrypted key has
  to be loaded in that agent first. A VM that refuses the configured key is reported as such, and
  nothing ran on it. When a connection fails or the key is refused, and the configured key is
  passphrase-protected, the error's hint mentions `ssh-add`; `vm port-forward` shows ssh's own log
  instead.
- **Windows passes arguments through unexpanded.** `agw` no longer expands `*`, `?`, `~`, `%VAR%` or
  `$VAR` in its own arguments on Windows, which it did before forwarding them, even after `--`. A
  quoted `*` now reaches the guest as `*`, as on macOS and Linux, where the shell decides. Expand a
  pattern yourself (PowerShell's `Get-ChildItem`, for example) where you relied on `agw` doing it,
  and write `$HOME\...` (PowerShell) or `%USERPROFILE%\...` (cmd) instead of `~` in a path such as
  `database restore`'s.
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
  without login startup, so builtins, shell functions and a leading `NAME=value` work; the one
  change in quoting is that a word starting with `=` is always quoted (in `agent exec` too), so zsh
  no longer expands it to a path. What changes is where `PATH` comes from: sshd's `PATH` plus
  `pam_env` before, the fixed `PATH` above now, so additions made only in profile files are not
  applied; use `vm exec VM bash -lc '...'` to get them. `agent exec` still runs in the agent's login
  shell.
- **Large environment values are refused up front.** One `NAME=VALUE` environment string over
  131,071 bytes is refused before anything runs, because Linux cannot execute it.
- **`vm shell` and `agent shell` without `--workspace` no longer print the message of the day or the
  last-login line.** sshd prints them only when it runs the login shell itself.
- **`vm port-forward` exits 0 when you stop it** with Ctrl-C, on macOS and Linux also with `SIGTERM`
  or `SIGHUP` (a closed terminal), and on Windows also with Ctrl-Break; started under `nohup`, it
  keeps running after its terminal closes. It exits non-zero when a forward cannot be set up (a busy
  local port, for example) instead of running with nothing forwarded.

## Create, reinit and the VM contract

`vm create`, `vm reinit` and `vm confirm-release` reach the VM through the same connection layer.

- **Create** installs `python3` while it provisions the VM, then connects over SSH at the VM's
  tailnet address. The first connection records the host key, and the VM contract's base checks
  follow: the Debian release first, then systemd, passwordless `sudo` for the admin, and the tools
  Agentworks runs (`install`, `flock`, `nohup`, `setsid`, `tar`). Provisioning is complete only once
  they pass. A VM that fails them is kept in a failed state, and one whose create is interrupted or
  killed before then is kept with its provisioning incomplete. `vm start`, `vm stop`, `vm rekey`,
  `vm recover`, `vm reinit`, `vm exec`, `vm shell` (with `--platform` too), `vm port-forward`,
  `vm backup` and `vm confirm-release` refuse either, while `vm describe`, `vm list` and
  `vm verify-connection` still run; delete it with `agw vm delete NAME`. A create killed before its
  platform finished leaves a row that records nothing addressing the backend: `vm delete` refuses
  it, and `vm delete --force NAME` removes the row and its host-key record and warns with what may
  remain on its vm-site, for you to remove there. When the check that failed is a missing `python3`
  or a missing host-key record, the error gives only that delete-and-recreate guidance, since
  `vm recover` refuses a VM whose provisioning never completed.
- **Cloud create** (AWS, Azure, GCP) waits up to 10 minutes for the new VM to answer over SSH at its
  public address, instead of 30 tries 10 seconds apart. It fails at once, instead of trying again,
  when the admin cannot run `sudo` without a password or the VM's host key changes during the
  create.
- **Proxmox create** no longer waits for the guest agent to report the VM's LAN address, and no
  longer prints it.
- **Reinit** starts with the same checks on the VM as it is. A live release that differs from the
  recorded one refuses first, as before. A failed check stops reinit before it changes anything and
  names recreation. The pam_env settings and an unsupported login shell only warn.
- **The operation log** records each of these requests by its shape: the command's arguments, or a
  script's shell, with the working directory, the names of its environment variables and its
  deadline. A script's text and an environment variable's value are not written. Requests still on
  the older SSH path (sessions, workspaces and backups) log as before.

### Initialization steps

Every step of `vm create` and `vm reinit` initialization, and of `agent create` and `agent reinit`,
runs through the new layer. Agent initialization runs as the admin only to create the account and
place its `authorized_keys`; every later step runs as the agent, which cannot run anything as root.

- **System files are written without following links.** The files initialization owns under `/etc`
  (the cloud-init host-key drop-in, the sysctl baseline, `/etc/fstab`'s `/proc` line, the tailscaled
  drop-in, the sshd and sudoers fragments, the identity profile, the `/etc/zsh/zprofile` block, the
  skel seeds, the grub SVE drop-in and the apt source lists) are written by the file program, which
  refuses a symbolic link at the file or any parent; a sudoers fragment is staged that way and
  renamed into place once `visudo` accepts it. A refused file is a step warning, and the link's
  target is left as it is.
- **sshd is reloaded only when its fragment changes.** The sshd fragment is written, checked with
  `sshd -t` and sshd reloaded only when its content differs, where every initialization used to
  reload sshd. It leaves no `.bak` file.
- **A file whose reload fails is put back.** When `sshd -t` or the sshd reload, loading the sysctl
  baseline (now `sysctl -p` of that file alone, not `sysctl --system`), or `systemctl daemon-reload`
  after the tailscaled drop-in fails, times out or is interrupted, the file gets its previous
  content back (or is removed if it is new), with a warning, so the next `vm reinit` tries again. If
  putting it back fails, a separate warning names the file, which then holds content that is not in
  effect and that the next `vm reinit` will not retry: put its previous content back or remove it,
  then reinit. If something else changed the file in the meantime, nothing is put back and the next
  `vm reinit` tries again.
- **`/etc/fstab` keeps its owner, group and mode** when its `/proc` line changes, and a `hidepid`
  value already on that line is kept, a name such as `hidepid=invisible` included; only a missing
  `hidepid`, `hidepid=0` or `hidepid=off` becomes `hidepid=1`.
- **Step warnings show what failed.** A failed step's warning ends with the last three lines of the
  command's error output, with secrets removed.
- **The DNS check warns when it cannot run.** A DNS probe that fails to run or times out warns and
  initialization goes on. The issue #117 latch still stops it. A mise lockfile step that cannot
  create its directory warns too.
- **Some failures stop initialization instead of warning.** A host key that does not match the VM's
  record, a VM with no record, and a VM that cannot run Agentworks requests fail initialization at
  the first request, with that error, instead of turning every later step into a warning. A failed
  install of the required system packages stops it, as before.
- **Linked rc files are not written through.** The source lines that make `~/.profile`, `~/.bashrc`,
  `~/.zprofile` and `~/.zshrc` load the Agentworks fragments are never appended through a symbolic
  link. When one of those files is a link (for example into a dotfiles checkout) and the file it
  links to already sources the fragment, initialization accepts it. When it does not, initialization
  warns, naming the line to add, and stays partial: until that line is in your dotfiles, that
  account's login shells lack `AGENTWORKS_AGENT` and the `PATH` entries of its install commands and
  mise. Add the line from the warning, such as `. /home/agentworks/.agentworks-profile.sh`, to the
  file the link points to, then reinit. The old behavior edited the checkout through the link, which
  then blocked its next `git pull`.
- **Other links in a home are refused too.** The profile and rc fragments, the mise config and
  lockfile, `authorized_keys` and the agent's `.ssh` are written by the file program, which refuses
  a link at the file or any parent. A mise config that is a link (or sits in a linked directory) is
  not written, with its own warning, so `mise_packages` does not reach it until the template drops
  `mise_packages` or the dotfiles drop the link. A local dotfiles destination that is a link is
  refused, and a git-sourced `mise.lock` that is a link in its repository is refused with a warning.
  Git checkouts and the apt key downloads follow links, as before.
- **A source line that cannot be added warns.** A failed source-line append used to be silent; it
  now warns and leaves initialization partial. A bash account no longer gets a `~/.zprofile` or
  `~/.zshrc` created for it.
- **Files in the home get a fixed mode.** The admin's profile and rc fragments and mise config, and
  the mise lockfile from either source, are written 0644. A `PATH` entry contributed twice keeps the
  precedence of its last occurrence for the admin too, as it already did for agents.
- **Local dotfiles are replaced through a temporary copy.** A local `dotfiles_source` directory is
  unpacked beside the destination first, and the previous tree is put back if the upload fails or is
  interrupted; a run killed outright leaves the previous tree beside the destination, and the next
  reinit moves it back. The replacement no longer leaves the reinit partial.
- **`dotfiles_destination` is checked when the template loads.** It is a path in the home (`dots` or
  `~/dots`) or an absolute path. It may not name the home itself or one of its parents, may not be
  or hold a path Agentworks manages in the home (`~/.ssh`, `~/.config/mise`, `~/.agentworks` and the
  `~/.agentworks-*.sh` fragments), and may not contain an empty, `.` or `..` component; a trailing
  `/` is dropped, and only its leading `~` is expanded. A stored destination such as `dots` keeps
  meaning `~/dots`.
- **Git source URLs may not carry credentials.** A `git::https://` source whose URL holds a user
  name or token, such as `https://user:token@github.com/...`, is refused when the template loads,
  without echoing it; declare a `git-credential` resource for the host instead. A subpath with an
  empty or `.` component is refused too, as one with `..` was.
- **A git dotfiles checkout follows its `ref`.** Every sync, fresh clone or reinit, fetches the
  template's `ref` (the remote's default branch when there is none) and checks it out detached, so a
  changed or dropped `ref` takes effect, a branch advances and a tag is checked out. A commit `ref`
  must be the full hash. The admin used to pull whatever branch the clone was on, so a local branch
  in the checkout is no longer advanced; commit your dotfiles changes upstream. A failed sync now
  skips the dotfiles install for agents too, as it does for the admin, instead of running it over
  the previous checkout.
- **A dotfiles clone whose origin holds a credential is refused.** An existing checkout must be a
  clone of the `dotfiles_source` URL itself; one cloned from a URL with a user name or token in it
  is refused, without echoing either URL. Point it at the plain URL with
  `git -C <dotfiles_destination> remote set-url origin <url>` and declare a `git-credential`
  resource for the host.
- **The admin's dotfiles install runs in a login shell,** in the checkout, as the agent's always
  has, with the new layer's environment and fixed `PATH` underneath it.
- **Git credentials reconcile under bash** for every account, so an admin or agent whose login shell
  is `sh` or `dash` gets its credentials.
- **Agent install commands check completion as the admin's do.** An agent's `test_exec` check runs
  in an interactive login shell (`-lic`), as the admin's always has, and the agent's mise config,
  lockfile and install follow the admin's steps; its profile, rc and mise warnings now also reach
  the agent's operation log.
- **A missing login shell warns.** Setting the admin's login shell to one that is not installed
  fails the shell configuration step with a warning, instead of running `usermod` with an empty
  shell.

## Start, recover, rekey and delete

`vm start`, the automatic start, `vm rekey` and `vm recover` wait for the VM to answer through the
new connection layer, so they need what it needs (see
[What these commands need](#what-these-commands-need)): OpenSSH 8.5 or newer on the workstation and
`python3` on the VM. Their connections run `ssh -F none` in batch mode, so `~/.ssh/config` does not
apply and an encrypted key must be loaded in the SSH agent. `vm start` checks the client, and that
the workstation is on Tailscale, before it starts anything; `vm rekey` and `vm recover` check both
before they resolve the auth key.

- **`vm start` no longer rejoins Tailscale.** It, and the automatic start of a VM that stopped on
  its own, start the VM and wait up to about four minutes for it to answer over SSH. A VM that does
  not answer fails with a hint to run `agw vm recover NAME`, and is left running. That includes a VM
  on an ephemeral auth key whose node expired while it was offline, and one whose node was removed
  in the Tailscale admin console. No start resolves the auth key any more.
- **`agw vm recover NAME`** is new. It starts a stopped VM first; then, through the platform's own
  channel, it records missing host keys once you confirm their fingerprints, then rewrites the
  admin's `authorized_keys` with your configured keys as `vm reinit` does (removing a key you
  replaced), restarts a broken SSH server, installs `python3`, starts `tailscaled` if it is not
  running, and rejoins Tailscale only when the node needs a login. Then it waits once for the VM to
  answer. See the [command reference](../../cli/command-reference.md) for what it refuses.
- **`vm rekey`** runs its steps as one script through the platform's own channel and keeps an
  operation log. When the VM does not answer over SSH at its new address after the rekey, the
  command fails with a hint to run `agw vm recover NAME`, instead of warning and exiting 0.
- **A node whose address cannot be read is logged out.** When `vm create`, `vm rekey` or
  `vm recover` joins the node but cannot read its tailnet address, it logs the node out and fails
  with a hint for the rerun: `agw vm recover NAME` after a rekey or a recover, and a fresh auth key
  (for a single-use one) before running a create again. When that logout fails too, the hint first
  names the node to remove in the Tailscale admin console, then gives the same rerun step.
- **An empty auth key is refused.** `vm rekey` and `vm recover` refuse an empty or blank Tailscale
  auth key, as `vm create` does.
- **`vm shell --platform` no longer takes `--workspace`.** Combining the two is refused; `cd` into
  the workspace directory inside the shell instead. The shell gets the account's own login
  environment, with nothing from the VM template or admin settings and no secret resolved for it. On
  a cloud VM it connects as every other VM connection does: `ssh -F none` with your configured key,
  so `~/.ssh/config` does not apply and an encrypted key must be loaded in the SSH agent, and it
  refuses a VM with no recorded host key. A platform without a native shell (Proxmox) is refused
  before anything is resolved.
- **`vm delete` keeps a VM whose backend it could not delete, unless forced.** An unset token
  secret, a removed vm-site or a disabled plugin used to warn and remove the row while the VM kept
  running. Now delete keeps the row and its host-key record whenever the backend VM was not deleted,
  a site that cannot be bound included, naming the backend identifiers the row holds; fix the cause
  and run it again, or remove the backend yourself and pass `--force`. `vm delete --force` now has
  one meaning: it also deletes the VM's children, and removes the row and the host-key record even
  when the backend delete could not complete, warning with those identifiers.
- **`vm delete`** logs the node out of Tailscale over the platform's own channel before the backend
  delete, and no longer stops each session first. It reports whether the logout worked, and goes on
  with the delete either way. It does not log out a cloud VM whose host key was never recorded, and
  names the node to remove in the Tailscale admin console instead. It reads the VM's power state
  once it holds the VM, so a WSL2 distro that the hold boots is still logged out; a VM that is still
  stopped or deallocated is not, and the warning names its node the same way. A backend delete that
  fails after the logout says how to finish the delete, and that `agw vm rekey NAME` puts the VM
  back on Tailscale instead.
- **A cloud VM's temporary SSH allow closes on `SIGTERM`, `SIGHUP` and Ctrl-Break.** Every allow
  Agentworks opens to a cloud VM (AWS, Azure, GCP) is removed when the command is ended on macOS and
  Linux by `SIGTERM` or `SIGHUP` (a closed terminal), or on Windows by Ctrl-Break, as it is on
  Ctrl-C: `vm create` rolls back, bootstrap allow included, and a root script (`vm recover`,
  `vm rekey`, the logout at `vm delete`) or a `vm shell --platform` session removes its own allow.
  Once a signal has started that cleanup, Ctrl-C included, a `SIGTERM`, `SIGHUP` or Ctrl-Break is
  ignored, so a closed terminal cannot cut the removal short; a second Ctrl-C still abandons the
  cleanup, as its message says, though one that lands while a firewall rule's removal is in flight
  waits for that removal to finish first (Azure and GCP bound that wait, then warn that the rule may
  remain). Closing the console window on Windows may start that cleanup, but Windows ends the
  process within seconds, so it cannot be relied on to finish: end the command with Ctrl-C or
  Ctrl-Break instead, and delete a rule a closed window left behind from the VM's firewall in the
  provider's console.
- **Output nobody reads no longer fails a command.** When the reader of `agw`'s output has gone (a
  pipe into `head` that has exited, `2>&1 | tee create.log` in a closed window, or the closed
  terminal itself), the rest of that output is dropped and the command carries on to the end, a
  create's rollback included, with the exit status it would have had. Output redirected to a file
  that cannot be written, such as on a full disk, still fails the command.

## Native harness setup

Artifact discovery for Claude Code, Codex and Grok Build runs the VM's `/usr/bin/python3` after the
account's login and interactive startup files. It no longer runs the first `python3` on the login
`PATH`, so an interpreter a version manager puts first is not used for it.

Artifact files, generated instruction sections and mapped native settings are published, checked and
retired by one small Python program per operation, with the content sent on its input instead of
staged in a temporary directory on the VM. Every such write on a VM, by any account, takes one lock,
`/run/lock/agentworks-files.lock`; one that cannot take it within 30 seconds fails, saying that
another Agentworks operation is writing files on the VM, and changes nothing.

## Third-party harness integrations, Git credential providers and secret backends

Every capability's stages receive a `RunContext`, whose execution accessors changed: a stage that
needs to reach the VM reads `ctx.admin_access()` or `ctx.agent_access()`, each that identity's
`VMAccess` from `agentworks.execution` or `None`, and `ctx.admin_target()` and `ctx.agent_target()`
are gone. Registration matches contract versions exactly, so each kind's version moved and an
implementation that still declares the old one is refused. Every shipped implementation already
declares the new one.

| Kind                | Old version | New version |
| ------------------- | ----------- | ----------- |
| harness-integration | 7           | 8           |
| git-credential      | 3           | 4           |
| secret-backend      | 1           | 3           |

Secret-backend skips 2, the number of the incompatible 0.14 contract, so no implementation written
for it can register. Beyond the version and the accessors:

- **Harness integrations.** `_probe_target(access)` receives the launch target's `VMAccess` and
  passes it to `require_commands(commands, access, ...)`. A setup facet's invocation carries
  `access`, `files`, that access's file operations under setup's per-request bound, and
  `redactions`, every secret the operation resolved or logs, to sanitize any guest text an error
  quotes, in place of `runner`; `request_environment(local)` gives a request's `env`, the setup
  environment with the facet's own variables but never an `AGENTWORKS_*` one, which the runner
  applied to every command. `SetupReadiness` no longer carries a runner. `probe_native` takes a
  `VMAccess` first and the invocation's `redactions`. `check_required_commands` and
  `run_user_shell_command` take a `VMAccess` and an optional `deadline` in seconds, and
  `run_user_shell_command` returns the command's exit status.
- **Git credential providers.** A provider's runup check reads the access the same way.
- **Secret backends.** Nothing else changes.

For a harness integration's or a Git credential provider's command checks, an indeterminate check
raises `ExternalError` and a request that may not have run raises its typed execution error. Code
that only warns catches `ConnectivityError`, `ExternalError` and `VMNotReady`, as the shipped
providers do, and lets every other error propagate; `VMNotReady` is on the list because a VM without
`python3` counts as indeterminate.

The capability READMEs under `cli/agentworks/capabilities/` describe each contract in full.

## Third-party VM platforms

The vm-platform capability is now contract version 2. Registration matches contract versions
exactly, so a platform that still declares version 1 is refused, and there is no version 1 adapter.
Every shipped platform already declares version 2. Change every third-party platform class to
declare:

```python
from typing import ClassVar

contract_version: ClassVar[int] = 2
```

and make the changes below. The docstrings in
[`cli/agentworks/capabilities/vm_platform/base.py`](../../cli/agentworks/capabilities/vm_platform/base.py)
state the full contract, and the
[vm-platform README](../../cli/agentworks/capabilities/vm_platform/README.md) its obligations.

### A root channel replaces the native transport

- **Implement `run_as_root(vm, ctx, script, deadline) -> RawRun`.** It is abstract and required. It
  runs `/bin/bash -s` as root through the platform's own channel, without the VM's Tailscale, with
  `script` on its stdin and nowhere else; it runs the script once and returns both streams with no
  exit status. The platform's own create-time join runs through it, and so do `vm recover`,
  `vm rekey` and the logout at `vm delete`.
- **Declare `recovery_needs_guest_sshd`.** It is `True` when that channel goes through the guest's
  sshd. It has no default, and registration refuses a platform that leaves it out.
- **`native_transport()` is gone,** along with `transient_route()` and `probe_failure_hint`. A cloud
  whose channel is SSH to the VM's public address subclasses `public_ssh.PublicAddressPlatform`, as
  the AWS, Azure and GCP platforms do, and supplies its route as `_transient_route`, its live
  address as `_public_address`, and `public_address_hint`.
- **Override `native_shell(vm, ctx) -> int` to offer `vm shell --platform`.** It is optional: the
  default offers no shell, and a platform without one should set `native_shell_unavailable_hint` to
  say where its operator reaches a VM by hand.
- **`read_host_keys(vm, ctx)`** has a default over `run_as_root`, used by `vm recover`. Override it
  only when the channel itself verifies the host key it is reading.
- **The platform transports are removed.** `agentworks.transports` no longer exports
  `ExecTransport`, `LimaTransport`, `RemoteLimaTransport`, `WSL2Transport`, `native_transport`,
  `wait_for_reconnect` or `TailscaleWait`, and `agentworks.plugins.proxmox.transport` is gone.

### Create returns an address

- **`ProvisionResult.tailnet_address` replaces `tailscale_ip`,** and the result no longer carries a
  transport. The address is required. `create()` joins Tailscale once through `run_as_root` with
  `tailscale_join.join_tailscale`, reads the node's address with `settle_tailnet_address`, and
  returns it. Core validates it, reaches the VM over SSH there, and checks the live Debian release
  itself.
- **A cloud's create finishes through `complete_cloud_bootstrap(target, auth_key, node=...)`.** Its
  `target` is a `public_ssh.PublicSSH` to the VM's public address under
  `request.provisioning_trust`. It waits with
  `PublicSSH.wait_until_answering(seconds=..., retry_refused_key=True)`, where a refused key raises
  `public_ssh.KeyRefused` and is retried until the bound, since a cloud's guest agent may install
  the key after sshd starts; then it waits for cloud-init, joins and settles the address. A
  cloud-init that finished with recoverable errors (exit status 2) warns and lets create carry on;
  any other failure's error quotes the exit status and cloud-init's own detail and errors
  (`cloud-init status --wait --long`), since the rollback then removes the VM.
- **`close_provisioning_access(vm, ctx)` replaces `post_tailscale_ready` and `secure_failed_vm`.**
  Phase A calls it once with the create's own context, however Phase A ends: after the VM contract
  passes, when Phase A fails, and when it is interrupted. A platform that opens provisioning access
  in `create`, such as a bootstrap firewall allow, closes it there. A platform built on
  `public_ssh.PublicAddressPlatform` has it called again after each root-channel route, so it must
  do nothing, silently, when the access is already closed. Rename an override of either old hook; a
  platform that overrode both with the same body keeps one. Registration refuses a platform that
  still defines either old name, naming `close_provisioning_access`, since nothing calls them any
  more.
- **`generate_bootstrap_script()` no longer takes `tailscale_auth_key`.** The script it generates
  holds no secret and stops once Tailscale is installed, so a provider may retain it.
  `run_bootstrap()` runs it through a root channel, and `parse_bootstrap_steps()` replaces
  `parse_bootstrap_output()` and `BootstrapResult`. `PROVISIONING_PACKAGES` now includes `python3`,
  which every request core sends needs.
- **`ProvisionRequest.ssh_private_key` is a `Path`,** never `None`.
- **`ProvisionRequest.provisioning_trust` is new.** It is the unverified host-key binding a platform
  uses when its create reaches the VM over a public address before core has recorded the VM's key.

# Upgrading to 0.20

Agentworks 0.20 reaches every VM through one new SSH connection layer, which carries each request,
environment included, on the OpenSSH client's standard input and verifies every VM against a
host-key store of its own instead of trusting whatever key it sees first. It also adds stored
runtime intent for VMs, sessions and named consoles, and runs tmux clients aimed at an agent
session's server as that agent. Third-party VM platform plugins move to vm-platform contract version
2, and third-party harness integrations, Git credential providers and secret backends to new
contract versions as well.

## Upgrade steps

Work through these in order; the sections after this list give the detail.

1. **Migrate the database and keep its backup.** Run `agw vm list`, which reads only the
   `[database]` settings of your configuration. It migrates the state database, first offering a
   backup, or taking one without asking in a non-interactive run while
   `[database] auto_backup_before_migration` is on (the default). Keep that backup for
   [Rollback](#rollback).
2. **Run `agw doctor` and fix what it reports.** It shows the OpenSSH client Agentworks resolved:
   every command that reaches a VM needs OpenSSH 8.5 or newer, on Windows the Win32-OpenSSH build
   ([What these commands need](#what-these-commands-need)). No ssh configuration applies to a VM
   connection, so the agent named by `SSH_AUTH_SOCK` serves the configured key, and an encrypted key
   must already be loaded there. Doctor does not check that the agent holds the key, so load an
   encrypted one now with `ssh-add`. Doctor also reports configuration this release refuses
   outright, such as a relative, `/` or `..` `paths.vm_workspaces`, which stops every command that
   loads the configuration ([Workspaces](#workspaces)), and stored agent or admin specs that need
   replacing before `vm reinit` or `agent reinit` will run
   ([Initialization steps](#initialization-steps)).
3. **Import host keys.** Run `agw vm import-host-keys --all` until it reports a record for every VM.
   Until a VM has one, every command that reaches it refuses it. Each run also rewrites the managed
   SSH entries, so your own `ssh` through them verifies against the store from then on
   ([Import existing host keys](#import-existing-host-keys)). A VM with nothing to import, or one
   that cannot rejoin Tailscale, gets its record from `agw vm recover NAME`.
4. **Reinit each existing VM once.** `agw vm reinit NAME` removes the sshd and sudoers grants and
   the admin's group access to agent tmux servers that this release no longer uses, which changes
   what your own `ssh` and `sudo` pass on
   ([Reinit retires grants nothing uses](#reinit-retires-grants-nothing-uses)). Read its warnings:
   it reports settings in `/etc/environment` and other `pam_env` files that commands no longer
   apply, and login shells Agentworks does not support
   ([What behaves differently](#what-behaves-differently)). A VM that reinit reports as unable to
   run Agentworks requests lacks `python3`; `agw vm recover NAME` installs it, then reinit again.
5. **Restart each running named console** with `agw console restart NAME`, once its VM has been
   reinitialized ([Named consoles](#named-consoles)). Restarting records that the console should
   run.
6. **Adopt desired runtime state** for existing sessions and named consoles, preview, and reconcile
   ([Adopt desired runtime state](#adopt-desired-runtime-state)).
7. **Refresh shell completions** with `agw completion install`.
8. **Check the changes that apply without an error.** These need action if they affect you:
   - a git dotfiles checkout now follows the template's `ref`, and a local branch in it no longer
     advances; dotfiles that ship a mise config now win over `mise_packages`; and an install
     command's directory now takes precedence over a mise shim of the same name
     ([Initialization steps](#initialization-steps));
   - `vm exec` no longer applies `PATH` additions made in profile files, and on Windows `agw` no
     longer expands `*`, `?`, `~` or variables in its arguments
     ([What behaves differently](#what-behaves-differently));
   - automation that reads results: under `vm exec` and `agent exec` a lost connection now exits 1
     instead of 255 ([What behaves differently](#what-behaves-differently)), and the describe
     commands' `instance_state` reports a stored spec that needs replacing as `replacement-required`
     ([Initialization steps](#initialization-steps));
   - `session attach` no longer carries your SSH environment, such as `SSH_AUTH_SOCK`, into an agent
     session's server
     ([Agent session tmux clients run as the agent](#agent-session-tmux-clients-run-as-the-agent));
   - a workspace tmuxinator config an earlier release generated must not be started on a VM with
     agent sessions until Agentworks rewrites it
     ([Agent session tmux clients run as the agent](#agent-session-tmux-clients-run-as-the-agent));
   - files an earlier release's interrupted backup or Remote Lima create left under `/var/tmp` are
     no longer cleaned up ([Backups](#backups),
     [Start, recover, rekey and delete](#start-recover-rekey-and-delete));
   - a Lima VM created before 0.20 reruns its create-time bootstrap at every start, and a WSL2 VM
     created before 0.20 takes the Windows computer's name at every start, until you recreate it or,
     for WSL2, set its hostname yourself
     ([Start, recover, rekey and delete](#start-recover-rekey-and-delete)).
9. **Update third-party plugins** to their new contract versions
   ([harness integrations, Git credential providers and secret backends](#third-party-harness-integrations-git-credential-providers-and-secret-backends),
   [VM platforms](#third-party-vm-platforms)).

## Import existing host keys

Agentworks keeps one host-key record per VM under `~/.config/agentworks/known_hosts/`, keyed by VM
name, and refuses to connect to an existing VM that has no record; it no longer reads or writes
`~/.ssh/known_hosts` for VMs. Run the import from the workstation where you already connected to
your VMs:

```console
agw vm import-host-keys --all
```

It copies the keys `~/.ssh/known_hosts` holds for each VM's tailnet address into that VM's record,
reading the file only; `--known-hosts PATH` reads another one, such as a copy from another
workstation. It prints one line per VM and exits non-zero while any selected VM ends without a
record, so run it again as a checklist. The [command reference](../../cli/command-reference.md)
describes the matching and each report line.

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

Every command that reaches a VM over SSH refuses a VM with no record, before it starts the VM,
resolves a secret or changes anything, and prints a hint that names the import. That covers
`vm start`, `vm shell`, `vm exec`, `vm reinit`, `vm backup`, `vm port-forward`, `vm rekey`, and the
workspace, agent, session and console commands, including the start of a stopped VM that any of them
would trigger; `vm rekey` checks first so it never spends a Tailscale auth key. One command differs:
`vm shell --platform` on a cloud VM (AWS, Azure, GCP) resolves the site's credentials first and then
refuses, before it opens any network rule. `agw vm recover NAME` is the one command that repairs a
VM without a record, starting it first if it is stopped. `vm describe`, `session describe` and the
status listings that span several VMs warn that the VM has no record, leave out what they would have
read over SSH, and carry on.

`vm delete` still works without a record, because deleting is what you do with a broken VM. It
cannot reach a cloud VM (AWS, Azure, GCP) to log its node out of Tailscale, though, so that step is
skipped with a warning. Import first to get the logout, or remove the node in the Tailscale admin
console afterwards.

`agw database restore` onto another workstation carries no host-key records, because the store lives
beside the database, not in it. Copy `~/.config/agentworks/known_hosts/` from the old workstation
along with the database. VMs created on 0.20 have keys only there. For older VMs, the import still
works as the fallback.

Your own `ssh` changes when the managed SSH entries (`awvm--NAME`, `awagent--NAME`) are next
rewritten, by the import, `vm create`, `vm reinit`, `vm recover`, `vm rekey`, `vm delete`,
`agent create`, `agent reinit`, `agent delete`, `session create` or `session delete` when it creates
or deletes an agent, or `agw config sync-ssh-config`; until then the earlier entries remain. The
rewritten entries verify the VM's key strictly against the store, so `ssh awvm--NAME`, and `scp`, VS
Code Remote-SSH or anything else that goes through those aliases, fails with
`Host key verification failed` for a VM with no record, and works again once the import or
`agw vm recover NAME` records one. The
[command reference](../../cli/command-reference.md#direct-ssh-aliases) describes the entries and how
settings of your own interact with them.

## New VMs

`vm create` records the VM's key on its first connection, over the tailnet, and writes it to the
store only. Your `~/.ssh/known_hosts` is no longer touched. `vm delete` removes the record.

## Reinit retires grants nothing uses

`vm create` no longer adds the grants below, and `vm reinit` removes them, so one `agw vm reinit` of
each existing VM brings it to the new state. Until then they stay in place, unused:

- **The console setenv sudoers grant**, `/etc/sudoers.d/51-agentworks-console-setenv`
  (`Defaults:<admin> setenv`), which carried an agent console shell's environment across `sudo` (see
  [Named consoles](#named-consoles)).
- **The sudoers `env_keep` fragment**, `/etc/sudoers.d/50-agentworks-env-keep`
  (`Defaults env_keep += "AGENTWORKS_* AW_*"`), which kept those variables across a `sudo` for an
  agent console shell. Each shell now gets its environment from its request instead. Once it is
  removed, your own `sudo` no longer keeps `AGENTWORKS_*` or `AW_*` variables: a command you run
  with `sudo` sees only what `sudo`'s default policy keeps. Pass the values it needs as part of the
  command, as in `sudo -u USER env AGENTWORKS_WORKSPACE="$AGENTWORKS_WORKSPACE" command`, which
  needs no sudoers environment grant (`sudo --preserve-env=NAME` may be refused for a name outside
  sudo's own list). A login shell (`sudo -i`) still gets the VM's identity variables from
  `/etc/profile.d`.
- **sshd's environment wildcard**, `/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf`
  (`AcceptEnv *`), which let an SSH client set any environment variable in a guest shell. Every
  request now carries its environment in the request, so nothing Agentworks runs sends environment
  through ssh. Reinit reloads sshd after removing it, and from then on sshd accepts only the
  variables its stock configuration lists, such as the locale ones, so an `ssh -o SetEnv=...` or
  `SendEnv` of your own outside those stops reaching the VM.
- **The admin's group access to agent tmux servers.** Earlier releases put the VM admin in the
  `tmux-agent-access` group and shared each agent's tmux socket directory and sockets with it, so
  the admin could connect to an agent's tmux server without switching accounts. Nothing uses that
  now (see
  [Agent session tmux clients run as the agent](#agent-session-tmux-clients-run-as-the-agent)).
  Reinit resets `/run/agentworks/agent-tmux-sockets` to `root:root` `0711` and each agent's
  directory under it to the agent's own `0700`, then deletes the group, which ends the admin's
  membership. If any directory could not be reset, reinit warns and keeps the group for the next
  reinit. `agent create` and `session create` also reset the directories they use, without a
  warning. An agent session started before the reset keeps running, but from the moment an agent's
  directory is reset, any account but the agent and root is refused a new connection to its servers,
  a console window not yet rebuilt and an old tmuxinator config included. A tool of your own that
  connects to an agent's tmux server as the admin must switch to the agent first, as Agentworks
  does.

## Agent session tmux clients run as the agent

Agentworks no longer runs a tmux client as root or as the VM admin against an agent session's tmux
server. Every such client started from the admin connection, from status checks and
`session stop --all` to `session attach`, `session logs` and named console windows, now switches to
the agent account first: the admin's connection elevates with `sudo`, and root checks the socket and
then runs the client as the agent with `runuser`. Attach clients run on a terminal of their own
(`runuser --pty`). What to expect:

- **Guest authentication logs gain entries.** Each switch is one `sudo` invocation plus one
  `runuser` session for the agent account, so `/var/log/auth.log` or the journal records both for
  every probe, including every poll while a console window waits for a stopped agent session. The
  batched status read of `session list --status` is one `sudo` invocation for the whole VM, with a
  `runuser` session for each tmux read it makes.
- **Attach no longer carries your SSH environment into the agent's server.** `sudo` resets the
  environment, so variables tmux would otherwise copy into the session on attach, such as
  `SSH_AUTH_SOCK`, no longer reach it. Under `sudo`'s default policy `TERM` and the locale variables
  still do.
- **Workspace tmuxinator configs from earlier releases are not covered until rewritten.** A
  workspace's `.tmuxinator.yml` that an earlier release generated starts plain tmux clients as the
  VM admin, including against agent sessions' servers. Agentworks rewrites it, with the switch, when
  a session in that workspace is next created, started, restarted or deleted; until then, do not
  start it on a VM with agent sessions.
- **Rows with a misplaced socket path stop being probed.** A session's stored socket path must sit
  directly inside its agent's socket directory or, for an admin session, the VM admin's, and be
  named for its session. Any other path reports `unknown` status instead of being probed at whatever
  it names, and `session logs`, `session attach` and teardown refuse it, for admin sessions as well
  as agent sessions.
- **A socket that is not the agent's own is never reached.** Before switching, root checks the
  socket: one that is a link, is not a socket or belongs to another account reads as `unknown`,
  `session attach` refuses it with a one-line reason, and a console window for such a session says
  so once and keeps waiting for it.

## Adopt desired runtime state

Schema migration 39 replaces the old VM `operator_stopped` flag with a desired runtime state:

| Existing VM policy                         | Migrated target |
| ------------------------------------------ | --------------- |
| Manually stopped (`operator_stopped=true`) | `stopped`       |
| Automatic activation permitted (`false`)   | `on-demand`     |

Migration does not infer `running` intent from a VM's observed power state. Existing sessions and
named consoles have unset targets, and reconciliation reports them as blocked rather than guessing
from old PIDs or timestamps. Newly created VMs, sessions and named consoles record `running` intent,
and so do explicit `vm start`, `vm recover` when it starts a stopped VM, and session and console
start commands; nothing clears intent when a runtime exits on its own.

In order:

1. Inspect stored intent with `agw vm list`, `agw session list` and `agw console list`, which read
   local inventory without connecting to VMs.
2. Record each known target with `agw vm set-desired-state`, `agw session set-desired-state` or
   `agw console set-desired-state`, which start and stop nothing. Leave unknown targets unset. A
   console's target does not set its member sessions' targets.
3. Preview with `agw vm reconcile NAME --dry-run`, which observes without changing anything or
   booting a stopped VM.
4. Apply with `agw vm reconcile NAME`, which may start or stop the VM and its children according to
   their targets, or `session reconcile` or `console reconcile` for one resource.

[Desired runtime state](desired-runtime-state.md) covers targets, WSL2 holds, batch secret
resolution, failure handling and JSON output.

### Rollback

An older binary refuses the newer schema. Rollback requires a full pre-migration database backup and
a compatible Agentworks release; do not reconstruct the retired flag or downgrade the schema by
hand. Restoring the database does not undo provider or guest changes made since the backup: a VM
reinitialized by 0.20 no longer has the `AcceptEnv` wildcard, the `env_keep` fragment, the
`51-agentworks-console-setenv` grant or the `tmux-agent-access` group that a 0.19 binary relies on
(without the grant its agent console panes degrade) until that binary's own `vm reinit` puts them
back.

## Connecting through the new layer

Every command that reaches a VM over SSH now does so through a new connection layer, which sends
each request over the OpenSSH client's standard input instead of on the `ssh` command line. That
covers create's checks of a new VM once the platform has joined it to the tailnet, all of VM and
agent initialization, and every `vm`, `agent`, `workspace`, `session` and `console` command's work
on the VM, `vm backup` and runtime reconciliation included. `vm start`, the automatic start of a VM
that stopped on its own, `vm rekey` and `vm recover` use it to wait for the VM to answer.
`vm describe` reads the VM's live resources as one request bounded at 30 seconds, where it made up
to three attempts before.

The scripts of `vm recover`, `vm rekey` and the logout at `vm delete`, and `vm shell --platform`,
use the platform's own channel instead (see
[Start, recover, rekey and delete](#start-recover-rekey-and-delete)); `vm shell --platform` needs no
`python3`, and on a cloud VM that channel is SSH to its public address under the same rules.

### What these commands need

`vm create`, `vm start`, `vm rekey`, `vm recover`, `vm reinit`, `vm confirm-release`,
`vm verify-connection`, `vm backup`, `vm exec`, `vm shell` (but not `vm shell --platform`) and
`vm port-forward`, and `agent shell` and `agent exec`, check the client first, so an unsupported one
is refused before `vm create` builds a VM, `vm start` starts one, or `vm rekey` and `vm recover`
spend a Tailscale auth key. `vm describe` warns and leaves the live resources out instead, and
session status reads (`session list --status`, `session describe`) warn and report that VM's
sessions as unknown. Every other command that reaches a VM, and the automatic start, fails at its
first request to the VM, which comes after any VM start and secret prompt.

- **OpenSSH 8.5 or newer on the workstation.** Agentworks uses the first `ssh` on your `PATH`
  (ignoring empty and relative `PATH` entries) and checks its version once per run. A missing or
  older client is refused, with the paths it tried. `agw doctor` shows which client Agentworks
  resolved.
- **The native Windows build on Windows.** These commands never search `PATH` for `ssh` there,
  because `PATH` commonly finds the `ssh` bundled with Git for Windows, whose argument handling
  breaks them. It looks for Win32-OpenSSH in `System32\OpenSSH` and then `Program Files\OpenSSH`
  (including the 32-bit file system redirects) and judges each. Windows 10's inbox client is OpenSSH
  8.1, so install the Win32-OpenSSH MSI from <https://github.com/PowerShell/Win32-OpenSSH/releases>.
- **No program from the current directory.** Windows looks a bare program name up in the current
  directory before `PATH`, and on POSIX an empty or relative `PATH` entry means the current
  directory. Agentworks now starts every workstation program by its path in an absolute `PATH`
  directory: `tailscale`, PowerShell, the 1Password CLI `op`, `git`, `taskkill`, local Lima's
  `limactl`, VS Code's `code` and your `EDITOR`. A program found only in the current directory
  counts as not installed, so `agw doctor` and the 1Password backend's readiness no longer vouch for
  one. An `EDITOR` given as a path is run as given. On Windows, `workspace create --open-vscode`
  does not start VS Code: its `code` command is a batch file, which only `cmd.exe` can run, and
  `cmd.exe` parses the workspace path by its own rules. The warning names the workspace file to open
  in VS Code yourself.
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

- **No ssh configuration is read.** Every connection the layer makes, and a cloud platform's SSH to
  a VM's public address, runs `ssh -F none`, so `~/.ssh/config` and the system configuration do not
  apply to it. Remote Lima's connection to its placement host still reads them. An `IdentityAgent`
  set only in `~/.ssh/config` (1Password, Secretive) is ignored; the agent named by `SSH_AUTH_SOCK`
  serves the key. Every connection runs in batch mode, so ssh never prompts for a key's passphrase:
  an encrypted key has to be loaded in that agent first. A VM that refuses the configured key is
  reported as such, and nothing ran on it. When a connection fails or the key is refused, and the
  configured key is passphrase-protected, the error's hint mentions `ssh-add`; `vm port-forward`
  shows ssh's own log instead.
- **Key paths ssh would misread are refused.** A configured private key whose path holds `%`, `$`,
  `"`, a backslash or a control character is refused by these commands, and is left out of the
  managed SSH config entries with a warning, so the operator's own `ssh` offers its default keys for
  those hosts. Move the key to a plain path. On Windows, a key path with an apostrophe and no
  whitespace is left out of the entries the same way, because the `ssh` shipped with Git for Windows
  reads no form of it. A home directory named with an apostrophe is fine: the entries name the files
  under it relative to `~`.
- **Windows passes arguments through unexpanded.** `agw` no longer expands `*`, `?`, `~`, `%VAR%` or
  `$VAR` in its own arguments on Windows, which it did before forwarding them, even after `--`. A
  quoted `*` now reaches the guest as `*`, as on macOS and Linux, where the shell decides. Expand a
  pattern yourself (PowerShell's `Get-ChildItem`, for example) where you relied on `agw` doing it,
  and write `$HOME\...` (PowerShell) or `%USERPROFILE%\...` (cmd) instead of `~` in a path such as
  `database restore`'s or `vm import-host-keys --known-hosts`'s.
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
- **Environment values may hold line breaks.** A plaintext `env` value or a secret an `env` entry
  references may now contain carriage returns and line feeds, which reach the guest intact here, in
  sessions and in console shells. Config load no longer warns about such a plaintext value, and no
  command refuses either. A plaintext value with a NUL is still refused, before it is sent.
  `agw env show` prints each value on one row, with a backslash as `\\` and line breaks and other
  control characters escaped (`\n`, `\r`, `\t`, `\x1b`), and `--resolve` shows a secret with line
  breaks that way instead of reporting an error for it.
- **`vm shell` and `agent shell` without `--workspace` no longer print the message of the day or the
  last-login line.** sshd prints them only when it runs the login shell itself.
- **`vm port-forward` exits 0 when you stop it** with Ctrl-C, on macOS and Linux also with `SIGTERM`
  or `SIGHUP` (a closed terminal), and on Windows also with Ctrl-Break; started under `nohup`, it
  keeps running after its terminal closes. It exits non-zero when a forward cannot be set up (a busy
  local port, for example) instead of running with nothing forwarded.

### Backups

`vm backup` archives the workspaces as detached work on the VM, so the archive keeps running when
the connection drops, and the backup says when it loses the connection and when it regains it, then
downloads the archive once it is complete. Nothing is staged in `/var/tmp` any more: the archive is
built in a root-only directory under `/var/lib/agentworks/detached`, which is removed when the
backup ends. One that a killed backup left behind is removed, once its archive has ended, by the
first backup on that VM a week or more later. Ctrl-C stops the archive's whole process group on the
VM, and an interrupted download leaves no partial file. A backup by an earlier release that was
killed part way may have left `/var/tmp/agentworks-backup-*`, `/var/tmp/_aw_detached_*` or
`/var/tmp/_aw_paths_*.txt` on the VM; this release does not look for them, so remove them as root on
the VM once no backup is running.

### Workspaces

- **Links are not followed.** A workspace's generated `.tmuxinator.yml` and the admin's
  `~/.config/tmuxinator` directory are written by the file program, which refuses a symbolic link at
  the file or any parent, so a workspace path below a link (most likely a `paths.vm_workspaces` that
  is, or lies under, a symbolic link on the VM), or a linked `~/.config` or `~/.config/tmuxinator`,
  now fails `workspace create`, `workspace copy` and `workspace rehome` where it was followed. A
  failed `workspace create` removes what it created, as for any other failure. A refused
  `workspace copy` leaves the extracted tree in the new directory and no workspace row, so remove
  that directory before retrying. A refused `workspace rehome` leaves the copied tree at the new
  path and the workspace where it was, its error naming both; remove the new directory before
  retrying. The session commands regenerate the config as a convenience: a refusal there, or any
  failed request, now warns and leaves the session command's result standing, where a failure of the
  regeneration used to fail the command. To put workspaces on a data volume, mount it, or bind-mount
  a directory of it, at `paths.vm_workspaces` instead of linking it there.
- **`paths.vm_workspaces` must be an absolute directory below the root.** A relative value, `/`
  itself, or a value with a `..` component is refused when the configuration loads, since every
  workspace path on the VM is built from it; `workspace rehome --target` follows the same rule. An
  accepted value is normalized: empty and `.` components are dropped, so a trailing `/` goes and a
  leading `//` reads as `/`. A workspace created under a relative value is rewritten, when the state
  database migrates, to the absolute path it already had on the VM, under the VM admin's home, where
  agents cannot reach it, as in 0.19; `agw workspace rehome <name>` moves it to
  `paths.vm_workspaces`. Its harness setup was recorded against the relative spelling, so its setup
  record is stale until the workspace is rehomed; rehome records the setup at the new path. One
  created under a value starting with `~` keeps its stored path, since which home it named is not
  recorded. `workspace rehome` refuses such a path before anything runs, and the session commands
  warn instead of updating its tmuxinator config and defer delivering MCP servers to its sessions.
  `workspace delete` removes its row, sessions, grants, group and tmuxinator link, but nothing the
  path names, warning that its directory and tmuxinator config are left on the VM to check by hand;
  the workspace can then be recreated under the same name. A stored path with a `..` component, from
  an absolute value typed with one or a relative value such as `../ws` once rewritten, names its
  directory only once the VM resolves it: the session commands warn instead of updating its
  tmuxinator config and defer delivering MCP servers to its sessions, `workspace delete` removes its
  directory and link, and `workspace rehome` moves it to a path without one.
- **`workspace rehome --target` takes an absolute path.** A relative path, a `host:path`, the root,
  or a path with an empty, `.` or `..` component is refused before anything runs; a trailing `/` is
  dropped. A target that is the workspace's directory, inside it, or around it once the VM resolves
  symbolic links is refused before the confirmation, where the copy used to run into its own source.
- **`workspace copy` refuses an occupied destination.** A directory, file or link already at the new
  workspace's path is refused before anything is extracted, as `workspace create` refuses it, where
  the copy used to extract into it.
- **`workspace copy` stages nothing on the VM.** The source tree travels through the workstation as
  a compressed tar stream that is never unpacked there, and the destination extracts it from the
  request's input as root instead of from a file in its `/var/tmp`. Extracted files are root's until
  the copy gives the tree to the admin and the workspace group, rather than keeping the source's
  owners in between. The extraction has no time limit, where the upload and the unpack were bounded
  at 300 and 120 seconds. The source read is written to its own operation log on the source VM,
  `workspace-copy-source`, beside the destination's `workspace-copy`.
- **`workspace repair` reads up to 64 MiB of a recursive step's output.** On a tree so large or so
  damaged that `chown -c` or `chmod -c` prints more, the ownership step stops reading and warns; it
  may have stopped part way, and running repair again continues it. A `getfacl` listing larger than
  that leaves the ACL comparison not verified, as a failed listing does.

### Sessions

- **The session's environment is off the command line.** Its values, secrets included, travel in the
  launch request's environment, which the session's own tmux server takes as its global environment
  and every pane inherits, instead of as `tmux new-session -e` arguments. The operation log records
  only their names.
- **Every status read is one request with a 10-second deadline.** That covers the reads before a
  start, restart, stop or delete, the reads of `session attach` and `session logs`, and the PID
  repair, which waited for as long as the VM took before, as well as the listings and
  `session describe`, which were bounded already. A read the VM does not answer in time leaves a
  listing showing `unknown`, as before, and stops a lifecycle command with a connection error
  instead of the refusal of an unknown runtime; nothing is torn down on a guess. The batched read of
  `session list --status` is one elevated request that reads each session's server as the account
  that owns it, where it read admin sessions as the admin and elevated for agent sessions; a session
  whose socket path is a link, is not a socket or belongs to another account shows `unknown`. A
  listing that cannot read a VM now warns once with the reason.
- **Launches are logged as the session's owner.** `session create`, `start` and `restart` write
  every request of the launch, an agent session's requests as the agent included, to the command's
  operation log by shape. `session stop` writes one `session-stop` log per VM and
  `session stop --all` one `session-stop-all` log per VM. The listings, `session describe` and the
  reads that prepare a start or restart write none.
- **`session attach` runs `tmux attach` itself.** For an admin session the connection starts `tmux`
  directly with its arguments rather than through the admin's shell. For an agent session it starts
  the switch to the agent around the same `tmux attach` (above: `sudo`, the socket check, then
  `runuser --pty`). `[terminal] clear_on_detach` applies as before.
- **The Codex recorder is written before the launch.** A `codex` session's conversation recorder,
  `~/.agentworks/codex/record-thread-v1.sh`, is written by one atomic file write before the session
  starts, instead of by its launch command. The write refuses a symbolic link at the file or a
  parent (a linked `~/.agentworks`, say), which the launch command followed. A refused or failed
  write warns and the session starts anyway, finding its conversation by discovery instead.

### Named consoles

- **Restart each running console after its VM's reinit.** A running console keeps its windows, their
  attach loops and its shells until `agw console restart NAME` rebuilds it, and once its VM is
  reinitialized an old window cannot reconnect to an agent session.
- **A console shell's environment is off the command line.** Each shell pane, and the admin shell
  window `console create --add-admin-shell` adds, takes its resolved values from a request on its
  own account's connection, which the pane's one-time command collects, instead of
  `tmux split-window -e` or `new-session -e` arguments, so no value appears on a guest command line.
  Like the other shells agentworks starts, its shell begins with the environment
  [What behaves differently](#what-behaves-differently) describes (the fixed `PATH`, no
  `SSH_CLIENT`, `SSH_CONNECTION`, `SSH_TTY` or `pam_env` variables) plus those values, and still
  runs its login startup files. An admin's shell keeps the `TERM`, `TMUX`, `TMUX_PANE`,
  `TERM_PROGRAM` and `TERM_PROGRAM_VERSION` tmux sets for its pane. An agent's shell pane switches
  to the agent with `sudo -n -u` before it collects its request, and sudo passes on only `TERM` of
  those five, so an agent's console shell has `TERM` and no `TMUX`, as under the `sudo --login` of
  earlier releases. Because each pane's command runs once, `tmux respawn-pane` cannot start its
  shell again: restore a shell pane with `agw console restore-session` and the admin shell window
  with `agw console restart`.
- **An agent's console shell connects as the agent.** Its environment no longer crosses `sudo`, so
  it no longer needs the `Defaults:<admin> setenv` grant of `51-agentworks-console-setenv`, which
  reinit removes ([Reinit retires grants nothing uses](#reinit-retires-grants-nothing-uses)). It
  needs the agent's own SSH access instead, as an agent session does: a console command that would
  open a shell for an agent that has no SSH access of its own refuses before it prompts for a pane
  secret or changes the console's tmux or definition, with the `agw agent reinit` hint. It may
  already have started a stopped VM, and `console start` records that the console should run, before
  that check. `restore-session` checks every agent of the session's shells, even when their panes
  are not missing. `console add-shell` and `add-sessions` check only when the console is running,
  since only then do they open the new shells; on a stopped console they record the new shell or
  member as before.
- **A shell pane whose directory is gone opens in the home directory with a warning,** where the
  pane printed the message itself.
- **Pane secrets resolve before the console changes.** `console create` and `restore-session`
  resolve the secrets of every shell they could open in one pass, once the VM is active and before
  any tmux request. `restore-session` resolves every configured shell's secrets even when few or no
  panes are missing, so a non-interactive restore fails on a secret it would not have used, and it
  resolves them before it checks the window's pane tags, so you may be prompted before a corrupt
  window is refused.
- **`console attach` runs `tmux attach` itself,** with its arguments, rather than through the
  admin's shell. `[terminal] clear_on_detach` applies as before.
- **A console build is logged by shape.** `console create`, `start` and `restart` write the admin's
  requests of a build to the `console-build` operation log as the other commands do: arguments, or a
  script's shell, with the names of its environment variables and its deadline. Parking a shell's
  request keeps no log.

## Create, reinit and the VM contract

- **Create** installs `python3` while it provisions the VM, then connects over SSH at the VM's
  tailnet address. The first connection records the host key, and the VM contract's base checks
  follow: the Debian release first, then systemd, passwordless `sudo` for the admin, and the tools
  Agentworks runs (`install`, `flock`, `setsid`, `tar`). Provisioning is complete only once they
  pass. A VM that fails them is kept in a failed state, and one whose create is interrupted or
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
  deadline. A script's text and an environment variable's value are not written.

### Initialization steps

Every step of `vm create` and `vm reinit` initialization, and of `agent create` and `agent reinit`,
runs through the new layer. Agent initialization runs as the admin only to create the account and
place its `authorized_keys`; every later step runs as the agent, which cannot run anything as root.

- **System files are written without following links.** The files initialization owns under `/etc`
  (the cloud-init host-key drop-in, the sysctl baseline, `/etc/fstab`'s `/proc` line, the tailscaled
  drop-in, the identity profile, the `/etc/zsh/zprofile` block, the skel seeds, the grub SVE drop-in
  and the apt source lists) are written by the file program, which refuses a symbolic link at the
  file or any parent. A refused file is a step warning, and the link's target is left as it is.
  Initialization no longer writes a sudoers fragment
  ([Reinit retires grants nothing uses](#reinit-retires-grants-nothing-uses)).
- **sshd is reloaded only when its fragment goes.** Initialization no longer writes an sshd fragment
  ([Reinit retires grants nothing uses](#reinit-retires-grants-nothing-uses)); it reloads sshd only
  when it removes the old one, where every initialization used to reload sshd. Until that reload
  succeeds the fragment is kept as `/etc/ssh/agentworks-accept-env.reload-pending`, which sshd does
  not read, so when the reload fails or a reinit is cut short before it, the next reinit reloads sshd.
- **A file whose reload fails is put back.** When loading the sysctl baseline (now `sysctl -p` of
  that file alone, not `sysctl --system`), or `systemctl daemon-reload` after the tailscaled drop-in
  fails, times out or is interrupted, the file gets its previous content back (or is removed if it
  is new), with a warning, so the next `vm reinit` tries again. If putting it back fails, a separate
  warning names the file, which then holds content that is not in effect and that the next
  `vm reinit` will not retry: put its previous content back or remove it, then reinit. If something
  else changed the file in the meantime, nothing is put back and the next `vm reinit` tries again.
- **`/etc/fstab` keeps its owner, group and mode** when its `/proc` line changes, and a `hidepid`
  value already on that line is kept, a name such as `hidepid=invisible` included; only a missing
  `hidepid`, `hidepid=0` or `hidepid=off` becomes `hidepid=1`.
- **Step warnings show what failed.** A failed step's warning ends with the last three lines of the
  command's error output, with secrets removed.
- **The DNS check warns when it cannot run.** A DNS probe that fails to run or times out warns and
  initialization goes on. DNS that tailscaled left latched in direct mode still stops it, with the
  manual fix as its hint. A mise lockfile step that cannot create its directory warns too.
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
  file the link points to, then reinit.
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
- **`dotfiles_destination` is checked before the dotfiles sync.** The value, as rendered for the
  account when it holds `${agw:NAME}` references, is a path in the home (`dots` or `~/dots`) or an
  absolute path. It may not name the home itself or one of its parents, may not be, hold or lie
  inside a path Agentworks manages in that account's home (`~/.ssh`, `~/.agentworks`,
  `~/.agentworks-artifacts` and the `~/.agentworks-*.sh` fragments, and for the admin also the
  workspaces' `~/.config/tmuxinator`, so an admin's `~/.config` is refused too), and may not contain
  an empty, `.` or `..` component; a trailing `/` is dropped, and only its leading `~` is expanded.
  A refused destination warns and syncs nothing. A stored destination such as `dots` keeps meaning
  `~/dots`.
- **Git source URLs may not carry credentials.** A `git::https://` source whose URL holds a user
  name or token, such as `https://user:token@github.com/...`, is refused when the template loads,
  without echoing it; declare a `git-credential` resource for the host instead. A subpath with an
  empty or `.` component is refused too, as one with `..` was.
- **Replace a stored spec whose source reference is now refused.** An agent created or reinitialized
  with `--spec`, or a VM created with `--admin-spec`, holding a source reference this release
  refuses (a `git::https://` URL with a user name or token in it, a remote written without `git::`,
  or such a harness settings `source`) keeps that spec stored. Every command that uses that agent's
  or VM's spec, a plain `agw agent reinit <name>` or `agw vm reinit <name>` among them, refuses it,
  naming the field but not the value; other commands, deletes and listings included, work as before.
  Supersede it with `agw agent reinit <name> --spec '<complete replacement spec>'` or
  `agw vm reinit <name> --admin-spec '<complete replacement admin spec>'`, or clear it with
  `--spec '{}'` or `--admin-spec '{}'`; `agw doctor` names the same repair. `vm reinit` now takes
  `--admin-template` and `--admin-spec`, as `agent reinit` takes `--update-template` and `--spec`.
  While a record awaits its replacement, inspection output (`resource show`, `resource list`'s
  used-by count, `secret describe`, and `graph show`'s dependents) leaves out the references that
  owner's stored spec makes. The describe commands' `instance_state` reports such a spec, and a
  saved harness activation list, as unavailable with reason `replacement-required` and issue
  `instance-spec-replacement-required`; an activation list used to read `unsupported-version` and
  `instance-spec-unsupported`. JSON stays at schema version 1, since a reclassified case is not a
  change of shape (see the command reference's
  [compatibility rules](../../cli/command-reference.md#errors-and-compatibility)), so a consumer
  that matched the old values must now accept the new ones.
- **A remote written without `git::` is refused.** A source reference (`dotfiles_source`,
  `mise_lockfile`, an artifact's `source`, a harness settings `source`) holding `://` or `::`, or
  whose first path component holds a colon that is not a Windows drive letter (as in
  `user@host:path`), which used to be read as a local path, is refused when it loads; write a git
  remote as `git::https://...` or `git::git@host:path`.
- **A git dotfiles checkout follows its `ref`.** Every sync, fresh clone or reinit, fetches the
  template's `ref` (the remote's default branch when there is none) and checks it out detached, so a
  changed or dropped `ref` takes effect, a branch advances and a tag is checked out. A commit `ref`
  must be the full hash. The admin used to pull whatever branch the clone was on, so a local branch
  in the checkout is no longer advanced; commit your dotfiles changes upstream. A failed sync now
  skips the dotfiles install for agents too, as it does for the admin, instead of running it over
  the previous checkout. A first clone is made beside the destination and moved there only once its
  `ref` is checked out, so a `ref` the remote does not serve leaves no destination behind and a
  corrected `dotfiles_source` clones afresh.
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
- **Agents follow the admin's mise and install-command order.** An agent now gets the mise config
  from `mise_packages`, then its dotfiles sync, then the mise lockfile and install, then its user
  install commands, as the admin always has. Three things change for agents:
  - A template with `mise_packages` whose dotfiles also provide `~/.config/mise/config.toml` now
    ends with the dotfiles' config, where the template's used to overwrite it. Drop `mise_packages`
    if the dotfiles own the config, or stop shipping it in the dotfiles.
  - Dotfiles that supply a mise config without `mise_packages` or `mise_lockfile` now get a
    `mise install`, and a prune on reinit when `mise_prune_on_reinit` is on.
  - User install commands run after the mise install instead of before the dotfiles, so they see
    what the dotfiles installed and mise's tools. The profile always carries mise's shims on `PATH`,
    behind the install commands' own `PATH` entries, as the admin's does; an install command's
    directory now takes precedence over a mise shim of the same name, where the shims used to win.
- **Every account's profile and rc fragments are written first.** Each account's profile, with
  mise's shims and every declared install-command `path`, and its rc fragment are written before its
  dotfiles sync, and their source lines are ensured again after the dotfiles install and the mise
  install, before the user install commands, and at the end. A user install command that fails
  warns, and its paths leave the profile once the account's install commands finish.
  - An agent's user install commands and their `test_exec` checks now find mise's shims and their
    declared paths on their login shell's `PATH`, on create and reinit alike; at v0.19.0 they found
    neither.
  - The admin's find them on a first create too, where they used to only from the next reinit. An
    admin install command or check that looks up a tool mise also provides can now find mise's on a
    first create.
  - The admin's dotfiles installer and mise install see the profile on a first create as well,
    including its `umask 027`, as they already did on reinit.
- **A missing login shell warns.** Setting the admin's login shell to one that is not installed
  fails the shell configuration step with a warning, instead of running `usermod` with an empty
  shell.

## Start, recover, rekey and delete

`vm start`, the automatic start, `vm rekey` and `vm recover` wait for the VM to answer through the
new connection layer, so they need what [What these commands need](#what-these-commands-need) lists.
`vm start` checks the client, and that the workstation is on Tailscale, before it starts anything;
`vm rekey` and `vm recover` check both before they resolve the auth key.

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
  once it holds the VM, so a stopped WSL2 distro is booted by the hold and logged out. A VM whose
  desired runtime state is stopped is not held (see
  [Desired runtime state](desired-runtime-state.md)), so nothing boots it: it is logged out only if
  it is already running. Any VM still stopped or deallocated at that point is reported as not logged
  out, and the warning names its node the same way. A backend delete that fails after the logout
  says how to finish the delete, and that `agw vm rekey NAME` puts the VM back on Tailscale instead.
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
- **A Lima VM's create-time bootstrap runs once.** Lima runs its provisioning on every boot, and a
  Lima VM created before 0.20 runs Agentworks' bootstrap again at each start: it resets the admin's
  `.bashrc` and `.zshrc` to the seed, which drops your edits and the lines `vm reinit` adds, puts
  back the key it was created with if you replaced it, and upgrades packages, taking each upgraded
  package's new configuration files over any you changed (`/etc/skel/.bashrc` among them). A Lima VM
  created by 0.20 runs it at create only. Recreate an older Lima VM to stop the reruns; until then,
  `agw vm reinit NAME` after a start restores the lines it adds and removes a replaced key again,
  but not your own edits.
- **A remote Lima create stages its detached files privately.** The create that runs on a placement
  host keeps its wrapper, PID, output and status files in a directory of its own under `/var/tmp`,
  which only the host user can enter and which is removed when the create ends, instead of at
  `/var/tmp/agentworks-lima-<instance>.*` beside other accounts' files. A rolled-back create and a
  later `vm delete` find that directory by its prefix and owner. Files an earlier release's
  interrupted create left at the old names are no longer looked for; remove them on the host. For
  the same reason `vm delete` does not stop a create an earlier release started that is still
  running on the host, so its `limactl` would run alongside the delete: before deleting such a VM,
  let that create finish, or on the host stop the `/bin/bash /var/tmp/agentworks-lima-<instance>.sh`
  process whose PID is in the `.pid` file beside it, and the `limactl` it started.
- **A WSL2 VM keeps its own hostname.** WSL names a distro after the Windows computer at every start
  unless `/etc/wsl.conf` names it, and a later Tailscale join takes that name too. A WSL2 VM created
  by 0.20 has `hostname` set under `[network]` there; one created before does not. Add it yourself
  (the hostname is in `agw vm describe NAME --output json`) and restart the distro with
  `wsl --terminate`, or recreate the VM. WSL still renames a VM whose hostname has an underscore,
  since it keeps only letters, digits and single hyphens, so such a VM keeps drifting until it is
  recreated under a name without underscores. `vm create` on WSL2 now refuses such a hostname before
  it does anything: one with an underscore, or with consecutive hyphens from the system slug. A slug
  cannot be changed, so an install whose slug has consecutive hyphens cannot create WSL2 VMs, and
  its existing ones keep drifting.
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

An optional hook is new as well: core calls `validate_create_hostname(hostname)` with the hostname
it composed for a new VM before any secret resolves. Override it to refuse a hostname your backend
would change; the default accepts every hostname.

### A root channel replaces the native transport

- **Implement `run_as_root(vm, ctx, script, deadline) -> RawRun`.** It is abstract and required. It
  runs `/bin/bash -s` as root through the platform's own channel, without the VM's Tailscale, with
  `script` on its stdin and nowhere else; it runs the script once and returns both streams with no
  exit status. The platform's own create-time join runs through it, and so do `vm recover`,
  `vm rekey` and the logout at `vm delete`. Its `deadline` starts at dispatch and bounds the script
  alone. Anything the channel does first (opening a route, confirming the guest, waiting for the VM
  to answer) needs a bound of its own, and a wait for the VM that runs out raises
  `root_script.ChannelUnreachable`.
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
- **The legacy transports are removed.** `agentworks.transports` and `agentworks.ssh` are gone, with
  `ExecTransport`, `LimaTransport`, `RemoteLimaTransport`, `WSL2Transport`, `SSHTransport`,
  `native_transport`, `wait_for_reconnect`, `TailscaleWait`, `SSHTarget`, `SSHResult`, `SSHError`,
  `SSHLogger`, `LOG_DIR` and `openssh_program`, and so is `agentworks.plugins.proxmox.transport`. A
  platform reaches its VM through its own `run_as_root` and core through `agentworks.execution`;
  `SSHError`'s base, `agentworks.errors.ConnectivityError`, is what the execution errors subclass.
  The operation log is `agentworks.execution.oplog.OperationLogFile`, its directory
  `agentworks.execution.oplog.LOG_DIR`, and a platform writes to it only through `request.progress`.

### Create returns an address

- **`ProvisionResult.tailnet_address` replaces `tailscale_ip`,** and the result no longer carries a
  transport. The address is required. `create()` joins Tailscale once through `run_as_root` with
  `tailscale_join.join_tailscale`, reads the node's address with `settle_tailnet_address`, and
  returns it. Core validates it, reaches the VM over SSH there, and checks the live Debian release
  itself. Pass both calls `log=request.progress.record`: `BootstrapProgress` gains `record`, so the
  join's output, with the key redacted, lands in the create's operation log, which a failed create
  names.
- **A cloud's create finishes through
  `complete_cloud_bootstrap(target, auth_key, node=..., log=...)`.** Its `target` is a
  `public_ssh.PublicSSH` to the VM's public address under `request.provisioning_trust`. It waits
  with `PublicSSH.wait_until_answering(seconds=..., retry_refused_key=True)`, where a refused key
  raises `public_ssh.KeyRefused` and is retried until the bound, since a cloud's guest agent may
  install the key after sshd starts; then it waits for cloud-init, joins and settles the address. A
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

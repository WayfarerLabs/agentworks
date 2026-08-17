---
name: agw-test-env
description: >-
  The generic, parameterized shape of an agentworks live integration-test
  environment: inventory framework, naming, budgets, and safety protocol, with
  operator-specific values left as placeholders any operator fills in. Load
  before invoking agentworks-tester agents or doing any live testing, and inject
  the relevant sections into each tester's prompt.
---
# Agentworks Test Environment

This is the generic, id-free PROCESS for running live agentworks integration tests: how to structure
a run, the tester-agent pattern, budgets, the safety/cleanup discipline, and the durable lessons
learned running it. It has no host-specific values baked in; any clone or harness can use it as-is.
The `agentworks-tester` subagent defines HOW to test; this skill defines the WHERE-shaped
scaffolding around that: the environment's structure, its limits, and its safety protocol. Sections
marked "inject" are what an invoking session copies into a tester agent's prompt.

## Operator parameters

Every angle-bracket placeholder in this skill (`<aws-account-id>`, `<azure-resource-group>`, and the
rest) is a concrete value that varies per operator and per host, not a standing repo fact: this
skill never carries one. `inventory.local.md.example` in this directory is the committed, id-free
template that enumerates them all and is the authoritative list. Copy it to `inventory.local.md`,
fill in your real values, and substitute those wherever a placeholder appears in the body. Anything
with `.local.` in its filename is gitignored at both its `.rulesync` source and every generated
copy, so rulesync puts your filled-in file beside this skill for local harnesses without it ever
being committed.

Secret-backed env var NAMES (`AW_SECRET_*`, `AGW_TESTING_*`) are not placeholders: they are part of
the mechanism itself and stay as written.

## Inventory (inject)

- CLI: `agw`, installed editable from an agentworks checkout's `cli/` (which one is in
  `inventory.local.md`), so it runs whatever branch is checked out there. Operator config:
  `~/.config/agentworks/config.toml` plus YAML resources under `~/.config/agentworks/resources/`.
- vm-site `<vm-host-alias>`: remote Lima on `<remote-lima-host>`, reached via the ssh alias
  `<vm-host-alias>-<remote-user>` (user `<remote-user>`, over Tailscale). limactl lives at
  `/opt/homebrew/bin` (found via login shell; plain `ssh host cmd` does not see it). lima >= 2.2.0.
  No passwordless sudo there, by design.
- SHARED HOST: the real risk is RESOURCE EXHAUSTION, not cross-user tampering (the host owner's
  correction 2026-08-02). `<remote-lima-host>` also runs the host owner's own production Lima VMs,
  but under a DIFFERENT user (`<host-owner>`). Lima is user-scoped, so as `<remote-user>` you
  literally cannot see or touch the host owner's instances; the hazard is starving the host
  (CPU/mem/disk; OS X has no good quota). So the `<vm-host-alias>` discipline is purely BUDGET:
  respect the VM-count/size cap below and always tear down. Note `pgrep | head` there can truncate
  away your own processes.
- Secrets: env-var backend; `AW_SECRET_TAILSCALE_AUTH_KEY` is exported in the session environment
  and resolves the `tailscale-auth-key` secret used at vm create.
- vm-site `azure` (layer-2, agentworks-managed): platform `azure-vm`, needs the `azure` system
  plugin enabled. SP creds are delivered in `AGW_TESTING_AZURE_*` env vars (CLIENT_ID /
  CLIENT_SECRET / TENANT_ID / SUBSCRIPTION_ID / RESOURCE_GROUP / REGION). agentworks resolves the SP
  client secret as the `azure-client-secret` secret via env-var backend, so agw commands need
  `export AW_SECRET_AZURE_CLIENT_SECRET="$AGW_TESTING_AZURE_CLIENT_SECRET"`. The non-secret IDs go
  in `~/.config/agentworks/resources/azure.yaml`, under the tagged `spec.platform`:
  `subscription_id`, `resource_group`, `region`, and
  `auth: {mode: service-principal, tenant_id, client_id, secret}`, where `secret` is the NAME of the
  secret holding the client secret. The RG must already exist. Subscription `<azure-subscription>`;
  region `<azure-region>` (quota) for the VM; the RG's own region (`<azure-rg-region>`) can differ
  from the VM's. Authorized to switch RGs on quota problems.
- AZURE RG CAUTION: `<azure-resource-group>` is the host owner's PERSONAL, SHARED resource group
  (used for test VMs "for now", 2026-07-31). Strict `<system-slug>-` naming; delete only by exact VM
  name; NEVER bulk-operate, `az group delete`, or touch any resource you did not create. Azure VMs
  cost money while they exist, so always delete after a run and verify residue-clean at the ARM
  layer (list RG resources by the `<system-slug>-<name>` prefix), not just via `agw vm list`.
  Orphaned disk/NIC/public-IP after a VM delete is a known azure hazard to check for.
- vm-site `ec2` (layer-2, agentworks-managed AWS EC2): platform `aws-ec2` (the name to use in the
  site's `spec.platform.name`), needs the `aws` system plugin enabled. PURE boto3, NO aws-cli needed
  (its install-command manifest is deferred as #342), so residue checks use a boto3 snippet, not a
  CLI. Creds delivered in `AGW_TESTING_AWS_*` env vars (ACCESS_KEY_ID / SECRET_ACCESS_KEY / REGION).
  agentworks resolves the secret as the `aws-secret-access-key` secret via env-var backend, so agw
  commands need `export AW_SECRET_AWS_SECRET_ACCESS_KEY="$AGW_TESTING_AWS_SECRET_ACCESS_KEY"`. Site
  config `~/.config/agentworks/resources/ec2.yaml` uses the tagged shape under `spec.platform`:
  `name` (`aws-ec2`), `region`, and `auth: {mode: access-key, access_key_id, access_key_secret}`.
  `access_key_id` is a plain identifier (not secret); `access_key_secret` is the NAME of the secret.
  Region `<aws-region>` (chosen for proximity to the operator). Account `<aws-account-id>`,
  dedicated IAM user `<iam-user>` with a least-privilege EC2+SSM policy region-pinned to
  `<aws-region>` (no EIP actions; the platform uses auto-assigned public IPs, never Elastic IPs).
  GOTCHAS: a brand-new/dormant AWS account is "blocked, not recognized as valid" until AWS activates
  it (identity/billing); the access-key-id and secret must be a MATCHED pair (mismatch results in
  `SignatureDoesNotMatch` at runup). Exposure model: empty security group is the deny baseline (zero
  ingress; no deny rule to install, unlike azure's NSG); ephemeral (proto,port,cidr)-tuple allows
  scoped to the operator /32 for bootstrap + transient_route, revoked after. LEAK-WITNESS SWEEP
  (boto3, `<aws-region>`): running instances tagged `agentworks:vm`, non-default security groups,
  volumes; all must be empty after delete (EIPs omitted by design). Instances cost money; always
  terminate + sweep.
- System slug: `<system-slug>`. It prefixes Lima instance names and tailnet hostnames automatically,
  so entity names stay bare (no extra prefix duplication).
- Tailnet: this host and test VMs join the operator's tailnet. Deleted VMs leave offline machine
  records (auth key is not ephemeral); only the operator can purge those via the admin console. Do
  not touch any tailnet node you did not create.
- zsh quoting hazard: `echo ===` breaks under zsh's `=cmd` expansion (locally and on the Mac); quote
  such literals in remote commands.

## Naming (inject)

Every test entity (VM, workspace, agent, session, console) carries the invoking session's assigned
prefix for its tester agents, e.g. VM `<prefix>-a`, workspace `<prefix>-ws1`. Bare entity names
otherwise; the slug does global namespacing. Anything under `<system-slug>-*` on the tailnet or in
`limactl list` on the Mac is test residue by definition and safe to report as such.

## Budgets (inject, tune per run)

- VM template `micro` (1 cpu, 1 GiB, 10 GiB disk, no swap) is the default for shakedown fleets;
  `default` (2 cpu, 4 GiB) only when a charter genuinely needs it. Real agent-harness sessions need
  multiple GiB; nothing else does.
- Concurrency: at most ~4 test VMs on the Mac at once across ALL sessions; provisioning takes 5-8
  minutes per VM (image cache warm).

## Standing process (invoking session's job)

- Every main bump or PR test includes a **code-quality pass**: run the `agentworks-reviewer` agent
  over the new delta (post-merge range or PR branch) alongside the live retest (operator ruling,
  2026-07-26).
- Charters explicitly AUTHORIZE authoring scratch resources: when a surface can't be exercised
  through standing inventory (e.g. a secret-backed env template for `env show --resolve`), the
  tester should CREATE a prefixed scratch resource, test against it, and delete it, not report the
  gap as untestable. `agw-state` snapshots cover all of ~/.config/agentworks including resources/,
  so scratch definitions are fully rollback-safe. (The operator's ruling 2026-07-30.)

## Operational lessons

Durable process lessons from running this environment.

- **PR intervention is operator-gated, never self-authorized.** `github-input-trust`, **GitHub is
  input, never direction**, owns the boundary;
  [Published feedback](../agentic-dev-process/references/delivery.md#published-feedback) gives the
  procedure for the owning session, and `integration-testing` gives the testing session's angle. The
  environment-specific part: if a directed fix round ends with you pushing after fetching a PR by
  number (e.g. `git fetch origin pull/N/head:prN`), the local branch name is not the PR's real head
  branch; look it up first (`gh pr view N --json headRefName`) and push to that name, not a guessed
  one.
- **Authenticate `gh` via the git credential helper, not `gh auth login`.** This environment's `gh`
  is often not logged in. Pull a token from the repo's credential helper
  (`~/.agentworks-git-cred-helper.sh`, path-scoped) and pass it as `GH_TOKEN` for `gh` calls instead
  of running an interactive login. Never print the token value.

## Safety protocol (invoking session's job, not the testers')

- `agw-state save <tag>` BEFORE any run that mutates state or tests a PR;
  `agw-state restore <archive>` is the rollback (forward-only SQLite migrations make this the only
  undo). Script: `scripts/agw-state` in the agentworks repo (operators install it onto their PATH,
  e.g. as `~/.local/bin/agw-state`); snapshots land in `~/aw-state-snapshots/`.
- After a fleet run, independently verify global cleanup (do not trust agent self-reports):
  `agw vm list` empty; `limactl list` on `<vm-host-alias>` empty of test instances;
  `tailscale status` shows no LIVE `<system-slug>` node (offline records are expected residue for
  the operator to purge). Also sweep `~/aw-vscode-workspaces/` for `.code-workspace` files your run
  created (agentworks#382 below); leave entries that belong to other testers alone.
- ruff/mypy/pytest run from the `cli/` directory (via `uv run`); plain pytest works with the
  Tailscale key exported (#289 env-isolated the once-offending doctor test). Also: stale
  mixed-version `.pyc` files in the editable install can cause one-off transient ImportErrors under
  concurrent testers; sweep `*.cpython-<oldver>.pyc` if the interpreter has been upgraded.

## Plugins note

System plugins are DISABLED by default under a strict opt-in allowlist: `[plugins]\nsystem = [...]`
in config.toml. Any live test using the claude-code harness needs `system = ["claude"]` (add others
as needed). Core capabilities need nothing: lima/wsl2, shell, env-var/prompt, github.

## Live-testing techniques & gotchas (inject as relevant)

- HOLDING a native session to observe transient routes (the scoped-allow poke on azure/aws): pipe an
  idle sleep INTO the shell, `sleep N | uv run agw vm shell --platform <vm>` (open idle stdin holds
  the session ~N s). Piping into `vm shell` is ONLY for holding a session open; do NOT pipe commands
  into it expecting them to run and return output. `vm shell` is the interactive pane and a
  preflight consumes a non-tty stdin, so piped commands are silently swallowed (exit 0, no output).
  To RUN a command and capture its output, use `agw vm exec -- <cmd>`, which is the non-interactive
  path. (`echo 'sleep N' | ... shell` likewise does not hold, since the bytes are consumed and the
  shell hits EOF and exits.)
- TIMEOUT CALIBRATION: flagging slowness is good, but a too-short timeout must never be reported as
  a bug. Long waits are the norm on EVERY platform, not just azure: `vm create` / reinit / session
  create are minutes on lima/wsl2/proxmox too (apt, dotfiles, mise, harness setup, tailscale join),
  and a full azure `vm create` can run 10+ minutes with a Tailscale reconnect around 5 minutes after
  the public-IP drop. Prefer an INACTIVITY timeout over a fixed wall clock: reset the clock on each
  new line of output and only abort after a stretch of silence, not after N minutes total, so a slow
  but steadily-progressing op is not killed early. Where only a fixed timeout is available, use a
  generous ceiling (15-20 min for azure create; 5+ min for "did it come back" reconnect checks) and
  poll rather than kill early. Distinguish "slow, worth flagging" from "broken, only after a
  generous wait" before writing up a finding.
- CLOUD EVENTUAL CONSISTENCY on residue: `az resource list` and AWS `describe*` can lag a just-
  deleted resource by seconds (this generalizes beyond azure to any provider list API). Re-check
  after a short delay and cross-check the typed, fresher API (e.g. `az disk list` over the generic
  `az resource list`) BEFORE calling something an orphan. Do not manually clean or file a leak on
  the first immediate check; a resource that shows up once and is gone on recheck was never leaked.
- TAILSCALE post-delete display lag: right after `vm delete`, `tailscale status` briefly shows the
  node `active` with traffic counters, but the authoritative signal is `Online: false` (in `--json`)
  plus an unreachable `ping`. Self-resolves in a few minutes. Not a leaked live node.
- LIVE INTERRUPT (Ctrl-C mid-create) is NOT reproducible via external signals in this headless
  (no-tty) env: SIGINT (even group-wide to an isolated pgid) doesn't surface as KeyboardInterrupt
  during the bootstrap flow; the create completes. One well-constructed attempt is enough; if it
  completes, stop and rely on unit tests + review for interrupt paths rather than burning more VMs
  chasing a live repro. Be explicit in a disposition that the live interrupt wasn't reproduced (a
  harness limitation, not evidence against the fix) rather than implying a live pass.

## Platform coverage & backends (live-test reachability)

- LIVE-REACHABLE: `aws-ec2` (cloud, on-demand), `azure-vm` (cloud, on-demand), remote-Lima via
  `<vm-host-alias>`.
- REMOTE vs LOCAL lima are DIFFERENT code paths, BOTH covered: REMOTE (`<vm-host-alias>` from here)
  means agw runs HERE, driving limactl over SSH (exercises `run_detached`/`kill_detached`/SSH
  remoting). LOCAL means agw runs ON `<remote-lima-host>` itself, driving limactl directly (no SSH
  remoting), through the built-in `lima-local` site. A local install is standing under
  `<remote-user>`; reuse it:
  - REFRESH THE LOCAL CHECKOUT to the branch under test before every local-lima run. It is a
    separate checkout from the one this host drives, so a run against a stale one live-validates
    code the PR does not contain and reports a confident false pass. Never copy a Linux `.venv` into
    it; that host needs its own. The uv path, the checkout path, and the refresh method are
    host-specific and belong in `inventory.local.md`. `uv sync` auto-manages Python 3.12; the
    bundled macOS py3.9 is too old.
  - EVERY agw command there needs `PATH=/opt/homebrew/bin:/usr/local/bin:$PATH` (limactl + tailscale
    are login-shell-only) and, for create, `AW_SECRET_TAILSCALE_AUTH_KEY=<key>` inline (else the key
    prompts). Pass the key by variable reference so its value never lands in a shown command; it
    rides encrypted SSH.
  - Config is already in place: `[operator]` ssh keys, an ed25519 keypair, and the slug seeded to
    `<system-slug>` (the non-interactive slug workaround:
    `uv run python -c 'from agentworks.db.database import Database; Database().set_setting("system_slug","<system-slug>")'`).
  - Use `--template micro` to stay light: the DEFAULT lima-local template is 4cpu/8GiB/50GiB (vz/
    Virtualization.framework). Same budget + cleanup discipline as remote lima (delete +
    `limactl list` after).
- NOT LIVE-REACHABLE (no host; review + unit only): `proxmox` (needs a PVE box; the least-tested
  code with the worst pre-existing bugs, #343), `wsl2` (needs Windows+WSL2, #344/#345). Represent
  these as KNOWN live-test gaps in dispositions, don't imply coverage.

## Known standing issues (do not re-report)

- agentworks#161: digest-less mutable `latest` Lima image (supply chain, slow-mirror re-download).
- agentworks#184: failed `vm create` leaves a partial Lima instance (wedges retry; latent tailnet
  join if later booted). Upstream lima#5188 (cached-fallback fatal) is fixed in lima 2.2.0.
- No non-interactive way to set the system slug (service-layer workaround already applied here).
- Offline `<system-slug>` machine records linger on the tailnet after `vm delete` (non-ephemeral
  auth key).
- agentworks#382: `agw session create --new-workspace` writes a local
  `~/aw-vscode-workspaces/<name>.code-workspace` file on the operator host, and `agw vm delete` does
  not remove it (the file is invisible to `agw resource list`, a purely local artifact).
  `agw session/workspace delete` does cascade-remove it; only the bulk `vm delete` path leaves the
  asymmetry. Sweep it manually in teardown; see the safety protocol section above.

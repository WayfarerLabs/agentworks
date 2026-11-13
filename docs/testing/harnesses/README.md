# Reusable test harnesses

Maintained reference harnesses owned by the `integration-testing` skill: runnable patterns for
driving real, shipped agentworks code against real backends or isolated local state, the kind of
verification that pipeline calls for at the live-validation stage. They are not disposable examples;
they are living tooling, and are kept working the same way any other part of the repo is.

**Maintenance contract:**

- If a harness is found out of date or failing, fix it; do not leave it to rot.
- Re-evaluate the set periodically: does each harness still demonstrate a pattern worth having, and
  does it still run cleanly against the current codebase.
- Grow the set over time as new reusable patterns emerge from live-testing work; a harness earns a
  place here once its pattern has proven useful more than once.
- **Never let a harness carry environment-specific data.** No account IDs, resource-group or
  subscription names, real hostnames, ssh aliases, regions, or usernames. Every harness works
  against an isolated `HOME`, a dummy fixture, or a value a reader is meant to substitute; that is
  what keeps them safe to run anywhere and safe to keep in a public repo.

Each script is self-contained and runnable from the `cli/` directory of an agentworks checkout. The
CLI drives honor `AGW_CLI_DIR` when it points at another prepared CLI tree.

## `native_resume_fixture.py`: native history and MCP protocol fixture

A standard-library Python loopback server for Claude Messages, Codex Responses and Streamable HTTP
MCP. It returns a fixed public assistant reply so a native client can create and resume its own
history without a paid model. It never launches a client, provisions a backend, writes native
history/config, reads credentials or decides whether Agentworks passed. The pattern has been used in
separate native print, exec and interactive observations; each managed acceptance run still owns its
actual version, mode, backend and isolation evidence.

Provision the authorized disposable VM/user first. Run the fixture on the native workload's machine,
or use an authorized tunnel that gives that workload access to its loopback listener. A workstation
loopback address alone is not the guest's loopback address. From the checkout's `cli/` directory:

```sh
umask 077
python3 ../docs/testing/harnesses/native_resume_fixture.py --port 0 --lifetime 1800
```

The first JSONL event reports the bound port. The default lifetime is 600 seconds; the maximum
is 3600. Pick a lifetime covering the bounded case, and stop the fixture when finished. It only
binds `127.0.0.1`, makes no outbound requests and prints safe observations to stdout. Keep any
redirected log in the test session's owned private proof directory.

| Endpoint        | Purpose                           | Public test authentication                                                  |
| --------------- | --------------------------------- | --------------------------------------------------------------------------- |
| `/v1/messages`  | Claude JSON or Messages SSE reply | `ANTHROPIC_API_KEY=AGW_PUBLIC_RESUME_PROVIDER_KEY`                          |
| `/v1/responses` | Codex JSON or Responses SSE reply | Custom provider with `requires_openai_auth=false`; no real account required |
| `/mcp`          | MCP handshake and canary tool     | `Authorization: Bearer AGW_PUBLIC_RESUME_MCP_CANARY`                        |

These constants are public fixture data. The MCP endpoint refuses missing/wrong bindings; provider
endpoints refuse unexpected supplied credentials. Never supply a real credential. This is a
controlled test endpoint, without OAuth, real inference or model-directed tool execution. Provider
`auth_valid` records acceptance of its anonymous/public-key policy; MCP `auth_valid` records the
exact canary comparison.

Prepare only owned native homes and configuration through the test session's normal authorization.
Record their baseline and the installed native versions. For Claude, select the fixture origin in
`ANTHROPIC_BASE_URL` and the public API key above in the actual guest workload environment. Normal
first-run key confirmation and workspace/project trust remain native UI actions. Do not seed
approval files or fabricate account state. See the supported
[Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway-connect).

For Codex, an owned native configuration can select a local Responses provider. Substitute the bound
`PORT`; the model name is only a protocol-fixture label, not a paid-model request:

```toml
model = "gpt-5.1"
model_provider = "agw_resume_fixture"
cli_auth_credentials_store = "file"
mcp_oauth_credentials_store = "file"

[analytics]
enabled = false

[model_providers.agw_resume_fixture]
name = "Owned resume protocol fixture"
base_url = "http://127.0.0.1:PORT/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

Use the actual login-selected native home; keep unrelated settings intact. The
[Codex custom-provider reference](https://learn.chatgpt.com/docs/config-file/config-advanced)
describes these native settings. Record the UI/daemon mode used rather than treating one mode as
evidence for every other mode. Disable unrelated external traffic in the owned test environment; do
not inherit operator provider credentials or reuse an operator's native home.

Define a normal session-scoped MCP artifact pointing to `http://127.0.0.1:PORT/mcp`, with
`Authorization: Bearer ${AGW_RESUME_MCP_TOKEN}` and the public token supplied through the workload's
ordinary declared environment. Select the bundle explicitly on the session template. Use
`agw resource schema artifact-bundle` and `agw resource schema session-template` for current shapes,
and `agw artifact show --session NAME` for the original-owner native registration name. Enable the
appropriate existing session workaround (`session-mcp-config` for Claude or `session-mcp-overrides`
for Codex); preserve the normal profile/conflict checks.

Drive the real installed `agw` CLI and actual native workload:

1. Record an already-running owned neighbor's process/native identity, run/carrier ownership and
   relevant shared native MCP tables. Keep that neighbor running throughout the target case.
2. Create/start the target using its ordinary session template and this exact `initial_prompt`:
   `AGW_PUBLIC_FRESH_PROMPT: Reply with a short public marker only. Do not call tools.` Complete
   normal owned native onboarding if needed. Wait for the actual native assistant reply and inspect
   its native-generated conversation ID/history. An immediately exited workload is not a persistent
   managed conversation; do not wrap it in a delay.
3. Run `agw session restart NAME --resume-only`, then submit:

   ```text
   AGW_PUBLIC_RESUME_PROMPT: Continue the same conversation with a short public marker only. Do not call tools.
   ```

   Observe the continued native conversation and its reply. Keep the native conversation ID and the
   new Agentworks managed-run/carrier ownership distinct. Never edit history, rollout, UUID or
   database records to make resume succeed.

4. Correlate fixture requests with the actual operations and native observations. Provider records
   measure user fresh/resume markers, a prior assistant reply and the canary tool declaration,
   including nested namespaces. MCP records measure the authenticated handshake/catalog. Pair these
   observations with the native ID/history and actual managed runtime; no fixture event supplies an
   Agentworks acceptance verdict or proves real-model cooperation.
5. Verify current-run files/claims, retired prior-run files, the unchanged neighbor and shared MCP
   registrations. Attribute ordinary native onboarding/history changes separately; do not claim
   whole-file byte identity when native trust preferences changed. Run the separate project-consent
   and linked-worktree relocation cases too; a session-carrier result does not cover those paths.

Keep original refusals, expired-endpoint outcomes and corrections. After the case, perform ordinary
managed session/resource cleanup, stop the fixture, independently check its process/listener and
owned files are gone, and restore/remove only the test session's owned configuration. Credential
removal and provider grant revocation are not observations supplied by this fixture. The normal
integration-testing snapshot, budget, gate, review and independent cleanup requirements still apply.

## `facet_fixture_cli.py`: process-local facet acceptance fixtures

Launches the selected tree's unchanged CLI with two fixed harness integrations registered through
`register_plugin` and added to that process's `SYSTEM_PLUGINS` index. This supplies a prerequisite
producer and an unsupported user facet for acceptance work. It does not add a product plugin loader
or replace managers, readiness evaluation, transports, or persistence. Registration exists only for
this invocation; enablement still requires ordinary config opt-in. Local schema/config checks are
launcher coverage. Full acceptance requires a separately authorized real VM run, actual-user native
inspection, and runtime observations.

On a POSIX host, prepare the reviewed CLI tree with `uv sync --frozen` in its `cli/` directory. Set
`AGW_CLI_DIR` to that absolute `cli/` path, or invoke that tree's `.venv/bin/python` directly. The
launcher enters the selected virtual environment in Python isolated mode and rejects an imported
`agentworks` package outside that tree. It preserves the virtual environment Python launcher path
rather than resolving its symlink to the base interpreter.

Supply `--home` with an existing absolute isolated test HOME whose `.config/agentworks/config.toml`,
manifests, keys, and state the test session prepared. The launcher refuses the current account's
real home directory and does not create or copy config, initialize infrastructure, or take
snapshots. The test session owns the snapshot before mutation and independent cleanup verification.
`HOME` becomes this explicit directory; the session's `PATH`, SSH agent, and backend authentication
environment remain available to ordinary CLI operations. This is not a credential isolation harness.

Add the fixture plugin alongside the backend plugins that the test session needs:

```toml
[plugins]
system = ["facet-test-fixtures"] # Include the test session's chosen backend plugins here.
```

Put these ordinary manifests under the isolated HOME's `.config/agentworks/resources/`. All
`facet-test-*` names are local test resources; substitute your test session's VM, workspace, and
actual user names in commands below. Agent templates can inherit the session's prepared base agent
template when its core user setup requires additional configuration.

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: facet-test-active
spec:
  harness_integrations:
    facet-test-readiness: {}
---
apiVersion: agentworks/v1
kind: admin-template
metadata:
  name: facet-test-active
spec:
  harness_integrations:
    facet-test-readiness: {}
---
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: facet-test-inactive
spec:
  harness_integrations:
    facet-test-readiness: null
---
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: facet-test-unsupported
spec:
  harness_integrations:
    facet-test-unsupported: {}
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: facet-test-required
spec:
  harness_integration:
    name: facet-test-readiness
    prerequisite: required
    command: sleep 600
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: facet-test-recommended
spec:
  harness_integration:
    name: facet-test-readiness
    prerequisite: recommended
    command: sleep 600
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: facet-test-none
spec:
  harness_integration:
    name: facet-test-readiness
    command: sleep 600
```

`facet-test-readiness` inherits the shipped shell integration's VM, user, workspace, and session
behavior. Its session-only `prerequisite` defaults to `none`, which returns no gaps and does not
request ancestor evidence. `required` and `recommended` consult `evidence.user` and return a gap
unless that actual user's evidence is current. Setup activations accept the shell's empty setup
config. To exercise admin setup, select `facet-test-active` as the VM's admin template; VM-wide
activation alone cannot satisfy this fixture's user prerequisite.

Run from `cli/`, replacing the values assigned to these shell variables with the test session's
inventory. Each `facet_cli` call starts a fresh process:

```sh
export AGW_CLI_DIR=/absolute/path/to/reviewed/cli
facet_test_home=/absolute/path/to/isolated/test-home
facet_test_vm=your-test-vm
facet_test_workspace=your-test-workspace
facet_test_user=your-test-agent
facet_cli() {
  uv run --no-sync --directory "$AGW_CLI_DIR" python ../docs/testing/harnesses/facet_fixture_cli.py \
    --home "$facet_test_home" -- "$@"
}
facet_cli resource list --kind harness-integration --include-disabled
facet_cli resource explain harness-integration/facet-test-readiness
facet_cli resource schema session-template
facet_cli resource show agent-template/facet-test-active --output json
facet_cli agent create "$facet_test_user" --vm "$facet_test_vm" --template facet-test-active
facet_cli agent describe "$facet_test_user" --output json
facet_cli session create facet-test-session --workspace "$facet_test_workspace" \
  --agent "$facet_test_user" --template facet-test-required
```

Use unique session names for independent cases, or explicitly delete the preceding session. Capture
exit codes, owning inspection evidence, and the actual runtime before and after each operation:

| Case                    | Preparation and command                                                                                                                                                                                              | Required observation                                                                                                                                                                                                          |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Current actual user     | Create an active agent as above; launch required, recommended, and none templates. Repeat using `--admin --vm "$facet_test_vm"` with admin activation.                                                               | Launch succeeds against the matching agent or admin evidence. Another user's completed setup cannot satisfy this user's gap.                                                                                                  |
| Absent and nonactivated | Complete admin and first-agent setup; create a second user from `facet-test-inactive` and launch each session template for that second user.                                                                         | Required refuses; recommended warns and launches; none launches. No implicit user setup occurs.                                                                                                                               |
| Stale                   | On a completed user, change its effective activation to `null` without reinit; retry required launch or restart an existing required session.                                                                        | Required refuses before runtime replacement; remediation identifies that user's owning setup. Restore activation or apply the intended change with `agent reinit`.                                                            |
| Incomplete              | On a disposable active user, select a user artifact bundle, then make its shell publication parent `~/.agentworks-artifacts` a regular file before `agent reinit`. Inspect the failed setup record before launching. | Failed publication leaves incomplete evidence; required refuses. Remove the deliberate obstruction and reinit before retry. Use the actual user's SSH transport for this preparation.                                         |
| Pending user creation   | Run `session create` with `--new-agent --agent-name facet-test-pending --agent-template facet-test-active` and the required session template, supplying the prepared workspace and VM.                               | Owning user setup completes before readiness and launch; inspect the new user's committed evidence and runtime.                                                                                                               |
| Unsupported user facet  | Create a disposable agent using `facet-test-unsupported`, or reinit one with `--update-template facet-test-unsupported`.                                                                                             | The declared config is valid, but binding preflight refuses the unimplemented user facet before native setup. New-owner creation must leave no row or native residue; an existing owner's confirmed files must remain intact. |

The incomplete case needs real artifact input: declare an ordinary `artifact-bundle` containing a
small inline hint, and select it with `artifacts: {bundles: [your-test-bundle]}` on the agent
template. Keep that case separate from the no-artifact readiness cases so artifact freshness cannot
mask prerequisite behavior. Do not edit persisted setup records to manufacture a verdict. Restart
refusal must leave the existing pane/process alive; exit status alone does not establish that.

The launcher neither certifies these cases nor cleans up remote resources. After the live drive,
delete its sessions and disposable owning resources through the ordinary CLI, verify native files
and backend residue independently, and remove the isolated HOME according to the session's snapshot
and cleanup plan.

## `secret_sources_drive.py`: value-free Secret Sources acceptance drive

Runs the selected tree's real `agw` console script through the Secret Sources acceptance cases on
POSIX Unix/Linux hosts, including current CI: implied environment preview, a no-TTY prompt block, a
mixed variadic preview, doctor readiness and no-impact preview, direct OnePassword backend migration
guidance, and a declared OnePassword source at a fake provider boundary under global
`--non-interactive`. That last case proves the global flag disables TTY interaction without
suppressing out-of-band provider work. The harness fails closed on Windows and every other
unsupported host before it reads the environment, selects a CLI, creates fixtures, looks up a
provider, or runs a child command. It never attempts a provider there. When `AGW_CLI_DIR` is set on
a supported host, the harness uses that tree's `.venv` and rejects an executable or imported
`agentworks` package outside the tree.

Child processes receive an allowlisted environment with no inherited home, credential, user-site, or
import path. `Path.home()` resolves to a fresh temporary directory. The OnePassword case uses a
POSIX executable named `op` on a fake-only `PATH`, verifies lookup resolves exactly that executable,
and cannot fall through to a real credential tool. Every command result is checked for a unique
sentinel before any other assertion. It prints only value-free case summaries. Run it from `cli/`
with `uv run python ../docs/testing/harnesses/secret_sources_drive.py`.

## `isolated_home_drive.sh`: isolated-HOME CLI drive

Runs the real `agw` CLI against a throwaway `HOME`, so a live drive never touches an operator's real
`~/.config/agentworks` (config, resources, DB). Every agentworks path is derived from `Path.home()`,
so pointing `HOME` at a fresh temp directory is enough to sandbox a full run: first-run behavior,
config init, resource creation, and so on can all be exercised with zero state-mutation risk and
zero cleanup (the temp `HOME` is discarded on exit). Use this pattern for anything that would
otherwise mutate operator state you cannot afford to lose.

## `recorder_drive.py`: real-code driver

Imports shipped code directly (skips the CLI and the filesystem) and drives it against a small
battery of representative payloads, recording what it actually returns or raises. Faster than a CLI
drive and it observes the real function under test rather than a re-implementation or a mock of it.
Best for checking a code-level contract (e.g. "this function never raises") on a function you can
call directly. The example here drives `agentworks.schema.extract_references`'s documented
never-raises contract; the project's own property-based test suite
(`tests/schema/test_extract_totality.py`) is the maintained, exhaustive version of this idea; this
script is the quick, readable, one-off version of the same technique, useful for spot-checking a
similar contract elsewhere in the codebase.

## `breaking_change_loader_drive.sh`: breaking-change loader drive

When a change makes an on-disk format (config, a resource shape, a DB schema) incompatible with what
it replaces, this pattern drives the real loader against a fixture written in the OLD shape and
asserts on the observed outcome: either a clean migration, or a loud, precise failure. A silent
wrong answer (the old file loads but is silently ignored or misinterpreted) is the one outcome this
pattern exists to catch; a crash with a clear, actionable message is an acceptable, honest failure
for a breaking change. The example here drives config.toml's ADR-0022 resource-section sunset (a
legacy inline `[azure]` section must be rejected loudly, not silently dropped).

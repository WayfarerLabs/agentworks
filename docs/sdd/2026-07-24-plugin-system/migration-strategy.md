# Migration strategy: four world-specific bundles out of the core (R11, R11.1)

Governs plan [Phases 8-11 and the migration closeout](./plan.md); mechanics per the
[framework LLD (a)](./plugin-framework-lld.md), the
[enablement LLD (b)](./enablement-producer-lld.md), and the
[surfaces LLD (c)](./plugin-surfaces-lld.md) (including its Phase 7 parity extension). This is a
strategy document: it fixes the inventory, the target shape, the per-bundle deltas, the handling of
the breaking change, and the order of work. The per-phase implementation detail stays in the plan
and the LLDs.

## 1. Current-state inventory (snapshot 2026-07-30, at HEAD of `feat/plugin-system-sdd`)

The five implementations moving, with their core seat and publish points:

| impl                  | code                                                                                                                              | seat                                                                           | publish                                     |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------- |
| `azure-vm` platform   | `capabilities/vm_platform/azure_vm.py` (954 lines, class `AzureVMPlatform` at `:208`)                                             | `VM_PLATFORM_REGISTRY`, `capabilities/vm_platform/__init__.py:44-48` (class)   | `vm_platform/__init__.py:89` (`publish_to`) |
| `azdo` git-credential | `capabilities/git_credential/azdo.py` (126 lines, class `AzDOCredentialProvider` at `:30`)                                        | `GIT_CREDENTIAL_PROVIDER_REGISTRY`, `git_credential/__init__.py:50-53` (class) | `git_credential/__init__.py:56`             |
| `claude-code` harness | `capabilities/harness/claude_code.py` (210 lines, class `ClaudeCodeHarness` at `:47`)                                             | `HARNESS_REGISTRY`, `capabilities/harness/__init__.py:43-46` (class)           | `harness/__init__.py:104-123`               |
| `proxmox` platform    | `capabilities/vm_platform/proxmox.py` (459 lines, class `ProxmoxPlatform` at `:38`) plus its sibling `proxmox_api.py` (253 lines) | `VM_PLATFORM_REGISTRY`, `vm_platform/__init__.py:44-48` (class)                | `vm_platform/__init__.py:89`                |
| `onepassword` backend | `secrets/onepassword.py` (411 lines, class `OnePasswordBackend` at `:224`, `name` at `:248`)                                      | `SECRET_BACKEND_REGISTRY`, `secrets/backends.py:185-189` (**instance**)        | `secrets/backends.py:194`                   |

The one manifest entry moving: the `az-cli` **system-install-command** at
`manifests/builtin/install-commands.yaml:15-22` (note: `system-`, not `user-`; the FRD's
`user_install_commands: ["az-cli"]` illustration is a hypothetical shape, the shipped row is
system-level and consumed by VM init).

**Shared helpers that STAY in core** (each has core or multi-bundle importers, verified):

- `capabilities/vm_platform/base.py` (the `VMPlatform` contract every platform extends).
- `capabilities/vm_platform/bootstrap_script.py`: imported by `lima.py:15`, `proxmox.py:12`,
  `azure_vm.py:11`, **and core** `vms/initializer/ssh_keys.py:118,164`.
- `capabilities/vm_platform/cloud_init.py`: imported by `lima.py:20`, `azure_vm.py:12`,
  `proxmox.py:13` (shared by azure AND proxmox), **and core** `vms/initializer/packages.py:11`.
- The capability `base.py`s (`harness/base.py`, `git_credential/base.py`, `capabilities/base.py`)
  and `secrets/base.py` / `env_var.py` / `prompt.py`.
- `harness_for` and `ensure_harness_enabled` (`capabilities/harness/__init__.py:49,68`): they key by
  registry name, not concrete class, so they are consumer machinery, not bundle code.

**Test footprint** (files mentioning each bundle, `grep -rl` over `cli/tests`): proxmox **43**,
azure/azdo **35**, onepassword **10**, claude-code **6**. Proxmox's number is dominated by its role
as the shared orchestrated-test fixture platform, not by proxmox-specific behavior tests.

**Operator-facing surface referencing the bundles today**: `sample-config.toml` `:118-123` (the
`azdo` git-credential example), `:154-194` (the onepassword mappings and chain examples), `:209`
(the `[plugins] enabled = ["azure"]` example, already present), `:211-217` (sites, including the
deprecated legacy `[azure]` / `[proxmox]` flat sections), `:314-343` (claude marketplaces/plugins
comments), `:397-427` (the `claude-code` harness section); `docs/guides/proxmox.md`;
`docs/guides/resources.md`; ADR 0021 (and the historical ADRs 0018/0020, which stay as records).

**Known discrepancy, flagged for the lead**: a `claude` **user-install-command** exists at HEAD
(`manifests/builtin/install-commands.yaml:60-69`, "Claude Code CLI"), but plan Phase 8 says "the
`claude` install-command does not exist" and scopes the claude plugin to the harness only, while FRD
R11/R11.1 assign a `claude` install-command to the claude plugin. This document follows the plan
(claude = harness only) and treats the `claude` install-command's home as an **open decision**:
moving it matches the FRD's vendor-specific rule but makes claude a second manifest-carrying plugin
and gates `user_install_commands = ["claude"]` (and the claude-marketplace flows that expect the CLI
installable) behind the plugin; keeping it core keeps Phase 8 trivially small but leaves a
vendor-specific row in the core bundle. Decide before Phase 8 lands.

## 2. Target shape

Each bundle becomes a package `agentworks/plugins/<name>/`:

- The package `__init__.py` carries the `PLUGIN` descriptor (`plugins/base.py:63-99`): `name`,
  `description`, `capabilities` keyed by kind with **impl classes** uniformly (the secret-backend
  adapter constructs the instance at seating, LLD a's adapter table), and `manifests` set to the
  package anchor when the plugin bundles YAML.
- Impl modules move via **`git mv`** (history preserved; do the move and any content edits in
  separate commits so rename detection holds), imports repointed at the core `base.py`s that stay.
- The package is appended to `_INSTALLED_MODULES` (`plugins/__init__.py:75`), which registers and
  indexes it (`plugins/__init__.py:54-77`).
- The core side **drops** the impl: registry dict entry, module import, `__all__` entry. The
  per-kind `publish_to`s iterate their registries, so no publish-site edit is needed beyond the
  registry drop; the plugin's rows are published by `publish_plugins` (`plugins/publish.py:84-90`)
  with `Origin.system_plugin(...)` instead of `Origin.built_in(...)`.

Plugin composition (pinned):

| plugin      | capabilities                                                                            | bundled manifests                                                               |
| ----------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `claude`    | `harness: (ClaudeCodeHarness,)`                                                         | none (see the flagged `claude` decision)                                        |
| `1password` | `secret-backend: (OnePasswordBackend,)`                                                 | none                                                                            |
| `proxmox`   | `vm-platform: (ProxmoxPlatform,)` (+ `proxmox_api.py` as a package sibling)             | none                                                                            |
| `azure`     | `vm-platform: (AzureVMPlatform,)`, `git-credential-provider: (AzDOCredentialProvider,)` | `manifests/install-commands.yaml` (`az-cli`), moved out of `manifests/builtin/` |

`azdo` is part of the azure plugin, not a standalone plugin (matches prior art and the one-vendor
grouping). The plugin package name `1password` starts with a digit, which is not an importable
module name: the **package directory** is `agentworks/plugins/onepassword/` while the **plugin
name** (the descriptor's `name`, the `[plugins] enabled` token, the origin's `plugin`) is
`"1password"`. The descriptor name is the operator-facing identity; the module name is an
implementation detail the index maps from.

## 3. Per-bundle before/after

Common to all four: the row's origin flips `built-in` -> `system-plugin <plugin> (<source>)`, the
row publishes unconditionally but **disabled** until the operator opts in, it disappears from the
default `resource list` (disabled hides), appears in the doctor roster as
`plugin <name>: ... disabled (not enabled in [plugins])`, and every consumer honors the disabled
state per its R14 model. Per bundle, the operator-visible delta:

- **claude**: `HARNESS_REGISTRY` keeps only `shell` (the default harness, so the common path is
  untouched). A `session-template` with `harness = "claude-code"` still lists **ready**; session
  create/restart raises the existing `ensure_harness_enabled` typed error
  (`capabilities/harness/__init__.py:68-101`) with the "enable plugin `claude`" hint until
  `[plugins] enabled = ["claude"]`.
- **1password**: the instance-seated kind, exercising the adapter's instance path. A `secret` with
  `backend_mappings.onepassword` stays ready; the disabled backend is excluded from
  `active_backends`, resolution, and mapping validation (the refactor's existing enablement gates),
  so mappings to it are inert until the plugin is enabled; a `[secret_config] backends` chain naming
  it skips it as disabled (distinct from the host-unsupported skip).
- **proxmox**: a `vm-site` on the proxmox platform is **not-ready** with
  `depends on vm-platform 'proxmox', which is disabled; enable plugin proxmox`, and
  `ensure_site_ready` refuses use. The deprecated legacy `[proxmox]` flat-section site gets the same
  hint (a feature: legacy configs are guided, not broken with an unknown-name error).
- **azure** (needs Phase 7 landed): all three kinds plus the manifest. The `azure-vm` site behaves
  like proxmox's; a `git-credential` with `provider = "azdo"` is not-ready via its R14 propagate
  hook and refused at use; the `az-cli` row publishes from the plugin's bundle (weak while
  disabled), so a vm-template with `system_install_commands = ["az-cli"]` finalizes cleanly and
  refuses at vm create/reinit through the Phase 7 recipe gate with the enable hint, and an operator
  who declares their own `az-cli` install-command overrides the disabled row with no collision
  error.

## 4. The guided breaking change (R11.1)

What breaks, deliberately, for an operator who upgrades without touching `[plugins]`: any config
referencing `azure-vm`, `azdo`, `proxmox`, `onepassword`, `claude-code`, or `az-cli`. Every such
reference lands on a present-but-disabled row, so the failure mode is always the typed "enable
plugin `<name>`" hint (site not-ready, credential not-ready, harness/recipe use refusal, backend
exclusion), **never** an unknown-name hard error. The default local path (`lima` / `wsl2` +
`shell` + `env-var` / `prompt` + `github` + the generic dev-tool install-commands) references none
of them; a capstone test pins that a default config builds and runs green with zero plugins enabled.

The guidance shipped with the change (each in the phase that makes it true, per doc lockstep):

- `sample-config.toml`: the `[plugins]` example (`:209`) gains the four shipped names; each
  bundle-referencing example section (azdo `:118`, onepassword `:154`, sites `:211`, claude-code
  harness `:397`) gains a one-line "requires `[plugins] enabled = [...]`" comment in its bundle's
  phase.
- `docs/guides/resources.md`: the migration note (what moved, why nothing hard-errors, how to
  re-enable); `docs/guides/proxmox.md` gains the enable step in Phase 10.
- The doctor roster lists all four plugins with their enable state, so `agw doctor` is the discovery
  surface the hint text points at.
- ADR 0021 records the migration and marks the R9 manifest limitation RESOLVED.
- Completions: no CLI surface changes in Phases 8-11 (`[plugins]` is config, not CLI), so the
  completion tree needs no regeneration beyond Phase 6's `--include-disabled`; verified per phase.

The re-enable path is one line, `[plugins] enabled = ["azure", "proxmox", ...]`, present on both
config load paths since Phase 3.

## 5. Sequencing and transition mechanics

Order: **claude -> 1password -> proxmox -> azure**, least-test-invasive first, each phase green and
complete on its own (no bridging aliases, no half-moved bundles):

1. **claude (Phase 8)**: smallest footprint (6 test files, one class, no manifest), and it proves
   the end-to-end shape (move, descriptor, index, origin flip, opt-in gate) on the kind whose
   use-gate already exists.
2. **1password (Phase 9)**: still small (10 files), adds the one novel mechanism claude does not
   exercise: the instance-seated adapter path and the secret-resolution exclusion gates.
3. **proxmox (Phase 10)**: the test-invasive one (43 files). Two-step inside the phase:
   - **Repoint first, migrate second.** While proxmox is still built-in, repoint every test that
     uses proxmox merely as "some platform fixture" to `lima` (core, always seated; the orchestrated
     conftest already stubs every platform's host support to ready, `tests/conftest.py:285-287`, so
     lima serves as the fixture platform on any host). This is a pure test refactor with no
     production change, lands green, and shrinks the migration diff to the tests that actually test
     proxmox behavior.
   - Then migrate the impl and, in the remaining proxmox-specific tests (the API/bootstrap/site
     tests), enable the plugin through the shared config fixture (`plugins_enabled = ("proxmox",)`),
     pinning one test on the disabled default (site not-ready with the hint).
4. **azure (Phase 11)**: the fullest exercise (35 files, three kinds, the only bundled manifest),
   deliberately last: it depends on Phase 7's parity (weak publication, recipe gate) and benefits
   from every mechanism the three earlier migrations proved. The `az-cli` entry moves from
   `manifests/builtin/install-commands.yaml` into `agentworks/plugins/azure/manifests/` in the same
   phase (delete + add, one commit with the descriptor's `manifests` anchor).

Per-phase mechanics checklist (the plan's DoD, restated as strategy): `git mv` the impl(s); author
the descriptor; append to `_INSTALLED_MODULES`; drop the core seat/import/`__all__`; update the
bundle's tests (enable-or-repoint) and pin the breaking-change hint through the bundle's real
consumer; land the bundle's doc/sample-config lines; full gate green.

## 6. Worked example: the claude bundle end to end

The representative case (Phase 8), shown once so the other three read as deltas:

1. `git mv cli/agentworks/capabilities/harness/claude_code.py cli/agentworks/plugins/claude/claude_code.py`
   (plus a new empty `plugins/claude/__init__.py` in the same commit). `claude_code.py`'s imports
   (`from agentworks.capabilities.harness.base import Harness, require_commands`) already point at
   the core base module that stays; no content change.
2. `plugins/claude/__init__.py`:

   ```python
   from agentworks.plugins.base import Plugin
   from agentworks.plugins.claude.claude_code import ClaudeCodeHarness

   PLUGIN = Plugin(
       name="claude",
       description="Claude Code session harness",
       capabilities={"harness": (ClaudeCodeHarness,)},
   )
   ```

3. `plugins/__init__.py`: import the module and append it to `_INSTALLED_MODULES`
   (`plugins/__init__.py:75`); the index registers it (seating `claude-code` into `HARNESS_REGISTRY`
   at import, via the adapter) and `SYSTEM_PLUGINS["claude"]` exists.
4. Core drop, `capabilities/harness/__init__.py`: remove the `ClaudeCodeHarness` import (`:20`), its
   `HARNESS_REGISTRY` entry (`:43-46`), and its `__all__` entry (`:28`). `publish_to` (`:104-123`)
   needs no edit: it iterates the registry, which now holds only `shell`; the `claude-code` row is
   published by `publish_plugins` with the `system-plugin` origin.
5. Behavior now, with no config change: the `claude-code` harness row is present-but-disabled;
   hidden from default `resource list`; `describe harness/claude-code` shows
   `Disabled: not enabled in [plugins] (plugin claude)`; doctor lists `plugin claude: ... disabled`;
   a `session-template` naming it lists ready and session create raises the `ensure_harness_enabled`
   error with the "enable plugin `claude`" hint. With `[plugins] enabled = ["claude"]`: enabled,
   listed, consumable, origin `system-plugin claude (agentworks.plugins.claude)`.
6. Tests (6 files): route the harness-using tests through the config fixture's
   `plugins_enabled=("claude",)`; add the breaking-change pin (disabled by default, typed error with
   the hint, enabled works); keep one seating test asserting `HARNESS_REGISTRY` no longer carries
   `claude-code` at core import.
7. Docs lockstep in the same phase: the sample-config claude-code comment (`:397-427`), the
   resources-guide migration note line for claude, ADR 0021's migration record.

## 7. Risks and safeguards

- **A core reference to a moved name survives by accident.** Safeguard: each phase greps the core
  for the moved identifiers (class name and registry key) as part of review; the seating test in
  step 6 pins the registry drop. Known benign residue: `install_claude_plugins` probes the `claude`
  CLI binary on the VM by name (`vms/initializer/driver.py:822-833`), a remote filesystem fact, not
  a registry reference.
- **The breaking change breaks more than intended.** Safeguard: the capstone default-path test (zero
  plugins enabled, default config, full build + local flows green) plus the
  enable-every-shipped-plugin fixture (curation cleanliness, LLD c 3b acceptance).
- **Legacy flat-section configs** (`[azure]` / `[proxmox]` deprecated site sections,
  `sample-config.toml:215-217`) must degrade to the hint, not an error. Safeguard: a per-phase test
  builds a registry from a legacy-section config with the plugin disabled and asserts the not-ready
  hint.
- **History loss on the moves.** Safeguard: `git mv` in its own commit, content edits separate.
- **Import-order surprises**: `agentworks.plugins` now imports capability modules at index build
  (seating). The lazy capability-registry loaders (`resources/graph.py:586-591`) and
  `publish_plugins`'s lazy `SYSTEM_PLUGINS` import (`plugins/publish.py:70`) exist precisely to
  tolerate this; the fixture's import-order test extends to the first real plugin in Phase 8.
- **Test-fixture drift during the proxmox repoint.** Safeguard: the repoint commit lands before the
  migration commit and must be green on its own, so any behavioral assumption a fixture silently
  made about proxmox surfaces as a lima diff, not as a migration failure.
- **The `claude` install-command decision** (section 1) is unresolved; Phase 8 must not land until
  the lead rules, because moving it later would be a second breaking change for
  `user_install_commands = ["claude"]` operators.

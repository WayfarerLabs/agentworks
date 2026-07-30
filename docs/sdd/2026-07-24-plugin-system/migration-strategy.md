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

Two manifest entries move, one per manifest-carrying plugin:

- `az-cli` **system-install-command** (`manifests/builtin/install-commands.yaml:15-22`, note
  `system-`, not `user-`; consumed by VM init) -> the **azure** plugin.
- `claude` **user-install-command** (`manifests/builtin/install-commands.yaml:60-69`, "Claude Code
  CLI", `path: [~/.local/bin]`, `test_exec: claude`; consumed by agent/admin init) -> the **claude**
  plugin.

So there are **two** manifest-carrying plugins (claude and azure), not one; each moves its
install-command entry out of `manifests/builtin/install-commands.yaml` and into its plugin package's
`manifests/`. Both depend on Phase 7 (manifest present-but-disabled parity) having landed.

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

**Stays in core, deliberately** (honesty against the "world-specific out of core" motivation): the
`claude_marketplaces` / `claude_plugins` agent-template + admin fields (`agents/templates.py:38-39`,
`vms/admin.py:58`) and the `install_claude_plugins` VM-init step
(`vms/initializer/driver.py:815-833`) are **not** migrated in this effort. They are
Claude-Code-specific but are not a capability impl or a declarable resource, so the plugin unit as
built (R6, capabilities + declarables only) has no seat for them; moving them needs the future
feature-capability kind (FRD Future direction). This is a conscious partial migration: the
`claude-code` harness and the `claude` install-command move now; the marketplace/plugin provisioning
fields wait. Operators enabling `[plugins] enabled = ["claude"]` get the harness and the
install-command; the marketplace fields keep working from core regardless (they only warn if the
`claude` CLI is absent, `driver.py:822-833`).

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
- The core side **drops** the impl from its literal registry seat (the dict entry), its module
  import, and its `__all__` entry, but the impl does **not** leave the code registry:
  `register_plugin` re-seats it there at import through the kind's adapter, and `_impl_for`
  (`graph.py:528`) reads the impl off that same registry to stamp the graph node. That is required
  (resolution reaches the impl through the node), and it means the per-kind core `publish_to` would
  re-publish the plugin-seated impl as a **built-in** row, colliding with `publish_plugins`'s
  `system-plugin` row at `Registry.add`. So each core `publish_to` **must skip the plugin-seated
  names** (`plugin_seated_names(kind)` off `_PLUGIN_SEATED`, the provenance the collision-message
  path already keeps), leaving `publish_plugins` (`plugins/publish.py:84-90`) as the sole publisher
  of the plugin's rows with `Origin.system_plugin(...)`. Phase 8 adds this filter to
  `secrets/backends.py::publish_to`; phases 9-11 add the same one-line filter to the harness /
  vm-platform / git-credential `publish_to`s. (This corrects an earlier draft that claimed "no
  publish-site edit is needed": that overlooked the plugin re-seating into the shared core
  registry.)

Plugin composition (pinned):

| plugin        | capabilities                                                                            | bundled manifests                                                               |
| ------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `onepassword` | `secret-backend: (OnePasswordBackend,)`                                                 | none                                                                            |
| `claude`      | `harness: (ClaudeCodeHarness,)`                                                         | `manifests/install-commands.yaml` (`claude`), moved out of `manifests/builtin/` |
| `proxmox`     | `vm-platform: (ProxmoxPlatform,)` (+ `proxmox_api.py` as a package sibling)             | none                                                                            |
| `azure`       | `vm-platform: (AzureVMPlatform,)`, `git-credential-provider: (AzDOCredentialProvider,)` | `manifests/install-commands.yaml` (`az-cli`), moved out of `manifests/builtin/` |

`azdo` is part of the azure plugin, not a standalone plugin (matches prior art and the one-vendor
grouping). The 1Password plugin's name is **`onepassword`**, matching the backend's registry name
(`OnePasswordBackend.name == "onepassword"`, `secrets/onepassword.py:248`): a name like `1password`
would make the origin source `agentworks.plugins.1password`, an invalid Python module (a leading
digit is not a legal identifier), so the descriptor `name`, the package directory
(`agentworks/plugins/onepassword/`), the `[plugins] enabled` token, and the origin's `plugin` are
all `onepassword`, uniformly. `claude` and `azure` gain a `manifests` anchor (their package's
`manifests/` subdirectory); the other two ship capabilities only.

## 3. Per-bundle before/after

Common to all four: the row's origin flips `built-in` -> `system-plugin <plugin> (<source>)`, the
row publishes unconditionally but **disabled** until the operator opts in, it disappears from the
default `resource list` (disabled hides), appears in the doctor roster as
`plugin <name>: ... disabled (not enabled in [plugins])`, and every consumer honors the disabled
state per its R14 model. Per bundle, the operator-visible delta:

- **claude** (needs Phase 7 landed): a capability **and** a manifest. `HARNESS_REGISTRY` keeps only
  `shell` (the default harness, so the common path is untouched). A `session-template` with
  `harness = "claude-code"` still lists **ready**; session create/restart raises the existing
  `ensure_harness_enabled` typed error (`capabilities/harness/__init__.py:68-101`) with the "enable
  plugin `claude`" hint until `[plugins] enabled = ["claude"]`. The `claude` install-command row
  publishes from the plugin's bundle (weak while disabled), so an agent/admin template with
  `user_install_commands = ["claude"]` finalizes cleanly and refuses at agent/vm create (and at
  session create `--new-agent`) through the Phase 7 recipe gate with the enable hint; an operator
  who declares their own `claude` user-install-command overrides the disabled row with no collision
  error, and (once enabled) an operator's TOML `claude` override still wins per BLOCKING 1.
- **onepassword**: the instance-seated kind, exercising the adapter's instance path. A `secret` with
  `backend_mappings.onepassword` stays ready; the disabled backend is excluded from
  `active_backends`, resolution, and mapping validation (the refactor's existing enablement gates),
  so mappings to it are inert until the plugin is enabled; a `[secret_config] backends` chain naming
  it skips it as disabled (distinct from the host-unsupported skip). A secret whose **only** mapping
  targets the disabled backend fails resolve with the Phase 8 plugin-aware hint (LLD b, "enable
  plugin `onepassword`"), not a generic unreachable message.
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
referencing `azure-vm`, `azdo`, `proxmox`, `onepassword`, `claude-code`, `az-cli`, or the `claude`
install-command. Every such reference lands on a present-but-disabled row, so the failure mode names
the plugin, **never** an unknown-name hard error:

- **site / credential / harness / install-command / template**: the typed "enable plugin `<name>`"
  hint (site not-ready and refused at use, credential not-ready and refused, harness/recipe refused
  at create/use).
- **secret-backend**: the one kind that gates by **exclusion, not refusal**. A disabled
  `onepassword` is silently dropped from the active chain, so a secret with **another** working
  backend still resolves (no failure at all). Only a secret whose **sole** mapping is the disabled
  backend fails, and Phase 8 gives that failure the plugin-aware hint ("enable plugin
  `onepassword`", LLD b), so it too names the plugin rather than the generic "secret unreachable".
  (Before the Phase 8 hint the message would be generic; the migration ships them together.)

The default local path (`lima` / `wsl2` + `shell` + `env-var` / `prompt` + `github` + the generic
dev-tool install-commands) references none of them; a capstone test pins that a default config
builds and runs green with zero plugins enabled.

The guidance shipped with the change (each in the phase that makes it true, per doc lockstep):

- `sample-config.toml`: the `[plugins]` example (`:209`) gains the four shipped names
  (`onepassword`, `claude`, `proxmox`, `azure`); each bundle-referencing example section (azdo
  `:118`, onepassword `:154`, sites `:211`, claude-code harness `:397`, the `claude` /
  `user_install_commands` comments `:314-347`) gains a one-line "requires
  `[plugins] enabled = [...]`" comment in its bundle's phase.
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

Order (per plan): **onepassword -> claude -> proxmox -> azure**, least-test-invasive first, each
phase green and complete on its own (no bridging aliases, no half-moved bundles):

1. **onepassword (Phase 8)**: the clean capability-only first migration (10 test files, one class,
   no manifest, does not need Phase 7). It proves the end-to-end shape (move, descriptor, index,
   origin flip, opt-in gate) plus the one mechanism unique to its kind: the instance-seated adapter
   path and the secret-resolution exclusion gates (with the Phase 8 plugin-aware unavailable hint,
   LLD b).
2. **claude (Phase 9)**: harness capability **plus** the `claude` install-command manifest (6 test
   files for the harness; the manifest adds the recipe-gate cases). Needs Phase 7. Proves a
   manifest-carrying plugin end to end on the kind whose harness use-gate already exists and whose
   install-command exercises the Phase 7 recipe gate (including the `--new-agent` path).
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
   - **Repoint the frozen migration import** (below): `_migrate_vm_sites` imports `ProxmoxPlatform`
     directly; move that import to the new plugin package path in the same phase.
4. **azure (Phase 11)**: the fullest exercise (35 files, three kinds, a bundled manifest),
   deliberately last: it depends on Phase 7's parity (weak publication, recipe gate) and benefits
   from every mechanism the three earlier migrations proved. The `az-cli` entry moves from
   `manifests/builtin/install-commands.yaml` into `agentworks/plugins/azure/manifests/` in the same
   phase (delete + add, one commit with the descriptor's `manifests` anchor). Repoint the frozen
   `AzureVMPlatform` migration import (below) in this phase.

**The frozen `db/migrations.py` import (Phases 10 and 11).** `_migrate_vm_sites`
(`db/migrations.py:72-83`) imports `AzureVMPlatform` / `ProxmoxPlatform` (alongside `LimaPlatform` /
`WSL2Platform`) **directly by class**, deliberately registry-independent: it is the v27 data
migration that backfills platform metadata and must not depend on the live registry (which may be
mid-construction on the retry path it guards). Migrating those two platforms therefore includes a
named step to repoint this frozen import at the new plugin-package module paths
(`agentworks.plugins.proxmox...`, `agentworks.plugins.azure...`), keeping the migration's
registry-bypassing philosophy intact. `test_db_migration_vm_sites.py` catches a wrong path, but the
repoint must be a deliberate step in Phases 10 (proxmox) and 11 (azure), not a surprise at test
time.

Per-phase mechanics checklist (the plan's DoD, restated as strategy): `git mv` the impl(s); author
the descriptor; append to `_INSTALLED_MODULES`; drop the core seat/import/`__all__`; update the
bundle's tests (enable-or-repoint) and pin the breaking-change hint through the bundle's real
consumer; land the bundle's doc/sample-config lines; full gate green.

## 6. Worked example: the claude bundle end to end

Claude (Phase 9) is the representative **manifest-carrying** case: one capability plus one bundled
declarable, so it exercises the origin flip, the harness use-gate, and the Phase 7 recipe gate in
one bundle. (Phase 8, onepassword, is the same minus the manifest and minus the Phase 7 dependency:
a capability-only migration.) Shown once so the other three read as deltas:

1. `git mv cli/agentworks/capabilities/harness/claude_code.py cli/agentworks/plugins/claude/harness.py`
   (plus a new `plugins/claude/__init__.py` and a `plugins/claude/manifests/` directory in the same
   commit). `harness.py`'s imports
   (`from agentworks.capabilities.harness.base import Harness, require_commands`) already point at
   the core base module that stays; no content change. (The moved file is renamed to the role name
   `harness.py`, matching `onepassword/backend.py`.)
2. Move the manifest: extract the `claude` `user-install-command` document from
   `manifests/builtin/install-commands.yaml:60-69` into
   `plugins/claude/manifests/install-commands.yaml` (delete from the builtin bundle, add to the
   plugin bundle, one commit).
3. `plugins/claude/__init__.py`:

   ```python
   from agentworks.plugins.base import Plugin
   from agentworks.plugins.claude.harness import ClaudeCodeHarness

   PLUGIN = Plugin(
       name="claude",
       description="Claude Code session harness and CLI install command",
       capabilities={"harness": (ClaudeCodeHarness,)},
       manifests="agentworks.plugins.claude",  # its manifests/ subdir holds the claude install-command
   )
   ```

4. `plugins/__init__.py`: import the module and append it to `_INSTALLED_MODULES`
   (`plugins/__init__.py:75`); the index registers it (seating `claude-code` into `HARNESS_REGISTRY`
   at import, via the adapter) and `SYSTEM_PLUGINS["claude"]` exists.
5. Core drop, `capabilities/harness/__init__.py`: remove the `ClaudeCodeHarness` import (`:20`), its
   `HARNESS_REGISTRY` **literal** entry (`:43-46`), and its `__all__` entry (`:28`).
   `HARNESS_REGISTRY` still holds `claude-code` at runtime, because the plugin re-seats it at
   import; so `publish_to` (`:104-123`) **does** need the one-line skip of
   `plugin_seated_names("harness")` (the same edit Phase 8 made to
   `secrets/backends.py::publish_to`), otherwise it would publish `claude-code` as a built-in row
   and collide with `publish_plugins`'s `system-plugin` row. With the skip, the `claude-code` row is
   published only by `publish_plugins` with the `system-plugin` origin, and the `claude`
   install-command is published (weak while disabled) from the plugin bundle.
6. Behavior now, with no config change: the `claude-code` harness row and the `claude`
   user-install-command row are both present-but-disabled, hidden from default `resource list`;
   `describe harness/claude-code` shows `Disabled: not enabled in [plugins] (plugin claude)`; doctor
   lists `plugin claude: ... disabled`. A `session-template` naming the harness lists ready and
   session create raises `ensure_harness_enabled` with "enable plugin `claude`". An agent-template
   with `user_install_commands = ["claude"]` finalizes cleanly (present-but-disabled row, no
   unknown-name error) and refuses at agent create / session create `--new-agent` with the same hint
   via the recipe gate. With `[plugins] enabled = ["claude"]`: both enabled, listed, consumable,
   origin `system-plugin claude (agentworks.plugins.claude...)`.
7. Tests: route the harness-using tests (6 files) through the config fixture's
   `plugins_enabled=("claude",)`; add the harness breaking-change pin and the install-command
   recipe-gate pins (disabled default, typed error with the hint on agent create AND session
   `--new-agent`, enabled works, operator override of `claude` wins); keep a seating test asserting
   `HARNESS_REGISTRY` no longer carries `claude-code` at core import; keep a builtin-bundle test
   asserting the `claude` install-command is gone from `manifests/builtin/`.
8. Docs lockstep in the same phase: the sample-config claude-code + `user_install_commands` comments
   (`:314-347`, `:397-427`), the resources-guide migration note line for claude, ADR 0021's
   migration record.

## 7. Risks and safeguards

- **A core reference to a moved name survives by accident.** Safeguard: each phase greps the core
  for the moved identifiers (class name and registry key) as part of review; the seating test in
  step 7 pins the registry drop. Known benign residue: `install_claude_plugins` probes the `claude`
  CLI binary on the VM by name (`vms/initializer/driver.py:822-833`), a remote filesystem fact, not
  a registry reference; and the frozen `db/migrations.py` import (handled by the named §5 repoint
  step, not a stray reference).
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
  tolerate this; the fixture's import-order test extends to the first real plugin (onepassword,
  Phase 8).
- **The frozen `db/migrations.py` platform import** (§5) is registry-independent by design, so a
  bundle move that only fixes registry seats would leave it importing a now-moved class. Safeguard:
  the named repoint step in Phases 10/11 and the existing `test_db_migration_vm_sites.py`.
- **Test-fixture drift during the proxmox repoint.** Safeguard: the repoint commit lands before the
  migration commit and must be green on its own, so any behavioral assumption a fixture silently
  made about proxmox surfaces as a lima diff, not as a migration failure.
- **The secret-backend hint is a new failure-path branch** (Phase 8, LLD b). Risk: it fires only on
  the sole-mapping-disabled case, so an over-broad implementation could change unrelated
  secret-unavailable messages. Safeguard: the map is empty when no secret-backend producer is
  disabled (message verbatim today's), and the Phase 8 test pins both the augmented and the
  unchanged message.

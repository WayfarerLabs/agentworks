# Fresh Cut, Part 3: 51 unsurveyed `cli/tests` files

A fresh cut of the sweep's decision inventory over half of the 102 test files under `cli/tests` the
map has never surveyed. The other half is a sibling delegate's. Group 1 (`match=` and
`assertRaisesRegex` sites) is not in scope here: `sweep-screen.py generate` produces those rows and
another delegate owns them, so every `pytest.raises(..., match=)` site in these files was skipped.

**Basis.** Every file was read at `c310d05b` (main), which is the base of the branch this file lands
on. Rows use the anchor grammar [sweep-inventory.md](sweep-inventory.md) defines under "Reading this
file mechanically", and every anchor here is a `path::qualname` span anchor, so nothing in this file
goes stale on a line move. Ids are provisional (`P3-`) and the effort lead renumbers them at
integration.

**Criteria.** The always-on `no-prose-policing-tests` rule as generalized by FRD R1.2, plus
[hla.md](hla.md)'s two doctrines, its source-guard split, and its borderline rubric. An assertion
earns a row when it pins prose or form this repository authors, re-validates interior state, or pins
a structure that should be derived from its canonical source. An assertion the criteria do not reach
gets no row, and a file with no such assertion is accounted for at the end instead.

## Counts

| Figure                      | Count |
| --------------------------- | ----: |
| Files read                  |    51 |
| Files with at least one row |    21 |
| Files with no row           |    30 |
| Rows                        |    31 |

<!-- prettier-ignore -->
| Group | Rows | delete | convert | keep |
| --- | ---: | ---: | ---: | ---: |
| 3 (report lines and hints) | 9 | 0 | 0 | 9 |
| 4 (schema, manifests, capabilities and platforms) | 4 | 0 | 3 | 1 |
| 5 (authored-artifact form policing) | 11 | 3 | 2 | 6 |
| 6 (source guards) | 7 | 4 | 0 | 3 |
| Total | 31 | 7 | 5 | 19 |

Three rows carry a `**[subtracted: <owner>]**` marker, because their file is inside an estate the
2026-08-19 re-scope moved to another effort: P3-001 and P3-014 to
`2026-08-18-secret-preview-contract`, P3-010 to `2026-08-19-instance-model`. They are decided rather
than left as questions, per the re-scope section's own rule that a subtracted row changes owner
rather than being cancelled. Six of the thirty no-row files sit in those estates too and say so.

## Two families that are read but deliberately not rowed

Both appear across many of these files, both would waste an executor's attention as rows, and both
are settled by criteria already in the map.

- **Injected-sentinel absence.** `assert sentinel not in str(error)` where the sentinel is a value
  the test itself injected is not a wording blacklist: it is a leak defense whose whole content is
  that the injected value did not reach an operator-facing surface. This is the injected-marker
  screen's rule applied to non-`match=` assertions. It appears in `secrets/test_line_safety.py`,
  `transports/test_sensitive_stdin.py`, `test_ssh_identity.py`, `vms/test_applied_state.py`,
  `test_instance_specs.py`, `test_secrets_resolve.py` and `test_typer_output_abort.py`, and it keeps
  everywhere.
- **Emitted command text.** A pin on the argv or shell text a transport sends
  (`"chmod a+x" in command`, `forwarded_argv[:4] == [...]`, `"kill-session" not in command`) is
  behavior at a boundary, not prose we display. This is the map's own exclusion reason for
  `transports/test_sensitive_stdin.py`, and it covers `test_initializer_workspaces_dir.py`,
  `test_console_lifecycle.py` and `test_consoles_reorder.py` here.

**Where JSON projection rows sit.** A machine-output projection is the JSON report, so its rows are
group 3 beside the human report lines rather than group 4. Group 4 here holds persisted-state and
platform-map rows.

## Group 3: report lines and hints

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P3-001 | `cli/tests/test_secret_cli.py::test_missing_ssh_keys_do_not_block_secret_list,test_missing_ssh_keys_do_not_block_secret_describe` | **[subtracted: secrets-preview]** resource-name presence in rendered output | keep | `"tailscale-auth-key" in result.output` is the auto-declared secret's own name, not prose about it. It is what separates the soft-load path working from a command that exits 0 with an empty listing, which is the whole subject of both tests; `exit_code == 0` alone would pass on an empty list. Value identity over a registry-computed name. |
| P3-002 | `cli/tests/test_version_command.py::test_resolve_version_falls_back_when_not_installed` | fallback-value equality | keep | `resolve_version() == "unknown"` is the whole test, and the call sits inside it, so this is what proves the fallback path runs at all rather than `PackageNotFoundError` propagating out of `agw version`. The token is a one-word sentinel the command prints in place of a version, not a sentence. Cost, stated plainly: we author the token, so renaming it fails this test with nothing broken; the only sentinel-free replacement is a weaker `isinstance(..., str)`, which is why this keeps rather than converts. |
| P3-003 | `cli/tests/test_graph_cli.py::test_missing_ssh_keys_do_not_block_graph_show` | resource-identity presence in rendered output | keep | Same shape as P3-001. `"secret/npm-token" in result.output` names the manifest the test wrote at `:149`, and it is the only assertion separating a real graph render from a clean exit over nothing. |
| P3-004 | `cli/tests/test_graph_cli.py::test_graph_show_json_projects_the_same_result` | JSON envelope and projection equality | keep | `schema_version`, `command`, and the `query`/`nodes`/`edges` key names are the shipped machine contract, which is a consumer interface rather than authored prose; every value in the expectation is read back off the `GraphResult` the test built. A rename here breaks every machine consumer and nothing else catches it. |
| P3-005 | `cli/tests/test_operational_json_cli.py::test_nonempty_operational_lists_have_exact_ordered_json,test_nonempty_operational_describes_have_exact_safe_json` | byte-exact JSON document equality | keep | The expectations are built from the row objects the test constructed (`{"name": row.name, ...}`), so what is pinned is the projection's field set, nesting and order, plus determinism (invoked twice), an ANSI-free stdout and an empty stderr. Same reasoning as B-015: this is the machine contract plus arity, not layout. The describe test additionally proves the unsafe stored state (`platform_metadata`, raw config) never reaches the document. |
| P3-006 | `cli/tests/resources/test_graph_query.py::test_json_projection_has_only_the_closed_safe_scalar_shape_and_nulls` | JSON projection dict equality | keep | The same contract one layer below the CLI, and the equality is what pins that no field is added, dropped or renamed and that absent facts project as explicit `null` rather than being omitted. All values come from the `GraphResult` the test built. |
| P3-007 | `cli/tests/resources/test_graph_query.py::test_human_projection_emits_every_unique_fact_once_in_result_order` | rendered-line counts, order and nesting over injected sentinels | keep | Every string asserted is a `*_SENTINEL` the test injected, so nothing authored is pinned. What is pinned is structure: each unique fact appears exactly once, nodes precede edges, edge order follows result order, and each edge's detail lines nest at level 3. A renderer that duplicated an edge or emitted a detail at top level is caught here and nowhere else. Cost: `detail_counts == [2, 1, 0]` is the tightest part and moves if the detail lines per edge change. |
| P3-008 | `cli/tests/resources/test_access.py::test_resolve_resource_rejects_unknown_kind_before_name_lookup` | hint enumeration derived from a production registry | keep | The regex assertion at `:115-120` compares what the hint enumerates against `sorted(KIND_REGISTRY)`, so the expectation is computed from the production table rather than restated. Same shape as C-011: the remediation silently degrades to a hint that names the wrong kinds, or misses a newly registered one, and only this catches it. |
| P3-009 | `cli/tests/test_instance_specs.py::test_admin_effective_validation_rejects_invalid_folded_declaration,test_admin_effective_validation_does_not_echo_custom_validator_input,test_admin_effective_validation_preserves_safe_mise_error_category` | field-name substring in a validation message | keep | `"mise_install_before" in str(...)`, `"mise_lockfile" in str(...)` and `"mise_packages" in str(...)` assert that the refusal names the field the operator's spec actually carried, which is their only route back to the offending key. The field names are the test's own input, not authored prose. Same reasoning as B-062/B-133; the paired `marker not in str(...)` halves are the leak defense named in the preamble. |

## Group 4: schema, manifests, capabilities and platforms

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P3-010 | `cli/tests/vms/test_applied_state.py::test_hardware_marker_codec_is_strict_and_empty,test_ssh_payload_codec_round_trips_closed_arms,test_initialization_slice_builder_uses_post_write_private_key_reference` | **[subtracted: instance-model]** persisted payload wire-shape equality | keep | `VersionedPayload(1, {"fingerprint": ..., "private_key_ref": ..., "status": "verified"})` is the shape written into the applied-state store and read back by a later execution, which principle 3 lists as a trust boundary. Same reasoning as A-079: a codec change that ships is a change to operator databases already on disk, and the version-skew refusal beside it (`UnsupportedAppliedStateVersionError` on payload version 2) is what makes a forward payload a clean refusal rather than a misread. |
| P3-011 | `cli/tests/test_instance_specs.py::test_spec_refuses_framework_fields` | hand-listed copy of a production constant | convert | The twelve parametrize values are exactly `OVERLAY_EXCLUDED_FIELDS` (verified by executing: `sorted(OVERLAY_EXCLUDED_FIELDS)` at HEAD is `apiVersion, declared_at, description, expires, framework, inherits, kind, metadata, name, origin, source, spec`). Replacement: parametrize over `sorted(OVERLAY_EXCLUDED_FIELDS)`, which the file already imports at `:16`. A field added to the exclusion set then gains a refusal case with no test edit, and `test_overlay_exclusions_follow_declared_resource_metadata` already guards how that set is composed. No production change. |
| P3-012 | `cli/tests/vms/test_vm_platform_debian_release.py::test_every_code_owned_platform_map_has_the_trixie_selector` | provider image identifiers restated per platform | convert | The five assertions restate the image selectors each code-owned map holds (`"debian-13-generic-amd64.qcow2"`, `"trixie"`, `"13"`, the Azure publisher/offer/sku tuple, the GCP family dict). They are a second copy of production data whose real correctness only a live boot proves, so every legitimate image update becomes a two-file edit. The invariant worth keeping is coverage: a map with no entry for the release Agentworks creates makes `code_owned_release_value` raise at provisioning time. Replacement: assert `CURRENT_DEBIAN_RELEASE in <map>` for each of the five maps, importing it from `agentworks.debian`, and rename the test to say "covers the current release". Verified by executing: all five maps contain `CURRENT_DEBIAN_RELEASE` (trixie) at HEAD, and the stronger `set(map) == set(DebianRelease)` does NOT hold, because none of them carries bookworm. No production change. Cost: a typo'd image identifier is then caught by live use rather than in CI. |
| P3-013 | `cli/tests/vms/test_vm_platform_debian_release.py::test_all_platforms_declare_contract_version_one` | hand-listed platform enumeration | convert | The set of six platform classes is maintained by hand, so a platform added at a different contract version is silently uncovered, which is exactly what a contract-version check exists to prevent (operator ruling 12). Replacement: after `import agentworks.plugins`, assert `{cls.contract_version for cls in descriptor_for("vm-platform").registry().values()} == {1}`. Verified by executing: the registry yields `aws-ec2, azure-vm, gcp-gce, lima, proxmox, wsl2`, all at contract version 1, so this is the same claim quantified over the live registry instead of a list. No production change. |

## Group 5: authored-artifact form policing

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P3-014 | `cli/tests/test_secrets_base.py::test_secret_config_default_chain` | **[subtracted: secrets-preview]** production-constant restatement | delete | `DEFAULT_SOURCE_CHAIN == ("env-var", "prompt")` is a second copy of the constant imported two lines above it, so it can only fail when someone edits the thing it restates, which is the shape the amended principle 3 calls cost rather than coverage. The line above it, `SecretConfig().sources == DEFAULT_SOURCE_CHAIN`, is derivation parity and stays: it is what proves a `SecretConfig` that declares no sources picks up the default chain. |
| P3-015 | `cli/tests/test_sample_config_tailscale.py::test_sample_config_builds_registry` | shipped sample driven through the production loader | keep | The shipped `sample-config.toml` is run through `load_config` and `build_registry` rather than having its text asserted, and what is checked afterwards is registry-computed provenance (`origin.variant == "auto-declared"`, `origin.source == ("vm-template", "default")`). Same shape as C-001. It catches a sample that stops parsing and an auto-declare path that stops firing, neither of which any other test covers for the shipped file. |
| P3-016 | `cli/tests/manifests/test_yaml_value.py::test_a_value_renders_as_the_yaml_a_document_carries,test_a_value_pyyaml_cannot_represent_renders_rather_than_raising,test_a_set_renders_in_a_stable_order` | rendered-YAML text equality | keep | Each rendered string is asserted together with `yaml.safe_load(rendered) == loaded`, so the pair is a round trip through the real loader rather than a pin on our spelling, and the module docstring says why both halves are needed. The one-line flow form (`[zsh, ripgrep]`, `{K: v}`) is load-bearing rather than cosmetic: these values are pasted inline into a generated sample line, so block style would break the document. The stable-order test is determinism, which every sample-pinning test depends on. |
| P3-017 | `cli/tests/resources/test_kind_registry.py::test_phase_1a_kinds_registered,test_secret_kind_attributes,test_admin_template_kind_attributes,test_named_console_template_kind_attributes` | registry attribute restatement | keep | Flagged for the lead rather than decided file-locally. These assert `miss_policy`, `auto_declare_names` and two `AdminConfig` defaults straight off `KIND_REGISTRY`, which reads as a restatement of the table. But the same per-kind attribute shape is a repo-wide convention: at HEAD it also lives in `resources/test_template_kinds.py`, `test_install_resource_kinds.py`, `test_vm_template_kind.py`, `test_harness_integration_kind.py`, `test_secret_backend_kind.py` and others, and `resources/test_admin_template_plurified.py:82` points at it as the single home for those facts. Deleting only this copy leaves the pattern half-retired, which is what principle 8 warns about, so this is a family decision. Keep here until the lead rules on the family. |
| P3-018 | `cli/tests/test_workflow_policy.py::test_only_the_deployment_job_holds_write_permissions,test_the_deployment_job_runs_only_the_deploy_action,test_the_deployment_job_waits_for_the_build_and_uses_the_pages_environment` | confinement checks over an authored workflow file | keep | R1.2 puts workflow files inside the rule's target, so this file is in scope, and it survives on what the assertions protect rather than how they read. Each is a boundary an ordinary edit can regress silently: exactly one job holds a write scope and every job that runs repository code is read-only; the token-holding job runs only `actions/deploy-pages` and nothing of ours beside it; the deploying job names the `github-pages` environment, which is where the GitHub-side branch restrictions and reviewers attach, so a rename drops those protections with no local signal. The deploying and producing jobs are found by their grants and their upload action rather than by name, so this is not a name pin. The file's own docstring already scopes the threat model to accidental regression, which is the model `hla.md` accepts for an in-tree guard. |
| P3-019 | `cli/tests/test_workflow_policy.py::test_the_gate_is_wired_to_every_dependency_result` | quantifier over the workflow's own job set | keep | `set(_needs(gate)) == set(_jobs(CI)) - {"ci-success"}` is derived from the workflow rather than restated, and it catches the real regression: a new CI job that the merge gate does not require, which is invisible until something red merges. `gate["if"] == "always()"` is what stops the gate skipping (and so reporting success) when a dependency fails. The docstring is honest that this is wiring and not a claim about what the gate script does. |
| P3-020 | `cli/tests/test_session_lifecycle_cli.py::test_retired_lifecycle_forms_fail_before_state` | retired-form rejection plus a no-state-opened guard | keep | The retired spellings themselves would not earn this (see P3-021), but the test also monkeypatches `get_db` and `load_config` to fail if called, so it holds that a grammar error opens no database and reads no config. That ordering is a real property a change moving state loading into the group callback would regress, and it is the same invariant `test_graph_cli.py::test_graph_grammar_errors_precede_config` holds for its own command. Cost: the three retired spellings are hand-listed. |
| P3-021 | `cli/tests/test_graph_cli.py::test_retired_resource_spellings_do_not_dispatch` | retired-surface absence | delete | `assert result.exit_code != 0` is the test's whole body for three argv forms. Typer rejects an unknown subcommand (`resource describe`, `resource describe-kind`) and an unknown option (`--write` on the live `resource schema` command) by construction, so this can only fail if someone deliberately adds the surface back, which is a reviewed edit that arrives with its own tests. Unlike P3-020 there is no state guard beside it. Same reasoning as C-018 and F-053. |
| P3-022 | `cli/tests/guide/test_shell_service.py::test_cli_exposes_only_default_list_and_single_topic_show,test_removed_evidence_option_is_rejected_by_the_cli` | retired-option and never-built-form rejection | delete | Delete `test_removed_evidence_option_is_rejected_by_the_cli` entirely (`guide --evidence` is a retired option, rejected by Typer by construction) and, in the first test, the three negative assertions: `old_option.exit_code != 0` (retired `--names-only`), `direct.exit_code != 0` (a bare topic is an unknown subcommand) and `multiple.exit_code != 0` (a surplus positional). Each is refused by the parser rather than by anything we wrote. The positive half stays and is the test's real content: the default, `list` and `show` all exit 0 with output, and `topics` is non-empty. |
| P3-023 | `cli/tests/guide/test_shell_service.py::test_packaged_foundational_topics_are_adjacent_and_render_through_the_shell_service` | authored index-order adjacency pin | convert | `indexed[start : start + len(slugs)] == slugs` pins that four hand-listed shipped topics sit next to each other in the index, which is a fact about authored `index-order:` frontmatter (core-model 15, prerequisites 16, virtual-machines 17, tailscale 18). Inserting a topic between them breaks the test although nothing behavioral broke, and the reading order is a review concern. The half worth keeping is that shipped shells actually render. Replacement: drop the slug tuple and the adjacency assertion, and assert every indexed topic renders: `assert all(render_guide(topic.slug, GuideMode.HUMAN).markdown for topic in catalog.indexed_topics())`. Verified by executing: 12 indexed topics at HEAD, all rendering non-empty. That is strictly more coverage than the four. No production change. |
| P3-024 | `cli/tests/guide/test_shell_render.py::test_core_model_uses_root_mapping_for_real_images_and_fragment` | authored-content count and link pin | convert | `rendered.count(".../main/docs/images/") == 2` and the exact `command-reference.md#named-consoles` link pin how many images the shipped core-model shell holds and which link it carries, so an author adding an image or retargeting a link fails a test about URL rewriting. The invariant is the rewrite itself, on real content, where the repository-root mapping is exercised. Replacement: keep one real-content assertion that at least one repository-root raw URL appears, and replace the link pin with idempotence, `rewrite_relative_destinations(rendered, topic.source) == rendered`, which says no relative destination survived the rewrite. Verified by executing at HEAD: the root-mapped count is 2 and the second pass is a no-op. No production change. |

## Group 6: source guards

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P3-025 | `cli/tests/resources/test_origin.py::test_external_plugin_still_not_constructible` | attribute-absence pin plus an annotation-substring scan | delete | `not hasattr(Origin, "external_plugin")` and `"external-plugin" not in variant_field.type` read the class and its raw annotation string and assert an identifier is absent. That is the doctrine's delete side, "pins how our code is written": adding the factory under any other name sails through, and the in-tree threat model is accidental regression, not deliberate reintroduction, which is what adding a variant to a `Literal` and a factory beside it would be. `"system-plugin" in variant_field.type` is the positive half of the same substring scan and goes with it; the four factory tests above already prove `system_plugin` exists and stamps its variant. Operator ruling 1 keeps checks that defend a real dynamic boundary, and this defends none: nothing loads external plugins yet. |
| P3-026 | `cli/tests/orchestration/test_node_protocol.py::test_real_capability_instances_are_readiness_not_nodes` | attribute-absence pin beside a protocol check | delete | Drop `not hasattr(instance, "key")` and `not hasattr(instance, "deps")`. The claim the test is named for, that a shipped capability instance is not walkable, is carried by `not isinstance(instance, Node)` on the line above, which fails the moment such an instance becomes structurally a node (`Node` is `runtime_checkable` and its members are `key`, `deps` and `secret_refs`). The two `hasattr` lines pin two specific attribute names on our own classes, so they also fail on a rename, or on a capability gaining an unrelated `key` attribute that does not make it a node. |
| P3-027 | `cli/tests/resources/test_access.py::test_resource_identity_is_frozen_and_slotted,test_resolve_resource_returns_exact_identity_row_and_origin` | slots-presence pin | delete | `assert not hasattr(identity, "__dict__")` and `assert not hasattr(resolved, "__dict__")` can only fail when someone removes `slots=True` from the dataclass they restate, and nothing in the accessor layer behaves differently without it: attribute injection is already refused by the frozen dataclass, which each test proves on the next line with `pytest.raises(FrozenInstanceError)`. Those `FrozenInstanceError` assertions stay. |
| P3-028 | `cli/tests/test_vm_create_tailscale_eager_resolve.py::test_join_tailscale_signature_requires_auth_key_kwarg` | signature-shape pin | delete | The test reads `inspect.signature(_join_tailscale)` and asserts `auth_key` is keyword-only with no default. Both call sites are first-party and inside the mypy run (`vms/initializer/credentials.py:67` and the `vms/initializer` re-export), so strict typing already refuses a caller that omits it, and `vms/test_tailscale_stdin_join.py` drives the real function with a key and asserts the key never reaches the command line. Unlike A-080, which survives because it is the sole enforcement of a prose-only invariant, the invariant here is expressed in the signature the type checker reads. Flagged as borderline: the one regression this and only this would catch is someone giving `auth_key` a `None` default, after which an omitting caller would join the tailnet with no key; that failure is loud (the join fails) rather than silent. |
| P3-029 | `cli/tests/test_typer_output_abort.py::test_typer_and_click_abort_are_distinct_classes` | upstream class-taxonomy premise | keep | `typer.Abort is not click.exceptions.Abort` plus the two `issubclass` refusals pin a third-party fact, which is the one exception `no-prose-policing-tests` allows: it can change under us on a version bump, and when it does the widened `except` tuple in `TyperHandler` becomes half dead while every conversion test still passes. Narrow, and about the library rather than about us. |
| P3-030 | `cli/tests/guide/test_shell_service.py::test_static_index_list_and_selected_render_do_not_load_operator_state_modules` | import-boundary confinement guard | keep | `hla.md`'s standing keep example, in its current home: the deleted `guide/test_power_import_boundary.py` (row L-102, `[dead]`) is gone and this is the guard that survived it. Rendering the index, listing topics and rendering a topic must not import `agentworks.config`, `agentworks.db`, `agentworks.resources` or `agentworks.secrets`, checked in a clean subprocess before and after the calls. It is a layering boundary the type system cannot express, an ordinary import regresses it, and nothing else catches that. |
| P3-031 | `cli/tests/resources/test_graph_query.py::test_human_projection_sanitizes_controls_from_every_dynamic_fact,test_existing_envelope_encodes_graph_data_before_write_and_escapes_controls` | control-character defense at the two output boundaries | keep | The first asserts no rendered line carries a character in the `Cc`, `Cf`, `Cs`, `Zl` or `Zp` categories while ordinary unicode survives, over kind and name values a hostile document could set; the second asserts the JSON envelope escapes `\x7f` and `\x80` rather than emitting them. These are terminal-injection and encoding defenses over untrusted input, the same family as E-179, and the absence halves are defenses rather than wording blacklists. |

## Files with no row, and why

Thirty of the fifty-one. Each was read in full at `c310d05b`; the reason says what the file's
assertions actually are.

<!-- prettier-ignore -->
| File | Why no row |
| --- | --- |
| `cli/tests/db/test_read_transaction.py` | Subtracted to instance-model. Snapshot isolation, nesting refusal and post-close behavior; the only strings are `entity_kind == "database"`. |
| `cli/tests/guide/test_shell_catalog.py` | Markdown fixtures the test authors as input, compared against discovered structure; every refusal is `GuideContentError`. |
| `cli/tests/manifests/test_editor_association.py` | The modeline expectations are built from `MODELINE_PREFIX`, `SCHEMA_DIRNAME` and `ENVELOPE_SCHEMA_FILENAME`, and the end-to-end test validates each written sample against the schema its own modeline names. Derivation parity, not a form pin. |
| `cli/tests/manifests/test_inherited_capability_config.py` | Schema-versus-loader divergence driven by a fixture plugin; the one quantified assertion (`required == ["name"]` per registered arm) is computed over the live capability registry. |
| `cli/tests/plugins/test_azure_logging.py` | Logger level and `isEnabledFor` behavior; no strings asserted. |
| `cli/tests/resources/test_declared_resource.py` | Field defaults, required-ness, and which fields reach the emitted schema for a model the test declares. |
| `cli/tests/resources/test_inheritance_merge.py` | Every value compared is one the test's own `declared` fixture supplied, and the suite carries its own non-vacuity guard. |
| `cli/tests/resources/test_singleton_publishing.py` | Registry structure and provenance (`variant`, `ALWAYS_MATERIALIZE_SOURCE`, one row per singleton kind) over manifests the test wrote. |
| `cli/tests/schema/test_extract.py` | Fixture models in, extracted references out. The one verbatim usage pin (`== "the Proxmox API token"`) reads back the string `_fixture_models.py:72` declares. |
| `cli/tests/schema/test_extract_completeness.py` | An oracle built from the validated object is compared against the walker; both sides are the test's own fixtures. |
| `cli/tests/schema/test_extract_totality.py` | Totality over an adversarial corpus and a seeded generator, with per-model non-vacuity; the hand-written `_EDGELESS_BY_DESIGN` map is asserted in both directions, so it cannot go stale silently. |
| `cli/tests/schema/test_fill.py` | Fill behavior over models the test declares; every rendered value comes from a `default_template` the test wrote. |
| `cli/tests/schema/test_owner_templates.py` | Emitted-schema structure for fixture models (required, nullable, marker placement), plus the validation-and-extraction agreement. |
| `cli/tests/secrets/test_line_safety.py` | Subtracted to secrets-preview. A structured refusal plus the injected-sentinel leak defense named in the preamble. |
| `cli/tests/secrets/test_result_precedence.py` | Subtracted to secrets-preview. Precedence matrices over production enum members; every expected reason is `SomeEnum.MEMBER` or `.value`. |
| `cli/tests/test_cli_helpers.py` | `parse_csv_filter` behavior over the test's own input. |
| `cli/tests/test_config_line_capture.py` | `declared_at` file and line capture over manifests the test wrote. |
| `cli/tests/test_console_lifecycle.py` | Tmux model state plus emitted command text, per the preamble. |
| `cli/tests/test_consoles_reorder.py` | Database order, forwarded kwargs and exit codes. |
| `cli/tests/test_debug_signal.py` | `debug_enabled()` and the `AGW_DEBUG` mirror; the only literal is the env var's own `"1"`. |
| `cli/tests/test_git_config.py` | Assertions read `git config --get-all` output over values the test wrote. |
| `cli/tests/test_initializer_workspaces_dir.py` | Emitted command text and the order the transport received it in, which is the observational form `hla.md` prefers. |
| `cli/tests/test_secrets_resolve.py` | Subtracted to secrets-preview. A backend-boundary conformance and defense suite: enum reasons, call lists, exception identity, and injected-sentinel non-leakage. The backend is third-party code, so none of its validation is interior. |
| `cli/tests/test_session_console_filter.py` | Filter results, forwarded kwargs, exit codes and `entity_kind`/`entity_name` over seeded rows. |
| `cli/tests/test_session_create_ephemeral_secret_target_parity.py` | One equality between two builders' outputs (`pre == post`) and the same over `compute_needed_secrets`. |
| `cli/tests/test_ssh_identity.py` | Parsing at an operator-file boundary: `error.kind` classifications, a fingerprint checked against real `ssh-keygen`, a detail-length threshold, and the leak defense from the preamble. |
| `cli/tests/test_status_observation.py` | Future cancellation and the shutdown call shape. |
| `cli/tests/test_tmux_model_conformance.py` | A differential oracle: the same operations against the hand model and a real tmux server, compared on structure with ids stripped. |
| `cli/tests/transports/test_sensitive_stdin.py` | Emitted argv plus the leak defense, both per the preamble. This matches the map's existing exclusion for the file. |
| `cli/tests/vms/test_live_vm_boundary.py` | Resolve counts, call order and values the test put in the environment. |

## Open questions for the lead

1. **Subtracted-estate rows.** Six of these files sit in the two re-scoped estates and three rows
   carry the marker. I decided them rather than skipping them, on the re-scope section's rule that a
   subtracted row changes owner rather than being cancelled. If the fresh cut is meant to leave
   those estates untouched, P3-001, P3-010 and P3-014 come out and their files join the no-row
   accounting.
2. **P3-017, the per-kind attribute family.** The row keeps, but the shape spans at least six files
   at HEAD and only one of them is mine. It wants a family ruling, and the other files are in the
   unsurveyed half or already outside this cut.
3. **P3-028** is the row I would most want a second reading of. I read the signature pin as
   type-checker-expressible and deleted it; the counter-reading is A-080's, that a signature pin is
   the sole enforcement of an invariant no behavioral test carries.
4. **Group placement for machine-output rows.** I put JSON projection rows in group 3 with the other
   report projections. If the lead would rather they batch with group 4's structured assertions,
   P3-004, P3-005 and P3-006 move as a set.

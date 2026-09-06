# Fresh cut, part 4: 51 previously unsurveyed `cli/tests` files

A fresh cut over half of the 102 test files under `cli/tests` that
[sweep-inventory.md](sweep-inventory.md) has never rowed. The other half belongs to a second
delegate. The ids here are provisional (`P4-nnn`); the effort lead renumbers them when this merges
into the map, and must, because `P4-` is not one of the prefixes the map's row grammar accepts (open
question 5).

**Basis: `c310d05b`** (main, the branch base). Every file was read at that tree, every claim about
production code was read there, and the claims marked as executed were run there. The map's row
grammar governs column 2, and every anchor here is a `path::qualname` span anchor: no row keeps
literal lines, so no row carries a `[line-anchored: ...]` marker.

The criteria are the always-on `no-prose-policing-tests` rule as generalized by R1.2, plus
[hla.md](hla.md)'s two doctrines, its source-guard split, and its borderline rubric. Group 1's
`match=` estate is not this document's: two `match=` sites exist in these files
(`test_vm_release.py` and `test_consoles_add_placement.py`) and both are left to `generate` and the
group-1 owner.

**Platform criterion applied.** The suite runs on `windows-latest` in CI at this basis, so a test
guarding a platform-conditional path is CI coverage rather than inert generality. That is why
`test_console_streams.py`, `test_subprocess_io.py`, `test_path_rendering.py` and the
`requires_posix_shell` tests in `test_command_checks.py` and `transports/test_remote_lima.py` carry
no row.

## Counts

| Measure                     | Count |
| --------------------------- | ----: |
| Files read                  |    51 |
| Files with at least one row |     9 |
| Files with no row           |    42 |
| Rows written                |    15 |

| Group                               | delete | convert |  keep |  Total |
| ----------------------------------- | -----: | ------: | ----: | -----: |
| 3 (report lines and hints)          |      1 |       0 |     5 |      6 |
| 4 (schema, manifests, capabilities) |      2 |       2 |     1 |      5 |
| 5 (authored-artifact form policing) |      0 |       2 |     0 |      2 |
| 6 (source guards)                   |      0 |       1 |     1 |      2 |
| **Total**                           |  **3** |   **5** | **7** | **15** |

Three rows (P4-006, P4-014, P4-015) address `cli/tests/db/test_instance_state.py`, which the map's
estate rule places in the `2026-08-19-instance-model` subtracted estate, so each carries
`[subtracted: instance-model]` and none is in the executable set. The other twelve are live.

## Group 3: report lines and hints

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P4-001 | `cli/tests/schema/test_markers.py::test_owner_display_reproduces_the_shipped_owner_string` | message-framing convention pin | delete | The whole test is `RefOwner(kind="git-credential", name="prod").display == "git-credential/prod"`. `display` exists only to frame operator-facing error text: its docstring says so (`schema/markers.py:78-81`) and its consumers are all message interpolations (`manifests/decode.py:227,248,293,302,367` and `schema/errors.py:477,481,491`). Nothing parses it back, so the assertion's only failure mode is a decision to spell the owner differently. Delete the test; the `label` override branch it never reaches loses nothing. |
| P4-002 | `cli/tests/resources/test_instances.py::test_list_view_renders_dash_for_kind_without_used_by_contract` | rendered placeholder cell plus column-header navigation | keep | Invariant: a kind with no used-by contract renders a dash, not `0`. Those are different operator-facing claims, "this kind has no such relationship" against "it has none right now", and the renderer is the only place the difference exists. The header strings `USED BY` and `DESCRIPTION` locate the column rather than being asserted; the assertion is on the cell. Cost: renaming either header breaks the locator. |
| P4-003 | `cli/tests/sessions/test_console_status.py::test_console_listing_status_column_follows_explicit_render_request` | rendered column-header presence and absence, plus column alignment | keep | Invariant: `render_console_listing` emits the status column only when the caller asks for it, and the value sits under its own header. `include_status` changes nothing but the rendering, so the rendered header is the only observable, and a regression that always emits the column makes every plain `console list` pay for status it never observed. The alignment half is structural. |
| P4-004 | `cli/tests/vms/test_status_observation.py::test_vm_listing_status_column_follows_explicit_render_request` | rendered column-header presence and absence, plus column alignment | keep | The VM twin of P4-003, same invariant over `inspect.render_vm_listing`, same reasoning. Keep both: they guard two independent renderers and neither covers the other. |
| P4-005 | `cli/tests/vms/test_status_observation.py::test_vm_listing_only_caps_name_column` | truncation-marker pin plus uncapped-column assertions | keep | Invariant: the name column is the only one truncated, and it truncates rather than silently dropping characters. The width comes from production (`inspect._NAME_CELL_WIDTH`), so only the three-dot marker is authored, and the other three assertions are the real content: a long site, template or tailscale host survives whole. A regression that caps every column hides operator data with no error. |
| P4-006 | `cli/tests/db/test_instance_state.py::test_malformed_persisted_record_raises_instead_of_becoming_absent` | **[subtracted: instance-model]** owner-name substring in a raised message | keep | Invariant: a malformed persisted record names the record it failed on. `entity_name` does not carry this to the operator: `AgentworksError` never interpolates `entity_kind` or `entity_name` into its message (`errors.py:36-44`) and the CLI renders `str(e)` and the hint alone (`cli/_entry.py:119-127`), so the substring is the only probe that the operator can tell which record failed. The sibling `secret not in str(...)` is the redaction half and stays either way. |

## Group 4: schema, manifests, capabilities and platforms

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P4-007 | `cli/tests/resources/test_resolved_spec.py::test_projection_is_complete_ordered_and_maps_layer_roles` | hardcoded copy of a resolved model's field list | convert | The ten-name literal restates `ResolvedVMTemplate`'s dataclass field order minus `name`, so adding a legitimate field to the model breaks it. The invariant worth keeping is that the projection covers every resolved field in declaration order. Replacement, no production change: build the expected sequence in the test as `[f.name for f in dataclasses.fields(ResolvedVMTemplate) if f.name != "name"]`. Verified by execution at this basis: that sequence equals the current literal exactly. **Do not derive it through `resolved_spec_default_paths` or `_resolved_field_names`**, because `project_resolved_spec` builds the spec from that same helper (`resources/resolved_spec.py:94-95`), so a version derived through it asserts the function against itself and can never fail. |
| P4-008 | `cli/tests/schema/test_markers.py::test_a_template_the_extractor_could_not_render_is_refused_at_construction` | authored refusal-message fragments discriminating same-type branches | delete | Seven parametrized cases each pair a bad template with a fragment of the message it should produce, then assert `expected in str(exc.value)`. **Callee-side raise screen, run by hand at this basis:** `SecretRef(...)` reaches `StateError` from five distinct paths, all in `_check_owner_template` (`schema/markers.py:185,191,196,200,205`, entered from `__post_init__` at `:116-118`), so case 1 does not apply. **No structural handle:** none of the five passes `entity_kind` or `entity_name`, so they are indistinguishable except by our own wording, and R2.2 forbids adding a discriminator for a test. **Injected-marker screen:** the needles are fragments of those production templates rather than markers the test wrote, so the screen is silent and the delete stands. Case 2's fallback applies: drop the `expected` parameter and the assertion, keep all seven templates under `pytest.raises(StateError)`. Cost, stated per the screen: a template refused by the wrong rule will now satisfy the test. |
| P4-009 | `cli/tests/test_machine_output.py::test_output_formats_are_closed_to_human_and_json,test_machine_output_commands_are_the_complete_v1_contract` | production enum restatements | delete | Two tests whose content is a literal copy of an enum (`OutputFormat`'s two members, `MachineOutputCommand`'s seventeen values and their order) plus a constructor call that raises because a `StrEnum` is closed. They can only fail when someone edits the enum they restate. **Verified by reading at this basis rather than assumed:** the shipped command values are asserted where they actually ship, at `test_machine_output_cli.py:67,304,620`, `test_operational_json_boundaries.py:133`, `test_graph_cli.py:121` and `test_resource_show.py:251`, and `--output yaml` is refused through the real CLI at `test_machine_output_cli.py:83,890` with a non-zero exit and empty stdout. Four values (`resource.kinds`, `secret.list`, `secret.describe`, `doctor`) have no such site, but the enum list gives them none either: it restates the enum rather than proving what any command emits. Delete both tests. |
| P4-010 | `cli/tests/test_machine_output.py::test_envelope_is_utf8_deterministic_and_has_one_trailing_newline` | byte-verbatim pin of an encoded envelope | convert | The `first == b'{"schema_version":1,...}'` equality is the only assertion in the file that constrains separator spelling, which no JSON consumer branches on. It does also prove the data is carried through unchanged, which nothing else in the test states. Replacement, no production change: `assert json.loads(first) == {"schema_version": 1, "command": "resource.list", "data": data}`. The neighbors carry everything else and stay: determinism (`first == second`), the single trailing newline, no byte-order mark, the snowman emitted as UTF-8 rather than escaped, and the envelope key order. |
| P4-015 | `cli/tests/db/test_instance_state.py::test_desired_overlay_round_trip_upsert_clear_and_canonical_storage` | **[subtracted: instance-model]** stored-JSON byte form pin | keep | Invariant: persisted payloads are stored canonically. `_encode_payload` sets `sort_keys=True`, `separators=(",", ":")` and `ensure_ascii=False` deliberately (`db/instance_state.py:206-214`), and this is the only assertion anywhere that reads the stored text rather than the decoded value, so deleting it leaves the storage form unstated. **Gap, recorded rather than fixed here:** the asserted payload is the single-key `{"value":"new"}`, which exercises the separators and neither the key sort nor the non-ASCII handling. Closing it costs one line, asserting the raw text of a multi-key non-ASCII payload instead, and the owning effort may take it or leave it. |

## Group 5: authored-artifact form policing

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P4-011 | `cli/tests/guide/test_shell_package.py::test_wheel_and_source_distribution_vendor_the_same_canonical_guide_sources` | hand-listed packaged-file inclusion checks | convert | Seven `in packaged` assertions name individual guide markdown files by path. The real invariant is that every authored guide markdown ships in the wheel, and the hand list does not carry it: the tree holds 18 such files at this basis over three `guide-content` directories, and the whole of `agentworks/secrets/guide-content/` is unnamed, so a packaging rule that dropped it passes today. Replacement, no production change: derive the expected names from the source tree and require each to be packaged, as `sorted(p.relative_to(project).as_posix() for p in (project / "agentworks").rglob("guide-content/*.md"))`. Verified by execution at this basis: that expression yields the 18 wheel-relative names in exactly the spelling `packaged` holds, including the seven the test names today. Keep the `.markdownlint.jsonc` exclusion assertion as it stands. |
| P4-012 | `cli/tests/guide/test_shell_package.py::test_wheel_and_source_distribution_vendor_the_same_canonical_guide_sources` | count pin on an authored document's image links | convert | Inside the installed-wheel probe, `core.markdown.count("raw.githubusercontent.com") == 2` restates how many relative images `README.md` currently carries, so adding one to the README fails the packaging suite with no bug. **Verified duplicate:** `guide/test_shell_render.py:468` asserts the same count of two against the same topic in the source tree, more cheaply. What this probe uniquely proves is that an installed render with no source tree still rewrites relative image destinations to absolute raw URLs (`guide/render.py:200`). Replacement, no production change: assert `"raw.githubusercontent.com" in core.markdown` and drop the count. |

## Group 6: source guards

<!-- prettier-ignore -->
| id | file and anchors | shape | disposition | justification |
| --- | --- | --- | --- | --- |
| P4-013 | `cli/tests/transports/test_abc.py::test_abc_surface_is_complete` | abstract-method set pinned against a module constant | keep | Reads the live class with `inspect.getmembers` rather than the source text, and asserts the abstract set equals `REQUIRED_METHODS`. It is the only probe that an `@abstractmethod` decorator has not silently been dropped: `test_concrete_transports_implement_abc` and `test_incomplete_subclass_cannot_be_instantiated` both still pass when a method stops being abstract, and the consequence is a subclass that constructs cleanly and raises `NotImplementedError` at attach time. The file's own comment records that this boundary already moved twice, when `copy_dir_to`, `write_file` and `interactive` became concrete defaults. Cost, stated: adding a genuine abstract method means editing `REQUIRED_METHODS` in the same change. |
| P4-014 | `cli/tests/db/test_instance_state.py::test_owner_inspection_keeps_recognized_and_future_records_closed` | **[subtracted: instance-model]** forbidden-substring list over executed SQL | convert | `assert not any("EXISTS(" in statement.upper() for statement in statements)` guards a real property: `inspect_owner_state` must take the known-owner branch, which selects the literal `1` instead of the four-way `EXISTS` case (`db/instance_state.py:621-629`), rather than re-probing an owner the caller already established in its snapshot. The blacklist is a weak way to say it, because `EXISTS (` with a space, or a join-shaped re-probe, both pass. An observational twin exists and is cheap. Replacement, no production change: delete the owner row (`DELETE FROM vms WHERE name = 'alpha'`) after seeding and before inspecting, then assert `inspect_owner_state` still reports `owner_exists is True`. **Verified by execution at this basis:** with the owner row gone, `inspect_owner_state` reports `True` (the known-owner branch) while `inspect_all_instance_state` reports `False` for the same records (the `EXISTS` branch), so the twin separates the two and no rewording of the SQL can bypass it. The rest of the test is unaffected. |

## Files with no row, and why

Forty-two of the 51 carry no assertion the criteria reach. Each is listed with the reason, so a
second reader can check the call rather than take it. The nine files that do carry rows are not
listed here; where a rowed file's other assertions are out of scope, its rows say so.

Six of these files (`agents/test_install_commands.py`, `plugins/test_grok.py`,
`test_initializer_apt.py`, `test_ssh_set_env.py`, `transports/test_remote_lima.py`,
`workspaces/test_acls.py`) already appear in the map's own "Files with no row" table, and two
(`guide/test_release_history.py`, `guide/test_shell_commands.py`) were cleared by the 2026-08-19
completeness re-scan. All eight were read again at this basis and all eight still hold. The third
file that re-scan cleared, `guide/test_shell_package.py`, does not, and carries P4-011 and P4-012.

| File                                                          | Why no row                                                                                                                                         |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli/tests/agents/test_install_commands.py`                   | Emitted shell-command text at a transport boundary, with home expansion as the substance.                                                          |
| `cli/tests/capabilities/test_secret_backend_client.py`        | Boundary validation at the secret-backend contract plus a redaction defense. In the secrets-preview estate, and hands over nothing.                |
| `cli/tests/manifests/test_decode_fill.py`                     | The one string needle is the value the test's own fixture template renders, so it pins nothing authored.                                           |
| `cli/tests/plugins/gcp/test_names.py`                         | Golden vectors for a collision-safe cloud naming formula. The names address real provider resources, so they are a contract, not a spelling.       |
| `cli/tests/plugins/test_cloud_bootstrap_secret_boundaries.py` | Secret-redaction and single-ephemeral-join defenses; the cloud-init needle is a blank key slot, which is structure.                                |
| `cli/tests/plugins/test_grok.py`                              | Registry origin, enablement, and the values the bundled plugin manifest decodes into. A decode regression is what the value pins catch.            |
| `cli/tests/plugins/test_provider_config_strings.py`           | Blank rejection at the operator-manifest boundary, plus model-to-JSON-schema parity. R2.1 keeps both.                                              |
| `cli/tests/resources/test_reference.py`                       | Frozen-dataclass and subclass invariants; no assertion touches authored form.                                                                      |
| `cli/tests/resources/test_live_publication.py`                | Registry finalization, live graph edges, and snapshot semantics. The two CLI needles are machine-readable names-only lines, which is the contract. |
| `cli/tests/resources/test_schema_directed_domain_merge.py`    | Merge semantics and provenance over test-authored inputs.                                                                                          |
| `cli/tests/schema/test_merge.py`                              | Merge behavior and contract-violation detection asserted as `merge_contract_error(...) is not None`, which pins no message.                        |
| `cli/tests/secrets/test_batch_completion.py`                  | Batch dooming and typed block reasons. In the secrets-preview estate, and hands over nothing.                                                      |
| `cli/tests/secrets/test_outcomes.py`                          | The rendered needle is the outcome's own status value, so it is derivation rather than a pin. Secrets-preview estate, nothing to hand over.        |
| `cli/tests/transports/test_remote_lima.py`                    | Emitted two-hop command text and host-intermediate cleanup at a transport boundary.                                                                |
| `cli/tests/vms/test_applied_state_checkpoint.py`              | Applied-state atomicity, rollback, and persisted event sequences.                                                                                  |
| `cli/tests/vms/test_describe_vm.py`                           | Call avoidance and warning counts. No assertion reads warning text.                                                                                |
| `cli/tests/workspaces/test_acls.py`                           | Emitted `setfacl` specification text, which is what the three call sites actually apply.                                                           |
| `cli/tests/workspaces/test_backend_git_identity.py`           | Emitted `git config` text, including the repo-local rather than global scope.                                                                      |
| `cli/tests/guide/test_release_history.py`                     | Topic-identifier mapping, unsafe-payload refusal, and size bounds. The topic strings are a route identifier, not prose.                            |
| `cli/tests/guide/test_shell_commands.py`                      | Derivation parity between authored guide markdown and the live CLI spec. It asserts that documented commands exist, never how they are worded.     |
| `cli/tests/test_apt_declared_at.py`                           | Source-location provenance for manifest-loaded entries.                                                                                            |
| `cli/tests/test_azure_collision.py`                           | Fail-closed probe behavior at the provider SDK boundary, with the cause chain as the assertion.                                                    |
| `cli/tests/test_command_checks.py`                            | Login-shell resolution and a redaction defense. Platform-conditional through `requires_posix_shell`, which is CI coverage.                         |
| `cli/tests/test_config_section_line_scanner.py`               | Parser behavior over inputs the test authors.                                                                                                      |
| `cli/tests/test_console_streams.py`                           | Legacy-console encoding and newline behavior, byte-exact. Platform-conditional and CI coverage.                                                    |
| `cli/tests/test_consoles_add_placement.py`                    | Placement, rollback, and typed conflicts. Its one `match=` site belongs to group 1.                                                                |
| `cli/tests/test_db_migration_harness_state.py`                | Historical migration v29 column and backfill facts. Instance-model estate, nothing to hand over.                                                   |
| `cli/tests/test_debian.py`                                    | Release parsing and registry-order classification, discriminated on `entity_kind`.                                                                 |
| `cli/tests/test_env_identity.py`                              | The `AGENTWORKS_*` variable names are the shipped on-VM contract, asserted as closed sets.                                                         |
| `cli/tests/test_git_credential_contexts.py`                   | Secret scoping, per-declaration context identity, and out-of-scope refusal.                                                                        |
| `cli/tests/test_git_credentials_subgraph_walk.py`             | Transitive requirement walk and auto-declaration provenance. The secret names are what the owner template renders, so they pin nothing authored.   |
| `cli/tests/test_initializer_apt.py`                           | Emitted apt and install-command text, plus resolve-before-mutate ordering asserted by call absence.                                                |
| `cli/tests/test_instance_descriptions.py`                     | Status and reason vocabularies, issue codes, and terminal-safety defenses asserted by Unicode category rather than by wording.                     |
| `cli/tests/test_instance_spec_cli.py`                         | Which lifecycle commands accept `--spec`, read off the real Typer command tree. That is the shipped CLI surface, not a second copy of it.          |
| `cli/tests/test_list_names_only.py`                           | Machine-readable one-name-per-line output that the three completion scripts parse; the names are the test's own seeds.                             |
| `cli/tests/test_operational_json_persisted_enums.py`          | Persisted-enum to JSON v1 parity, the `unknown` sentinel, and proof that raw persisted text never reaches any surface.                             |
| `cli/tests/test_path_rendering.py`                            | The three branches of the host-path spelling rule, with the expectation built the way the function builds it. Platform-conditional.                |
| `cli/tests/test_runnable_list_safety.py`                      | Consent confinement: which seams a plain and a status-enriched list may reach, and that neither writes.                                            |
| `cli/tests/test_ssh_set_env.py`                               | `SetEnv` argument construction and its quoting, which is an injection defense at a shell boundary.                                                 |
| `cli/tests/test_subprocess_io.py`                             | Byte-exact stdin delivery and a secret-redaction defense. Platform-conditional and CI coverage.                                                    |
| `cli/tests/test_value_provenance.py`                          | Layer-fold provenance behavior and a single re-export identity assertion.                                                                          |
| `cli/tests/test_vm_release.py`                                | Release observation, drift refusal, and rollback. Its one `match=` site belongs to group 1.                                                        |

## Open questions for the effort lead

1. **Subtracted-estate rows.** P4-006, P4-014 and P4-015 target
   `cli/tests/db/test_instance_state.py`, which the map's estate rule assigns to
   `2026-08-19-instance-model`. They are written and marked `[subtracted: instance-model]`,
   following the map's convention that a subtracted row keeps its disposition so the owning effort
   inherits a decided row. Drop the marker, drop the rows, or hand them over as they stand: the
   lead's call. Four further files in this half fall in a subtracted estate and produced nothing to
   hand over; they are named in the accounting table above.
2. **Group 4 against group 3 for schema-file rows.** P4-001 sits in group 3 because it pins message
   framing, while P4-008 sits in group 4 because it is a structured refusal, even though both are in
   `cli/tests/schema/test_markers.py`. That splits one file across two PRs. If the lead would rather
   keep a file whole, move P4-001 into group 4. The same choice arises for
   `cli/tests/test_machine_output.py` (group 4) and `cli/tests/db/test_instance_state.py` (groups 3,
   4 and 6).
3. **`guide/test_shell_render.py:468` carries no row and is reachable.** It asserts
   `rendered.count("https://raw.githubusercontent.com/.../docs/images/") == 2`, the same
   authored-README image count P4-012 converts. That file is not in this half's list and the
   2026-08-19 re-scan recorded it as carrying no in-scope site. Someone should row it.
4. **A stale comment, not a row.** `cli/tests/test_subprocess_io.py:5-7` says "CI runs Linux, where
   that rewriting does not happen, so the round-trip test below cannot fail there". CI runs
   `windows-latest` at this basis, so the sentence is wrong about the suite it describes. It is a
   comment rather than an assertion, so it earns no row; whoever next executes in that file should
   fix it in passing.
5. **These ids do not parse, by construction.** `sweep_screen/inventory.py`'s `ROW_ID` accepts only
   the `A` to `F`, `L`, `RB` and `G1` prefixes, so a `P4-nnn` row pasted into the map is not read as
   a row at all: `resolve`, `attribute` and `totals` skip it silently rather than failing. Renumber
   into an accepted prefix at integration. The anchors themselves are verified: every one was
   resolved by appending these rows to a scratch copy of the map under `RB-9nn` ids, and all 16
   anchors over the 15 rows came back `resolved` at this basis. That scratch copy is not committed.

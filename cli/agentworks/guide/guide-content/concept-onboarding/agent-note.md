Offer a path that starts by discovering the operator's choices, then use current CLI help and
command-owned facts to walk the selected configuration:

- Ask which VM platforms the operator wants, show installed `vm-platform` choices, then help create
  the matching `vm-site` resources.
- Establish whether placement is local or remote before selecting the site and SSH requirements.
- Help select an existing SSH identity by presence, or offer non-overwriting key generation at an
  operator-chosen path.
- Discover the required secret backends and configured sources, then help declare only the named
  secret references the selected providers need.
- Compare available VM templates with the operator's compute and operating-system goal before
  creating a VM.
- If system packages are needed, help choose operator-owned resources or the optional `apt` catalog
  before enabling that plugin.
- If user installation steps are needed, prefer built-in package fields, then consider the optional
  `install-command` catalog or an operator-owned resource.
- Ask which Git hosts and credential sources are needed before configuring `git-credential`
  resources.
- Help choose workspace templates and repository paths before creating workspaces.
- Ask which coding harnesses are desired, then match harness integrations and agent templates to the
  operator's approval and sandbox preferences.
- Help choose session templates and explicit VM, workspace, and Agentworks-managed agent inputs
  before creating the first session.
- For an existing installation, compare current inventory and doctor results with the operator's
  goal, then offer the smallest missing configuration or verification step.

Use `agw resource list --include-disabled --output json` to discover choices, `agw resource explain`
and `agw resource sample` for current configuration shapes, and the applicable command's `--help`
for exact creation syntax.

---
description: Install per-user npm CLI tools independently of mise.
---

# npm CLI tools

Admin and agent templates can select npm CLI packages. Native npm installs them under the target
user's home, and the managed shell profile makes their commands available in sessions. npm and mise
finish before custom user install commands.

Start with `agw resource explain admin-template` or `agw resource explain agent-template` for npm
settings, and `agw resource sample <kind>` for a starter manifest. Runtime provisioning is separate:
a supported Node.js/npm installation must exist before npm package setup. Mise is optional;
`agw resource explain vm-template` describes its VM toggle.

Changing a manifest takes effect when the relevant user is initialized again. If authorized to
install the selected software, use `agw vm reinit <name>` for the admin user or
`agw agent reinit <name>` for an agent. These operations also rerun other configured initialization
steps. Otherwise inspect the manifests without running initialization. See the repository's
`docs/guides/npm.md` for runtime prerequisites, prefix layout, release age filtering, and retry
behavior.

# Installing npm CLI tools

Agentworks installs declared npm CLI packages separately for each admin or agent user. It invokes
native npm, without sudo, and does not require mise.

## Runtime prerequisites

Provision Node.js and npm before the per-user package installation phase, using the VM's apt
packages, system install commands, or another existing runtime installation. A mise-managed runtime
also works when mise is enabled, but neither installer invokes or enables the other.

Use a supported Node.js LTS release and an npm version compatible with it. Agentworks requires npm
11.10.0 or newer because earlier versions do not enforce the native minimum release age option.
Package `engines` requirements are enforced with `--engine-strict`; incompatible packages fail
instead of merely warning. Agentworks does not automatically upgrade or replace the runtime. npm's
own supported Node.js range remains the runtime provisioner's responsibility; Agentworks checks the
npm feature version and enforces installed packages' engines.

The optional `apt` plugin includes a `nodejs` package entry backed by NodeSource. Its bundled npm
version depends on the selected Node.js release. Check the runtime's actual `node --version` and
`npm --version`; selecting that catalog entry alone does not guarantee a sufficiently recent npm. A
user install command cannot bootstrap npm for this phase because custom user commands run afterward.

## Declare packages

Add npm settings to an admin-template or agent-template manifest:

```yaml
apiVersion: agentworks/v1
kind: admin-template
metadata:
  name: default
spec:
  npm_enabled: true
  npm_packages:
    - typescript@latest
  npm_min_release_age_days: 7
```

`agw resource explain admin-template` and `agw resource explain agent-template` describe the fields;
`agw resource sample <kind>` prints a starter manifest. Scoped registry packages, exact versions,
tags, and version ranges are supported. File paths, tarballs, Git repositories, URLs, and package
aliases are rejected.

Package lists are empty by default, so npm performs no work until packages are selected. Agent
templates inherit npm settings using the ordinary template rules.

## Prefix and PATH

The npm prefix is `~/.local` in the target user's home. Packages install under
`~/.local/lib/node_modules`, and executable links go under `~/.local/bin`. Agentworks adds that
executable directory to the user's managed shell profile before custom install commands run.
Sessions inherit the same PATH.

The stable prefix is independent of Node's installation directory. Changing Node does not move the
packages; packages with native extensions may still need reinstallation for the new runtime.

## Release age and reinitialization

The default minimum release age is seven days. npm applies the configured age to registry-resolved
releases, including dependencies and explicitly pinned versions. Git, file, and tarball dependencies
do not have registry publication timestamps, so npm cannot enforce their age. A tag or range may
resolve to an older eligible release; an exact version that is too new fails. Set
`npm_min_release_age_days: 0` to remove the age restriction. Agentworks clears inherited age
exclusions on npm versions that support them.

Initialization runs each selected install again, letting npm reconcile the requested version or
range. It does not uninstall packages removed from the list or prune unrelated packages. Failures
warn and initialization continues; there is no fallback that drops the age or engine checks. After
fixing a prerequisite or package failure, rerun initialization to retry.

Set `npm_enabled: false` to skip npm probes, installs, and its managed PATH addition. Existing
packages remain on disk. Another source, such as dotfiles or a custom install command, can
independently add the same directory to PATH.

## Installation order

Admin and agent setup both run dotfiles setup, mise installation, npm package installation, and
custom user install commands in that order. Both package installers finish their attempts before
custom commands start, and their managed executable paths are available to those commands on the
first initialization.

Mise is optional at VM scope. Set `mise_enabled: false` on the VM template to skip its repository,
system package, per-user setup, activation, and managed shim PATH. This does not disable npm or
remove an existing mise installation.

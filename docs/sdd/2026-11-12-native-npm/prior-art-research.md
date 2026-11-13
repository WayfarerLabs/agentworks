# Prior art

Research checked on 2026-11-12.

- [npm configuration](https://docs.npmjs.com/using-npm/config/) documents `min-release-age` in days
  and strict engine checking. Use npm's resolver rather than implementing a second dependency
  resolver.
- [npm's changelog](https://github.com/npm/cli/blob/latest/CHANGELOG.md) records minimum release age
  support in 11.10.0. Reject earlier npm versions rather than allowing an unknown option to silently
  weaken the requested policy.
- [npm folders](https://docs.npmjs.com/cli/v11/configuring-npm/folders/) specifies Unix global
  packages under the prefix's `lib/node_modules` and executables under `bin`. A user-owned prefix
  avoids sudo and survives runtime version changes.
- [npm package specifications](https://docs.npmjs.com/cli/v11/using-npm/package-spec/) distinguish
  registry names from Git, files, tarballs, and aliases. The initial feature accepts registry
  specifications only.
- The installed Node is 24.18.0 with npm 11.16.0. Registry metadata for npm 11.10.0 requires Node
  `^20.17.0 || >=22.9.0`; current npm latest has a newer engine range. Recommend a supported Node
  LTS and check the actual npm version.

## Claims not relied on

The latest npm is not assumed to run on every existing Node version. The minimum age filter is not
treated as a replacement for package trust. mise's npm backend does not satisfy the requested
independent native npm installation mechanism.

## Open verification

Exercise age filtering against actual npm with a local registry, including transitive releases and
pinned versions. The operator selected explicit runtime provisioning through existing installation
mechanisms.

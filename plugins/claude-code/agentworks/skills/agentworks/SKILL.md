---
name: agentworks
description: >-
  Install or update the Agentworks CLI, verify its version, and run its built-in agent guide. Use
  when the operator wants to bootstrap Agentworks assistance.
compatibility: >-
  Requires Python 3.12 or newer and network access only when installing or updating the CLI.
metadata:
  agentworks-package-version: "1.0.0"
  agentworks-min-cli-version: "0.14.0"
---

# Agentworks CLI bootstrap

You are my external Agentworks assistant agent, not an Agentworks-managed agent resource. Use this
prompt only to make a compatible `agentworks-cli` available and hand off to its built-in agent
guide. Agentworks requires Python 3.12 or newer and provides the `agw` command.

## Install and hand off

1. Run `agw version`.
2. If it reports a valid version at least 0.14.0 and I did not request an update, retain it and skip
   installation. Otherwise, use an exact compatible stable version at least 0.14.0 that I requested,
   or read `https://pypi.org/pypi/agentworks-cli/json` and select the latest compatible
   non-prerelease.
3. When installation or update is needed, run `uv tool install --upgrade 'agentworks-cli==VERSION'`.
   If installation is unavailable or fails, stop before the guide and leave that exact pinned
   command.
4. After installation or update, run `agw version` again and require the selected exact version. For
   a retained installation, require version 0.14.0 or newer.
5. Run `agw guide --agent` and obey the returned guide context for all further Agentworks help.

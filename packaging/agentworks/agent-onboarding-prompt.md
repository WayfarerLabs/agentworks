# Agentworks setup

I'd like your help getting up and running with Agentworks, a CLI for configuring and operating development environments, workspaces, and sessions for coding agents.

The public repository is available at <https://github.com/WayfarerLabs/agentworks> if you or I need to inspect the source before installing. The CLI is self-documenting through its help and guide output, with additional notes for assistant agents.

The CLI is published on PyPI as `agentworks-cli`. The recommended installation method is `uv`, although other Python tool installers should work with Python 3.12 or newer.

```shell
uv tool install --upgrade 'agentworks-cli>=0.14'
```

Once installed, run `agw guide --agent` and follow its guidance to get started.

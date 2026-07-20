# Built-in resource manifests

App-shipped resources published with the `built-in` origin; loaded by
`agentworks/manifests/builtin.py` through the same loader as operator manifests. Currently ships the
reserved vm-sites (`vm-sites.yaml`) and the built-in catalog entries (`apt-sources.yaml`,
`apt-packages.yaml`, `install-commands.yaml`). Future built-ins and plugins (their own origin
variants) are the mechanism's further consumers.

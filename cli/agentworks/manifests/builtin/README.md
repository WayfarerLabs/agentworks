# Built-in resource manifests

App-shipped resources published with the `built-in` origin; loaded by
`agentworks/manifests/builtin.py` through the same loader as operator manifests. Currently ships the
reserved vm-sites (`vm-sites.yaml`). The optional `apt` plugin owns five apt sources and five apt
packages; the optional `install-command` plugin owns six user install commands. Both are disabled
until named in `[plugins].system`. Future built-ins and plugins (their own origin variants) are the
mechanism's further consumers.

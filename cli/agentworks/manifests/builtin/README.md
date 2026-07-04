# Built-in resource manifests

App-shipped resources published with the `built-in` origin. Carries the built-in secret backends
(`secret-backends.yaml`); loaded by `agentworks/manifests/builtin.py` through the same loader as
operator manifests. Future plugins reuse this mechanism with their own origin variants.

# Built-in resource manifests

App-shipped resources published with the `built-in` origin; loaded by
`agentworks/manifests/builtin.py` through the same loader as operator manifests. Currently empty
(the bundled secret-backend manifests died in the 2026-07-07 capability collapse); the mechanism
stays wired, and future built-ins and plugins (their own origin variants) are its consumers.

# Native SSH binding

The private `_ssh_native_binding.py` helper composes cloud-selected public IPv4 endpoints with
loaded `operator.ssh` settings. It creates an immutable connection, an ordinary carrier and a
factory for fresh managed delivery carriers using that same connection. Construction does not
inspect local identity or trust files, start a process, enroll trust or activate a route.

Each provider's `resolve_native_execution_binding` first requires explicit SSH settings, then reads
and verifies the persisted provider identity under the caller's deadline. AWS reads the exact EC2
instance and account. GCP also verifies the persisted network, subnet and named external access
configuration. Azure reads the exact VM, then its linked NIC and public IP identities under the same
deadline, with service retries disabled. Linked Azure resources may use other resource groups within
the persisted subscription. SDK credential setup and authentication retries remain subject to the
SDK's behavior; successful late results are rejected without replaying the read.

Trust uses the configured managed directory and endpoint lookup with no host key alias. Identity,
agent, client and keepalive selections come from `SSHSettings`. The early guest facts probe enters
as root, directly for a root delivery account or through sudo without prompting for another account,
and resolves the named delivery account's actual identity. The binding supplies no independent
availability guarantee. Route activation, delivery custody and native lifecycle ownership belong to
the caller.

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
as root, directly for root delivery, otherwise through passwordless sudo to root, and resolves the
named delivery account's actual identity. AWS, Azure and GCP explicitly supply the no-idle-stop
independent-job availability fact in both passive resolution and owned preparation. The shared SSH
helper defaults to no guarantee; SSH connectivity alone says nothing about platform availability.
This fact excludes explicit VM stop, reboot, host loss and external policy. Route activation,
delivery custody and native lifecycle ownership belong to the caller.

# Using AWS with Agentworks

Agentworks provisions EC2 through the opt-in `aws` system plugin. Enable it before using an
`aws-ec2` site or a VM template that requests the optional guest `aws-cli` install command:

```toml
[plugins]
system = ["aws"]
```

## Optional guest AWS CLI

Add `aws-cli` to a template's `system_install_commands` when a guest needs AWS CLI v2:

```yaml
spec:
  apt: [snapd]
  system_install_commands: [aws-cli]
```

Stock Agentworks Debian guests do not include snapd. VM initialization installs the template's apt
packages before system install commands, so the `apt` entry makes the `snap` command available
before the `aws-cli` resource runs. A working snapd service and access to the Snap Store remain
prerequisites. If installing snapd or the AWS CLI fails, VM initialization reports a `partial`
status.

The resource invokes `sudo snap install aws-cli --classic`. Classic confinement grants the snap
traditional access to the host without strict sandbox isolation. The resource uses `/snap/bin/aws`
as its completion check, so init and reinit skip the command once that path exists. Snapd owns
subsequent automatic refreshes, which keeps the installed CLI current but means this declarable
cannot pin an AWS CLI minor version.

This is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3 and the
host's configured AWS credential chain. Installing the guest CLI does not run `aws configure`, write
an AWS credentials or profile file, change host authentication, or make the CLI an EC2 lifecycle
dependency.

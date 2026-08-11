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
  system_install_commands: [aws-cli]
```

The guest must already have a working `snap` command, snapd service, and access to the Snap Store.
The resource invokes `sudo snap install aws-cli --classic`. It uses `/snap/bin/aws` as its
completion check, so init and reinit skip the command once that path exists. Snapd owns subsequent
automatic refreshes.

This is guest tooling, not a credential mechanism. EC2 provisioning continues to use boto3 and the
host's configured AWS credential chain. Installing the guest CLI does not run `aws configure`, write
an AWS credentials or profile file, change host authentication, or make the CLI an EC2 lifecycle
dependency.

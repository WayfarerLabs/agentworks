# Using Amazon EC2 with Agentworks

Agentworks provisions Debian VMs on Amazon EC2 through the opt-in `aws` system plugin. Each
`aws-ec2` VM site targets one region and may name a subnet. If no subnet is configured, Agentworks
uses a default subnet in the region's default VPC.

## Enable and authenticate the platform

Enable the plugin in `~/.config/agentworks/config.toml`:

```toml
[plugins]
system = ["aws"]
```

Ambient authentication uses the normal boto3 credential chain. An explicit access-key site names the
secret containing its secret access key and never falls back to ambient credentials:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: ec2-dev
spec:
  platform:
    name: aws-ec2
    region: us-east-1
    auth:
      mode: access-key
      access_key_id: AKIAEXAMPLE
      access_key_secret: aws-secret-access-key
```

Add `subnet_id` when the site should use a particular subnet. Add `assume_role_arn` under `auth`
when the access key should assume a role. That source identity then needs `sts:AssumeRole`, and the
destination role trust policy must admit it.

## IAM permissions

The selected identity needs these actions for the complete shipped lifecycle:

```text
ec2:AuthorizeSecurityGroupIngress
ec2:CreateSecurityGroup
ec2:CreateTags
ec2:DeleteSecurityGroup
ec2:DescribeImages
ec2:DescribeInstances
ec2:DescribeInstanceTypes
ec2:DescribeSecurityGroups
ec2:DescribeSubnets
ec2:RevokeSecurityGroupIngress
ec2:RunInstances
ec2:StartInstances
ec2:StopInstances
ec2:TerminateInstances
ssm:GetParameter
```

`ssm:GetParameter` is used only to resolve Debian's public AMI parameter. Describe actions normally
need `Resource: "*"`; scope them with region conditions where appropriate. `RunInstances` must be
allowed for every resource type in the actual request, including the Debian AMI, subnet, security
group, instance, network interface, and EBS volume. The request also tags the instance, volume, and
security group with `agentworks:vm`, which is why `ec2:CreateTags` is required.

Agentworks does not use Elastic IPs, instance profiles, snapshots, or explicit volume deletion. It
therefore needs no Elastic IP actions, `iam:PassRole`, snapshot actions, or `ec2:DeleteVolume` for
the shipped request. An account whose selected AMI or EBS encryption defaults use a customer-managed
KMS key may also require the KMS actions that key policy and EBS encryption require.

## Safety checks

Before creating resources, `vm create` uses STS to bind the VM to the selected 12-digit AWS account.
Credential, permission, transport, and invalid-response failures stop there. Agentworks does not use
IAM policy simulation; each EC2 operation remains authoritative.

Before opening an SSH ingress tuple, Agentworks uses EC2's exact-request `DryRun` to verify that it
can revoke the tuple. Only `DryRunOperation` is positive evidence; every other result stops before
the route opens. A failed explicit delete keeps the VM row so the operator can correct IAM and retry
`agw vm delete`.

## Network and cleanup model

Each VM receives one Agentworks-owned security group. Its empty ingress set is the deny baseline.
Agentworks opens TCP/22 only to the detected operator IPv4 address plus `operator.ssh_allow_cidrs`,
first for bootstrap and later for a native platform shell. It revokes only the tuples that operation
opened.

`agw vm delete <name>` refreshes the STS identity and refuses an account mismatch. It reads the
recorded instance and security group, verifies their `agentworks:vm` ownership and recorded
association before termination, then waits for termination and deletes the group after its network
interface detaches. An account-bound instance that is already absent needs no termination. An absent
legacy instance remains ambiguous, so Agentworks retains its row.

Create rollback never accepts `NotFound` for a newly returned provider ID as proof of cleanup. It
retries the exact termination or group deletion because the resource may still be propagating. If
AWS never positively accepts cleanup, Agentworks retains the VM row in failed state with its known
account, region, instance, group, and ownership identifiers. Retry `agw vm delete <name>` after
provider permissions or availability recover; that explicit path also handles a retained row whose
create stopped after creating only the security group. If any create request may have succeeded but
did not return its provider ID, inspect instances and security groups in the site-selected account
and region. Act only on an instance with both the exact `agentworks:vm` tag and the known security
group association: terminate that verified instance manually, then use `agw vm delete <name>` to
remove the retained group. Agentworks can only target provider IDs it received.

Rows created before account binding carry no account ID. Agentworks deletes one only when the live
instance's ID, ownership tag, and security-group association prove that the current account owns the
target. An already-absent legacy target is intentionally ambiguous, so Agentworks retains the row
instead of risking a wrong-account orphan.

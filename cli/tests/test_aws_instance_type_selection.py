"""EC2 instance-type selection and catalog validation: the standard
compute/memory model resolves to the smallest fitting type from the built-in
Graviton ladder or the site's ``platform_config.instance_types`` override,
plus the internal arch-to-Debian-SSM-segment mapping.
"""

from __future__ import annotations

import pytest

from agentworks.capabilities.config import validate_capability_config
from agentworks.errors import ConfigError
from agentworks.plugins.aws.platform import (
    _DEBIAN_ARCH_SEGMENT,
    _DEFAULT_INSTANCE_TYPES,
    AwsEC2Config,
    _instance_catalog,
    _InstanceType,
    _select_instance_type,
)
from agentworks.schema import RefOwner


class TestSelectInstanceType:
    def test_exact_match_wins(self) -> None:
        t = _select_instance_type(_DEFAULT_INSTANCE_TYPES, cpus=4, memory_gib=16)
        assert t.type == "t4g.xlarge"
        assert (t.cpus, t.memory_gib) == (4, 16)

    def test_off_ratio_rounds_up_to_smallest_fit(self) -> None:
        """4 vCPU / 8 GiB has no exact ladder entry; it rounds up to the
        smallest entry satisfying BOTH axes, over-provisioning memory."""
        t = _select_instance_type(_DEFAULT_INSTANCE_TYPES, cpus=4, memory_gib=8)
        assert t.type == "t4g.xlarge"  # 4 vCPU / 16 GiB
        assert t.memory_gib > 8

    def test_picks_smallest_across_both_axes(self) -> None:
        t = _select_instance_type(_DEFAULT_INSTANCE_TYPES, cpus=2, memory_gib=8)
        assert t.type == "t4g.large"  # 2/8 beats 4/16

    def test_no_fit_raises_with_largest_in_message(self) -> None:
        with pytest.raises(ConfigError) as exc:
            _select_instance_type(_DEFAULT_INSTANCE_TYPES, cpus=64, memory_gib=256)
        assert "m7g.4xlarge" in str(exc.value)
        assert exc.value.hint is not None

    def test_selection_independent_of_catalog_order(self) -> None:
        unsorted = (
            _InstanceType(8, 32, "big", "arm64"),
            _InstanceType(2, 8, "small", "arm64"),
            _InstanceType(4, 16, "mid", "arm64"),
        )
        assert _select_instance_type(unsorted, cpus=2, memory_gib=8).type == "small"

    def test_default_ladder_is_all_graviton_arm64(self) -> None:
        """The built-in ladder is Graviton (arm64) end to end, so the default
        image segment is arm64 and no operator config is needed to get it."""
        assert {t.arch for t in _DEFAULT_INSTANCE_TYPES} == {"arm64"}


class TestInstanceCatalog:
    """The catalog RESOLVER: the shape is the model's business now, so
    what is left here is the default (domain knowledge) and the mapping
    onto the selection tuple. The shape rejections are asserted through
    the core, so this file keeps proving a bad catalog never reaches
    selection."""

    def test_no_override_returns_builtin(self) -> None:
        assert _instance_catalog(_config({})) is _DEFAULT_INSTANCE_TYPES

    def test_valid_override_parses(self) -> None:
        catalog = _instance_catalog(
            _config({"instance_types": [{"cpus": 4, "memory": 16, "type": "m7i.xlarge", "arch": "x86_64"}]})
        )
        assert catalog == ((4, 16, "m7i.xlarge", "x86_64"),)

    @pytest.mark.parametrize(
        "bad",
        [
            {"instance_types": "t4g.small"},  # not a list
            {"instance_types": []},  # empty: a site on which nothing can launch
            {"instance_types": [{"cpus": 4, "memory": 16, "type": "x"}]},  # missing arch
            {"instance_types": [{"cpus": 4, "memory": 16, "arch": "arm64"}]},  # missing type
            {"instance_types": [{"cpus": 0, "memory": 16, "type": "x", "arch": "arm64"}]},  # non-positive
            {"instance_types": [{"cpus": True, "memory": 16, "type": "x", "arch": "arm64"}]},  # bool cpus
            {"instance_types": [{"cpus": 4, "memory": 16, "type": "", "arch": "arm64"}]},  # empty type
            {"instance_types": [{"cpus": 4, "memory": 16, "type": "x", "arch": "amd64"}]},  # Debian spelling, not EC2
            {"instance_types": [{"cpus": 4, "memory": 16, "type": "x", "arch": "arm64", "gpu": 1}]},  # unknown
            {"instance_types": ["t4g.small"]},  # entry not a table
        ],
    )
    def test_malformed_override_raises(self, bad: dict[str, object]) -> None:
        with pytest.raises(ConfigError):
            _config(bad)

    def test_arch_error_names_the_ec2_vocabulary(self) -> None:
        """The arch value must be the EC2 name; the message points the operator
        at the accepted spellings rather than silently mapping Debian's."""
        with pytest.raises(ConfigError, match="arch: must be one of"):
            _config({"instance_types": [{"cpus": 4, "memory": 16, "type": "x", "arch": "aarch64"}]})


class TestArchToDebianSegment:
    def test_mapping_is_ec2_to_debian(self) -> None:
        """The EC2 arch names map to Debian's image-naming segments; the arm64
        spelling is shared, x86_64 becomes amd64."""
        assert _DEBIAN_ARCH_SEGMENT == {"x86_64": "amd64", "arm64": "arm64"}


def _config(blob: dict[str, object]) -> AwsEC2Config:
    """``blob`` validated as an aws-ec2 site's config, through the core."""
    validated = validate_capability_config(
        kind="vm-platform",
        config={"name": "aws-ec2", "region": "us-east-1", **blob},
        owner=RefOwner(kind="vm-site", name="aws"),
    )
    assert isinstance(validated, AwsEC2Config)
    return validated

"""The Lima site's SSH placement host is one ssh argument, refused at the config boundary otherwise."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentworks.capabilities.vm_platform.lima import LimaConfig


@pytest.mark.parametrize("host", ["me@gpu-box", "gpu-box", "me@10.0.0.5", "me@gpu-box.example.com"])
def test_a_one_word_placement_host_is_accepted(host: str) -> None:
    config = LimaConfig.model_validate({"name": "lima", "placement": {"mode": "ssh", "host": host}})

    assert config.placement.host == host  # type: ignore[union-attr]


@pytest.mark.parametrize("host", ["me@gpu-box -oProxyCommand=x", "gpu box", "gpu-box\n", "gpu\tbox", "gpu\x1bbox"])
def test_a_placement_host_with_whitespace_or_a_control_character_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        LimaConfig.model_validate({"name": "lima", "placement": {"mode": "ssh", "host": host}})

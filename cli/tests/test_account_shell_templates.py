"""The admin and agent template ``shell`` fields accept only the four shells the first stage parses under."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentworks.agents.template import AgentTemplate
from agentworks.errors import ConfigError
from agentworks.vms.admin import AdminConfig
from tests.conftest import ManifestDoc


@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "zsh"])
def test_the_four_supported_shells_load_as_written(shell: str) -> None:
    assert AdminConfig.model_validate({"shell": shell}).shell == shell
    assert AgentTemplate.model_validate({"name": "t", "shell": shell}).shell == shell


@pytest.mark.parametrize(
    ("written", "stored"),
    [("/usr/bin/zsh", "zsh"), ("/bin/bash", "bash"), ("/usr/local/bin/dash", "dash"), ("/bin/sh", "sh")],
)
def test_a_shell_path_loads_as_its_basename(written: str, stored: str) -> None:
    assert AdminConfig.model_validate({"shell": written}).shell == stored
    assert AgentTemplate.model_validate({"name": "t", "shell": written}).shell == stored


@pytest.mark.parametrize("shell", ["tcsh", "fish", "csh", "/usr/bin/fish", "/bin/zsh/", "", "Bash", "nologin"])
def test_any_other_shell_is_refused_on_the_shell_field(shell: str) -> None:
    for model in (AdminConfig, AgentTemplate):
        with pytest.raises(PydanticValidationError) as raised:
            model.model_validate({"name": "t", "shell": shell})
        assert [error["loc"] for error in raised.value.errors()] == [("shell",)]


@pytest.mark.parametrize("kind", ["admin-template", "agent-template"])
def test_an_unsupported_shell_in_a_manifest_refuses_at_config_load(kind: str, make_config) -> None:  # noqa: ANN001
    from agentworks.bootstrap import build_registry

    with pytest.raises(ConfigError, match="shell"):
        build_registry(make_config(manifests=[ManifestDoc(kind, "default", {"shell": "tcsh"})]))


@pytest.mark.parametrize("kind", ["admin-template", "agent-template"])
def test_a_shell_path_in_a_manifest_loads(kind: str, make_config) -> None:  # noqa: ANN001
    from agentworks.bootstrap import build_registry

    build_registry(make_config(manifests=[ManifestDoc(kind, "default", {"shell": "/usr/bin/zsh"})]))

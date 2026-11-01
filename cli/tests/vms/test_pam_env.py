"""The pam_env alert: the probe's parsing, and a warning that never fails the caller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.vms.manager.pam_env import PAM_ENV_FILES, parse_pam_env_probe, warn_pam_env_settings


def target(stdout: str = "", *, error: Exception | None = None) -> Any:
    def run(command: str, **kwargs: object) -> SimpleNamespace:
        if error is not None:
            raise error
        return SimpleNamespace(stdout=stdout, returncode=0)

    return SimpleNamespace(run=run)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", ()),
        ("\n\n", ()),
        ("/etc/environment\n", ("/etc/environment",)),
        ("~/.pam_environment\n/etc/environment\n", ("/etc/environment", "~/.pam_environment")),
        (" /etc/security/pam_env.conf \r\n", ("/etc/security/pam_env.conf",)),
        # The guest's text is input: only the known names count.
        ("/etc/default/locale\nFOO=1\n/etc/environment extra\n", ()),
    ],
)
def test_the_probe_output_yields_the_known_files_it_names(stdout: str, expected: tuple[str, ...]) -> None:
    assert parse_pam_env_probe(stdout) == expected


def test_locale_is_not_one_of_the_checked_files() -> None:
    assert "/etc/default/locale" not in PAM_ENV_FILES


def test_a_file_with_settings_produces_one_warning(captured_output) -> None:
    warn_pam_env_settings(target("/etc/environment\n"), "box")

    assert len(captured_output.warnings) == 1


def test_a_clean_vm_produces_no_warning(captured_output) -> None:
    warn_pam_env_settings(target(""), "box")

    assert captured_output.warnings == []


def test_a_failed_probe_warns_and_does_not_raise(captured_output) -> None:
    warn_pam_env_settings(target(error=RuntimeError("connection lost")), "box")

    assert len(captured_output.warnings) == 1

"""Required guest packages fail initialization if apt cannot install them."""

import pytest

from agentworks.capabilities.vm_platform.cloud_init import INIT_SYSTEM_PACKAGES
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer.packages import _install_system_packages, apt_install
from tests.phase_b_support import StepGuest


def test_system_install_includes_python_without_template_selection() -> None:
    guest = StepGuest(local_files=False)

    _install_system_packages(guest.files, guest.log)

    install = next(
        request
        for request in guest.commands()
        if isinstance(request, Command) and request.argv[:2] == ("apt-get", "install")
    )
    assert "python3" in install.argv
    assert install.elevated


def test_required_package_failure_propagates() -> None:
    guest = StepGuest(local_files=False)
    failure = CommandFailed("box", 100, Result(Completed(100)), streams_logged=True)
    guest.respond(apt_install(INIT_SYSTEM_PACKAGES), failure)

    with pytest.raises(CommandFailed) as caught:
        _install_system_packages(guest.files, guest.log)
    assert caught.value is failure

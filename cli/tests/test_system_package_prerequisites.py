"""Required guest packages fail initialization if apt cannot install them."""

from unittest.mock import Mock

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import Transport
from agentworks.vms.initializer.packages import _install_system_packages


def test_system_install_includes_python_without_template_selection():
    target = Mock(spec=Transport)
    target.run.return_value = SSHResult(0, "", "")
    _install_system_packages(target, Mock())
    install = next(call for call in target.run.call_args_list if "apt-get install" in call.args[0])
    assert "python3" in install.args[0].split()
    assert install.kwargs["sudo"] is True


def test_required_package_failure_propagates():
    failure = SSHError("fixture apt failure")

    def run(command, **kwargs):
        if "apt-get install" in command:
            raise failure
        return SSHResult(0, "", "")

    target = Mock(spec=Transport)
    target.run.side_effect = run
    with pytest.raises(SSHError) as caught:
        _install_system_packages(target, Mock())
    assert caught.value is failure

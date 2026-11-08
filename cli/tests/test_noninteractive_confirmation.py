"""Disabling interactions cannot block on input or grant implicit consent."""

from unittest.mock import Mock

import pytest

from agentworks import output


@pytest.mark.parametrize("default", [False, True])
def test_noninteractive_confirmation_declines_without_touching_input(monkeypatch, default):
    handler = Mock(spec=output.OutputHandler)
    monkeypatch.setattr(output, "_handler", handler)
    monkeypatch.setattr(output, "_non_interactive", True)
    assert output.confirm("fixture", default=default) is False
    handler.confirm.assert_not_called()


@pytest.mark.parametrize("answer", [False, True])
def test_interactive_confirmation_still_uses_the_handler(monkeypatch, answer):
    handler = Mock(spec=output.OutputHandler)
    handler.confirm.return_value = answer
    monkeypatch.setattr(output, "_handler", handler)
    monkeypatch.setattr(output, "_non_interactive", False)
    assert output.confirm("fixture") is answer
    handler.confirm.assert_called_once()

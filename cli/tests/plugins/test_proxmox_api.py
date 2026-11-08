"""Tests for the Proxmox API client."""

from __future__ import annotations

import http.client
import json
import urllib.error
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentworks.plugins.proxmox.api import (
    ERROR_BODY_LIMIT_BYTES,
    ProxmoxAPI,
    ProxmoxAPIError,
    _InvalidQGAExecStatus,
)


@pytest.fixture()
def api() -> ProxmoxAPI:
    return ProxmoxAPI(
        api_url="https://pve.example.com:8006",
        token_id="user@pam!token",
        token_secret="secret-value",
        verify_ssl=False,
    )


def _mock_response(data: Any, status: int = 200) -> MagicMock:
    """Create a mock urllib response."""
    body = json.dumps({"data": data}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestRequestBuilding:
    """Test that requests are built with correct auth and URL."""

    @patch("urllib.request.urlopen")
    def test_auth_header(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("123")
        api.next_id()

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "PVEAPIToken=user@pam!token=secret-value"

    @patch("urllib.request.urlopen")
    def test_base_url(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("123")
        api.next_id()

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://pve.example.com:8006/api2/json/cluster/nextid"

    @patch("urllib.request.urlopen")
    def test_get_method(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("123")
        api.next_id()

        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"

    @patch("urllib.request.urlopen")
    def test_post_with_data(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("UPID:node:001")
        api.clone_vm("pve", 9000, 100, "test-vm")

        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.data is not None
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"

    @patch("urllib.request.urlopen")
    def test_trailing_slash_stripped(self, mock_urlopen: MagicMock) -> None:
        api = ProxmoxAPI(
            api_url="https://pve.example.com:8006/",
            token_id="u@p!t",
            token_secret="s",
            verify_ssl=False,
        )
        mock_urlopen.return_value = _mock_response("123")
        api.next_id()

        req = mock_urlopen.call_args[0][0]
        assert "//" not in req.full_url.split(":", 2)[-1]


class TestResponseParsing:
    """Test response parsing."""

    @patch("urllib.request.urlopen")
    def test_next_id_returns_int(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("456")
        result = api.next_id()
        assert result == 456

    @patch("urllib.request.urlopen")
    def test_clone_returns_upid(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("UPID:pve:001:task")
        result = api.clone_vm("pve", 9000, 100, "test")
        assert result == "UPID:pve:001:task"

    @patch("urllib.request.urlopen")
    def test_vm_status_returns_dict(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        status_data = {"status": "running", "vmid": 100, "name": "test"}
        mock_urlopen.return_value = _mock_response(status_data)
        result = api.vm_status("pve", 100)
        assert result["status"] == "running"
        assert result["vmid"] == 100

    @patch("urllib.request.urlopen")
    def test_vm_status_forwards_status_only_timeout(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response({"status": "running"})

        api.vm_status("pve", 100, timeout=10)

        assert mock_urlopen.call_args.kwargs["timeout"] == 10

    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_sends_argv_and_optional_input(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response({"pid": 42})

        pid = api.guest_agent_exec(
            "pve",
            100,
            command=["/bin/bash", "-lc", "read value"],
            input_data="sensitive input\n",
            timeout=9.5,
        )

        req = mock_urlopen.call_args.args[0]
        assert pid == 42
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {
            "command": ["/bin/bash", "-lc", "read value"],
            "input-data": "sensitive input\n",
        }
        assert mock_urlopen.call_args.kwargs["timeout"] == 9.5

    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_omits_input_for_immediate_eof(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response({"pid": 42})

        api.guest_agent_exec("pve", 100, command=["/bin/true"])

        req = mock_urlopen.call_args.args[0]
        assert json.loads(req.data) == {"command": ["/bin/true"]}

    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_reads_pid(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        status = {"exited": True, "exitcode": 0}
        mock_urlopen.return_value = _mock_response(status)

        assert api.guest_agent_exec_status("pve", 100, pid=42, timeout=3) == status

        req = mock_urlopen.call_args.args[0]
        assert req.get_method() == "GET"
        assert req.full_url.endswith("/nodes/pve/qemu/100/agent/exec-status?pid=42")
        assert mock_urlopen.call_args.kwargs["timeout"] == 3

    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_normalizes_wire_booleans(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
    ) -> None:
        mock_urlopen.return_value = _mock_response({"exited": 1, "exitcode": 0, "out-truncated": 0, "err-truncated": 0})

        result = api.guest_agent_exec_status("pve", 100, pid=42)

        assert result == {"exited": True, "exitcode": 0, "out-truncated": False, "err-truncated": False}

    @pytest.mark.parametrize("field", ["exited", "out-truncated", "err-truncated"])
    @pytest.mark.parametrize("value", [-1, 2, "1", 0.0, None])
    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_rejects_invalid_wire_booleans(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
        field: str,
        value: object,
    ) -> None:
        status: dict[str, object] = {"exited": 1, "exitcode": 0}
        status[field] = value
        mock_urlopen.return_value = _mock_response(status)

        with pytest.raises(_InvalidQGAExecStatus):
            api.guest_agent_exec_status("pve", 100, pid=42)

    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_reads_a_running_status(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response({"exited": 0})

        assert api.guest_agent_exec_status("pve", 100, pid=42) == {"exited": False}

    @pytest.mark.parametrize(
        "status",
        [
            {},
            {"exited": "yes"},
            {"exited": 0, "exitcode": 0},
            {"exited": 0, "signal": 9},
            {"exited": 0, "out-data": ""},
            {"exited": 0, "err-data": ""},
            {"exited": 0, "out-truncated": 0},
            {"exited": 0, "err-truncated": 0},
            {"exited": 1},
            {"exited": 1, "exitcode": 0, "signal": 9},
            {"exited": 1, "exitcode": 0, "signal": None},
            {"exited": 1, "exitcode": 0, "out-truncated": 1},
            {"exited": 1, "exitcode": 0, "err-truncated": 1},
            {"exited": 1, "signal": 0},
            {"exited": 1, "signal": -1},
            {"exited": 1, "exitcode": -1},
            {"exited": 1, "exitcode": "0"},
            {"exited": 1, "exitcode": 0, "out-data": 1},
            {"exited": 1, "exitcode": 0, "err-data": 1},
        ],
    )
    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_rejects_invalid_status(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
        status: dict[str, object],
    ) -> None:
        mock_urlopen.return_value = _mock_response(status)

        with pytest.raises(_InvalidQGAExecStatus) as caught:
            api.guest_agent_exec_status("pve", 100, pid=42)

        assert "pve" in str(caught.value)
        assert "100" in str(caught.value)
        assert "42" in str(caught.value)

    @pytest.mark.parametrize("response", [None, [], "invalid"])
    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_status_rejects_malformed_data(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
        response: object,
    ) -> None:
        mock_urlopen.return_value = _mock_response(response)

        with pytest.raises(_InvalidQGAExecStatus) as caught:
            api.guest_agent_exec_status("pve", 100, pid=42)

        assert "pve" in str(caught.value)
        assert "100" in str(caught.value)
        assert "42" in str(caught.value)

    @pytest.mark.parametrize("response", [None, {}, {"pid": None}, {"pid": "42"}, {"pid": True}])
    @patch("urllib.request.urlopen")
    def test_guest_agent_exec_rejects_malformed_pid(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
        response: object,
    ) -> None:
        mock_urlopen.return_value = _mock_response(response)

        with pytest.raises(ProxmoxAPIError):
            api.guest_agent_exec("pve", 100, command=["/bin/true"])


class TestErrorHandling:
    """Test error handling."""

    @patch("urllib.request.urlopen")
    def test_http_error_raises(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://pve.example.com:8006/api2/json/cluster/nextid",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b"authentication failure"),
        )
        with pytest.raises(ProxmoxAPIError, match="401"):
            api.next_id()

    @patch("urllib.request.urlopen")
    def test_a_huge_error_body_is_read_only_in_part(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        """Every message quotes the body, so a 2 MB one would reach the terminal whole."""
        mock_urlopen.side_effect = _http_error(500, b"x" * 2_000_000)

        with pytest.raises(ProxmoxAPIError) as caught:
            api.next_id()

        assert len(str(caught.value)) < 2 * ERROR_BODY_LIMIT_BYTES

    @pytest.mark.parametrize("status", [None, "running", ["running"]])
    @patch("urllib.request.urlopen")
    def test_a_malformed_vm_status_is_typed(self, mock_urlopen: MagicMock, api: ProxmoxAPI, status: Any) -> None:
        mock_urlopen.return_value = _mock_response(status)

        with pytest.raises(ProxmoxAPIError):
            api.vm_status("pve", 100)

    @patch("urllib.request.urlopen")
    def test_transport_error_is_typed(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        source = urllib.error.URLError("provider unavailable")
        mock_urlopen.side_effect = source

        with pytest.raises(ProxmoxAPIError) as caught:
            api.next_id()

        assert caught.value.__cause__ is source

    @pytest.mark.parametrize("body", ["truncated", "not-utf-8"])
    @patch("urllib.request.urlopen")
    def test_an_http_error_whose_body_cannot_be_read_keeps_its_code(
        self, mock_urlopen: MagicMock, api: ProxmoxAPI, body: str
    ) -> None:
        class _Truncated(BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise http.client.IncompleteRead(b"partial")

        fp = _Truncated() if body == "truncated" else BytesIO(b"\xff\xfe bad")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://pve.example.com:8006/api2/json/cluster/nextid",
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=fp,
        )

        with pytest.raises(ProxmoxAPIError) as caught:
            api.next_id()

        assert caught.value.code == 503

    @patch("urllib.request.urlopen")
    def test_a_malformed_http_response_is_typed(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        source = http.client.IncompleteRead(b"partial")
        mock_urlopen.side_effect = source

        with pytest.raises(ProxmoxAPIError) as caught:
            api.next_id()

        assert caught.value.__cause__ is source

    @patch("urllib.request.urlopen")
    def test_invalid_json_is_typed(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        response = _mock_response(None)
        response.read.return_value = b"not-json"
        mock_urlopen.return_value = response

        with pytest.raises(ProxmoxAPIError) as caught:
            api.next_id()

        assert isinstance(caught.value.__cause__, json.JSONDecodeError)

    @pytest.mark.parametrize("response_body", [None, {}])
    @patch("urllib.request.urlopen")
    def test_malformed_response_envelope_is_typed(
        self,
        mock_urlopen: MagicMock,
        api: ProxmoxAPI,
        response_body: Any,
    ) -> None:
        response = _mock_response(None)
        response.read.return_value = json.dumps(response_body).encode()
        mock_urlopen.return_value = response

        with pytest.raises(ProxmoxAPIError):
            api.next_id()

    @pytest.mark.parametrize("listing", [None, {"vmid": 100, "name": "dev"}, "dev"])
    @patch("urllib.request.urlopen")
    def test_a_malformed_vm_listing_is_typed(self, mock_urlopen: MagicMock, api: ProxmoxAPI, listing: Any) -> None:
        """A listing that is not a list is the typed API error, not a crash in the code that reads its entries."""
        mock_urlopen.return_value = _mock_response(listing)

        with pytest.raises(ProxmoxAPIError):
            api.list_vms("pve")

    @patch("urllib.request.urlopen")
    def test_a_vm_listing_is_returned(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response([{"vmid": 100, "name": "dev"}])

        assert api.list_vms("pve") == [{"vmid": 100, "name": "dev"}]

    @patch("urllib.request.urlopen")
    def test_task_failure_raises(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        task_data = {"status": "stopped", "exitstatus": "ERROR: clone failed"}
        mock_urlopen.return_value = _mock_response(task_data)
        with pytest.raises(ProxmoxAPIError, match="Task failed"):
            api.wait_for_task("pve", "UPID:pve:001", timeout=1)

    @patch("urllib.request.urlopen")
    def test_task_timeout_raises(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        task_data = {"status": "running"}
        mock_urlopen.return_value = _mock_response(task_data)
        with pytest.raises(ProxmoxAPIError, match="timed out"):
            api.wait_for_task("pve", "UPID:pve:001", timeout=0, poll_interval=0)


class TestVMOperations:
    """Test VM operation methods build correct requests."""

    @patch("urllib.request.urlopen")
    def test_start_vm(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("UPID:pve:start")
        result = api.start_vm("pve", 100)
        assert result == "UPID:pve:start"
        req = mock_urlopen.call_args[0][0]
        assert "/nodes/pve/qemu/100/status/start" in req.full_url
        assert req.get_method() == "POST"

    @patch("urllib.request.urlopen")
    def test_stop_vm(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("UPID:pve:stop")
        result = api.stop_vm("pve", 100)
        assert result == "UPID:pve:stop"
        req = mock_urlopen.call_args[0][0]
        assert "/nodes/pve/qemu/100/status/stop" in req.full_url

    @patch("urllib.request.urlopen")
    def test_delete_vm(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("UPID:pve:delete")
        result = api.delete_vm("pve", 100)
        assert result == "UPID:pve:delete"
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "DELETE"

    @patch("urllib.request.urlopen")
    def test_configure_vm(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response(None)
        api.configure_vm("pve", 100, cores=4, memory=8192)
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "PUT"
        assert b"cores=4" in req.data
        assert b"memory=8192" in req.data

    @patch("urllib.request.urlopen")
    def test_resize_disk(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response(None)
        api.resize_disk("pve", 100, "scsi0", "+20G")
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "PUT"
        assert "/resize" in req.full_url


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://pve.example.com:8006/api2/json/cluster/nextid?vmid=100",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body),
    )


class TestVMIDInUse:
    """``/cluster/nextid?vmid=N``, the one read any token may make about any VMID (measured on PVE 8 and 9)."""

    @patch("urllib.request.urlopen")
    def test_a_free_vmid_is_echoed(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("100")

        assert api.vmid_in_use(100) is False
        assert mock_urlopen.call_args[0][0].full_url.endswith("/cluster/nextid?vmid=100")

    @patch("urllib.request.urlopen")
    def test_a_held_vmid_is_a_400_saying_it_exists(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.side_effect = _http_error(400, b'{"errors":{"vmid":"VM 100 already exists"},"data":null}')

        assert api.vmid_in_use(100) is True

    @pytest.mark.parametrize(
        ("code", "body"),
        [
            (400, b'{"errors":{"vmid":"value must be at least 100"},"data":null}'),
            (400, b"not json"),
            (401, b"authentication failure"),
            (500, b'{"errors":{"vmid":"VM 100 already exists"}}'),
        ],
    )
    @patch("urllib.request.urlopen")
    def test_any_other_error_shows_neither(
        self, mock_urlopen: MagicMock, api: ProxmoxAPI, code: int, body: bytes
    ) -> None:
        mock_urlopen.side_effect = _http_error(code, body)

        with pytest.raises(ProxmoxAPIError) as caught:
            api.vmid_in_use(100)

        assert caught.value.code == code

    @patch("urllib.request.urlopen")
    def test_an_answer_for_another_vmid_shows_neither(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response("101")

        with pytest.raises(ProxmoxAPIError):
            api.vmid_in_use(100)


class TestTaskOperations:
    """Test task control methods build correct requests."""

    @patch("urllib.request.urlopen")
    def test_stop_task(self, mock_urlopen: MagicMock, api: ProxmoxAPI) -> None:
        mock_urlopen.return_value = _mock_response(None)
        api.stop_task("pve", "UPID:pve:00001234:root@pam:")
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "DELETE"
        # The UPID is percent-encoded in the path, matching wait_for_task.
        assert req.full_url == (
            "https://pve.example.com:8006/api2/json/nodes/pve/tasks/UPID%3Apve%3A00001234%3Aroot%40pam%3A"
        )


class TestSSLConfig:
    """Test SSL configuration."""

    def test_verify_ssl_true_no_custom_context(self) -> None:
        api = ProxmoxAPI(
            api_url="https://pve.example.com:8006",
            token_id="u@p!t",
            token_secret="s",
            verify_ssl=True,
        )
        assert api._ssl_ctx is None

    def test_verify_ssl_false_creates_context(self) -> None:
        api = ProxmoxAPI(
            api_url="https://pve.example.com:8006",
            token_id="u@p!t",
            token_secret="s",
            verify_ssl=False,
        )
        assert api._ssl_ctx is not None
        assert api._ssl_ctx.check_hostname is False

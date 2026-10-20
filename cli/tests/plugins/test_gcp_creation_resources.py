"""Real lifecycle custody with scripted GCE receipts, without provider execution."""

from __future__ import annotations

import copy
import json
from contextlib import closing, contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation_operation import ActivationState
from agentworks.plugins.gcp._creation_api import ROOT
from agentworks.plugins.gcp._creation_resources import CreationResources, Resource
from agentworks.plugins.gcp._creation_shapes import firewall_body
from agentworks.plugins.gcp.names import derive_names

PROJECT = "project"
ZONE = "us-central1-a"
GLOBAL = f"projects/{PROJECT}/global"
ZONAL = f"projects/{PROJECT}/zones/{ZONE}"
NETWORK = f"{GLOBAL}/networks/default"


class ScriptedAPI:
    cleanup_incomplete = False

    def __init__(self, owner):
        self.owner = owner
        self.controller = None
        self.live = {}
        self.calls = []
        self.operations = {}
        self.fault = None
        self.primary = KeyboardInterrupt("original provider request")
        self.preserve_disk = False
        self.insert_pending = False
        self.delete_target = None

    def get(self, path, deadline, *, missing=False):
        assert not deadline.expired
        self.calls.append(("GET", path))
        value = self.live.get(path)
        if value is None and not missing:
            raise StateError("synthetic selected resource unavailable")
        return copy.deepcopy(value)

    @contextmanager
    def exchange(self, method, path, deadline, *, body=None, missing=False):
        assert not deadline.expired
        self.calls.append((method, path))
        assert self.controller is not None
        row = self.owner.inspect_lifecycle_obligation(self.controller._id)
        assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert b"join-secret-sentinel" not in row.payload
        if method == "GET":
            result = copy.deepcopy(self.operations[path])
            result["status"] = "DONE"
        else:
            parsed = urlsplit(path)
            request_id = parse_qs(parsed.query)["requestId"][0]
            key = next(
                key
                for key, resource in self.controller.resources.items()
                if resource.path == parsed.path
                or (method == "POST" and resource.path == parsed.path + "/" + body["name"])
            )
            resource = self.controller.resources[key]
            saved = json.loads(row.payload)["resources"][key]
            assert saved["delete_entered" if method == "DELETE" else "create_entered"]
            target = str(101 + list(self.controller.resources).index(key))
            if method == "POST":
                self.live[resource.path] = {
                    **copy.deepcopy(body),
                    "id": target,
                    "kind": f"compute#{resource.kind}",
                    "selfLink": ROOT + resource.path,
                }
                if key == "instance":
                    disk = self.controller.resources["disk"]
                    self.live[resource.path]["disks"] = [
                        {
                            "boot": True,
                            "autoDelete": True,
                            "type": "PERSISTENT",
                            "source": ROOT + disk.path,
                        }
                    ]
                    self.live[disk.path] = {
                        "name": disk.path.rsplit("/", 1)[-1],
                        "id": "104",
                        "kind": "compute#disk",
                        "selfLink": ROOT + disk.path,
                        "users": [ROOT + resource.path],
                    }
            else:
                if self.delete_target is not None:
                    target = self.delete_target
                self.live.pop(resource.path, None)
                if key == "instance" and not self.preserve_disk:
                    self.live.pop(self.controller.resources["disk"].path, None)
                elif key == "instance":
                    self.live[self.controller.resources["disk"].path]["users"] = []
            operation = f"{'Delete' if method == 'DELETE' else 'Insert'}_{key}"
            result = {
                "kind": "compute#operation",
                "name": operation,
                "clientOperationId": request_id,
                "targetId": target,
                "targetLink": ROOT + resource.path,
                "operationType": "delete" if method == "DELETE" else "insert",
                "status": "RUNNING" if self.insert_pending and method == "POST" else "DONE",
                "selfLink": ROOT + resource.scope + "/operations/" + operation,
            }
            self.operations[resource.scope + "/operations/" + operation] = copy.deepcopy(result)
            if self.fault == (method, key):
                raise self.primary
        yield result


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    with closing(Database(tmp_path / "state.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "sample"), "proof")
        api = ScriptedAPI(owner)
        names = derive_names("sample")
        resources = {
            "deny": Resource(
                f"{GLOBAL}/firewalls/{names.deny_rule}",
                "firewall",
                firewall_body(names, NETWORK, ("192.0.2.1/32",), allow=False),
            ),
            "allow": Resource(
                f"{GLOBAL}/firewalls/{names.allow_rule}",
                "firewall",
                firewall_body(names, NETWORK, ("192.0.2.1/32",), allow=True),
            ),
            "instance": Resource(f"{ZONAL}/instances/sample", "instance", {"name": "sample"}),
            "disk": Resource(f"{ZONAL}/disks/sample", "disk", {}),
        }
        controller = CreationResources(owner, api, resources)
        api.controller = controller
        yield SimpleNamespace(
            owner=owner,
            database=database,
            api=api,
            controller=controller,
            resources=resources,
            deadline=Deadline.after(30),
        )


def insert_all(p):
    p.controller.register(p.deadline)
    for key in ("deny", "allow", "instance"):
        p.controller.insert(key, p.deadline)
    p.controller.capture_disk(p.resources["disk"].path, p.deadline)


def test_resource_construction_is_passive_and_resolve_requires_disposition(owned):
    p = owned
    assert not p.api.calls and not p.owner.list_pending_lifecycle_obligations()
    insert_all(p)
    assert not p.controller.resolve(p.deadline)
    assert p.owner.inspect_lifecycle_obligation(p.controller._id).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_known_operations_are_observed_without_insert_replay(owned):
    p = owned
    p.api.insert_pending = True
    insert_all(p)
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 3
    assert len([path for method, path in p.api.calls if method == "GET" and "/operations/" in path]) == 3
    with pytest.raises(StateError):
        p.controller.insert("instance", p.deadline)
    assert p.resources["instance"].create_state is ActivationState.SUCCEEDED


def test_lost_insert_receipt_retains_unknown_association_without_name_adoption(owned):
    p = owned
    p.controller.register(p.deadline)
    p.api.fault = ("POST", "instance")
    with pytest.raises(KeyboardInterrupt) as caught:
        p.controller.insert("instance", p.deadline)
    assert caught.value is p.api.primary
    assert p.resources["instance"].resource_id is None
    assert p.resources["instance"].path in p.api.live
    p.api.fault = None
    with pytest.raises(StateError):
        p.controller.delete("instance", p.deadline)
    with pytest.raises(StateError):
        p.controller.insert("instance", p.deadline)
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 1
    assert not any(method == "DELETE" for method, _path in p.api.calls)


def test_exact_vm_and_disk_absence_are_separately_observed(owned):
    p = owned
    insert_all(p)
    p.api.preserve_disk = True
    assert p.controller.delete("instance", p.deadline)
    assert not p.resources["disk"].absent
    assert p.controller.delete("disk", p.deadline)
    assert ("GET", p.resources["disk"].path) in p.api.calls
    assert any(method == "DELETE" and "/disks/" in path for method, path in p.api.calls)
    for key in ("allow", "deny"):
        assert p.controller.delete(key, p.deadline)
    assert p.controller.resolve(p.deadline)
    assert p.owner.inspect_lifecycle_obligation(p.controller._id).state is LifecycleObligationState.RESOLVED


def test_auto_delete_is_not_boot_disk_absence(owned):
    p = owned
    insert_all(p)
    assert p.controller.delete("instance", p.deadline)
    assert not p.resources["disk"].absent
    assert p.controller.delete("disk", p.deadline)
    assert p.resources["disk"].absent
    assert not any(method == "DELETE" and "/disks/" in path for method, path in p.api.calls)


def test_changed_disk_identity_refuses_delete(owned):
    p = owned
    insert_all(p)
    p.api.preserve_disk = True
    assert p.controller.delete("instance", p.deadline)
    p.api.live[p.resources["disk"].path]["id"] = "999"
    with pytest.raises(StateError):
        p.controller.delete("disk", p.deadline)
    assert not any(method == "DELETE" and "/disks/" in path for method, path in p.api.calls)


def test_delete_target_mismatch_retains_custody_and_does_not_replay(owned):
    p = owned
    insert_all(p)
    p.api.delete_target = "999"
    with pytest.raises(ValidationError):
        p.controller.delete("instance", p.deadline)
    with pytest.raises(StateError):
        p.controller.delete("instance", p.deadline)
    assert len([path for method, path in p.api.calls if method == "DELETE"]) == 1
    assert not p.controller.resolve(p.deadline)


def test_committed_receipt_publication_interruption_reconciles_exact_revision(owned, monkeypatch):
    p = owned
    original = LifecycleObligation.publish_payload
    primary = KeyboardInterrupt("receipt publication committed")
    fired = False

    def publish(self, **kwargs):
        nonlocal fired
        result = original(self, **kwargs)
        if not fired and json.loads(kwargs["payload"])["resources"]["instance"]["resource_id"] == "103":
            fired = True
            raise primary
        return result

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    p.controller.register(p.deadline)
    with pytest.raises(KeyboardInterrupt) as caught:
        p.controller.insert("instance", p.deadline)
    assert caught.value is primary and p.resources["instance"].resource_id == "103"
    p.controller.reconcile(p.deadline)
    assert p.controller._publication is None
    assert (
        json.loads(p.owner.inspect_lifecycle_obligation(p.controller._id).payload)["resources"]["instance"][
            "resource_id"
        ]
        == "103"
    )
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 1


def test_acceptance_is_retained_before_failed_publication(owned, monkeypatch):
    p = owned
    insert_all(p)
    original = LifecycleObligation.publish_payload
    primary = KeyboardInterrupt("acceptance committed")
    fired = False

    def publish(self, **kwargs):
        nonlocal fired
        result = original(self, **kwargs)
        if not fired and json.loads(kwargs["payload"])["accepted"]:
            fired = True
            raise primary
        return result

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    with pytest.raises(KeyboardInterrupt):
        p.controller.accept(p.deadline)
    assert p.controller.accepted
    p.controller.accept(p.deadline)
    assert p.controller.delete("allow", p.deadline)
    assert p.controller.resolve(p.deadline)
    assert p.resources["instance"].path in p.api.live and p.resources["disk"].path in p.api.live


def test_accepted_resource_controller_refuses_vm_or_disk_rollback(owned):
    p = owned
    insert_all(p)
    p.controller.accept(p.deadline)
    for key in ("instance", "disk", "deny"):
        with pytest.raises(StateError):
            p.controller.delete(key, p.deadline)
    assert not any(method == "DELETE" for method, _path in p.api.calls)


def test_extra_disk_attachment_prevents_vm_auto_delete(owned):
    p = owned
    insert_all(p)
    p.api.live[p.resources["instance"].path]["disks"].append(
        {
            "boot": False,
            "autoDelete": True,
            "type": "PERSISTENT",
            "source": ROOT + ZONAL + "/disks/other",
        }
    )
    with pytest.raises(StateError):
        p.controller.delete("instance", p.deadline)
    assert not any(method == "DELETE" for method, _path in p.api.calls)

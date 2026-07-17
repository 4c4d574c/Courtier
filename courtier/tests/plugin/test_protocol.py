"""Tests for JSON-RPC protocol message types."""

import json

import pytest
from pydantic import ValidationError

from courtier.plugin.protocol import (
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)
from courtier.plugin.sdk.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PLUGIN_CRASHED,
    TIMEOUT_ERROR,
    TOOL_NOT_FOUND,
)


class TestJSONRPCRequest:
    def test_serialize_request(self):
        req = JSONRPCRequest(id=1, method="tool.execute", params={"tool": "echo"})
        dumped = req.model_dump_json()
        data = json.loads(dumped)
        assert data == {"id": 1, "method": "tool.execute", "params": {"tool": "echo"}}

    def test_default_params(self):
        req = JSONRPCRequest(id=1, method="plugin.health")
        assert req.params == {}

    def test_roundtrip_from_line(self):
        req = JSONRPCRequest(id=42, method="checker.check", params={"text": "hello"})
        line = req.model_dump_json()
        parsed = JSONRPCRequest.model_validate_json(line)
        assert parsed.id == 42
        assert parsed.method == "checker.check"
        assert parsed.params == {"text": "hello"}


class TestJSONRPCResponse:
    def test_success_response(self):
        resp = JSONRPCResponse(id=1, result={"success": True, "data": [1, 2, 3]})
        data = json.loads(resp.model_dump_json())
        assert data["id"] == 1
        assert data["result"] == {"success": True, "data": [1, 2, 3]}
        assert data["error"] is None

    def test_error_response(self):
        resp = JSONRPCResponse(
            id=1,
            error={"code": -32000, "message": "Tool not found"},
        )
        data = json.loads(resp.model_dump_json())
        assert data["id"] == 1
        assert data["result"] is None
        assert data["error"] == {"code": -32000, "message": "Tool not found"}

    def test_is_error_property(self):
        resp = JSONRPCResponse(id=1, error={"code": -32000, "message": "bad"})
        assert resp.is_error is True

        resp2 = JSONRPCResponse(id=1, result={"ok": True})
        assert resp2.is_error is False


class TestJSONRPCNotification:
    def test_notification_has_no_id(self):
        notif = JSONRPCNotification(
            method="plugin.register",
            params={"capabilities": [{"type": "tool", "name": "echo"}]},
        )
        data = json.loads(notif.model_dump_json())
        assert "id" not in data
        assert data["method"] == "plugin.register"
        assert data["params"]["capabilities"][0]["name"] == "echo"

    def test_is_notification(self):
        notif = JSONRPCNotification(method="plugin.shutdown", params={})
        assert notif.is_notification is True

    def test_from_dict(self):
        raw = {"method": "health.check", "params": {"ts": 123}}
        notif = JSONRPCNotification.model_validate(raw)
        assert notif.method == "health.check"


class TestErrorCodes:
    def test_standard_codes_defined(self):
        assert PARSE_ERROR == -32700
        assert METHOD_NOT_FOUND == -32601
        assert INVALID_PARAMS == -32602
        assert INTERNAL_ERROR == -32603
        assert TIMEOUT_ERROR == -32000
        assert TOOL_NOT_FOUND == -32001
        assert PLUGIN_CRASHED == -32002


class TestValidationErrors:
    def test_request_without_method_raises(self):
        with pytest.raises(ValidationError):
            JSONRPCRequest(id=1)

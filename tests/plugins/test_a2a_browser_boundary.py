"""Exercise the real HTTP handler offline, with sentinel RPC dispatch."""
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from plugins.platforms.a2a.adapter import A2ARequestHandler
from plugins.platforms.a2a.security import A2ASecurityContext


def handler(headers=None, *, token="", client_ip="127.0.0.1"):
    context = A2ASecurityContext(
        bearer_token=token, peer_tokens=(), trusted_peers=frozenset(),
        allow_all_users=False, requested_host="127.0.0.1", push_secret="",
    )
    dispatch = Mock(return_value={"result": "sentinel"})
    adapter = SimpleNamespace(
        _security_context=context, _rate_limiter=SimpleNamespace(allow=lambda _: True),
        _route_for_request=lambda *args: {"agent": {}}, _rpc_message_send=dispatch,
    )
    request = object.__new__(A2ARequestHandler)
    request.server = SimpleNamespace(adapter=adapter, server_address=("127.0.0.1", 8080))
    request.client_address = (client_ip, 50000)
    request.path = "/"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}).encode()
    request.rfile = BytesIO(body)
    request.headers = Message()
    for name, value in {"Host": "127.0.0.1:8080", "Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}.items():
        request.headers[name] = value
    request._json = Mock()
    return request, dispatch


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:8080"}, {"Sec-Fetch-Site": "cross-site"},
    {"Host": "evil.example:8080"}, {"Host": "evil.example\\@127.0.0.1:8080"},
    {"Host": "127.0.0.1:8081"}, {"Host": "localhost:bad"},
    {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
])
def test_tokenless_browser_and_rebinding_requests_never_dispatch(headers):
    request, dispatch = handler(headers)
    request.do_POST()
    assert request._json.call_args.args[0] in {403, 415}
    dispatch.assert_not_called()
    assert request.rfile.tell() == 0


def test_tokenless_rejects_non_loopback_socket():
    request, dispatch = handler(client_ip="192.0.2.1")
    request.do_POST()
    assert request._json.call_args.args[0] == 403
    dispatch.assert_not_called()


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080"])
def test_tokenless_machine_json_remains_supported(host):
    request, dispatch = handler({"Host": host, "Content-Type": "application/json; charset=utf-8"})
    request.do_POST()
    assert request._json.call_args.args[0] == 200
    dispatch.assert_called_once()


def test_configured_bearer_clients_keep_remote_access():
    request, dispatch = handler({"Host": "agent.example", "Authorization": "Bearer test-token"}, token="test-token", client_ip="192.0.2.1")
    request.do_POST()
    assert request._json.call_args.args[0] == 200
    dispatch.assert_called_once()


def test_configured_bearer_rejects_invalid_credentials():
    request, dispatch = handler({"Authorization": "Bearer wrong"}, token="test-token")
    request.do_POST()
    assert request._json.call_args.args[0] == 401
    dispatch.assert_not_called()


def test_tokenless_get_rejects_rebinding_before_route_resolution():
    request, dispatch = handler({"Host": "evil.example:8080"})
    request.do_GET()
    assert request._json.call_args.args[0] == 403
    dispatch.assert_not_called()


def test_tokenless_oversized_port_is_rejected_without_parsing_body():
    request, dispatch = handler({"Host": "127.0.0.1:" + "9" * 5000, "Content-Type": "application/json"})
    request.do_POST()
    assert request._json.call_args.args[0] == 403
    assert request.rfile.tell() == 0
    dispatch.assert_not_called()

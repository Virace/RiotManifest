import types

import pytest
from urllib3 import exceptions as urllib3_exceptions

from riotmanifest.http_client import HttpClient, HttpClientError, HttpResponse, http_get, http_get_bytes, http_get_json


def test_http_response_json_success():
    response = HttpResponse(status=200, data=b'{"ok": true}', headers={})
    assert response.json() == {"ok": True}


def test_http_response_json_invalid_raises():
    response = HttpResponse(status=200, data=b"\xff\xfe\x00", headers={})
    with pytest.raises(HttpClientError, match="JSON"):
        response.json()


def test_http_client_get_success():
    client = HttpClient()
    fake_resp = types.SimpleNamespace(status=200, data=b"abc", headers={"X-Test": "1"})
    client._pool = types.SimpleNamespace(request=lambda *args, **kwargs: fake_resp)

    result = client.get("https://example.invalid")
    assert result.status == 200
    assert result.data == b"abc"
    assert result.headers["X-Test"] == "1"


def test_http_client_get_http_error_raises():
    client = HttpClient()

    def _raise_http_error(*args, **kwargs):
        raise urllib3_exceptions.HTTPError("network down")

    client._pool = types.SimpleNamespace(request=_raise_http_error)
    with pytest.raises(HttpClientError, match="HTTP 请求失败"):
        client.get("https://example.invalid")


def test_http_client_get_status_error_raises():
    client = HttpClient()
    fake_resp = types.SimpleNamespace(status=503, data=b"", headers={})
    client._pool = types.SimpleNamespace(request=lambda *args, **kwargs: fake_resp)

    with pytest.raises(HttpClientError, match="HTTP 状态异常"):
        client.get("https://example.invalid")


def test_http_helpers_delegate_to_default_client(monkeypatch):
    class _DummyClient:
        def get(self, url, headers=None, timeout=None):  # pylint: disable=unused-argument
            return HttpResponse(status=200, data=b'{"value": 7}', headers={})

    monkeypatch.setattr("riotmanifest.http_client._DEFAULT_HTTP_CLIENT", _DummyClient())
    assert http_get("https://example.invalid").status == 200
    assert http_get_bytes("https://example.invalid") == b'{"value": 7}'
    assert http_get_json("https://example.invalid") == {"value": 7}

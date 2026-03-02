"""基于 urllib3 的同步 HTTP 请求封装."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import urllib3
from urllib3 import exceptions as urllib3_exceptions
from urllib3.util import Timeout


class HttpClientError(Exception):
    """HTTP 客户端通用异常."""

    pass


@dataclass(frozen=True)
class HttpResponse:
    """HTTP 响应对象."""

    status: int
    data: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        """将响应体解析为 JSON 对象.

        Returns:
            解析得到的 JSON 对象.

        Raises:
            HttpClientError: 当响应体不是合法 UTF-8 JSON 时抛出.
        """
        try:
            return json.loads(self.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpClientError("响应 JSON 解析失败") from exc


class HttpClient:
    """同步 HTTP 客户端封装."""

    def __init__(
        self,
        *,
        num_pools: int = 16,
        maxsize: int = 64,
        timeout: Timeout | None = None,
    ):
        """初始化连接池与默认超时.

        Args:
            num_pools: 底层连接池数量.
            maxsize: 每个池的最大连接数.
            timeout: 默认请求超时配置.
        """
        self.timeout = timeout or Timeout(connect=30.0, read=90.0)
        self._pool = urllib3.PoolManager(
            num_pools=max(1, num_pools),
            maxsize=max(1, maxsize),
            retries=False,
        )

    def get(self, url: str, headers: Mapping[str, str] | None = None, timeout: Timeout | None = None) -> HttpResponse:
        """发起 GET 请求并返回响应对象.

        Args:
            url: 请求地址.
            headers: 可选请求头.
            timeout: 可选本次请求超时配置.

        Returns:
            响应对象.

        Raises:
            HttpClientError: 请求失败或响应状态码异常时抛出.
        """
        request_timeout = timeout or self.timeout
        try:
            response = self._pool.request(
                "GET",
                url,
                headers=dict(headers) if headers else None,
                timeout=request_timeout,
                preload_content=True,
            )
        except urllib3_exceptions.HTTPError as exc:
            raise HttpClientError(f"HTTP 请求失败: {url}") from exc

        if response.status >= 400:
            raise HttpClientError(f"HTTP 状态异常: {response.status}, url={url}")

        return HttpResponse(
            status=response.status,
            data=bytes(response.data or b""),
            headers={str(k): str(v) for k, v in response.headers.items()},
        )


_DEFAULT_HTTP_CLIENT = HttpClient()


def http_get(url: str, headers: Mapping[str, str] | None = None, timeout: Timeout | None = None) -> HttpResponse:
    """发起 GET 请求并返回原始响应对象."""
    return _DEFAULT_HTTP_CLIENT.get(url=url, headers=headers, timeout=timeout)


def http_get_bytes(url: str, headers: Mapping[str, str] | None = None, timeout: Timeout | None = None) -> bytes:
    """发起 GET 请求并返回响应字节数据."""
    return http_get(url=url, headers=headers, timeout=timeout).data


def http_get_json(url: str, headers: Mapping[str, str] | None = None, timeout: Timeout | None = None) -> Any:
    """发起 GET 请求并将响应解析为 JSON."""
    return http_get(url=url, headers=headers, timeout=timeout).json()

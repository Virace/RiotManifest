# -*- coding: utf-8 -*-
# @Author  : Virace
# @Email   : Virace@aliyun.com
# @Site    : x-item.com
# @Software: Pycharm
# @Create  : 2026/3/2 01:45
# @Update  : 2026/3/2 01:45
# @Detail  : urllib3 HTTP wrapper

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import urllib3
from urllib3 import exceptions as urllib3_exceptions
from urllib3.util import Timeout


class HttpClientError(Exception):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    data: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpClientError("响应 JSON 解析失败") from exc


class HttpClient:
    def __init__(
        self,
        *,
        num_pools: int = 16,
        maxsize: int = 64,
        timeout: Optional[Timeout] = None,
    ):
        self.timeout = timeout or Timeout(connect=30.0, read=90.0)
        self._pool = urllib3.PoolManager(
            num_pools=max(1, num_pools),
            maxsize=max(1, maxsize),
            retries=False,
        )

    def get(self, url: str, headers: Optional[Mapping[str, str]] = None, timeout: Optional[Timeout] = None) -> HttpResponse:
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


def http_get(url: str, headers: Optional[Mapping[str, str]] = None, timeout: Optional[Timeout] = None) -> HttpResponse:
    return _DEFAULT_HTTP_CLIENT.get(url=url, headers=headers, timeout=timeout)


def http_get_bytes(url: str, headers: Optional[Mapping[str, str]] = None, timeout: Optional[Timeout] = None) -> bytes:
    return http_get(url=url, headers=headers, timeout=timeout).data


def http_get_json(url: str, headers: Optional[Mapping[str, str]] = None, timeout: Optional[Timeout] = None) -> Any:
    return http_get(url=url, headers=headers, timeout=timeout).json()

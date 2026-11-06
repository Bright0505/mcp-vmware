"""vCenter vSphere Automation REST API client(唯讀,非同步)。

唯一的非 GET 呼叫是 session 生命週期:
  - POST   /api/session  登入,取得 session token
  - DELETE /api/session  登出
其餘所有 inventory 查詢一律使用 GET,不涉及任何資源異動。

HTTP 層使用 httpx2(MCP Python SDK 2.x 隨附的 httpx 後繼版本)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx2

from .config import Config

logger = logging.getLogger(__name__)

_SESSION_HEADER = "vmware-api-session-id"


class VCenterError(RuntimeError):
    """vCenter API 呼叫失敗。"""


class VCenterClient:
    """精簡的唯讀 vCenter REST client。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._http = httpx2.AsyncClient(
            base_url=config.base_url,
            verify=not config.insecure,
            timeout=httpx2.Timeout(30.0),
        )

    async def aclose(self) -> None:
        """登出並關閉連線。"""
        async with self._lock:
            token, self._token = self._token, None
        if token is not None:
            try:
                await self._http.request(
                    "DELETE",
                    "/api/session",
                    headers={_SESSION_HEADER: token},
                )
            except httpx2.HTTPError as exc:  # 登出失敗不應中斷關閉流程
                logger.warning("vCenter 登出失敗: %s", exc)
        await self._http.aclose()

    async def __aenter__(self) -> "VCenterClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _login(self) -> str:
        """POST /api/session — 以 Basic Auth 換取 session token。"""
        try:
            resp = await self._http.post(
                "/api/session",
                auth=(self._config.user, self._config.password),
            )
        except httpx2.HTTPError as exc:
            raise VCenterError(f"無法連線 vCenter: {exc}") from exc

        if resp.status_code in (401, 403):
            raise VCenterError("vCenter 認證失敗,請檢查帳號密碼。")
        if resp.status_code >= 400:
            raise VCenterError(
                f"vCenter 登入失敗 (HTTP {resp.status_code}): {resp.text}"
            )

        token = resp.json()
        if not isinstance(token, str) or not token:
            raise VCenterError("vCenter 登入回應格式非預期。")
        return token

    async def _session_token(self) -> str:
        """取得可用的 session token;必要時登入(同一時間只會有一個登入請求)。"""
        token = self._token
        if token is not None:
            return token
        async with self._lock:
            if self._token is None:
                self._token = await self._login()
            return self._token

    async def _invalidate(self, stale: str) -> None:
        """丟棄過期的 token(若期間已有其他請求換到新 token 則保留)。"""
        async with self._lock:
            if self._token == stale:
                self._token = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """對 vCenter 發出 GET 請求;session 失效時自動重新登入並重試一次。"""
        for attempt in (1, 2):
            token = await self._session_token()
            try:
                resp = await self._http.get(
                    path,
                    params=params,
                    headers={_SESSION_HEADER: token},
                )
            except httpx2.HTTPError as exc:
                raise VCenterError(f"vCenter 請求失敗: {exc}") from exc

            if resp.status_code == 401 and attempt == 1:
                await self._invalidate(token)  # session 過期,下一輪重新登入
                continue
            if resp.status_code == 404:
                raise VCenterError(f"找不到資源: {path}")
            if resp.status_code >= 400:
                raise VCenterError(
                    f"vCenter API 錯誤 (HTTP {resp.status_code}): {resp.text}"
                )
            if not resp.content:
                return None
            return resp.json()

        raise VCenterError("vCenter session 重試後仍失敗。")

    @staticmethod
    def encode_id(value: str) -> str:
        """將 managed object id 安全地放入 URL path。"""
        return quote(value, safe="")

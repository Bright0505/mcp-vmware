"""HTTP 模式:MCP over Streamable HTTP(端點 `/mcp/`),供 Open WebUI 原生 MCP 連線使用。

- 驗證:`MCP_AUTH_TOKEN`(共用 Bearer token,可逗號分隔多組)與選用的 `MCP_OAUTH_*`,
  詳見 `auth.py`。未設 token 時不驗證並在 log 警告。
- `/health` 免驗證(容器健康檢查用)。
- vCenter client 由 `server.lifespan` 建立:SDK 的 session manager 啟動時進入一次、
  程序結束時登出,所有請求共用同一個 vCenter session。
- 缺 VCENTER_HOST/USER/PASSWORD 時直接啟動失敗(與 stdio 模式相同),
  健康檢查會因此失敗,不會出現「容器活著但每次呼叫都錯」的狀態。
- stateless + JSON response:無 server 端 session,前面不需要 sticky routing。
"""

from __future__ import annotations

import contextlib
import logging
import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, server
from .auth import BearerTokenMiddleware, OAuthVerifier, load_auth_tokens, load_oauth_config
from .config import load_config

logger = logging.getLogger("mcp_vmwhere")

SERVICE_NAME = os.getenv("MCP_SERVER_NAME", "mcp-vmwhere")


def create_app() -> Starlette:
    # 缺必填設定 → ConfigError,由入口印出後結束程序
    load_config()

    auth_tokens = load_auth_tokens()
    oauth_config = load_oauth_config()
    if auth_tokens:
        logger.info("Shared-token auth enabled (%d token(s) accepted)", len(auth_tokens))
    else:
        logger.warning("Shared-token auth disabled: MCP_AUTH_TOKEN is not set")
    if oauth_config:
        logger.info(
            "OAuth access tokens accepted on /mcp (issuer=%s, audience=%s)",
            oauth_config.issuer,
            oauth_config.audience,
        )

    dns_protection = os.getenv("MCP_ENABLE_DNS_PROTECTION", "false").lower() == "true"
    mcp_app = server.mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=dns_protection),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        # session manager 必須在執行中,/mcp/ 才能服務;它會進入 server.lifespan 建立 vCenter client
        async with server.mcp.session_manager.run():
            yield

    async def health(_request):
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": __version__,
                "auth_enabled": bool(auth_tokens),
                "oauth_enabled": oauth_config is not None,
            }
        )

    async def root(_request):
        return JSONResponse(
            {
                "name": SERVICE_NAME,
                "version": __version__,
                "status": "ok",
                "endpoints": {"health": "/health", "mcp": "/mcp/"},
            }
        )

    middleware = []
    if auth_tokens or oauth_config:
        middleware.append(
            Middleware(
                BearerTokenMiddleware,
                tokens=auth_tokens,
                exempt_paths=("/health",),
                oauth=OAuthVerifier(oauth_config) if oauth_config else None,
            )
        )

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/", root),
            Mount("/mcp", app=mcp_app),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )

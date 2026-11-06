"""HTTP 模式入口(MCP over Streamable HTTP,端點 /mcp/)。

以 PYTHONPATH=src(容器內為 /app/src)執行 `python -m http_server`,
port 取自 HTTP_PORT(預設 8000)。
"""

import logging
import os
import sys

import uvicorn

from mcp_vmware.config import ConfigError
from mcp_vmware.http_app import create_app

logger = logging.getLogger("mcp_vmware")


def run_http_server() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    host = os.getenv("HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("HTTP_PORT", "8000"))
    try:
        app = create_app()
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_http_server()

"""入口:檢查設定後,以 stdio transport 執行 MCP server。

vCenter client 由 server 的 lifespan 建立與關閉(見 `server.lifespan`);
此處先讀一次設定,讓缺少環境變數時能立刻給出清楚的錯誤,而不是等到第一次呼叫 tool。
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_config
from .server import mcp

logger = logging.getLogger("mcp_vmwhere")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    try:
        load_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

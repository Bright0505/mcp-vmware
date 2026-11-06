# MCP VMware — vCenter 唯讀查詢,HTTP 模式(Streamable HTTP,端點 /mcp/)

FROM python:3.11-slim AS base

WORKDIR /app

# healthcheck 用
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt README.md ./
COPY src/ ./src/

# constraints.txt 鎖住所有遞移依賴,重建時解析出同一棵依賴樹。
# 依賴有意變更時,在建好的 image 內 `pip freeze` 重新產生。
RUN pip install --no-cache-dir -c constraints.txt .

ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Production stage(以 --target production 建置)
FROM base AS production

RUN useradd --create-home --shell /bin/bash mcpuser && \
    chown -R mcpuser:mcpuser /app

USER mcpuser

ENV HTTP_PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${HTTP_PORT}/health" || exit 1

CMD ["python", "-m", "http_server"]

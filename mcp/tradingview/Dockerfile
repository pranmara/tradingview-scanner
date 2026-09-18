# Generic container for a community TradingView MCP server exposed over streamable-HTTP.
# The package name and entrypoint are build args so you can swap in whichever server you use;
# match the tool names it exposes via TV_MCP_OHLCV_TOOL / TV_MCP_SNAPSHOT_TOOL in .env.
FROM python:3.12-slim
ARG MCP_PACKAGE=tradingview-mcp-server
ARG MCP_ENTRYPOINT=tradingview-mcp-server
ENV MCP_ENTRYPOINT=${MCP_ENTRYPOINT} \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "${MCP_PACKAGE}" \
 && useradd --create-home --uid 10002 mcp
USER mcp
EXPOSE 8000
# Flags follow the FastMCP convention; adjust if your server's CLI differs.
CMD ["sh", "-c", "exec ${MCP_ENTRYPOINT} --transport streamable-http --host ${MCP_HOST} --port ${MCP_PORT}"]

"""Entry point for the Claude Desktop extension (.mcpb, uv runtime)."""

from line_mcp import store
from line_mcp.server import mcp

if __name__ == "__main__":
    if store.get_store() is not None:
        store.start_live_sync()
    mcp.run()

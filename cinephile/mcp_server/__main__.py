"""Run the MCP server.

python -m cinephile.mcp_server             stdio transport (what the agent uses)
python -m cinephile.mcp_server --http      streamable HTTP on 127.0.0.1:8001
"""

from __future__ import annotations

import argparse

from cinephile.config import configure_logging
from cinephile.mcp_server.server import mcp
from cinephile.storage.db import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Cinephile MCP server")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8001, help="port for --http (default 8001)")
    args = parser.parse_args()

    configure_logging("WARNING")  # stderr only; stdout carries the protocol in stdio mode
    init_db()
    if args.http:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
